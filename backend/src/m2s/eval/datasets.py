"""Evaluation datasets.

MAESTRO v3 is a ~100GB zip; we pull individual members with HTTP Range
requests (remotezip) so only the pieces we need are downloaded.
"""

from __future__ import annotations

import csv
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MAESTRO_BASE = "https://storage.googleapis.com/magentadata/datasets/maestro/v3.0.0"
MAESTRO_ZIP = f"{MAESTRO_BASE}/maestro-v3.0.0.zip"
MAESTRO_CSV = f"{MAESTRO_BASE}/maestro-v3.0.0.csv"


@dataclass(frozen=True)
class MaestroPiece:
    composer: str
    title: str
    split: str
    midi: str
    audio: str
    duration: float

    @property
    def slug(self) -> str:
        return Path(self.audio).stem


def maestro_index(root: Path) -> list[MaestroPiece]:
    root.mkdir(parents=True, exist_ok=True)
    csv_path = root / "maestro-v3.0.0.csv"
    if not csv_path.exists():
        urllib.request.urlretrieve(MAESTRO_CSV, csv_path)
    with csv_path.open(newline="") as f:
        return [
            MaestroPiece(
                composer=r["canonical_composer"],
                title=r["canonical_title"],
                split=r["split"],
                midi=r["midi_filename"],
                audio=r["audio_filename"],
                duration=float(r["duration"]),
            )
            for r in csv.DictReader(f)
        ]


def fetch_maestro(root: Path, pieces: list[MaestroPiece]) -> list[tuple[Path, Path]]:
    """Download (audio, midi) for the given pieces into root; returns local paths."""
    from remotezip import RemoteZip

    out: list[tuple[Path, Path]] = []
    with RemoteZip(MAESTRO_ZIP) as z:
        for p in pieces:
            paths = []
            for member in (p.audio, p.midi):
                dest = root / Path(member).name
                if not dest.exists():
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    with z.open(f"maestro-v3.0.0/{member}") as src, tmp.open("wb") as dst:
                        while chunk := src.read(1 << 20):
                            dst.write(chunk)
                    tmp.rename(dest)
                paths.append(dest)
            out.append((paths[0], paths[1]))
    return out
