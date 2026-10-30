"""Re-run automatic decisions on an edited ScoreDoc, keeping everything the user locked."""

from __future__ import annotations

from typing import Literal

from m2s.hands.heuristic_dp import separate_hands
from m2s.hands.voices import assign_voices
from m2s.models.scoredoc import ScoreDoc
from m2s.notation.key_spelling import estimate_key

Rederive = Literal["hands", "voices", "spelling", "key"]


def sustain_spans(doc: ScoreDoc) -> list[tuple[int, int]]:
    return [(p.start, p.end) for p in doc.pedals if p.kind == "sustain"]


def beat_ticks(doc: ScoreDoc) -> int:
    m = doc.meters[0]
    return doc.ppq * 3 // 2 if (m.den == 8 and m.num % 3 == 0 and m.num > 3) else doc.ppq * 4 // m.den


def arrange_hands(doc: ScoreDoc, style: str, use_svsep: bool = True) -> None:
    """Heuristic hands + voices; then (optionally) piano_svsep re-predicts the staves of
    unlocked notes from a rendering of that result, and voices are re-assigned."""
    from m2s.hands.svsep import refine_staffs

    separate_hands(doc.notes, style)
    assign_voices(doc.notes, beat_ticks(doc), sustain_spans(doc))
    used = "heuristic"
    if use_svsep and refine_staffs(doc):
        assign_voices(doc.notes, beat_ticks(doc), sustain_spans(doc))
        used = "piano_svsep"
    doc.provenance.versions["hands"] = used  # shown in the editor


def rederive(doc: ScoreDoc, what: Rederive, style: str = "classical", use_svsep: bool = True) -> ScoreDoc:
    doc = doc.model_copy(deep=True)
    if what == "hands":
        arrange_hands(doc, style, use_svsep)
    elif what == "voices":
        assign_voices(doc.notes, beat_ticks(doc), sustain_spans(doc))
    if what in ("hands", "voices") and doc.provenance.versions.get("simplify") in ("1", "2"):
        from m2s.notation.simplify import one_voice_per_staff

        one_voice_per_staff(doc)  # a readable / simplified score stays one voice per staff
    elif what == "spelling":
        for n in doc.notes:
            if "spelling" not in n.locked:
                n.spelling = None
    elif what == "key":
        doc.keys = [estimate_key(doc.notes)]
    return doc
