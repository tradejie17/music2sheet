"""Readable and simplified scores (Priors.simplify).

A faithful transcription notates everything the pianist played: grace notes, chords
with doubled octaves, every finger release as a rest or a shorter value, a second
voice whenever one note is held over others, every pedal change. Sheet music for
people to read is written differently. This module rewrites the generated ScoreDoc at
one of two levels (level 0 leaves it untouched):

1 易读 (readable): every melody and harmony note stays, but
   * grace notes / turns, doubled pitch classes inside chords, quiet short inner
     notes and chord notes beyond four per hand are dropped;
   * onsets are re-snapped on the same grid without sextuplets, triplets only where
     a beat is clearly ternary, and with near-simultaneous onsets merged into chords;
   * one voice per staff: each chord lasts until the next one in its hand when the
     gap is short or pedalled, otherwise it ends on a value that needs no tie;
   * pedal marks are replaced by one "con Ped." (the pedals stay in the doc, so
     playback and MIDI are unchanged); long high/low stretches switch clef.
2 简化 (simplified arrangement): additionally the right hand keeps its melody (the top
   line, plus at most one harmony note) and the left hand's figuration becomes one
   block chord (bass + two chord tones) per harmonic window, on a grid one level
   coarser and without tuplets.

Everything runs on the ScoreDoc after hands and voices are assigned (S5); performed
onsets (note id → seconds), when available, let the re-snapping use the real timing
rather than the faithful score's grid. Notes the user locked are never dropped.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, replace
from fractions import Fraction

import numpy as np

from m2s.models.artifacts import TempoMap
from m2s.models.scoredoc import ClefChange, MeterChange, ScoreDoc, ScoreNote

F = Fraction


@dataclass(frozen=True)
class Profile:
    level: int
    coarsen: int  # grid levels coarser than the faithful score's grid
    tuplets: bool  # plain triplets (simple beats) / duplets (compound beats) allowed
    tuplet_scale: float  # complexity multiplier for that tuplet in the subdivision Viterbi
    finest_cost: float | None  # complexity of the finest native division (None: as faithful)
    cluster_s: float  # onsets closer than this (per staff) become one chord
    max_chord: tuple[int, int]  # notes per chord in (RH, LH)
    keep_octaves: bool  # RH top note doubled an octave below stays
    quiet: float  # inner notes quieter than this × the loudest neighbour (and short) go
    legato_beats: float  # gaps up to this many beats close up in the single voice
    compound: bool  # triplet-based pieces are rewritten in compound meter (3/4 → 9/8)
    lh_blocks: bool
    straighten: bool  # anticipations (one grid unit before a beat, held over it) start on the beat
    min_unit_s: float = 0.0  # finest grid step at least this long (s): 1/8 at ♩ ≤ 107 for 0.28


PROFILES = {
    1: Profile(1, 0, True, 1.6, 6.0, 0.07, (4, 4), True, 0.55, 0.5, False, False, False),
    2: Profile(2, 1, False, 1.0, None, 0.09, (2, 3), False, 0.7, 1.0, True, True, True, 0.28),
}

ORNAMENT_IOI_S = 0.09  # grace notes / turn notes follow each other faster than this
ORNAMENT_GAP_S = 0.2  # ... and the main note stands alone for at least this long
QUIET_DUR_S = 0.15
GRACE_DUR_S = 0.08  # a note this short, a step or two from a longer note that follows ...
GRACE_LEAD_S = 0.1  # ... within this time, is a grace note
TERNARY_SHARE = 0.5  # level 2: off-beat onsets mostly on triplet positions → compound meter
HARMONY_WINDOW_S = 1.8  # level 2: target length of a left-hand block chord


@dataclass
class Stats:
    dropped: dict[str, int]
    notes_in: int
    notes_out: int


def beat_ticks(doc: ScoreDoc) -> int:
    m = doc.meters[0]
    return doc.ppq * 3 // 2 if (m.den == 8 and m.num % 3 == 0 and m.num > 3) else doc.ppq * 4 // m.den


def simplify(
    doc: ScoreDoc,
    level: int,
    onset_s: dict[str, float] | None = None,
    grid_level: int = 2,
    offset_s: dict[str, float] | None = None,
) -> Stats | None:
    """Rewrite `doc` in place for `level` (0 = no-op). onset_s / offset_s: performed
    times per note id (seconds); grid_level: native subdivision steps of the faithful
    grid (1/8 → 1, 1/16 → 2, 1/32 → 3)."""
    if not level:
        return None
    p = PROFILES[level]
    if p.compound:
        _to_compound(doc)
    bt = beat_ticks(doc)
    tm = doc.tempo_map
    on = dict(onset_s or {})
    if tm is not None:
        for n in doc.notes:
            on.setdefault(n.id, tm.tick_to_sec(n.start))
    else:
        on = {n.id: n.start / doc.ppq for n in doc.notes}
    dropped: dict[str, int] = defaultdict(int)
    notes = list(doc.notes)
    n_in = len(notes)

    def keep(kept: list[ScoreNote], why: str) -> list[ScoreNote]:
        ids = {n.id for n in kept}
        out = [n for n in notes if n.id in ids or n.locked]
        dropped[why] += len(notes) - len(out)
        return out

    notes = keep(_drop_ornaments(notes, on, offset_s or {}), "ornament")
    notes = keep(_reduce_chords(notes, on, p, bt), "chord")
    lvl = max(1, grid_level - p.coarsen)
    if p.min_unit_s:
        lvl = _floor_level(doc, bt, lvl, p.min_unit_s)
    unit = _unit(doc, bt, lvl)
    if tm is not None:
        _resnap(doc, notes, on, p, lvl, bt)
        if p.level >= 2:
            notes = keep(_one_onset_per_tick(notes, on, tm), "merged")
    notes = keep(_dedupe(notes), "merged")
    # the re-snapped grid can merge neighbouring onsets into one chord: cap it again
    notes = keep(_reduce_chords(notes, on, p, bt), "chord")
    if p.straighten:
        _straighten(notes, bt, unit)
    if p.lh_blocks:
        notes = _lh_blocks(doc, notes, on, bt, dropped)
    _single_voice(doc, notes, p, bt, unit)
    notes = keep(_dedupe(notes), "merged")
    doc.notes = sorted(notes, key=lambda n: (n.start, n.staff, n.pitch))
    doc.clefs = _auto_clefs(doc)
    doc.engraving.pedal_marks = False
    doc.engraving.ottava = True
    doc.engraving.tidy = True
    doc.provenance.versions["simplify"] = str(level)
    return Stats(dict(dropped), n_in, len(doc.notes))


# ---------------------------------------------------------------- note selection


def _events(notes: list[ScoreNote], on: dict[str, float], tol: float = 0.035) -> list[list[ScoreNote]]:
    """Notes of one staff grouped by (performed) onset."""
    out: list[list[ScoreNote]] = []
    for n in sorted(notes, key=lambda n: on[n.id]):
        if out and on[n.id] - on[out[-1][0].id] <= tol:
            out[-1].append(n)
        else:
            out.append([n])
    return out


def _drop_ornaments(notes: list[ScoreNote], on: dict[str, float], off: dict[str, float]) -> list[ScoreNote]:
    """Grace notes, turns and mordents: 1–3 single notes, each a step or two from the
    note they lead into, played faster than ORNAMENT_IOI_S and right before a note that
    then stands alone for ORNAMENT_GAP_S; and (with performed offsets) any note shorter
    than GRACE_DUR_S a step or two from a note at least twice as long that starts within
    GRACE_LEAD_S after it, even when the two were quantized into one chord."""
    drop: set[str] = set()
    for staff in (1, 2):
        sn = sorted((n for n in notes if n.staff == staff and n.id in off), key=lambda n: on[n.id])
        for i, n in enumerate(sn):
            d = off[n.id] - on[n.id]
            if d > GRACE_DUR_S:
                continue
            for m in sn[i + 1 :]:
                if on[m.id] - on[n.id] > GRACE_LEAD_S:
                    break
                if 1 <= abs(m.pitch - n.pitch) <= 2 and off[m.id] - on[m.id] >= 2 * d and on[m.id] > on[n.id]:
                    drop.add(n.id)
                    break
    for staff in (1, 2):
        ev = _events([n for n in notes if n.staff == staff], on)
        t = [on[e[0].id] for e in ev]
        for j in range(1, len(ev)):
            main = ev[j]
            after = t[j + 1] - t[j] if j + 1 < len(ev) else np.inf
            if after < ORNAMENT_GAP_S:
                continue
            i = j
            while (
                i - 1 >= 0 and j - (i - 1) <= 3 and t[i] - t[i - 1] <= ORNAMENT_IOI_S and len(ev[i - 1]) == 1
            ):
                i -= 1
            if i == j:
                continue
            before = t[i] - t[i - 1] if i > 0 else np.inf
            if before < ORNAMENT_GAP_S:
                continue
            ref = max(x.pitch for x in main)
            group = [ev[k][0] for k in range(i, j)]
            if all(abs(g.pitch - ref) <= 3 for g in group):
                drop.update(g.id for g in group)
    return [n for n in notes if n.id not in drop]


def _reduce_chords(notes: list[ScoreNote], on: dict[str, float], p: Profile, bt: int) -> list[ScoreNote]:
    """Per chord: keep the top (melody) and bottom (bass) note; drop inner notes that
    double a pitch class already kept, quiet short inner notes, and the softest inner
    notes beyond the profile's chord size."""
    out: list[ScoreNote] = []
    for staff in (1, 2):
        sn = [n for n in notes if n.staff == staff]
        cap = p.max_chord[staff - 1]
        vel_sorted = sorted((on[n.id], n.velocity) for n in sn)
        tv = np.array([x[0] for x in vel_sorted])
        vv = np.array([x[1] for x in vel_sorted])
        by_tick: dict[int, list[ScoreNote]] = defaultdict(list)
        for n in sn:
            by_tick[n.start].append(n)
        for grp in by_tick.values():
            grp = sorted(grp, key=lambda n: n.pitch)
            if len(grp) == 1:
                out += grp
                continue
            top, bottom = grp[-1], grp[0]
            if p.level >= 2 and staff == 1:
                # melody + at most one harmony note: the loudest inner note that isn't
                # an octave double of the melody
                inner = [n for n in grp[:-1] if (top.pitch - n.pitch) % 12 and top.pitch - n.pitch <= 12]
                inner.sort(key=lambda n: -n.velocity)
                out += [top] + inner[: cap - 1]
                continue
            kept = [top, bottom]
            pcs = {top.pitch % 12, bottom.pitch % 12}
            t0 = on[grp[0].id]
            loud = float(vv[np.searchsorted(tv, t0 - 0.25) : np.searchsorted(tv, t0 + 0.25, "right")].max())
            inner = sorted(grp[1:-1], key=lambda n: -n.velocity)
            for n in inner:
                octave_melody = staff == 1 and p.keep_octaves and n.pitch == top.pitch - 12
                if n.pitch % 12 in pcs and not octave_melody:
                    continue
                if n.velocity < p.quiet * loud and n.dur <= bt // 2 and not octave_melody:
                    continue
                if len(kept) >= cap:
                    continue
                kept.append(n)
                pcs.add(n.pitch % 12)
            out += kept
    return out


def _dedupe(notes: list[ScoreNote]) -> list[ScoreNote]:
    """One note per (staff, start, pitch); the louder one wins."""
    best: dict[tuple[int, int, int], ScoreNote] = {}
    for n in notes:
        k = (n.staff, n.start, n.pitch)
        if k not in best or (n.velocity, n.dur) > (best[k].velocity, best[k].dur):
            best[k] = n
    return list(best.values())


# ---------------------------------------------------------------- rhythm


def _resnap(
    doc: ScoreDoc, notes: list[ScoreNote], on: dict[str, float], p: Profile, lvl: int, bt: int
) -> None:
    from m2s.quantize.quantizer import requantize
    from m2s.quantize.subdiv_dp import COMPOUND, SIMPLE

    model = COMPOUND if bt == doc.ppq * 3 // 2 else SIMPLE
    cands = model.candidates(lvl, False)
    if p.finest_cost is not None and lvl >= 2:  # finest division only for real runs, not stray onsets
        model = replace(model, complexity={**model.complexity, cands[-1]: p.finest_cost})
    if p.tuplets:
        cands.append(model.tuplet[0])
    free = [n for n in notes if "time" not in n.locked]
    requantize(free, on, doc.tempo_map, bt, cands, model, p.cluster_s, p.tuplet_scale)


def _to_compound(doc: ScoreDoc) -> None:
    """Level 2 has no tuplets, so a piece whose off-beat onsets sit mostly on triplet
    positions (continuous triplet figuration in 2/4, 3/4, 4/4) is rewritten in the
    compound meter that writes them as plain eighths: each beat becomes a dotted
    quarter (2/4 → 6/8, 3/4 → 9/8, 4/4 → 12/8). Sounding times don't change."""
    m = doc.meters[0]
    if len(doc.meters) > 1 or m.den != 4 or m.num not in (2, 3, 4) or doc.tempo_map is None:
        return
    bt = doc.ppq
    off = [n.start % bt for n in doc.notes if n.start % bt]
    if not off:
        return
    tern = sum(t % (bt // 3) == 0 and t % (bt // 4) != 0 for t in off)
    if tern < TERNARY_SHARE * len(off):
        return

    def sc(t: int) -> int:
        return t * 3 // 2

    for n in doc.notes:
        end = sc(n.start + n.dur)
        n.start = sc(n.start)
        n.dur = max(end - n.start, 1)
    for q in doc.pedals:
        q.start, q.end = sc(q.start), sc(q.end)
    doc.pickup_ticks = sc(doc.pickup_ticks)
    doc.meters = [MeterChange(tick=0, num=m.num * 3, den=8)]
    doc.tempos = [t.model_copy(update={"tick": sc(t.tick), "bpm": round(t.bpm * 1.5, 1)}) for t in doc.tempos]
    doc.tempo_map = TempoMap(anchors=[(sc(t), s) for t, s in doc.tempo_map.anchors])
    doc.clefs = []
    doc.provenance.versions["simplify_meter"] = f"{m.num}/{m.den}→{m.num * 3}/8"


def _floor_level(doc: ScoreDoc, bt: int, lvl: int, min_s: float) -> int:
    """The finest subdivision level ≤ `lvl` whose grid step lasts at least `min_s`
    seconds at the piece's tempo (level 1 at the least)."""
    bpm = doc.tempos[0].bpm if doc.tempos else 100.0  # quarters per minute
    beat_s = 60.0 / bpm * bt / doc.ppq
    while lvl > 1 and _unit(doc, bt, lvl) / bt * beat_s < min_s:
        lvl -= 1
    return lvl


def _one_onset_per_tick(notes: list[ScoreNote], on: dict[str, float], tm) -> list[ScoreNote]:
    """Level 2: when the coarser grid lands two different played onsets of one hand on
    the same tick (a fast run), keep one of them instead of stacking neighbouring
    notes into a cluster: in the right hand the one with the highest note (a skyline
    over the grid slots: the melody of a broken-chord figure sits on top), in the left
    hand the one played nearest to that grid point."""
    groups: dict[tuple[int, int], list[ScoreNote]] = defaultdict(list)
    for n in notes:
        groups[(n.staff, n.start)].append(n)
    out: list[ScoreNote] = []
    for (_, t), grp in groups.items():
        times = [on[n.id] for n in grp]
        if max(times) - min(times) <= 0.06:
            out += grp
            continue
        if grp[0].staff == 1:
            ref = on[max(grp, key=lambda n: (n.pitch, -abs(on[n.id] - tm.tick_to_sec(t)))).id]
        else:
            ts = tm.tick_to_sec(t)
            ref = min(times, key=lambda x: abs(x - ts))
        out += [n for n in grp if abs(on[n.id] - ref) <= 0.06]
    return out


def _straighten(notes: list[ScoreNote], bt: int, unit: int) -> None:
    """Level 2: a chord pushed one grid unit ahead of a beat and held across it (the
    pop-style anticipation) starts on that beat instead, which saves the tie."""
    for staff in (1, 2):
        starts = sorted({n.start for n in notes if n.staff == staff})
        nxt = dict(zip(starts, starts[1:]))
        for n in notes:
            if n.staff != staff or n.start % bt == 0 or "time" in n.locked:
                continue
            beat = (n.start // bt + 1) * bt
            follow = nxt.get(n.start)
            if (
                beat - n.start <= unit
                and n.start + n.dur >= beat + bt // 2
                and (follow is None or follow > beat)
            ):
                end = n.start + n.dur
                n.start, n.dur = beat, end - beat


def _lh_blocks(
    doc: ScoreDoc, notes: list[ScoreNote], on: dict[str, float], bt: int, dropped: dict[str, int]
) -> list[ScoreNote]:
    """Level 2: the left hand's notes in each harmonic window become one chord: the
    lowest note plus the two most prominent other pitch classes, voiced close above it.
    Windows where the right hand is silent keep the left hand as it is."""
    bars = doc.barline_ticks(max(n.start + n.dur for n in notes) + 1)
    bpb = (bars[2] - bars[1]) // bt if len(bars) > 2 else 4
    sec_per_beat = 60.0 / doc.tempos[0].bpm * bt / doc.ppq if doc.tempos else 0.6  # bpm: quarters
    divs = [d for d in range(1, bpb + 1) if bpb % d == 0]
    wb = min(divs, key=lambda d: abs(np.log(d * sec_per_beat / HARMONY_WINDOW_S)))
    win = wb * bt
    rh = sorted((n.start, n.start + n.dur) for n in notes if n.staff == 1)
    lh = [n for n in notes if n.staff == 2]
    out = [n for n in notes if n.staff == 1]
    windows: list[tuple[int, int]] = []
    for m0, m1 in zip(bars[:-1], bars[1:]):
        t = m0
        while t < m1:
            windows.append((t, min(t + win, m1)))
            t += win
    by_win: dict[int, list[ScoreNote]] = defaultdict(list)
    starts = [w[0] for w in windows]
    for n in lh:
        by_win[max(0, bisect.bisect_right(starts, n.start) - 1)].append(n)
    for wi, grp in sorted(by_win.items()):
        a, b = windows[wi]
        rh_busy = any(s < b and e > a for s, e in rh)
        onsets = {n.start for n in grp}
        if not rh_busy or len(onsets) == 1 or any(n.locked for n in grp):
            out += grp
            continue
        bass = min(grp, key=lambda n: (n.pitch, n.start))
        weight: dict[int, float] = defaultdict(float)
        for n in grp:
            weight[n.pitch % 12] += n.dur * (n.velocity + 1)
        pcs = [pc for pc in sorted(weight, key=lambda pc: -weight[pc]) if pc != bass.pitch % 12][:2]
        start = a + (min(onsets) - a) // bt * bt  # on the beat at or before the first onset
        # held for the longest single written value that fits the window (a 9/8 bar:
        # dotted half, then a rest under the pedal, rather than dotted half tied to a
        # dotted quarter in every chord note)
        end = next((e for e in range(b, start, -(bt // 2)) if _pieces(start, e, bars, bt, doc.ppq) == 1), b)
        chord = [bass.model_copy(update={"start": start, "dur": end - start, "voice": 1})]
        used = {bass.id}
        for pc in pcs:
            cands = [n for n in grp if n.pitch % 12 == pc and n.id not in used]
            src = min(cands, key=lambda n: abs(n.pitch - (bass.pitch + 9)))
            pitch = src.pitch
            while pitch - bass.pitch > 16:
                pitch -= 12
            while pitch - bass.pitch < 3:
                pitch += 12
            used.add(src.id)
            chord.append(
                src.model_copy(update={"pitch": pitch, "start": start, "dur": end - start, "voice": 1})
            )
        if len(chord) < 3:  # e.g. alternating octaves: keep the octave rather than a bare bass
            octave = [n for n in grp if n.pitch == bass.pitch + 12 and n.id not in used]
            if octave:
                chord.append(octave[0].model_copy(update={"start": start, "dur": end - start, "voice": 1}))
        dropped["accompaniment"] += len(grp) - len(chord)
        out += chord
    return out


# ---------------------------------------------------------------- durations / voices


def _single_voice(doc: ScoreDoc, notes: list[ScoreNote], p: Profile, bt: int, unit: int) -> None:
    """One voice per staff: notes starting together form a chord with one duration,
    and each chord ends where the next one in its hand starts when the gap between
    them is short or under the pedal; otherwise its end snaps to the value that needs
    the fewest written notes (ties) and rests."""
    # sustain-pedal coverage, re-pedalling gaps (< 1/4 beat) bridged
    spans: list[list[int]] = []
    for q in sorted((q for q in doc.pedals if q.kind == "sustain"), key=lambda q: q.start):
        if spans and q.start - spans[-1][1] < bt // 4:
            spans[-1][1] = max(spans[-1][1], q.end)
        else:
            spans.append([q.start, q.end])
    end_tick = max(n.start + n.dur for n in notes) + 4 * bt
    bars = doc.barline_ticks(end_tick)

    def pedalled(t0: int, t1: int) -> bool:
        return any(s <= t0 and e >= t1 for s, e in spans)

    for staff in (1, 2):
        by_start: dict[int, list[ScoreNote]] = defaultdict(list)
        for n in notes:
            if n.staff == staff:
                by_start[n.start].append(n)
        starts = sorted(by_start)
        for i, t in enumerate(starts):
            grp = by_start[t]
            nxt = starts[i + 1] if i + 1 < len(starts) else None
            end = max(n.start + n.dur for n in grp)
            if nxt is not None and end < nxt and _rest_reads_better(grp, t, end, nxt, bars, bt, doc.ppq):
                pass
            elif nxt is not None and (end >= nxt or nxt - end <= p.legato_beats * bt or pedalled(end, nxt)):
                end = nxt
            else:
                limit = nxt if nxt is not None else end_tick
                end = _nice_end(t, end, limit, bars, bt, doc.ppq, unit)
            for n in grp:
                if "time" not in n.locked:
                    n.dur = max(end - n.start, 1)
                if "voice" not in n.locked:
                    n.voice = 1
                n.tie_next = n.tie_next and "time" in n.locked


def _rest_reads_better(
    grp: list[ScoreNote], t: int, end: int, nxt: int, bars: list[int], bt: int, ppq: int
) -> bool:
    """A chord that ends on a single written value keeps that end, and the gap before
    the next chord is written as a rest, when holding it on would take tied values in
    every chord note: a rest is one symbol, each tie is one more notehead and arc per
    note. Single notes are always held (one tie ≈ one rest)."""
    if len(grp) < 2 or _pieces(t, end, bars, bt, ppq) != 1:
        return False
    held = len(grp) * _pieces(t, nxt, bars, bt, ppq)
    return len(grp) + _pieces(end, nxt, bars, bt, ppq) < held


def one_voice_per_staff(doc: ScoreDoc) -> None:
    """After hands or voices are re-derived on a readable / simplified doc: back to one
    voice per staff, durations re-fitted the way `simplify` does it."""
    level = int(doc.provenance.versions.get("simplify", "1"))
    p = PROFILES.get(level, PROFILES[1])
    from m2s.quantize.quantizer import GRID_LEVEL

    bt = beat_ticks(doc)
    lvl = max(1, GRID_LEVEL.get(doc.provenance.versions.get("min_grid", "1/16"), 2) - p.coarsen)
    if p.min_unit_s:
        lvl = _floor_level(doc, bt, lvl, p.min_unit_s)
    if doc.notes:
        _single_voice(doc, doc.notes, p, bt, _unit(doc, bt, lvl))


def _unit(doc: ScoreDoc, bt: int, lvl: int) -> int:
    """Finest grid step at subdivision level `lvl` (simple beats halve, compound beats
    split in three, then halve)."""
    return bt // (2**lvl if bt == doc.ppq * 4 // doc.meters[0].den else 3 * 2 ** (lvl - 1))


def _pieces(s: int, e: int, bars: list[int], bt: int, ppq: int) -> int:
    """How many written values (tied pieces / rests) the span [s, e) takes."""
    from m2s.notation.build_m21 import decompose

    if e <= s:
        return 0
    beat = F(bt, ppq)
    n = 0
    i = bisect.bisect_right(bars, s) - 1
    while s < e:
        m0, m1 = bars[i], bars[i + 1]
        fe = min(e, m1)
        a, b = F(s - m0, ppq), F(fe - m0, ppq)
        tern = {int(x // beat) for x in (a, b) if (x / beat).denominator % 3 == 0}
        mlen = F(m1 - m0, ppq)
        nb = mlen / beat
        half = mlen / 2 if nb.denominator == 1 and nb >= 4 and nb % 2 == 0 else None
        n += len(decompose(a, b, beat, tern, half, whole_beats=True))
        s, i = fe, i + 1
    return n


def _nice_end(start: int, end: int, limit: int, bars: list[int], bt: int, ppq: int, unit: int) -> int:
    """End tick for a chord followed by a real rest: on the grid, close to the played
    end, cheap to write (note values + following rest values), never past the next
    chord."""
    cands = {limit}
    for u in (unit, bt // 2, bt):
        for k in (end // u, end // u + 1):
            c = k * u
            if start < c <= limit:
                cands.add(c)
    best, best_cost = limit, None
    for c in sorted(cands):
        cost = abs(c - end) / bt + 0.5 * (_pieces(start, c, bars, bt, ppq) + _pieces(c, limit, bars, bt, ppq))
        if best_cost is None or cost < best_cost:
            best, best_cost = c, cost
    return best


# ---------------------------------------------------------------- clefs


# diatonic step of each pitch class (sharps spelled up from the white key below)
_STEP = [0, 0, 1, 1, 2, 3, 3, 4, 4, 5, 5, 6]
# (bottom line, top line) of each clef as MIDI pitches: E4–F5, G2–A3
_CLEF_LINES = {"treble": (64, 77), "bass": (43, 57)}
CLEF_SWITCH_COST = 12.0  # one clef change ≈ this many (squared) ledger lines saved
CLEF_MIDBAR_COST = 8.0  # extra for a change inside a bar (at a beat)
CLEF_AWAY_COST = 1.0  # per bar in the other clef: ties go home


def _step(p: int) -> int:
    return (p // 12) * 7 + _STEP[p % 12]


def ledger_lines(pitch: int, clef: str) -> int:
    """Ledger lines a note needs on a staff with this clef."""
    lo, hi = (_step(x) for x in _CLEF_LINES[clef])
    s = _step(pitch)
    return max(0, (s - hi) // 2, (lo - s) // 2)


def _ledger_cost(lines: int) -> float:
    """Superlinear: one note 5 lines off a staff reads far worse than five notes one
    line off; past 3 lines (where readers start counting) it climbs faster still."""
    return lines**2 + 15.0 * max(0, lines - 3)


def _auto_clefs(doc: ScoreDoc) -> list[ClefChange]:
    """Per staff, the home clef or the other one for each beat, chosen by a two-state
    Viterbi: cost = _ledger_cost of the beat's notes in that clef (a few very
    far notes outweigh many slightly-off ones), counted after the 8va / 8vb brackets
    `build_m21` will add, + CLEF_AWAY_COST per bar away from home, + CLEF_SWITCH_COST per
    change (+ CLEF_MIDBAR_COST when it falls inside a bar). A one-bar excursion only
    switches when it saves many ledger lines; a high or low passage keeps one clef
    through a bar that sits in between. Existing clef changes are kept as they are
    when the doc already has some."""
    from m2s.notation.build_m21 import ottava_spans

    if doc.clefs or not doc.notes:
        return list(doc.clefs)
    bars = doc.barline_ticks(max(n.start + n.dur for n in doc.notes))
    bt = beat_ticks(doc)
    seg = [t for m0, m1 in zip(bars[:-1], bars[1:]) for t in range(m0, m1, bt)]  # beat starts
    at_bar = np.array([t in set(bars) for t in seg])
    ns = len(seg)
    out: list[ClefChange] = []
    for staff, clefs in ((1, ("treble", "bass")), (2, ("bass", "treble"))):
        shift = [(t0, t1, -12 if kind == "8va" else 12) for t0, t1, kind in ottava_spans(doc, staff)]
        cost = np.zeros((ns, 2))
        has = np.zeros(ns, bool)
        for n in doc.notes:
            if n.staff != staff:
                continue
            p = n.pitch + next((d for t0, t1, d in shift if t0 <= n.start <= t1), 0)
            i = min(bisect.bisect_right(seg, n.start) - 1, ns - 1)
            cost[i] += [_ledger_cost(ledger_lines(p, c)) for c in clefs]
            has[i] = True
        beats_per_bar = max(1, round(ns / max(1, len(bars) - 1)))
        cost[has, 1] += CLEF_AWAY_COST / beats_per_bar
        acc = np.array([0.0, CLEF_SWITCH_COST]) + cost[0]
        back = np.zeros((ns, 2), int)
        for i in range(1, ns):
            sw = CLEF_SWITCH_COST + (0.0 if at_bar[i] else CLEF_MIDBAR_COST)
            new = np.empty(2)
            for c in (0, 1):
                stay, switch = acc[c], acc[1 - c] + sw
                back[i, c] = c if stay <= switch else 1 - c
                new[c] = min(stay, switch) + cost[i, c]
            acc = new
        state = [int(np.argmin(acc))]
        for i in range(ns - 1, 0, -1):
            state.append(back[i, state[-1]])
        state.reverse()
        prev = 0
        for i, c in enumerate(state):
            if c != prev:
                out.append(ClefChange(tick=seg[i], staff=staff, clef=clefs[c]))
                prev = c
    return sorted(out, key=lambda c: (c.tick, c.staff))
