#!/usr/bin/env python3
"""CLI entrypoint — serve, call, batch, verify."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn
from twilio.rest import Client

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.call_manager import (
    generate_call_id,
    place_call,
    run_call_batch,
    wait_for_call_completion,
)
from src.config import CALLS_DIR, METADATA_DIR, RECORDINGS_DIR, TRANSCRIPTS_DIR, get_settings
from src.recorder import redownload_missing_recordings
from src.recording_check import print_recording_report
from src.scenarios import get_scenario, list_scenarios
from src.whisper_transcribe import retranscribe_all_recordings
from src.server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run")


def cmd_serve(args: argparse.Namespace) -> None:
    settings = get_settings()
    app = create_app(settings)
    logger.info("Starting server on port %s", settings.port)
    logger.info("Target number: %s", settings.target_phone_number)
    logger.info("Media stream: %s", settings.media_stream_url)
    uvicorn.run(app, host="0.0.0.0", port=settings.port)


def cmd_call(args: argparse.Namespace) -> None:
    settings = get_settings()
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    scenario = get_scenario(args.scenario)
    call_id = generate_call_id(args.index)

    call_sid = place_call(client, settings, scenario, call_id)
    print(f"Call placed: {call_sid} (call_id={call_id}, scenario={scenario.id})")

    if args.wait:
        status = wait_for_call_completion(client, call_sid)
        print(f"Call finished with status: {status}")


def cmd_batch(args: argparse.Namespace) -> None:
    settings = get_settings()
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    scenario_ids = None
    if args.scenarios:
        scenario_ids = [s.strip() for s in args.scenarios.split(",")]

    print(f"Running batch calls to {settings.target_phone_number}")
    print("Ensure the server is running (python run.py serve) and ngrok is active.")
    print()

    results = run_call_batch(
        client,
        settings,
        scenario_ids=scenario_ids,
        start_index=args.start_index,
        wait_for_completion=args.wait,
    )

    print("\n=== Batch Results ===")
    for r in results:
        status = r.get("status", "unknown")
        print(f"  {r['call_id']}: {r['scenario_id']} -> {status}")
        if "error" in r:
            print(f"    Error: {r['error']}")


def cmd_verify(_args: argparse.Namespace) -> None:
    recordings = list(RECORDINGS_DIR.glob("recording-*.mp3"))
    transcripts = list(TRANSCRIPTS_DIR.glob("transcript-*.txt"))
    bug_report = CALLS_DIR / "BUG_REPORT.md"

    both_sides = 0
    agent_only = 0
    for p in transcripts:
        content = p.read_text(encoding="utf-8")
        if "AGENT:" in content and "PATIENT:" in content:
            both_sides += 1
        elif "AGENT:" in content:
            agent_only += 1

    print("=== calls/ check ===\n")
    print(f"recordings:  {len(recordings)} (need 10)")
    print(f"transcripts: {len(transcripts)}")
    print(f"both sides:  {both_sides} (need 10)")
    if agent_only:
        print(f"agent-only:  {agent_only}")
    print()

    issue_count = 0
    if bug_report.exists():
        issue_count = bug_report.read_text(encoding="utf-8").count("**Severity:**")

    ok = len(recordings) >= 10 and both_sides >= 10 and issue_count > 0
    if ok:
        print("looks good for submission (recordings + two-sided transcripts + bugs)")
    else:
        print("still missing something — run batch --wait, then retranscribe if needed")


def cmd_download_recordings(_args: argparse.Namespace) -> None:
    settings = get_settings()
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    print("Re-downloading missing recordings from Twilio...")
    count = redownload_missing_recordings(
        client,
        METADATA_DIR,
        RECORDINGS_DIR,
        settings.twilio_account_sid,
        settings.twilio_auth_token,
    )
    print(f"Downloaded {count} recording(s). Run 'python run.py retranscribe' next.")


def cmd_retranscribe(_args: argparse.Namespace) -> None:
    settings = get_settings()
    print("Re-transcribing all recordings from audio (dual-channel Whisper)...")
    count = retranscribe_all_recordings(
        RECORDINGS_DIR, TRANSCRIPTS_DIR, settings.openai_api_key
    )
    print(f"Done — re-transcribed {count} recordings.")
    print("Run 'python run.py verify' to check results.")


def cmd_check_recordings(_args: argparse.Namespace) -> None:
    raise SystemExit(print_recording_report(RECORDINGS_DIR))


def cmd_list(_args: argparse.Namespace) -> None:
    print("Available scenarios:\n")
    for s in list_scenarios():
        edge = " [edge case]" if s["edge_case"] else ""
        print(f"  {s['id']}{edge}")
        print(f"    {s['name']}: {s['description']}")
        print()


def main() -> None:
    for d in (CALLS_DIR, RECORDINGS_DIR, TRANSCRIPTS_DIR, METADATA_DIR):
        d.mkdir(parents=True, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="Patient Voice Bot — AI caller for Athena agent QA testing"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve_parser = sub.add_parser("serve", help="Start webhook/media-stream server")
    serve_parser.set_defaults(func=cmd_serve)

    call_parser = sub.add_parser("call", help="Place a single test call")
    call_parser.add_argument(
        "--scenario",
        default="schedule-routine",
        help="Scenario ID (default: schedule-routine)",
    )
    call_parser.add_argument("--index", type=int, default=1, help="Call index number")
    call_parser.add_argument(
        "--wait", action="store_true", help="Wait for call to complete"
    )
    call_parser.set_defaults(func=cmd_call)

    batch_parser = sub.add_parser("batch", help="Run multiple test calls")
    batch_parser.add_argument(
        "--scenarios",
        help="Comma-separated scenario IDs (default: first 10)",
    )
    batch_parser.add_argument(
        "--start-index", type=int, default=1, help="Starting call index"
    )
    batch_parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for each call to finish and allow recording download",
    )
    batch_parser.set_defaults(func=cmd_batch)

    list_parser = sub.add_parser("list", help="List available scenarios")
    list_parser.set_defaults(func=cmd_list)

    verify_parser = sub.add_parser("verify", help="Check submission readiness")
    verify_parser.set_defaults(func=cmd_verify)

    retranscribe_parser = sub.add_parser(
        "retranscribe", help="Re-build all transcripts from recordings"
    )
    retranscribe_parser.set_defaults(func=cmd_retranscribe)

    download_parser = sub.add_parser(
        "download-recordings",
        help="Re-fetch missing MP3 recordings from Twilio",
    )
    download_parser.set_defaults(func=cmd_download_recordings)

    check_rec = sub.add_parser(
        "check-recordings",
        help="Analyze stereo MP3 levels (patient vs agent channel)",
    )
    check_rec.set_defaults(func=cmd_check_recordings)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
