"""MusicXML → PDF with the MuseScore 4 CLI.

MuseScore 4.7 headless crashes during process teardown *after* writing the output
(SIGABRT, `mutex lock failed`), so success = a fresh, non-empty output file,
regardless of the exit code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from m2s.config import settings


class RenderError(RuntimeError):
    pass


def musicxml_to_pdf(xml_path: Path, pdf_path: Path, timeout_s: float = 60) -> Path:
    mscore = settings().mscore_path
    if not mscore.exists():
        raise RenderError(f"MuseScore not found at {mscore} (brew install --cask musescore)")
    pdf_path.unlink(missing_ok=True)
    try:
        r = subprocess.run(
            [str(mscore), "-o", str(pdf_path), str(xml_path)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"MuseScore timed out after {timeout_s}s") from e
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise RenderError(f"MuseScore produced no PDF (rc={r.returncode}): {r.stderr[-500:]}")
    return pdf_path
