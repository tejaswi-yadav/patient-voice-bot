"""Post-call Whisper transcription + merge with realtime patient lines."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from openai import OpenAI

from src.config import PROJECT_ROOT
from src.transcription import (
    CallTranscript,
    TranscriptEntry,
    format_offset_timestamp,
    load_realtime_snapshot,
    merge_realtime_and_whisper,
    normalize_entry_timestamps,
    save_realtime_snapshot,
    save_transcript,
)

logger = logging.getLogger(__name__)

_BUNDLED_FFMPEG = (
    PROJECT_ROOT
    / "tools"
    / "ffmpeg"
    / "ffmpeg-8.1.1-essentials_build"
    / "bin"
    / "ffmpeg.exe"
)

DIARIZE_PROMPT = """This is a phone call between a PATIENT (caller) and an AI AGENT (medical office receptionist at Pivot Point Orthopedics).

The AGENT typically speaks first with a greeting like "Thank you for calling..." or asks to create a demo patient profile.

Transcribe the full conversation and label each turn.
Return JSON:
{
  "entries": [
    {"role": "agent", "text": "exact words spoken"},
    {"role": "patient", "text": "exact words spoken"}
  ]
}

Rules:
- role must be "agent" or "patient"
- Include ALL agent lines — hold messages, greetings, questions, confirmations
- Include ALL patient lines — only words actually spoken aloud
- One conversational turn per entry
- Do NOT include stage directions or narration
- Preserve exact wording"""


def _ffmpeg_path() -> str | None:
    system = shutil.which("ffmpeg")
    if system:
        return system
    if _BUNDLED_FFMPEG.exists():
        return str(_BUNDLED_FFMPEG)
    return None


def _ffmpeg_available() -> bool:
    return _ffmpeg_path() is not None


AGENT_MARKERS = (
    "pivot point",
    "thank you for calling",
    "demo patient",
    "para español",
    "para espanol",
    "recorded for quality",
    "orthopedics",
)


def _split_stereo_channels(recording_path: Path, tmp_dir: Path) -> tuple[Path, Path] | None:
    """Extract raw left (c0) and right (c1) channels as WAV files."""
    if not _ffmpeg_available():
        return None

    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return None

    ch0_wav = tmp_dir / "ch0.wav"
    ch1_wav = tmp_dir / "ch1.wav"

    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(recording_path), "-af", "pan=mono|c0=c0", str(ch0_wav)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [ffmpeg, "-y", "-i", str(recording_path), "-af", "pan=mono|c0=c1", str(ch1_wav)],
            capture_output=True,
            check=True,
        )
        if ch0_wav.stat().st_size > 1000 and ch1_wav.stat().st_size > 1000:
            return ch0_wav, ch1_wav
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.debug("Channel split failed: %s", exc)

    return None


def _boost_quiet_channel(ffmpeg: str, input_wav: Path, output_wav: Path) -> Path:
    """Normalize quiet patient TTS audio before Whisper."""
    try:
        subprocess.run(
            [
                ffmpeg, "-y", "-i", str(input_wav),
                "-af", "dynaudnorm,volume=2.5",
                str(output_wav),
            ],
            capture_output=True,
            check=True,
        )
        return output_wav
    except (subprocess.CalledProcessError, OSError):
        return input_wav


def _quick_channel_text(client: OpenAI, wav_path: Path, max_chars: int = 400) -> str:
    """Fast whisper sample to identify which channel is the agent."""
    try:
        with wav_path.open("rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )
        text = result if isinstance(result, str) else str(result)
        return text[:max_chars].lower()
    except Exception:
        return ""


def _score_agent_channel(text: str) -> int:
    return sum(1 for marker in AGENT_MARKERS if marker in text)


def _channel_rms(ffmpeg: str, wav_path: Path) -> float:
    """Estimate channel loudness via ffmpeg astats (higher = more speech energy)."""
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-i", str(wav_path),
                "-af", "astats=metadata=1:reset=1",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in (proc.stderr or "").splitlines():
            if "RMS level dB" in line:
                value = line.split("RMS level dB:")[-1].strip().split()[0]
                return float(value)
    except (subprocess.CalledProcessError, OSError, ValueError):
        pass
    return -100.0


def _identify_channels(
    client: OpenAI, ch0_wav: Path, ch1_wav: Path, tmp_dir: Path
) -> tuple[Path, Path]:
    """Auto-detect which channel is agent vs patient using content markers."""
    ch0_text = _quick_channel_text(client, ch0_wav)
    ch1_text = _quick_channel_text(client, ch1_wav)
    ch0_score = _score_agent_channel(ch0_text)
    ch1_score = _score_agent_channel(ch1_text)

    logger.info(
        "Channel detection scores — ch0=%d ch1=%d (ch0 preview: %r)",
        ch0_score,
        ch1_score,
        ch0_text[:80],
    )

    ffmpeg = _ffmpeg_path() or ""
    if ch0_score == ch1_score:
        ch0_rms = _channel_rms(ffmpeg, ch0_wav)
        ch1_rms = _channel_rms(ffmpeg, ch1_wav)
        logger.info("Channel RMS — ch0=%.1f dB ch1=%.1f dB", ch0_rms, ch1_rms)
        # Outbound: agent (PSTN) is usually louder; patient TTS can be quieter
        if ch0_rms >= ch1_rms:
            agent_wav, patient_wav = ch0_wav, ch1_wav
        else:
            agent_wav, patient_wav = ch1_wav, ch0_wav
    elif ch0_score > ch1_score:
        agent_wav, patient_wav = ch0_wav, ch1_wav
    else:
        agent_wav, patient_wav = ch1_wav, ch0_wav

    boosted = tmp_dir / "patient_boosted.wav"
    patient_wav = _boost_quiet_channel(ffmpeg, patient_wav, boosted)
    return patient_wav, agent_wav


def _is_likely_hallucination(text: str) -> bool:
    """Filter common Whisper hallucinations on silent/near-silent audio."""
    t = text.strip()
    if len(t) < 3:
        return False
    if t in ("...", "..", ".", "…") or all(c in ".… \t" for c in t):
        return True
    lower = t.lower()
    hallucination_markers = [
        "subscribe",
        "thank you for watching",
        "urbanization",
        "industrialization",
        "maritime capital",
        "iberia",
        "achievers of the world",
    ]
    return any(marker in lower for marker in hallucination_markers)


def _whisper_segments(
    client: OpenAI,
    audio_path: Path,
    *,
    prompt: str | None = None,
) -> list[dict]:
    """Run Whisper on a single audio file and return timed segments."""
    kwargs: dict = {
        "model": "whisper-1",
        "response_format": "verbose_json",
        "timestamp_granularities": ["segment"],
    }
    if prompt:
        kwargs["prompt"] = prompt

    with audio_path.open("rb") as f:
        result = client.audio.transcriptions.create(file=f, **kwargs)

    segments = []
    if hasattr(result, "segments") and result.segments:
        for seg in result.segments:
            text = (seg.get("text") if isinstance(seg, dict) else getattr(seg, "text", "")).strip()
            start = seg.get("start") if isinstance(seg, dict) else getattr(seg, "start", 0)
            if text and not _is_likely_hallucination(text):
                segments.append({"start": float(start), "text": text})
    elif hasattr(result, "text") and result.text.strip():
        segments.append({"start": 0.0, "text": result.text.strip()})

    return segments


CLEANUP_PROMPT = """You are cleaning a phone call transcript between PATIENT and AGENT.

The raw transcript comes from automated speech recognition and may contain:
- Duplicate consecutive lines
- Whisper hallucinations on silent audio (gibberish, "feels weird", random words)
- Broken fragments that should be one turn

Return a cleaned JSON:
{
  "entries": [
    {"role": "agent", "text": "cleaned spoken line"},
    {"role": "patient", "text": "cleaned spoken line"}
  ]
}

Rules:
- Keep only real conversational speech
- Remove hallucinations, gibberish, and duplicate lines
- Merge fragments from the same speaker into one turn
- Preserve chronological order
- Keep agent greetings and patient responses that make sense
- If patient never spoke clearly, omit patient entries rather than keep garbage"""


def _consolidate_agent_entries(
    client: OpenAI, entries: list[TranscriptEntry], scenario_name: str
) -> list[TranscriptEntry]:
    """Clean agent-only whisper segments; leave patient entries untouched."""
    agent_entries = [e for e in entries if e.role == "agent"]
    if len(agent_entries) < 2:
        return entries

    raw = [{"role": e.role, "text": e.text, "timestamp": e.timestamp} for e in agent_entries]
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": CLEANUP_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Scenario: {scenario_name}\n\n"
                    f"Clean AGENT lines only:\n{json.dumps(raw, indent=2)}"
                ),
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    parsed = json.loads(response.choices[0].message.content or "{}")
    cleaned = parsed.get("entries", [])
    if not cleaned:
        return entries

    patient_entries = [e for e in entries if e.role == "patient"]
    agent_result = [
        TranscriptEntry(
            role="agent",
            text=e.get("text", "").strip(),
            timestamp=format_offset_timestamp(i * 8.0),
        )
        for i, e in enumerate(cleaned)
        if e.get("role") == "agent" and e.get("text", "").strip()
    ]
    return merge_realtime_and_whisper(patient_entries, agent_result)


def _transcribe_dual_channel(
    client: OpenAI,
    recording_path: Path,
    transcript: CallTranscript,
) -> bool:
    """Transcribe patient and agent channels separately. Returns True on success."""
    with tempfile.TemporaryDirectory() as tmp:
        raw_channels = _split_stereo_channels(recording_path, Path(tmp))
        if not raw_channels:
            return False

        ch0_wav, ch1_wav = raw_channels
        patient_wav, agent_wav = _identify_channels(
            client, ch0_wav, ch1_wav, Path(tmp)
        )

        patient_segs = _whisper_segments(
            client,
            patient_wav,
            prompt="Patient on a phone call speaking English or Spanish.",
        )
        agent_segs = _whisper_segments(
            client,
            agent_wav,
            prompt="Medical office AI receptionist greeting and questions.",
        )

        if not patient_segs and not agent_segs:
            return False

        merged: list[tuple[float, str, str]] = []
        for seg in patient_segs:
            merged.append((seg["start"], "patient", seg["text"]))
        for seg in agent_segs:
            merged.append((seg["start"], "agent", seg["text"]))
        merged.sort(key=lambda x: x[0])

        for start, role, text in merged:
            transcript.add_entry(role, text, format_offset_timestamp(start))

        logger.info(
            "Dual-channel Whisper: %d patient + %d agent segments",
            len(patient_segs),
            len(agent_segs),
        )
        return True


def _transcribe_mono_diarized(
    client: OpenAI,
    recording_path: Path,
    transcript: CallTranscript,
) -> bool:
    """Fallback: transcribe full recording and use GPT to diarize."""
    with recording_path.open("rb") as audio_file:
        whisper_result = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="verbose_json",
        )

    raw_text = whisper_result.text if hasattr(whisper_result, "text") else str(whisper_result)
    if not raw_text.strip():
        return False

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": DIARIZE_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Scenario: {transcript.scenario_name}\n\n"
                    f"Raw transcription:\n{raw_text}"
                ),
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )

    parsed = json.loads(response.choices[0].message.content or "{}")
    entries = parsed.get("entries", [])
    if not entries:
        return False

    for i, entry in enumerate(entries):
        role = entry.get("role", "")
        text = entry.get("text", "").strip()
        if not text:
            continue
        ts = format_offset_timestamp(i * 5.0)  # approximate ordering
        if role in ("patient", "agent"):
            transcript.add_entry(role, text, ts)

    logger.info("Mono diarization produced %d entries", len(transcript.entries))
    return True


def transcribe_recording(
    recording_path: Path,
    transcript: CallTranscript,
    api_key: str,
) -> CallTranscript:
    """Transcribe recording via Whisper into a fresh transcript object."""
    whisper_transcript = CallTranscript(
        call_id=transcript.call_id,
        scenario_id=transcript.scenario_id,
        scenario_name=transcript.scenario_name,
        started_at=transcript.started_at,
        ended_at=transcript.ended_at,
    )

    if not recording_path.exists():
        logger.warning("Recording not found: %s", recording_path)
        return whisper_transcript

    client = OpenAI(api_key=api_key)
    logger.info("Transcribing recording: %s", recording_path.name)

    if _transcribe_dual_channel(client, recording_path, whisper_transcript):
        return whisper_transcript

    logger.info("Dual-channel split unavailable, falling back to mono diarization")
    _transcribe_mono_diarized(client, recording_path, whisper_transcript)
    return whisper_transcript


def enrich_transcript_from_recording(
    recording_path: Path,
    transcript: CallTranscript,
    api_key: str,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Merge realtime OpenAI transcript with Whisper recording transcription.

    Patient lines: preserved from realtime OpenAI events (authoritative).
    Agent lines: from Whisper dual-channel recording (captures remote party).
    """
    if any(e.role == "patient" for e in transcript.entries):
        save_realtime_snapshot(transcript, output_dir)
        realtime_entries = transcript.snapshot_entries()
    else:
        snapshot = load_realtime_snapshot(transcript.call_id, output_dir)
        if snapshot:
            logger.info(
                "Loaded %d realtime snapshot entries for %s",
                len(snapshot),
                transcript.call_id,
            )
            realtime_entries = snapshot
        else:
            realtime_entries = transcript.snapshot_entries()

    realtime_patient_count = len([e for e in realtime_entries if e.role == "patient"])
    logger.info(
        "Merging transcript for %s — %d realtime entries (%d patient)",
        transcript.call_id,
        len(realtime_entries),
        realtime_patient_count,
    )

    whisper_transcript = transcribe_recording(recording_path, transcript, api_key)
    whisper_patient_count = len(whisper_transcript.entries_by_role("patient"))
    whisper_agent_count = len(whisper_transcript.entries_by_role("agent"))
    logger.info(
        "Whisper produced %d patient + %d agent segments",
        whisper_patient_count,
        whisper_agent_count,
    )

    merged = merge_realtime_and_whisper(
        realtime_entries, whisper_transcript.snapshot_entries()
    )

    client = OpenAI(api_key=api_key)
    merged = _consolidate_agent_entries(client, merged, transcript.scenario_name)
    merged = normalize_entry_timestamps(merged)

    transcript.clear_entries()
    for entry in merged:
        transcript.add_entry(entry.role, entry.text, entry.timestamp)

    logger.info(
        "Final merged transcript: %d entries (%d patient, %d agent)",
        len(transcript.entries),
        len(transcript.entries_by_role("patient")),
        len(transcript.entries_by_role("agent")),
    )
    return save_transcript(transcript, output_dir)


def retranscribe_all_recordings(
    recordings_dir: Path,
    transcripts_dir: Path,
    api_key: str,
) -> int:
    """Re-transcribe all recordings that have matching metadata."""
    count = 0
    for rec_path in sorted(recordings_dir.glob("recording-*.mp3")):
        # Skip legacy misnamed files (CallSid instead of call_id)
        call_id = rec_path.stem.replace("recording-", "")
        if call_id.startswith("CA"):
            continue

        json_path = transcripts_dir / f"transcript-{call_id}.json"
        realtime_path = transcripts_dir / f"realtime-transcript-{call_id}.json"
        if realtime_path.exists():
            data = json.loads(realtime_path.read_text(encoding="utf-8"))
            transcript = CallTranscript(
                call_id=data["call_id"],
                scenario_id=data["scenario_id"],
                scenario_name=data["scenario_name"],
                started_at=data["started_at"],
                ended_at=data.get("ended_at"),
            )
            for entry in data.get("entries", []):
                transcript.add_entry(
                    entry["role"], entry["text"], entry["timestamp"]
                )
        elif json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            transcript = CallTranscript(
                call_id=data["call_id"],
                scenario_id=data["scenario_id"],
                scenario_name=data["scenario_name"],
                started_at=data["started_at"],
                ended_at=data.get("ended_at"),
            )
        else:
            transcript = CallTranscript(
                call_id=call_id,
                scenario_id="unknown",
                scenario_name="Unknown",
                started_at="",
            )

        enrich_transcript_from_recording(rec_path, transcript, api_key, transcripts_dir)
        count += 1
        logger.info("Re-transcribed %s (%d entries)", call_id, len(transcript.entries))

    return count
