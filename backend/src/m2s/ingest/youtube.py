"""Optional YouTube input via yt-dlp (needs the Deno JS runtime since yt-dlp 2025.11).

Any failure raises YoutubeError with a hint to upload the file instead.
"""

from __future__ import annotations

import shutil
from pathlib import Path


class YoutubeError(RuntimeError):
    pass


def download_audio(url: str, out_dir: Path, max_seconds: float | None = None) -> tuple[Path, str]:
    """Download best audio to out_dir. Returns (path, title)."""
    try:
        import yt_dlp
    except ImportError as e:
        raise YoutubeError("yt-dlp is not installed (uv sync --extra youtube)") from e
    if shutil.which("deno") is None:
        raise YoutubeError("Deno is required by yt-dlp for YouTube (brew install deno)")

    out_dir.mkdir(parents=True, exist_ok=True)

    def check_length(info, *, incomplete):
        d = info.get("duration")
        if max_seconds and d and d > max_seconds:
            return f"video is {d:.0f}s; limit is {max_seconds:.0f}s"
        return None

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "match_filter": check_length,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:  # yt-dlp raises many error types
        raise YoutubeError(f"YouTube download failed: {e}. Please upload the audio file instead.") from e
    files = sorted(out_dir.glob("source.*"))
    if not files:
        reason = "filtered by length limit" if info else "no file produced"
        raise YoutubeError(f"YouTube download failed ({reason}). Please upload the audio file instead.")
    return files[0], (info or {}).get("title") or "YouTube audio"
