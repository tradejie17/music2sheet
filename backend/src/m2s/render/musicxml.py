"""ScoreDoc → MusicXML text (deterministic) + idmap."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from m2s.models.scoredoc import ScoreDoc
from m2s.notation.build_m21 import build_score


def render_musicxml(doc: ScoreDoc) -> tuple[str, dict[str, str]]:
    score, idmap = build_score(doc)
    with tempfile.TemporaryDirectory() as td:
        # notation (rests, ties, beams, tuplets, accidentals) is complete: skip music21's own pass
        path = Path(score.write("musicxml", fp=Path(td) / "score.musicxml", makeNotation=False))
        xml = path.read_text()
    xml = re.sub(r"\s*<encoding-date>[^<]*</encoding-date>", "", xml)
    xml = re.sub(r'<score-partwise version="[^"]*"', '<score-partwise version="4.0"', xml, count=1)
    if doc.pickup_ticks:  # anacrusis: tell readers the short first bar is intentional
        xml = re.sub(
            r'<measure (implicit="no" )?number="0"', '<measure implicit="yes" number="0"', xml, count=1
        )
    return xml, idmap
