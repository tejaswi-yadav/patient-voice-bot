"""Outbound calls — Twilio only dials the assessment number."""

from __future__ import annotations

import logging
import time
import xml.sax.saxutils as saxutils
from datetime import datetime, timezone

from twilio.rest import Client

from src.config import ALLOWED_TARGET, Settings
from src.scenarios import SUBMISSION_BATCH_IDS, Scenario, get_scenario

logger = logging.getLogger(__name__)


def generate_call_id(index: int) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%m%d-%H%M")
    return f"{index:02d}-{timestamp}"


def build_outbound_twiml(settings: Settings, scenario_id: str, call_id: str) -> str:
    stream_url = saxutils.escape(settings.media_stream_url, {'"': "&quot;"})
    scenario_id_escaped = saxutils.escape(scenario_id, {'"': "&quot;"})
    call_id_escaped = saxutils.escape(call_id, {'"': "&quot;"})

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<Stream url="{stream_url}">'
        f'<Parameter name="scenario_id" value="{scenario_id_escaped}" />'
        f'<Parameter name="call_id" value="{call_id_escaped}" />'
        "</Stream>"
        "</Connect>"
        "</Response>"
    )


def place_call(
    client: Client,
    settings: Settings,
    scenario: Scenario,
    call_id: str,
) -> str:
    """Place a single outbound call. Returns Twilio Call SID."""
    if settings.target_phone_number != ALLOWED_TARGET:
        raise ValueError(
            f"Refusing to call {settings.target_phone_number}. "
            f"Only {ALLOWED_TARGET} is permitted."
        )

    twiml = build_outbound_twiml(settings, scenario.id, call_id)

    logger.info(
        "Placing call %s -> %s (scenario: %s)",
        settings.phone_number_from,
        settings.target_phone_number,
        scenario.id,
    )

    call = client.calls.create(
        from_=settings.phone_number_from,
        to=settings.target_phone_number,
        twiml=twiml,
        record=True,
        recording_channels="dual",
        recording_status_callback=settings.recording_callback_url,
        recording_status_callback_method="POST",
        status_callback=settings.status_callback_url,
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        status_callback_method="POST",
        timeout=30,
    )

    logger.info("Call initiated: SID=%s call_id=%s", call.sid, call_id)
    return call.sid


def run_call_batch(
    client: Client,
    settings: Settings,
    scenario_ids: list[str] | None = None,
    start_index: int = 1,
    *,
    wait_for_completion: bool = False,
    recording_grace_seconds: float = 30.0,
) -> list[dict[str, str]]:
    """Place a series of calls with cooldown between each."""
    if scenario_ids:
        scenarios = [get_scenario(sid) for sid in scenario_ids]
    else:
        scenarios = [get_scenario(sid) for sid in SUBMISSION_BATCH_IDS]

    results: list[dict[str, str]] = []

    for i, scenario in enumerate(scenarios, start=start_index):
        call_id = generate_call_id(i)
        try:
            call_sid = place_call(client, settings, scenario, call_id)
            status = "initiated"
            if wait_for_completion:
                status = wait_for_call_completion(client, call_sid)
                logger.info(
                    "Call %s finished (%s); waiting %.0fs for recording...",
                    call_id,
                    status,
                    recording_grace_seconds,
                )
                time.sleep(recording_grace_seconds)
            results.append(
                {
                    "call_id": call_id,
                    "call_sid": call_sid,
                    "scenario_id": scenario.id,
                    "status": status,
                }
            )
        except Exception as exc:
            logger.exception("Call %s failed", call_id)
            results.append(
                {
                    "call_id": call_id,
                    "scenario_id": scenario.id,
                    "status": "failed",
                    "error": str(exc),
                }
            )

        if i < start_index + len(scenarios) - 1:
            logger.info(
                "Cooling down %ss before next call...",
                settings.call_cooldown_seconds,
            )
            time.sleep(settings.call_cooldown_seconds)

    return results


def wait_for_call_completion(
    client: Client,
    call_sid: str,
    poll_interval: float = 5.0,
    timeout: float = 240.0,
) -> str:
    """Poll until call reaches a terminal state."""
    elapsed = 0.0
    while elapsed < timeout:
        call = client.calls(call_sid).fetch()
        if call.status in ("completed", "busy", "no-answer", "failed", "canceled"):
            return call.status
        time.sleep(poll_interval)
        elapsed += poll_interval
    return "timeout"
