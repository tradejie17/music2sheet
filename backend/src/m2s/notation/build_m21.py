"""ScoreDoc → music21 Score (grand staff) + idmap for click-to-select.

ScoreDoc notes are logical (one sounding note each). For each (staff, voice,
measure) we lay out a gap-free timeline of chords and rests, then decompose it into
plainly-notatable values ourselves (music21 is never asked to invent durations):

  * a beat is *ternary* when any note/rest boundary inside it sits on a triplet
    position; every value inside a ternary beat is a triplet value, so each beat can
    carry exactly one triplet bracket (or two half-beat brackets);
  * plain values may cross a beat (syncopation) only between binary positions;
  * pieces of one note are tied.

idmap: "m{measure}|s{staff}|q{offset-in-measure as n/d quarters}|p{midi}" → ScoreNote id,
for every fragment (OSMD clicks map back through it). Voice is not part of the key:
music21 renumbers voices per part on export, and (measure, staff, onset, pitch) is
unique except for a tied continuation meeting a new strike of the same pitch in another
voice; the strike keeps the key.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction

from music21 import (
    chord,
    clef,
    duration,
    expressions,
    key,
    layout,
    metadata,
    meter,
    note,
    spanner,
    stream,
    tempo,
    tie,
)
from music21 import pitch as m21pitch

from m2s.models.scoredoc import ScoreDoc, ScoreNote
from m2s.notation.key_spelling import spell, spelling_table

F = Fraction
BINARY_VALUES = [F(x) for x in ("4", "3", "2", "3/2", "1", "3/4", "1/2", "3/8", "1/4", "3/16", "1/8", "1/16")]
TRIPLET_VALUES = [F(x) for x in ("4/3", "2/3", "1/3", "1/6", "1/12")]
CLEFS = {
    "treble": clef.TrebleClef,
    "bass": clef.BassClef,
    "treble8vb": clef.Treble8vbClef,
    "bass8va": clef.Bass8vaClef,
}


@dataclass
class Frag:
    """Part of a ScoreNote inside one measure (cut at barlines only)."""

    start: Fraction  # quarter lengths from measure start
    dur: Fraction
    note: ScoreNote
    tie: str | None = None  # start / continue / stop, w.r.t. neighbouring measures


@dataclass
class Span:
    start: Fraction
    end: Fraction
    frags: list[Frag]  # empty = rest


def is_triplet(x: Fraction) -> bool:
    return x.denominator % 3 == 0


def _greedy(start: Fraction, dur: Fraction, values: list[Fraction]) -> list[tuple[Fraction, Fraction]]:
    out = []
    while dur > 0:
        take = next((v for v in values if v <= dur), None)
        if take is None:  # not expressible in this family: fall back to anything that fits
            take = next((v for v in sorted(BINARY_VALUES + TRIPLET_VALUES, reverse=True) if v <= dur), dur)
        out.append((start, take))
        start, dur = start + take, dur - take
    return out


def decompose(
    start: Fraction,
    end: Fraction,
    beat: Fraction,
    ternary: set[int],
    half: Fraction | None = None,
    whole_beats: bool = False,
) -> list[tuple[Fraction, Fraction]]:
    """Cut [start, end) (measure-relative quarters) into notatable (offset, length) pieces.

    half: the bar's middle (4/4 → 2 quarters), which a single value off the beat may not
    cross; None when the bar has no such middle (3/4, pickups).
    whole_beats: a span over several whole beats that ends inside a later beat is written
    as its whole beats in one value plus the rest (3½ beats → 3 + ½, not 1 + 1 + 1½).
    Off for faithful scores, whose output stays as it was (Engraving.tidy)."""
    dur = end - start
    k0, k1 = int(start // beat), int((end - F(1, 10**6)) // beat)
    if k0 == k1:  # inside one beat
        if k0 in ternary:
            return _greedy(start, dur, TRIPLET_VALUES)
        if dur in BINARY_VALUES:
            return [(start, dur)]
        return _greedy(start, dur, BINARY_VALUES)
    # crosses beats: one plain value is fine when every touched beat is binary
    touched = set(range(k0, k1 + 1))
    crosses_half = half is not None and start < half < end
    if (
        not touched & ternary
        and dur in BINARY_VALUES
        and (dur <= beat or start % beat == 0 or not crosses_half)
    ):
        return [(start, dur)]
    out, t = [], start
    while t < end:
        k = int(t // beat)
        nxt = min(end, (k + 1) * beat)
        if t % beat == 0 and k not in ternary:  # absorb following whole binary beats
            j = k
            if whole_beats:
                while (j + 2) * beat <= end and j + 1 not in ternary:
                    j += 1
                whole = min(end, (j + 1) * beat) - t
                v = next((v for v in BINARY_VALUES if v <= whole and v % beat == 0), None)
                if v is not None:
                    nxt = t + v
            else:
                while (j + 1) * beat < end and j + 1 not in ternary:
                    j += 1
                whole = min(end, (j + 1) * beat) - t
                if whole in BINARY_VALUES:
                    nxt = t + whole
        out += _greedy(t, nxt - t, TRIPLET_VALUES if k in ternary else BINARY_VALUES)
        t = nxt
    return out


def _ternary_beats(spans: list[Span], beat: Fraction) -> set[int]:
    if beat == F(3, 2):  # compound beats divide in three natively
        return set()
    out = set()
    for s in spans:
        for b in (s.start, s.end):
            rel = b / beat
            if is_triplet(rel):
                out.add(int(b // beat))
    return out


def _tie_for_piece(frag_tie: str | None, i: int, k: int) -> str | None:
    incoming = frag_tie in ("stop", "continue") or i > 0
    outgoing = frag_tie in ("start", "continue") or i < k - 1
    if incoming and outgoing:
        return "continue"
    return "start" if outgoing else ("stop" if incoming else None)


def _m21_pitch(n: ScoreNote, table) -> m21pitch.Pitch:
    sp = n.spelling or spell(n.pitch, table)
    p = m21pitch.Pitch()
    p.step = sp.step
    p.octave = sp.octave
    p.accidental = m21pitch.Accidental(sp.alter) if sp.alter else None
    return p


def _timeline(frs: list[Frag], mlen: Fraction) -> list[Span]:
    by_slot: dict[tuple[Fraction, Fraction], list[Frag]] = defaultdict(list)
    for f in frs:
        by_slot[(f.start, f.dur)].append(f)
    spans, t = [], F(0)
    for (s, d), grp in sorted(by_slot.items()):
        e = s + d
        if e <= t:
            continue  # fully overlapped inside a voice (shouldn't happen): drop
        s = max(s, t)  # partial overlap: clip the later one
        if s > t:
            spans.append(Span(t, s, []))
        spans.append(Span(s, min(e, mlen), sorted(grp, key=lambda f: f.note.pitch)))
        t = min(e, mlen)
    if t < mlen:
        spans.append(Span(t, mlen, []))
    return spans


def _fill_voice(
    vs,
    frs,
    mlen,
    beat,
    table,
    idmap,
    mno,
    st,
    hide_rests: bool = False,
    tidy: bool = False,
    simile: dict | None = None,
    m0: Fraction = F(0),
) -> None:
    spans = _timeline(frs, mlen)
    ternary = _ternary_beats(spans, beat)
    n_beats = mlen / beat
    half = mlen / 2 if n_beats.denominator == 1 and n_beats >= 4 and n_beats % 2 == 0 else None
    for sp in spans:
        pieces = decompose(sp.start, sp.end, beat, ternary, half, tidy)
        for i, (ps, pd) in enumerate(pieces):
            if not sp.frags:
                r = note.Rest(quarterLength=pd)
                if hide_rests:  # second-voice filler rests: keep the timing, don't print them
                    r.style.hideObjectOnPrint = True
                vs.insert(ps, r)
                continue
            pitches = [_m21_pitch(f.note, table) for f in sp.frags]
            el = (
                note.Note(pitches[0], quarterLength=pd)
                if len(pitches) == 1
                else chord.Chord(pitches, quarterLength=pd)
            )
            el.id = sp.frags[0].note.id
            members = [el] if isinstance(el, note.Note) else list(el.notes)
            for cn, f in zip(members, sp.frags):
                cn.id = f.note.id
                t = _tie_for_piece(f.tie, i, len(pieces))
                if t:
                    cn.tie = tie.Tie(t)
            vs.insert(ps, el)
            for f in sp.frags:  # every written piece is clickable
                k = f"m{mno}|s{st}|q{ps}|p{f.note.pitch}"
                # a held (tied) piece in one voice can share its key with a new strike of the
                # same pitch in another voice: the strike wins (clicks select what was played)
                if (i == 0 and f.tie in (None, "start")) or k not in idmap:
                    idmap[k] = f.note.id
    _bracket_tuplets(vs, beat, ternary, simile if tidy else None, m0)


def _bracket_tuplets(
    vs: stream.Voice, beat: Fraction, ternary: set[int], simile: dict | None = None, m0: int = 0
) -> None:
    """One triplet bracket per ternary beat (or two per beat when they split evenly).

    simile (readable scores): state shared by the measures of one staff/voice. A group
    that repeats the rhythm of the group right before it (no plain beat in between)
    keeps its time modification but prints no bracket and no "3", the way engravers
    write long triplet passages."""
    by_beat: dict[int, list] = defaultdict(list)
    for el in vs.notesAndRests:
        by_beat[int(F(el.offset) // beat)].append(el)
    for k in sorted(ternary):
        els = sorted(by_beat.get(k, []), key=lambda e: e.offset)
        groups: list[list] = [els]
        if len(els) > 1:  # prefer two half-beat groups when the beat splits evenly
            acc, cut = F(0), None
            for i, e in enumerate(els):
                acc += F(e.quarterLength)
                if acc == beat / 2:
                    cut = i + 1
            if cut is not None and all(F(e.quarterLength) <= beat / 2 for e in els):
                groups = [els[:cut], els[cut:]]
        for g in groups:
            span = sum((F(e.quarterLength) for e in g), F(0))
            normal = duration.convertQuarterLengthToType(span / 2)
            hide = False
            if simile is not None:
                start = F(m0) + F(g[0].offset)  # absolute quarters
                sig = tuple(F(e.quarterLength) for e in g)
                hide = simile.get("end") == start and simile.get("sig") == sig
                simile.update(end=start + span, sig=sig)
            for i, e in enumerate(g):
                t = duration.Tuplet(3, 2, normal)
                if hide:
                    t.bracket = False
                    t.tupletActualShow = None
                t.type = (
                    "startStop"
                    if len(g) == 1
                    else ("start" if i == 0 else ("stop" if i == len(g) - 1 else None))
                )
                d = duration.Duration(type=e.duration.type, dots=e.duration.dots)
                if not e.duration.tuplets:  # value was written plain: convert to its triplet form
                    d = duration.Duration(F(e.quarterLength) * F(3, 2))
                d.appendTuplet(t)
                e.duration = d


def _is_attack(el) -> bool:
    t = el.tie if isinstance(el, note.Note) else (el.notes[0].tie if el.notes else None)
    return t is None or t.type == "start"


# 8va over treble-staff stretches that sit above G5 and reach E6 (3+ ledger lines);
# 8vb under bass-staff stretches that stay below F2 and reach A#1. A note so far out
# that it needs 5+ ledger lines (B6 and up, D1 and down) gets a bracket even alone.
# Every bracket then grows to the edges of its phrase (up to a rest or a barline) over
# notes that still read well an octave off (8va: C5 and up, 8vb: C3 and down), brackets
# less than a bar apart join, and a bracket that would print its notes below (8va) an
# un-bracketed neighbour in the same beat loses that beat; one lasting less than a
# beat is dropped, and so are short brackets that come back within two bars.
# Fields: kind, core (run member), extreme (run qualifies), far (lone note qualifies),
# shiftable (may sit inside a bracket).
OTTAVA_RULES = {
    1: (
        "8va",
        lambda lo, hi: lo >= 79,
        lambda lo, hi: hi >= 88,
        lambda lo, hi: hi >= 95,
        lambda lo, hi: lo >= 72,
    ),
    2: (
        "8vb",
        lambda lo, hi: hi <= 41,
        lambda lo, hi: lo <= 34,
        lambda lo, hi: lo <= 26,
        lambda lo, hi: hi <= 48,
    ),
}
OTTAVA_MIN_EVENTS = 6  # shorter runs: ledger lines read better than a bracket that keeps toggling


def ottava_spans(doc: ScoreDoc, staff: int) -> list[tuple[int, int, str]]:
    """(first attack tick, last attack tick, '8va'|'8vb') brackets for `staff`; see
    OTTAVA_RULES."""
    kind, core, extreme, far, shiftable = OTTAVA_RULES[staff]
    by_t: dict[int, list[ScoreNote]] = defaultdict(list)
    for n in doc.notes:
        if n.staff == staff:
            by_t[n.start].append(n)
    if not by_t:
        return []
    ts = sorted(by_t)
    lo = [min(n.pitch for n in by_t[t]) for t in ts]
    hi = [max(n.pitch for n in by_t[t]) for t in ts]
    end = [max(n.start + n.dur for n in by_t[t]) for t in ts]
    bars = doc.barline_ticks(max(end) + 1)
    meters = sorted(doc.meters, key=lambda m: m.tick)

    def bar_of(t: int) -> int:
        return bisect.bisect_right(bars, t) - 1

    def beat_of(t: int) -> tuple[int, int]:
        m = next(m for m in reversed(meters) if m.tick <= t)
        compound = m.den == 8 and m.num % 3 == 0 and m.num > 3
        bt = doc.ppq * 3 // 2 if compound else doc.ppq * 4 // m.den
        b = bar_of(t)
        return b, (t - bars[b]) // bt

    def ok(i: int) -> bool:
        return shiftable(lo[i], hi[i])

    # seeds: runs of core events with an extreme note, or one far note
    seeds: list[list[int]] = []
    run: list[int] = []
    for i in range(len(ts) + 1):
        if i < len(ts) and core(lo[i], hi[i]):
            run.append(i)
            continue
        if len(run) >= OTTAVA_MIN_EVENTS and any(extreme(lo[j], hi[j]) for j in run):
            seeds.append([run[0], run[-1]])
        run = []
    seeds += [[i, i] for i in range(len(ts)) if far(lo[i], hi[i]) and ok(i)]
    # grow to the phrase edges: no rest between, same bar, still reads well shifted
    for s in seeds:
        while (
            s[0] > 0
            and ok(s[0] - 1)
            and end[s[0] - 1] >= ts[s[0]]
            and bar_of(ts[s[0] - 1]) == bar_of(ts[s[0]])
        ):
            s[0] -= 1
        while (
            s[1] + 1 < len(ts)
            and ok(s[1] + 1)
            and end[s[1]] >= ts[s[1] + 1]
            and bar_of(ts[s[1] + 1]) == bar_of(ts[s[1]])
        ):
            s[1] += 1
    seeds.sort()
    spans: list[list[int]] = []
    for s in seeds:
        if spans and s[0] <= spans[-1][1] + 1:
            spans[-1][1] = max(spans[-1][1], s[1])
            continue
        if spans:
            a = spans[-1][1]
            mlen = bars[bar_of(ts[a]) + 1] - bars[bar_of(ts[a])]
            if ts[s[0]] - end[a] < mlen and all(ok(j) for j in range(a + 1, s[0])):
                spans[-1][1] = max(spans[-1][1], s[1])
                continue
        spans.append(list(s))

    # short brackets (a few attacks) that come back within two bars of each other
    # without joining toggle on and off: ledger lines read better there
    def short(sp: list[int]) -> bool:
        return sp[1] - sp[0] + 1 < OTTAVA_MIN_EVENTS

    def close(a: list[int], b: list[int]) -> bool:
        return bar_of(ts[b[0]]) - bar_of(ts[a[1]]) <= 2

    spans = [
        sp
        for k, sp in enumerate(spans)
        if not (
            short(sp)
            and (
                (k > 0 and short(spans[k - 1]) and close(spans[k - 1], sp))
                or (k + 1 < len(spans) and short(spans[k + 1]) and close(sp, spans[k + 1]))
            )
        )
    ]
    out: list[tuple[int, int, str]] = []

    def clash(i0: int, i1: int, side: int) -> bool:
        """An un-bracketed neighbour in the edge beat sits above (8va) / below (8vb)
        the bracketed notes of that beat: the print would show a false register jump."""
        beat = beat_of(ts[i0 if side == 0 else i1])
        near = range(max(0, i0 - 24), i0) if side == 0 else range(i1 + 1, min(len(ts), i1 + 25))
        outside = [j for j in near if beat_of(ts[j]) == beat]
        ins = [j for j in range(i0, i1 + 1) if beat_of(ts[j]) == beat]
        if not outside:
            return False
        if staff == 1:
            return max(hi[j] for j in outside) > min(lo[j] for j in ins)
        return min(lo[j] for j in outside) < max(hi[j] for j in ins)

    out: list[tuple[int, int, str]] = []
    for i0, i1 in spans:
        # trim a clashing edge beat off the bracket (its notes take ledger lines)
        while i0 <= i1 and clash(i0, i1, 0):
            b = beat_of(ts[i0])
            while i0 <= i1 and beat_of(ts[i0]) == b:
                i0 += 1
        while i0 <= i1 and clash(i0, i1, 1):
            b = beat_of(ts[i1])
            while i0 <= i1 and beat_of(ts[i1]) == b:
                i1 -= 1
        if i0 > i1:
            continue
        m = next(m for m in reversed(meters) if m.tick <= ts[i0])
        bt = doc.ppq * 3 // 2 if (m.den == 8 and m.num % 3 == 0 and m.num > 3) else doc.ppq * 4 // m.den
        if end[i1] - ts[i0] < bt or (i1 - i0 + 1 < 2 and not far(lo[i0], hi[i0])):
            continue
        out.append((ts[i0], ts[i1], kind))
    return out


def build_score(doc: ScoreDoc) -> tuple[stream.Score, dict[str, str]]:
    ppq = doc.ppq

    def ql(t: int) -> Fraction:
        return F(t, ppq)

    table = spelling_table(doc.keys[0])
    first_m = doc.measure_len(doc.meters[0])
    end_tick = max((n.start + n.dur for n in doc.notes), default=first_m)
    bars = doc.barline_ticks(max(end_tick, 1))
    measures = list(zip(bars[:-1], bars[1:]))
    first_no = 0 if doc.pickup_ticks else 1
    meters = sorted(doc.meters, key=lambda m: m.tick)

    def meter_at(t: int):
        return next(m for m in reversed(meters) if m.tick <= t)

    def beat_ql(t: int) -> Fraction:
        m = meter_at(t)
        compound = m.den == 8 and m.num % 3 == 0 and m.num > 3
        return F(3, 2) if compound else F(4, m.den)

    # explicit ties (tie_next) → the following same-pitch note gets a "stop"
    tie_stop_ids: set[str] = set()
    for n in doc.notes:
        if n.tie_next:
            nxt = min(
                (
                    o
                    for o in doc.notes
                    if o.pitch == n.pitch and o.staff == n.staff and o.start >= n.start + n.dur
                ),
                key=lambda o: o.start,
                default=None,
            )
            if nxt is not None:
                tie_stop_ids.add(nxt.id)

    frags: dict[tuple[int, int, int], list[Frag]] = defaultdict(list)  # (staff, voice, measure idx)
    for n in doc.notes:
        s, e = n.start, n.start + n.dur
        parts = [(mi, m0, m1) for mi, (m0, m1) in enumerate(measures) if m1 > s and m0 < e]
        for i, (mi, m0, m1) in enumerate(parts):
            fs, fe = max(s, m0), min(e, m1)
            incoming = i > 0 or n.id in tie_stop_ids
            outgoing = i < len(parts) - 1 or n.tie_next
            t = (
                "continue"
                if incoming and outgoing
                else ("start" if outgoing else ("stop" if incoming else None))
            )
            frags[(n.staff, n.voice, mi)].append(Frag(ql(fs - m0), ql(fe - fs), n, t))

    score = stream.Score()
    score.metadata = metadata.Metadata(title=doc.title)
    score.metadata.composer = ""
    idmap: dict[str, str] = {}
    staffs = {1: stream.PartStaff(id="RH"), 2: stream.PartStaff(id="LH")}
    elem_at: dict[int, list[tuple[int, object]]] = {1: [], 2: []}  # staff → (abs tick, element), pedals
    used_voices = {st: sorted({n.voice for n in doc.notes if n.staff == st}) or [1] for st in (1, 2)}
    simile: dict[tuple[int, int], dict] = defaultdict(dict)  # (staff, voice) → last triplet group

    for st, part in staffs.items():
        for mi, (m0, m1) in enumerate(measures):
            m = stream.Measure(number=first_no + mi)
            mlen = ql(m1 - m0)
            if mi == 0:
                start_clef = next((c.clef for c in doc.clefs if c.staff == st and c.tick == 0), None)
                m.insert(0, CLEFS[start_clef or ("treble" if st == 1 else "bass")]())
                m.insert(0, key.KeySignature(doc.keys[0].fifths))
                m.insert(0, meter.TimeSignature(f"{meters[0].num}/{meters[0].den}"))
                if doc.pickup_ticks:
                    m.paddingLeft = ql(first_m) - mlen
                if st == 1 and doc.tempos:
                    bpm = doc.tempos[0].bpm
                    if beat_ql(0) == F(3, 2):
                        dotted = note.Note(type="quarter", dots=1)
                        m.insert(0, tempo.MetronomeMark(number=round(bpm / 1.5), referent=dotted))
                    else:
                        m.insert(0, tempo.MetronomeMark(number=round(bpm)))
            for mc in meters[1:]:
                if mc.tick == m0:
                    m.insert(0, meter.TimeSignature(f"{mc.num}/{mc.den}"))
            for c in doc.clefs:
                if c.staff == st and m0 <= c.tick < m1 and c.tick > 0:
                    m.insert(ql(c.tick - m0), CLEFS[c.clef]())
            voices = [v for v in used_voices[st] if v == used_voices[st][0] or frags.get((st, v, mi))]
            for v in voices:
                vs = stream.Voice(id=str(v))
                _fill_voice(
                    vs,
                    frags.get((st, v, mi), []),
                    mlen,
                    beat_ql(m0),
                    table,
                    idmap,
                    mi + first_no,
                    st,
                    hide_rests=v != voices[0],
                    tidy=doc.engraving.tidy,
                    simile=simile[(st, v)],
                    m0=ql(m0),
                )
                if v == voices[0]:  # pedal anchors: first voice only, so start/stop keep XML order
                    for el in vs.notes:
                        if _is_attack(el):
                            elem_at[st].append((m0 + int(el.offset * ppq), el))
                m.insert(0, vs)
            part.append(m)
        # beams need the time-signature context, so only once measures are in the part
        part.makeBeams(inPlace=True)
        part.makeAccidentals(inPlace=True, cautionaryPitchClass=False)

    # pedal marks live under the bass staff only; a pedal with no bass attack inside it
    # is dropped from the notation (it is still in the ScoreDoc and the MIDI export)
    if not doc.engraving.pedal_marks:  # simplified scores: one "con Ped." instead
        if any(p.kind == "sustain" for p in doc.pedals):
            first = staffs[2].getElementsByClass(stream.Measure).first()
            te = expressions.TextExpression("con Ped.")
            te.style.fontStyle = "italic"
            te.placement = "below"
            first.insert(0, te)
    for p in doc.pedals if doc.engraving.pedal_marks else []:
        els = [el for t, el in sorted(elem_at[2], key=lambda x: x[0]) if p.start <= t < p.end]
        if els:
            pm = expressions.PedalMark(els[0], els[-1]) if len(els) > 1 else expressions.PedalMark(els[0])
            staffs[2].insert(0, pm)

    if doc.engraving.ottava:
        for st in (1, 2):
            attacks = sorted(elem_at[st], key=lambda x: x[0])
            ticks = [t for t, _ in attacks]
            for t0, t1, kind in ottava_spans(doc, st):
                els = [el for t, el in attacks if t0 <= t <= t1]
                if els and ticks:
                    # pitches stay as they sound; the octave-shift direction moves the print
                    staffs[st].insert(0, spanner.Ottava(*els, type=kind, transposing=False))

    score.insert(0, staffs[1])
    score.insert(0, staffs[2])
    score.insert(
        0, layout.StaffGroup([staffs[1], staffs[2]], name="Piano", abbreviation="Pno.", symbol="brace")
    )
    return score, idmap
