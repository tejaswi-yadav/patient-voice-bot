"""Analyze dual-channel recording levels (patient vs agent leg)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

_BUNDLED_FFPROBE = (
    PROJECT_ROOT
    / "tools"
    / "ffmpeg"
    / "ffmpeg-8.1.1-essentials_build"
    / "bin"
    / "ffprobe.exe"
)
_BUNDLED_FFMPEG = _BUNDLED_FFPROBE.parent / "ffmpeg.exe"


def _ffmpeg_bin() -> str | None:
    system = shutil.which("ffmpeg")
    if system:
        return system
    if _BUNDLED_FFMPEG.exists():
        return str(_BUNDLED_FFMPEG)
    return None


def _channel_rms_db(ffmpeg: str, wav_path: Path) -> float:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-i",
        str(wav_path),
        "-af",
        "astats=metadata=1:reset=1",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    for line in result.stderr.splitlines():
        if "RMS level dB" in line:
            try:
                return float(line.split("RMS level dB:")[1].strip())
            except ValueError:
                continue
    return -100.0


def analyze_recording(recording_path: Path) -> dict[str, float | str]:
    """Return RMS dB for each stereo channel (ch0, ch1)."""
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        return {"error": "ffmpeg not found"}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        ch0 = tmp_dir / "ch0.wav"
        ch1 = tmp_dir / "ch1.wav"
        for ch, out in (("c0", ch0), ("c1", ch1)):
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(recording_path),
                "-af",
                f"pan=mono|c0={ch}",
                str(out),
            ]
            subprocess.run(cmd, capture_output=True, check=False)

        return {
            "ch0_rms_db": _channel_rms_db(ffmpeg, ch0),
            "ch1_rms_db": _channel_rms_db(ffmpeg, ch1),
        }


def check_recordings_dir(recordings_dir: Path) -> list[dict[str, object]]:
    """Analyze all MP3s; flag when one channel is much quieter than the other."""
    results: list[dict[str, object]] = []
    for path in sorted(recordings_dir.glob("recording-*.mp3")):
        call_id = path.stem.replace("recording-", "")
        levels = analyze_recording(path)
        if "error" in levels:
            results.append({"call_id": call_id, **levels})
            continue

        ch0 = float(levels["ch0_rms_db"])
        ch1 = float(levels["ch1_rms_db"])
        gap = abs(ch0 - ch1)
        # Both legs should be roughly above -45 dB for "reviewable" dual audio
        quiet = min(ch0, ch1)
        ok = quiet > -45.0 and gap < 35.0
        results.append(
            {
                "call_id": call_id,
                "ch0_rms_db": round(ch0, 1),
                "ch1_rms_db": round(ch1, 1),
                "gap_db": round(gap, 1),
                "ok": ok,
            }
        )
    return results


def print_recording_report(recordings_dir: Path) -> int:
    rows = check_recordings_dir(recordings_dir)
    if not rows:
        print("No recordings found.")
        return 1

    ok_count = sum(1 for r in rows if r.get("ok"))
    print("=== recording channel check ===\n")
    print(f"{'call_id':<18} {'ch0 dB':>8} {'ch1 dB':>8} {'gap':>6}  ok")
    print("-" * 50)
    for row in rows:
        if "error" in row:
            print(f"{row['call_id']:<18} ERROR: {row['error']}")
            continue
        mark = "yes" if row["ok"] else "LOW"
        print(
            f"{row['call_id']:<18} {row['ch0_rms_db']:>8} {row['ch1_rms_db']:>8} "
            f"{row['gap_db']:>6}  {mark}"
        )
    print()
    print(f"{ok_count}/{len(rows)} recordings have balanced, audible dual channels")
    print("Target: both channels louder than -45 dB after re-run with PATIENT_OUTBOUND_GAIN")
    return 0 if ok_count == len(rows) else 1
