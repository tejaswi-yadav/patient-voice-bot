"""Build and persist conversation transcripts from realtime events."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class TranscriptEntry:
    role: str
    text: str
    timestamp: str


@dataclass
class CallTranscript:
    call_id: str
    scenario_id: str
    scenario_name: str
    started_at: str
    ended_at: str | None = None
    entries: list[TranscriptEntry] = field(default_factory=list)

    def add_patient(self, text: str) -> None:
        if text.strip():
            self.entries.append(
                TranscriptEntry(
                    role="patient",
                    text=text.strip(),
                    timestamp=_now_iso(),
                )
            )

    def add_agent(self, text: str) -> None:
        if text.strip():
            self.entries.append(
                TranscriptEntry(
                    role="agent",
                    text=text.strip(),
                    timestamp=_now_iso(),
                )
            )

    def clear_entries(self) -> None:
        self.entries.clear()

    def snapshot_entries(self) -> list[TranscriptEntry]:
        """Return a shallow copy of current entries."""
        return [
            TranscriptEntry(role=e.role, text=e.text, timestamp=e.timestamp)
            for e in self.entries
        ]

    def entries_by_role(self, role: str) -> list[TranscriptEntry]:
        return [e for e in self.entries if e.role == role]

    def add_entry(self, role: str, text: str, timestamp: str) -> None:
        if text.strip():
            self.entries.append(
                TranscriptEntry(role=role, text=text.strip(), timestamp=timestamp)
            )

    def to_text(self) -> str:
        lines = [
            f"Call ID: {self.call_id}",
            f"Scenario: {self.scenario_name} ({self.scenario_id})",
            f"Started: {self.started_at}",
            f"Ended: {self.ended_at or 'in progress'}",
            "",
            "=" * 60,
            "",
        ]
        for entry in self.entries:
            speaker = "PATIENT" if entry.role == "patient" else "AGENT"
            lines.append(f"[{entry.timestamp}] {speaker}: {entry.text}")
            lines.append("")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "entries": [
                {
                    "role": e.role,
                    "text": e.text,
                    "timestamp": e.timestamp,
                }
                for e in self.entries
            ],
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def format_offset_timestamp(seconds: float) -> str:
    """Format seconds from call start as MM:SS."""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _parse_timestamp_seconds(ts: str) -> float:
    """Parse MM:SS or HH:MM:SS to comparable seconds."""
    parts = ts.strip().split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        pass
    return 0.0


def _is_call_offset_timestamp(ts: str) -> bool:
    """True for MM:SS timestamps from Whisper (not wall-clock HH:MM:SS)."""
    parts = ts.strip().split(":")
    if len(parts) != 2:
        return False
    try:
        return int(parts[0]) < 60
    except ValueError:
        return False


def _patient_sort_seconds(
    index: int, entry: TranscriptEntry, agent_entries: list[TranscriptEntry]
) -> float:
    """Place realtime patient lines after agent greetings when timestamps are wall-clock."""
    if _is_call_offset_timestamp(entry.timestamp):
        return _parse_timestamp_seconds(entry.timestamp)

    agent_times = sorted(
        _parse_timestamp_seconds(a.timestamp)
        for a in agent_entries
        if _is_call_offset_timestamp(a.timestamp)
    )
    if agent_times:
        anchor = agent_times[min(3, len(agent_times) - 1)] + 2.0
    else:
        anchor = 5.0
    return anchor + index * 8.0


def merge_realtime_and_whisper(
    realtime_entries: list[TranscriptEntry],
    whisper_entries: list[TranscriptEntry],
) -> list[TranscriptEntry]:
    """Merge realtime patient speech with Whisper agent transcription."""
    realtime_patient = [e for e in realtime_entries if e.role == "patient"]
    realtime_agent = [e for e in realtime_entries if e.role == "agent"]
    whisper_patient = [e for e in whisper_entries if e.role == "patient"]
    whisper_agent = [e for e in whisper_entries if e.role == "agent"]

    patient_entries = realtime_patient or whisper_patient
    agent_entries = whisper_agent or realtime_agent
    agents_sorted = sorted(
        agent_entries,
        key=lambda e: (
            _parse_timestamp_seconds(e.timestamp)
            if _is_call_offset_timestamp(e.timestamp)
            else 0.0
        ),
    )

    merged: list[tuple[float, int, TranscriptEntry]] = []
    for i, entry in enumerate(agents_sorted):
        ts = (
            _parse_timestamp_seconds(entry.timestamp)
            if _is_call_offset_timestamp(entry.timestamp)
            else float(i)
        )
        merged.append((ts, i, entry))
    for i, entry in enumerate(patient_entries):
        merged.append((_patient_sort_seconds(i, entry, agents_sorted), 1000 + i, entry))

    merged.sort(key=lambda x: (x[0], x[1]))
    return [entry for _, _, entry in merged]


def normalize_entry_timestamps(
    entries: list[TranscriptEntry], seconds_per_turn: float = 6.0
) -> list[TranscriptEntry]:
    """Reassign MM:SS timestamps in conversation order for readable transcripts."""
    return [
        TranscriptEntry(
            role=entry.role,
            text=entry.text,
            timestamp=format_offset_timestamp(i * seconds_per_turn),
        )
        for i, entry in enumerate(entries)
    ]


def save_realtime_snapshot(transcript: CallTranscript, output_dir: Path) -> Path:
    """Persist realtime OpenAI events before Whisper merge overwrites the final transcript."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"realtime-transcript-{transcript.call_id}.json"
    path.write_text(
        json.dumps(transcript.to_dict(), indent=2),
        encoding="utf-8",
    )
    return path


def load_realtime_snapshot(
    call_id: str, output_dir: Path
) -> list[TranscriptEntry] | None:
    """Load preserved realtime entries for retranscribe / merge."""
    path = output_dir / f"realtime-transcript-{call_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        TranscriptEntry(
            role=e["role"],
            text=e["text"],
            timestamp=e["timestamp"],
        )
        for e in data.get("entries", [])
    ]


def save_transcript(transcript: CallTranscript, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    txt_path = output_dir / f"transcript-{transcript.call_id}.txt"
    json_path = output_dir / f"transcript-{transcript.call_id}.json"

    txt_path.write_text(transcript.to_text(), encoding="utf-8")
    json_path.write_text(
        json.dumps(transcript.to_dict(), indent=2),
        encoding="utf-8",
    )
    return txt_path, json_path
