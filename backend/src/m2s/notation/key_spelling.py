"""Key estimation (profile correlation, duration-weighted) and pitch spelling.

Key profiles: Temperley (Kostka–Payne corpus), with the first and last 8 beats of the
piece weighted 3× (openings and cadences state the key). Chosen by a sweep over all
ASAP scores (`m2s eval-asap` data): key-signature accuracy 89.5% on full pieces vs
80.3% for Krumhansl–Kessler without edge weighting.

Spelling uses the line of fifths: diatonic pitch classes take the key signature's
spelling; the minor key's raised 7th is spelled as a leading tone; every other
chromatic pitch class takes the spelling closest to the key's centre on the line
of fifths. Context-sensitive spelling (e.g. partitura ps13) can replace this later;
a ScoreNote.spelling override always wins.
"""

from __future__ import annotations

import re

import numpy as np

from m2s.models.scoredoc import KeyChange, ScoreNote, Spelling

# Temperley (Kostka–Payne) key profiles
MAJOR = np.array([0.748, 0.060, 0.488, 0.082, 0.670, 0.460, 0.096, 0.715, 0.104, 0.366, 0.057, 0.400])
MINOR = np.array([0.712, 0.084, 0.474, 0.618, 0.049, 0.460, 0.105, 0.747, 0.404, 0.067, 0.133, 0.330])
EDGE_BEATS, EDGE_WEIGHT = 8, 3.0

STEPS = "FCGDAEB"  # line-of-fifths order, F = -1
STEP_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
# major-key tonic pc → key signature (prefer ≤ 5 flats/6 sharps; F#=6 over Gb=-6)
MAJOR_FIFTHS = {0: 0, 7: 1, 2: 2, 9: 3, 4: 4, 11: 5, 6: 6, 1: -5, 8: -4, 3: -3, 10: -2, 5: -1}


def lof_name(pos: int) -> tuple[str, int]:
    """Line-of-fifths position (C=0, G=1, F=-1, F#=6, Bb=-2…) → (step, alter)."""
    idx = pos + 1
    return STEPS[idx % 7], idx // 7


def lof_pos(step: str, alter: int) -> int:
    return STEPS.index(step) - 1 + 7 * alter


def estimate_key(notes: list[ScoreNote], ppq: int = 960) -> KeyChange:
    hist = np.zeros(12)
    if not notes:
        return KeyChange(tick=0, fifths=0, mode="major")
    first = min(n.start for n in notes)
    last = max(n.start + n.dur for n in notes)
    edge = EDGE_BEATS * ppq
    for n in notes:
        w = EDGE_WEIGHT if (n.start < first + edge or n.start > last - edge) else 1.0
        hist[n.pitch % 12] += n.dur * w
    if hist.sum() == 0:
        return KeyChange(tick=0, fifths=0, mode="major")
    best = (-2.0, 0, "major")
    for tonic in range(12):
        for mode, prof in (("major", MAJOR), ("minor", MINOR)):
            r = float(np.corrcoef(hist, np.roll(prof, tonic))[0, 1])
            if r > best[0]:
                best = (r, tonic, mode)
    _, tonic, mode = best
    major_pc = tonic if mode == "major" else (tonic + 3) % 12
    fifths = MAJOR_FIFTHS[major_pc]
    if mode == "minor" and major_pc == 6:  # E♭ minor (6♭) is far more common than D♯ minor (6♯)
        fifths = -6
    return KeyChange(tick=0, fifths=fifths, mode=mode)


def parse_key(text: str) -> KeyChange:
    m = re.fullmatch(r"\s*([A-Ga-g])([#b]?)\s+(major|minor)\s*", text)
    if not m:
        raise ValueError(f"bad key {text!r}")
    step, acc, mode = m.group(1).upper(), m.group(2), m.group(3)
    pos = lof_pos(step, {"#": 1, "b": -1, "": 0}[acc])
    fifths = pos if mode == "major" else pos - 3  # relative major is 3 fifths below
    if not -7 <= fifths <= 7:
        raise ValueError(f"key {text!r} needs more than 7 accidentals")
    return KeyChange(tick=0, fifths=fifths, mode=mode)


def spelling_table(key: KeyChange) -> dict[int, tuple[str, int]]:
    """pitch class → (step, alter) for this key."""
    f = key.fifths
    table: dict[int, tuple[str, int]] = {}
    for pos in range(f - 1, f + 6):  # the 7 diatonic notes
        step, alter = lof_name(pos)
        table[(STEP_PC[step] + alter) % 12] = (step, alter)
    if key.mode == "minor":  # raised 7th: tonic is at f+3; leading tone at f+3+5
        step, alter = lof_name(f + 8)
        table.setdefault((STEP_PC[step] + alter) % 12, (step, alter))
    centre = f + 2
    for pc in range(12):
        if pc in table:
            continue
        cands = [p for p in range(centre - 12, centre + 13) if (p * 7) % 12 == pc]
        best = min(cands, key=lambda p: (abs(p - centre), -p))
        table[pc] = lof_name(best)
    return table


def spell(pitch: int, table: dict[int, tuple[str, int]]) -> Spelling:
    step, alter = table[pitch % 12]
    octave = (pitch - STEP_PC[step] - alter) // 12 - 1
    return Spelling(step=step, alter=alter, octave=octave)
