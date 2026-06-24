"""Download and manage Twilio call recordings."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
from twilio.rest import Client

logger = logging.getLogger(__name__)


def download_recording(
    client: Client,
    recording_sid: str,
    call_id: str,
    output_dir: Path,
    account_sid: str,
    auth_token: str,
) -> Path | None:
    """Download a Twilio recording as MP3."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"recording-{call_id}.mp3"

    try:
        recording = client.recordings(recording_sid).fetch()
        media_url = f"https://api.twilio.com{recording.uri.replace('.json', '.mp3')}"

        with httpx.Client(timeout=60.0) as http:
            response = http.get(media_url, auth=(account_sid, auth_token))
            response.raise_for_status()
            output_path.write_bytes(response.content)

        logger.info("Saved recording to %s", output_path)
        return output_path
    except Exception:
        logger.exception("Failed to download recording %s", recording_sid)
        return None


def save_call_metadata(call_id: str, metadata: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"metadata-{call_id}.json"
    path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return path


def redownload_missing_recordings(
    client: Client,
    metadata_dir: Path,
    output_dir: Path,
    account_sid: str,
    auth_token: str,
) -> int:
    """Re-fetch recordings from Twilio for calls whose MP3 is missing locally."""
    output_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for meta_path in sorted(metadata_dir.glob("metadata-*.json")):
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        call_id = meta_path.stem.replace("metadata-", "")
        output_path = output_dir / f"recording-{call_id}.mp3"
        if output_path.exists() and output_path.stat().st_size > 1000:
            continue

        call_sid = data.get("call_sid")
        if not call_sid:
            continue

        try:
            recordings = client.recordings.list(call_sid=call_sid, limit=5)
            for recording in recordings:
                path = download_recording(
                    client=client,
                    recording_sid=recording.sid,
                    call_id=call_id,
                    output_dir=output_dir,
                    account_sid=account_sid,
                    auth_token=auth_token,
                )
                if path:
                    count += 1
                    break
        except Exception:
            logger.exception("Failed to re-download recording for %s", call_id)

    return count
