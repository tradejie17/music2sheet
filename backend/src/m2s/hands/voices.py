"""Voice assignment within each staff (max 2 voices) + legato cleanup.

Rules, in order of preference for readability:
  * notes starting together with equal duration form one chord;
  * a start-group whose durations differ by more than half a beat splits: higher
    sub-chord → voice 1 (stems up), lower → voice 2; smaller differences are unified;
  * a new chord while voice 1 still sounds: overlaps up to half a beat (and half the
    held note) are legato and get truncated; only a held note of ≥ 1 beat pushes the
    new chord to voice 2 (when free) — fewer voices read better;
  * finally, gaps inside a voice are closed when tiny (< 1/4 beat) or covered by the
    sustain pedal (legato snap).
Durations/voices the user locked are left alone.
"""

from __future__ import annotations

from itertools import groupby

from m2s.models.scoredoc import PPQ, ScoreNote


def _two_classes(grp: list[ScoreNote], beat_ticks: int) -> list[list[ScoreNote]]:
    durs = sorted({n.dur for n in grp})
    if len(durs) == 1:
        return [grp]
    lo, hi = durs[0], durs[-1]
    if hi - lo <= beat_ticks // 2:  # near-equal: one chord with the most common duration
        common = max(durs, key=lambda d: (sum(n.dur == d for n in grp), d))
        for n in grp:
            if "time" not in n.locked:
                n.dur = common
        return [grp]
    short = [n for n in grp if n.dur - lo <= hi - n.dur]
    long = [n for n in grp if n.dur - lo > hi - n.dur]
    for n in short:
        if "time" not in n.locked:
            n.dur = lo
    for n in long:
        if "time" not in n.locked:
            n.dur = hi
    return [short, long]


def _truncate(sounding: list[ScoreNote], at: int) -> None:
    for n in sounding:
        if n.start + n.dur > at and "time" not in n.locked:
            n.dur = max(at - n.start, 1)


def assign_voices(
    notes: list[ScoreNote], beat_ticks: int = PPQ, pedals: list[tuple[int, int]] | None = None
) -> None:
    """pedals: sustain-pedal spans (start, end) in ticks; gaps they cover are closed."""
    for staff in (1, 2):
        staff_notes = sorted((n for n in notes if n.staff == staff), key=lambda n: (n.start, n.pitch))
        sounding: dict[int, list[ScoreNote]] = {1: [], 2: []}

        def end(v: int, sounding=sounding) -> int:
            return max((n.start + n.dur for n in sounding[v]), default=0)

        def place(chord: list[ScoreNote], v: int, start: int, sounding=sounding) -> None:
            _truncate(sounding[v], start)
            for n in chord:
                if "voice" not in n.locked:
                    n.voice = v
            sounding[v] = chord

        for start, g in groupby(staff_notes, key=lambda n: n.start):
            grp = list(g)
            classes = _two_classes(grp, beat_ticks)
            if len(classes) == 2:
                hi, lo = sorted(classes, key=lambda c: -sum(n.pitch for n in c) / len(c))
                place(hi, 1, start)
                place(lo, 2, start)
                continue
            chord = classes[0]
            held = sounding[1]
            overlap = end(1) - start
            held_dur = max((n.dur for n in held), default=0)
            if overlap <= 0:
                place(chord, 1, start)
            elif overlap <= min(beat_ticks // 2, held_dur // 2):  # legato overlap: truncate
                place(chord, 1, start)
            elif end(2) <= start and held_dur >= beat_ticks:  # genuinely held note → voice 2
                place(chord, 2, start)
            else:
                place(chord, 1, start)
        _legato_snap(staff_notes, beat_ticks, pedals or [])


def _legato_snap(staff_notes: list[ScoreNote], beat_ticks: int, pedals: list[tuple[int, int]]) -> None:
    """Close the gap to the next chord of the same voice when it is < 1/4 beat, or when the
    sustain pedal covers it: Transkun gives finger-release durations, and under the pedal
    those gaps are not heard as rests (91% of the rests on pedalled pieces came from this)."""

    def pedalled(t0: int, t1: int) -> bool:
        return any(s <= t0 and e >= t1 for s, e in pedals)

    for v in (1, 2):
        vn = sorted((n for n in staff_notes if n.voice == v), key=lambda n: n.start)
        starts = sorted({n.start for n in vn})
        nxt = {s: t for s, t in zip(starts, starts[1:])}
        for n in vn:
            t = nxt.get(n.start)
            if t is None or "time" in n.locked:
                continue
            end = n.start + n.dur
            gap = t - end
            if 0 < gap and (gap < beat_ticks // 4 or pedalled(end, t)):
                n.dur = t - n.start
