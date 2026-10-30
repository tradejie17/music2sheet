"""Normalize any input audio to a canonical WAV (44.1 kHz, ≤2 ch, PCM16).

The canonical file's sha256 is the content key for every downstream cache entry,
so `-bitexact` + stripped metadata keep the output byte-stable across runs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from m2s.pipeline.cache import file_sha256

SAMPLE_RATE = 44100


class IngestError(RuntimeError):
    pass


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise IngestError(f"ffprobe could not read {path.name}: {r.stderr.strip()[:300]}")
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except (KeyError, ValueError) as e:
        raise IngestError(f"{path.name}: no audio duration found") from e


def to_canonical_wav(src: Path, dst: Path, max_seconds: float | None = None) -> float:
    """Convert src → dst. Returns duration in seconds."""
    if shutil.which("ffmpeg") is None:
        raise IngestError("ffmpeg not found on PATH")
    duration = probe_duration(src)
    if max_seconds is not None and duration > max_seconds:
        raise IngestError(f"audio is {duration:.0f}s; limit is {max_seconds:.0f}s")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
        "-vn", "-map_metadata", "-1", "-ac", "2", "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le", "-bitexact", "-fflags", "+bitexact", str(dst),
    ]  # fmt: skip
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise IngestError(f"ffmpeg failed: {r.stderr.strip()[:500]}")
    return duration


def ingest(src: Path, work_dir: Path, max_seconds: float | None = None) -> tuple[Path, str, float]:
    """Return (canonical wav path, audio sha256, duration)."""
    wav = work_dir / "audio.wav"
    duration = to_canonical_wav(src, wav, max_seconds)
    return wav, file_sha256(wav), duration
