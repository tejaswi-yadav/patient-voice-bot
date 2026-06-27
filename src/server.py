"""Twilio Media Stream <-> OpenAI Realtime bridge."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.openai_realtime import connect_realtime_with_retry, is_ws_open
from fastapi import FastAPI, Form, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.rest import Client

from src.analyzer import analyze_call, append_to_bug_report, save_analysis
from src.audio_utils import boost_mulaw_payload_b64
from src.config import (
    CALLS_DIR,
    METADATA_DIR,
    RECORDINGS_DIR,
    TRANSCRIPTS_DIR,
    Settings,
    get_settings,
)
from src.recorder import download_recording, save_call_metadata
from src.scenarios import Scenario, get_scenario
from src.transcription import CallTranscript, save_realtime_snapshot, save_transcript
from src.whisper_transcribe import enrich_transcript_from_recording

logger = logging.getLogger(__name__)

LOG_EVENT_TYPES = {
    "error",
    "response.content.done",
    "response.done",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_stopped",
    "input_audio_buffer.speech_started",
    "session.created",
    "session.updated",
    "conversation.item.input_audio_transcription.completed",
    "response.output_audio_transcript.done",
}

# Shared state across requests (single-process deployment)
active_calls: dict[str, dict[str, Any]] = {}
call_id_by_sid: dict[str, str] = {}  # CallSid -> call_id (persists after finalize)
pending_analysis: dict[str, dict[str, Any]] = {}  # call_id -> state awaiting recording
pending_recordings: dict[str, str] = {}  # recording_sid -> call_id


def build_patient_instructions(scenario: Scenario, max_duration: int) -> str:
    goals_text = "\n".join(f"- {goal}" for goal in scenario.goals)
    return f"""You are simulating a real patient calling a medical office's AI receptionist on the phone.

{scenario.patient_context}

Your goals for this call:
{goals_text}

CRITICAL BEHAVIOR RULES:
1. WAIT for the agent to speak first. Do not talk until you hear them greet you.
2. You are ON A LIVE PHONE CALL. Speak only words you would say aloud to the receptionist.
3. NEVER narrate, explain, or describe your role. Forbidden phrases: "Here we go", "Sure thing", "Let me take on the role", "As the patient", "I will now say".
4. NEVER refer to yourself as "the patient" — you ARE the patient. Just say "Hi, this is Maria" not "Hi, this is Maria, the patient".
5. Do NOT read instructions aloud. Do NOT announce what you are about to do.
6. Keep responses concise (1-3 sentences). Use contractions and natural speech.
7. Answer questions the agent asks using details from your patient context.
8. Steer the conversation toward completing your goals.
9. If the agent misunderstands you, politely correct them.
10. After your goals are met, thank them and say goodbye naturally.
11. Do NOT reveal you are an AI or test bot.
12. If put on hold, wait patiently and acknowledge when they return.
13. Speak ONLY in English — never Spanish or any other language.

You are the PATIENT on the phone. The other person is the medical office AI AGENT. Speak only dialogue — never stage directions."""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Patient Voice Bot", version="1.0.0")
    twilio_client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    @app.get("/")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "running",
                "target": settings.target_phone_number,
                "active_calls": len(active_calls),
            }
        )

    @app.post("/recording-callback")
    async def recording_callback(
        RecordingSid: str = Form(...),
        CallSid: str = Form(...),
        RecordingStatus: str = Form(...),
        RecordingUrl: str = Form(default=""),
    ) -> JSONResponse:
        call_state = active_calls.get(CallSid, {})
        call_id = call_state.get("call_id") or call_id_by_sid.get(CallSid, CallSid[:8])

        if RecordingStatus == "completed":
            logger.info("Recording completed for call %s: %s", call_id, RecordingSid)

            path = download_recording(
                client=twilio_client,
                recording_sid=RecordingSid,
                call_id=call_id,
                output_dir=RECORDINGS_DIR,
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
            )

            if path:
                await complete_call_pipeline(call_id, CallSid, str(path), settings)

        return JSONResponse({"status": "ok"})

    @app.post("/call-status")
    async def call_status(
        CallSid: str = Form(...),
        CallStatus: str = Form(...),
        CallDuration: str = Form(default="0"),
    ) -> JSONResponse:
        logger.info("Call %s status: %s (duration: %ss)", CallSid, CallStatus, CallDuration)

        if CallStatus in ("completed", "busy", "no-answer", "failed", "canceled"):
            await finalize_call(CallSid, settings, twilio_client)

        return JSONResponse({"status": "ok"})

    @app.websocket("/media-stream")
    async def handle_media_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        logger.info("Media stream connected")

        stream_sid: str | None = None
        call_sid: str | None = None
        call_id: str | None = None
        scenario: Scenario | None = None
        transcript: CallTranscript | None = None
        openai_ws: Any = None
        latest_media_timestamp = 0
        last_assistant_item: str | None = None
        response_start_timestamp: int | None = None
        mark_queue: list[str] = []
        hangup_scheduled = False
        pending_audio: list[str] = []
        session_ready = asyncio.Event()
        session_configured = asyncio.Event()
        patient_has_spoken = asyncio.Event()

        # Start OpenAI connection immediately — must NOT block Twilio message loop
        openai_connect_task = asyncio.create_task(
            connect_realtime_with_retry(
                settings.openai_api_key,
                settings.openai_realtime_model,
                open_timeout=settings.openai_ws_open_timeout,
                max_retries=settings.openai_ws_max_retries,
            )
        )

        async def append_audio_to_openai(payload: str) -> None:
            if openai_ws and is_ws_open(openai_ws):
                await openai_ws.send(
                    json.dumps(
                        {"type": "input_audio_buffer.append", "audio": payload}
                    )
                )

        async def flush_pending_audio() -> None:
            for payload in pending_audio:
                await append_audio_to_openai(payload)
            pending_audio.clear()

        async def receive_from_twilio() -> None:
            nonlocal stream_sid, call_sid, call_id, scenario, transcript
            nonlocal openai_ws, latest_media_timestamp, hangup_scheduled

            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    event = data.get("event")
                    if event != "media":
                        logger.info("Twilio event: %s", event)

                    if event == "connected":
                        continue

                    if event == "start":
                        stream_sid = data["start"]["streamSid"]
                        call_sid = data["start"].get("callSid")
                        params = data["start"].get("customParameters", {})

                        scenario_id = params.get("scenario_id", "schedule-routine")
                        call_id = params.get(
                            "call_id", call_sid[:8] if call_sid else "unknown"
                        )
                        scenario = get_scenario(scenario_id)

                        transcript = CallTranscript(
                            call_id=call_id,
                            scenario_id=scenario.id,
                            scenario_name=scenario.name,
                            started_at=datetime.now(timezone.utc).isoformat(),
                        )

                        if call_sid:
                            active_calls[call_sid] = {
                                "call_id": call_id,
                                "scenario_id": scenario.id,
                                "transcript": transcript,
                                "stream_sid": stream_sid,
                            }
                            call_id_by_sid[call_sid] = call_id

                        # Await parallel OpenAI connection (started at stream open)
                        try:
                            openai_ws = await openai_connect_task
                        except Exception:
                            logger.exception(
                                "OpenAI connection failed for call %s", call_id
                            )
                            break

                        await initialize_openai_session(
                            openai_ws, scenario, settings
                        )
                        try:
                            await asyncio.wait_for(
                                session_configured.wait(), timeout=5.0
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "No session.updated within 5s — VAD may not be active"
                            )
                        await flush_pending_audio()
                        session_ready.set()

                        asyncio.create_task(
                            auto_hangup(
                                call_sid,
                                settings.max_call_duration_seconds,
                                twilio_client,
                            )
                        )
                        asyncio.create_task(
                            nudge_patient_if_silent(
                                openai_ws, scenario, 20.0, patient_has_spoken
                            )
                        )
                        logger.info(
                            "Stream started: call_id=%s scenario=%s",
                            call_id,
                            scenario.id,
                        )

                    elif event == "media":
                        latest_media_timestamp = int(data["media"]["timestamp"])
                        payload = data["media"]["payload"]
                        if session_ready.is_set():
                            await append_audio_to_openai(payload)
                        else:
                            pending_audio.append(payload)

                    elif event == "mark":
                        if mark_queue:
                            mark_queue.pop(0)

                    elif event == "stop":
                        logger.info("Stream stopped for call %s", call_id)
                        if call_sid:
                            await finalize_call(
                                call_sid, settings, twilio_client
                            )
                        break

            except WebSocketDisconnect:
                logger.info("Twilio WebSocket disconnected")
            except Exception:
                logger.exception("Error in receive_from_twilio")

        async def send_to_twilio() -> None:
            nonlocal stream_sid, last_assistant_item, response_start_timestamp
            nonlocal openai_ws, hangup_scheduled

            try:
                openai_ws = await openai_connect_task

                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    event_type = response.get("type", "")

                    if event_type in LOG_EVENT_TYPES:
                        logger.debug("OpenAI event: %s", event_type)

                    if event_type == "session.updated":
                        session_cfg = response.get("session", {})
                        audio_input = session_cfg.get("audio", {}).get("input", {})
                        turn_detection = audio_input.get("turn_detection") or session_cfg.get(
                            "turn_detection"
                        )
                        if turn_detection:
                            logger.info(
                                "VAD configured: type=%s create_response=%s",
                                turn_detection.get("type"),
                                turn_detection.get("create_response"),
                            )
                            session_configured.set()
                        else:
                            logger.warning(
                                "session.updated missing turn_detection — VAD may be off"
                            )

                    if event_type == "error":
                        logger.error("OpenAI error: %s", response)

                    if not session_ready.is_set():
                        continue

                    if (
                        event_type
                        == "conversation.item.input_audio_transcription.completed"
                    ):
                        text = response.get("transcript", "")
                        if transcript and text:
                            transcript.add_agent(text)
                            logger.info("AGENT: %s", text[:120])

                    if event_type in (
                        "response.output_audio_transcript.done",
                        "response.audio_transcript.done",
                    ):
                        text = response.get("transcript", "")
                        if transcript and text:
                            transcript.add_patient(text)
                            patient_has_spoken.set()
                            logger.info("PATIENT: %s", text[:120])

                            if (
                                _is_goodbye(text)
                                and call_sid
                                and not hangup_scheduled
                            ):
                                hangup_scheduled = True
                                asyncio.create_task(
                                    delayed_hangup(call_sid, 3.0, twilio_client)
                                )

                    if event_type in (
                        "response.output_audio.delta",
                        "response.audio.delta",
                    ) and "delta" in response and stream_sid:
                        raw_delta = response["delta"]
                        audio_payload = boost_mulaw_payload_b64(
                            raw_delta,
                            settings.patient_outbound_gain,
                        )
                        await websocket.send_json(
                            {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": audio_payload},
                            }
                        )

                        item_id = response.get("item_id")
                        if item_id and item_id != last_assistant_item:
                            response_start_timestamp = latest_media_timestamp
                            last_assistant_item = item_id
                        await _send_mark(websocket, stream_sid, mark_queue)

                    # VAD create_response=True auto-triggers responses — do not also send response.create here

                    if event_type == "input_audio_buffer.speech_started":
                        if last_assistant_item and mark_queue and stream_sid:
                            elapsed = 0
                            if response_start_timestamp is not None:
                                elapsed = (
                                    latest_media_timestamp
                                    - response_start_timestamp
                                )
                            if elapsed < settings.interruption_min_ms:
                                logger.debug(
                                    "Ignoring early speech_started (%dms < %dms)",
                                    elapsed,
                                    settings.interruption_min_ms,
                                )
                                continue
                            if is_ws_open(openai_ws):
                                await openai_ws.send(
                                    json.dumps(
                                        {
                                            "type": "conversation.item.truncate",
                                            "item_id": last_assistant_item,
                                            "content_index": 0,
                                            "audio_end_ms": max(elapsed, 0),
                                        }
                                    )
                                )
                            await websocket.send_json(
                                {"event": "clear", "streamSid": stream_sid}
                            )
                            mark_queue.clear()
                            last_assistant_item = None
                            response_start_timestamp = None

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in send_to_twilio")

        try:
            await asyncio.gather(receive_from_twilio(), send_to_twilio())
        except Exception:
            logger.exception("Media stream error")
        finally:
            if not openai_connect_task.done():
                openai_connect_task.cancel()
            if openai_ws and is_ws_open(openai_ws):
                await openai_ws.close()
            logger.info("Media stream closed for call %s", call_id)

    return app


async def initialize_openai_session(
    openai_ws: Any,
    scenario: Scenario,
    settings: Settings,
) -> None:
    instructions = build_patient_instructions(
        scenario, settings.max_call_duration_seconds
    )

    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": settings.openai_realtime_model,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        "create_response": True,
                    },
                    "transcription": {"model": "whisper-1"},
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": settings.openai_voice,
                },
            },
            "instructions": instructions,
        },
    }
    await openai_ws.send(json.dumps(session_update))
    logger.info("OpenAI session.update sent (%s)", scenario.id)
    # Patient waits for agent greeting — VAD triggers response after agent speaks


async def _send_mark(
    websocket: WebSocket, stream_sid: str, mark_queue: list[str]
) -> None:
    mark_event = {
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {"name": "responsePart"},
    }
    await websocket.send_json(mark_event)
    mark_queue.append("responsePart")


def _is_goodbye(text: str) -> bool:
    lower = text.lower()
    goodbye_phrases = [
        "goodbye",
        "good bye",
        "have a great day",
        "have a good day",
        "thank you, bye",
        "thanks, bye",
        "talk to you later",
        "take care",
    ]
    return any(phrase in lower for phrase in goodbye_phrases)


async def nudge_patient_if_silent(
    openai_ws: Any,
    scenario: Scenario,
    delay: float,
    patient_has_spoken: asyncio.Event,
) -> None:
    """Prompt patient only if VAD never triggered a response (fallback)."""
    try:
        done, _ = await asyncio.wait(
            [
                asyncio.create_task(asyncio.sleep(delay)),
                asyncio.create_task(patient_has_spoken.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            task.cancel()

        if patient_has_spoken.is_set():
            logger.debug("Skipping nudge — patient already spoke")
            return

        nudge = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            f"[INTERNAL — do not read aloud] The agent has finished speaking. "
                            f"Respond with spoken dialogue only in English. "
                            f"You are: {scenario.patient_context.split('.')[0]}. "
                            f"Goal: {scenario.goals[0]}. "
                            f"Say your next line now — no preamble, no narration."
                        ),
                    }
                ],
            },
        }
        await openai_ws.send(json.dumps(nudge))
        await openai_ws.send(json.dumps({"type": "response.create"}))
        logger.info("Sent patient nudge after silence (VAD fallback)")
    except Exception:
        logger.exception("Failed to nudge patient")


async def delayed_hangup(call_sid: str, delay: float, client: Client) -> None:
    await asyncio.sleep(delay)
    try:
        client.calls(call_sid).update(status="completed")
        logger.info("Hung up call %s after goodbye", call_sid)
    except Exception:
        logger.exception("Failed to hang up call %s", call_sid)


async def auto_hangup(
    call_sid: str | None, max_seconds: int, client: Client
) -> None:
    if not call_sid:
        return
    await asyncio.sleep(max_seconds)
    try:
        call = client.calls(call_sid).fetch()
        if call.status in ("in-progress", "ringing"):
            client.calls(call_sid).update(status="completed")
            logger.info("Auto hangup after %ss for call %s", max_seconds, call_sid)
    except Exception:
        logger.exception("Auto hangup failed for %s", call_sid)


async def finalize_call(
    call_sid: str, settings: Settings, client: Client
) -> None:
    state = active_calls.pop(call_sid, None)
    if not state:
        return

    if state.get("finalized"):
        return
    state["finalized"] = True

    transcript: CallTranscript = state.get("transcript")
    if not transcript:
        return

    transcript.ended_at = datetime.now(timezone.utc).isoformat()
    scenario_id = state.get("scenario_id", "schedule-routine")

    # Save interim + realtime snapshot; full pipeline runs when recording arrives
    save_realtime_snapshot(transcript, TRANSCRIPTS_DIR)
    save_transcript(transcript, TRANSCRIPTS_DIR)
    pending_analysis[transcript.call_id] = {
        "call_sid": call_sid,
        "scenario_id": scenario_id,
        "transcript": transcript,
    }
    logger.info(
        "Call %s ended — awaiting recording for transcription/analysis",
        transcript.call_id,
    )


async def complete_call_pipeline(
    call_id: str,
    call_sid: str,
    recording_path: str,
    settings: Settings,
) -> None:
    """Run Whisper transcription and bug analysis after recording is ready."""
    state = pending_analysis.pop(call_id, None)
    if not state:
        logger.warning("No pending state for call %s", call_id)
        return

    transcript: CallTranscript = state["transcript"]
    scenario = get_scenario(state.get("scenario_id", "schedule-routine"))
    recording = Path(recording_path)

    try:
        txt_path, json_path = enrich_transcript_from_recording(
            recording,
            transcript,
            settings.openai_api_key,
            TRANSCRIPTS_DIR,
        )
        logger.info("Final transcript saved: %s (%d entries)", txt_path, len(transcript.entries))

        analysis = analyze_call(transcript, scenario, settings.openai_api_key)
        analysis_path = save_analysis(call_id, analysis, METADATA_DIR)
        append_to_bug_report(call_id, scenario, analysis, CALLS_DIR / "BUG_REPORT.md")
        logger.info("Saved analysis: %s", analysis_path)
    except Exception:
        logger.exception("Pipeline failed for call %s", call_id)

    save_call_metadata(
        call_id,
        {
            "call_sid": call_sid,
            "scenario_id": scenario.id,
            "scenario_name": scenario.name,
            "transcript_txt": str(TRANSCRIPTS_DIR / f"transcript-{call_id}.txt"),
            "recording_path": recording_path,
            "transcript_entries": len(transcript.entries),
        },
        METADATA_DIR,
    )
