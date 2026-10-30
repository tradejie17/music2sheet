"""Readable / simplified scores (notation/simplify.py, Priors.simplify)."""

from fractions import Fraction as Fr

import pytest

from m2s.eval.synth import make_performance
from m2s.models.artifacts import PedalEvent
from m2s.models.priors import Priors
from m2s.models.scoredoc import KeyChange, MeterChange, ScoreDoc, ScoreNote, ScorePedal, TempoMark
from m2s.notation.build_m21 import decompose, ottava_spans
from m2s.notation.simplify import (
    PROFILES,
    _auto_clefs,
    _floor_level,
    _single_voice,
    beat_ticks,
    ledger_lines,
    simplify,
)
from m2s.pipeline.stages import Ctx, s3_s4_quantize, s5_s6_arrange
from m2s.render.musicxml import render_musicxml
from m2s.render.validate import check_measures

MELODY = [76, 74, 72, 74, 76, 76, 76, 74, 74, 74, 76, 79, 79, 77, 76, 74]


def _score():
    """8 bars of 4/4: a melody in quarters with a harmony note on each downbeat, a doubled
    octave, a grace note before beat 3, a held inner note (second voice in the faithful
    score) and an eighth-note left-hand arpeggio."""
    out, melody = [], []
    for bar in range(8):
        b = bar * 4
        for i in range(4):
            p = MELODY[(bar * 4 + i) % len(MELODY)]
            melody.append((p, Fr(b + i)))
            out.append((p, Fr(b + i), Fr(1)))
        top = MELODY[(bar * 4) % len(MELODY)]
        out += [(top - 12, Fr(b), Fr(1)), (top - 4, Fr(b), Fr(1)), (top - 16, Fr(b), Fr(1))]
        out.append((67, Fr(b), Fr(2)))  # held under the melody
        out.append((MELODY[(bar * 4 + 2) % len(MELODY)] + 2, Fr(b + 2) - Fr(1, 12), Fr(1, 12)))  # grace
        for j, p in enumerate([48, 55, 64, 55, 48, 55, 64, 55]):  # LH arpeggio
            out.append((p, Fr(b) + Fr(j, 2), Fr(1, 2)))
    return out, melody


@pytest.fixture(scope="module")
def perf():
    score, melody = _score()
    p = make_performance(score, bpm=90, jitter_s=0.008, seed=3)
    p.transcription.pedals = [
        PedalEvent(id=f"p{i}", kind="sustain", start_s=float(p.beat_times[i * 4]) + 0.05,
                   end_s=float(p.beat_times[i * 4 + 4]) - 0.01)
        for i in range(8)
    ]  # fmt: skip
    # transcription ids of the melody notes (pitch, nearest onset)
    on = {}
    for n in p.transcription.notes:
        on.setdefault(n.pitch, []).append(n)
    mel_ids = []
    for pitch, pos in melody:
        t = float(p.beat_times[0]) + float(pos) * 60 / 90
        mel_ids.append(min(on[pitch], key=lambda n: abs(n.onset_s - t)).id)
    return p, mel_ids


def _run(perf, level, tmp_path):
    p, _ = perf
    ctx = Ctx(audio=tmp_path / "x.wav", audio_sha="0" * 64, priors=Priors(simplify=level))
    doc, _ = s3_s4_quantize(ctx, p.transcription, p.beats)
    return s5_s6_arrange(ctx, doc, title="t")


def _melody_recall(doc, mel_ids):
    have = {i for n in doc.notes for i in n.src_ids}
    return sum(i in have for i in mel_ids) / len(mel_ids)


def test_level_0_is_the_faithful_score(perf, tmp_path):
    default = _run(perf, 0, tmp_path)
    p, _ = perf
    ctx = Ctx(audio=tmp_path / "x.wav", audio_sha="0" * 64, priors=Priors())
    doc, _ = s3_s4_quantize(ctx, p.transcription, p.beats)
    doc = s5_s6_arrange(ctx, doc, title="t")
    assert doc.model_dump() == default.model_dump()
    assert "simplify" not in doc.provenance.versions and doc.engraving.pedal_marks
    before = doc.model_dump()
    assert simplify(doc, 0) is None and doc.model_dump() == before
    assert any(n.voice == 2 for n in doc.notes)  # the held note: the faithful score has 2 voices
    xml, _ = render_musicxml(doc)
    assert "<pedal" in xml


def test_level_1_reads_as_one_voice_and_keeps_the_music(perf, tmp_path):
    p, mel_ids = perf
    faithful = _run(perf, 0, tmp_path)
    doc = _run(perf, 1, tmp_path)
    assert {n.voice for n in doc.notes} == {1}
    assert doc.provenance.versions["simplify"] == "1"
    assert _melody_recall(doc, mel_ids) >= 0.95
    assert len(doc.notes) >= 0.75 * len(faithful.notes)
    grace = {n.id for n in p.transcription.notes if n.offset_s - n.onset_s < 0.08}
    assert grace and not grace & {i for n in doc.notes for i in n.src_ids}  # grace notes dropped
    # octave doubling below an inner note is gone, the melody's octave stays
    for st in (1, 2):
        for t in {n.start for n in doc.notes if n.staff == st}:
            chord = [n for n in doc.notes if n.staff == st and n.start == t]
            assert len(chord) <= 4
    assert doc.pedals == faithful.pedals  # still played and exported to MIDI ...
    xml, idmap = render_musicxml(doc)
    assert "<pedal" not in xml and "con Ped." in xml  # ... but printed once
    assert check_measures(xml) == []
    assert set(idmap.values()) == {n.id for n in doc.notes}
    # one voice: no chord may overlap the next one in its hand
    for st in (1, 2):
        notes = sorted((n for n in doc.notes if n.staff == st), key=lambda n: n.start)
        starts = sorted({n.start for n in notes})
        nxt = dict(zip(starts, starts[1:]))
        assert all(n.start + n.dur <= nxt.get(n.start, 10**9) for n in notes)


def test_level_2_is_a_simple_arrangement(perf, tmp_path):
    _, mel_ids = perf
    faithful = _run(perf, 0, tmp_path)
    doc = _run(perf, 2, tmp_path)
    assert {n.voice for n in doc.notes} == {1}
    assert _melody_recall(doc, mel_ids) >= 0.9
    for t in {n.start for n in doc.notes if n.staff == 1}:
        assert len([n for n in doc.notes if n.staff == 1 and n.start == t]) <= 2
    lh0 = {n.start for n in faithful.notes if n.staff == 2}
    lh2 = {n.start for n in doc.notes if n.staff == 2}
    assert len(lh2) <= len(lh0) / 2  # arpeggios became block chords
    for t in lh2:
        chord = sorted(n.pitch for n in doc.notes if n.staff == 2 and n.start == t)
        assert 1 <= len(chord) <= 3 and chord[0] == 48  # the bass note stays at the bottom
    assert all(n.start % (doc.ppq // 4) == 0 for n in doc.notes)  # no tuplet positions
    xml, _ = render_musicxml(doc)
    assert "<time-modification>" not in xml and "<tuplet" not in xml
    assert check_measures(xml) == []


def test_simplify_keeps_locked_notes():
    notes = [
        ScoreNote(id="m", pitch=72, start=0, dur=960),
        ScoreNote(id="d", pitch=60, start=0, dur=960),  # octave double of C: would go
        ScoreNote(id="x", pitch=64, start=0, dur=960),
        ScoreNote(id="y", pitch=67, start=0, dur=960),
        ScoreNote(id="z", pitch=48, start=0, dur=960, locked={"pitch"}),
    ]
    doc = ScoreDoc(
        meters=[MeterChange(tick=0, num=4, den=4)], keys=[KeyChange(tick=0, fifths=0)], notes=notes
    )
    simplify(doc, 2)
    ids = {n.id for n in doc.notes}
    assert "m" in ids and "z" in ids and len([n for n in doc.notes if n.staff == 1]) <= 3


def test_whole_beats_grouping():
    one, half = Fr(1), Fr(2)
    assert decompose(Fr(0), Fr(7, 2), one, set(), half, whole_beats=True) == [
        (Fr(0), Fr(3)),
        (Fr(3), Fr(1, 2)),
    ]
    assert len(decompose(Fr(0), Fr(7, 2), one, set(), half)) == 3  # faithful scores unchanged
    assert decompose(Fr(1), Fr(13, 4), one, set(), half, whole_beats=True) == [
        (Fr(1), Fr(2)),
        (Fr(3), Fr(1, 4)),
    ]


def test_ottava_spans_and_simile_triplets():
    high = [
        ScoreNote(id=f"h{i}", pitch=p, start=i * 480, dur=480)
        for i, p in enumerate([84, 88, 91, 86, 81, 83, 86])
    ]
    low = [ScoreNote(id="l", pitch=72, start=7 * 480, dur=480)]
    trip = [ScoreNote(id=f"t{i}", pitch=60, start=3840 + i * 320, dur=320) for i in range(12)]
    doc = ScoreDoc(
        meters=[MeterChange(tick=0, num=4, den=4)],
        keys=[KeyChange(tick=0, fifths=0)],
        notes=high + low + trip,
    )
    # the run's bracket grows to the end of its phrase over the C5 that still reads well
    assert ottava_spans(doc, 1) == [(0, 7 * 480, "8va")]
    doc.engraving.ottava = doc.engraving.tidy = True
    xml, _ = render_musicxml(doc)
    assert check_measures(xml) == []
    assert 'type="down"' in xml and "<octave-shift" in xml
    assert "<octave>6</octave>" in xml  # pitches stay as they sound
    # four triplet beats in a row: the number is printed on the first only
    assert xml.count('<tuplet bracket="no"') == 3 or xml.count('show-number="none"') == 3


def _doc(notes, num=4, den=4, pedals=()):
    return ScoreDoc(
        meters=[MeterChange(tick=0, num=num, den=den)],
        keys=[KeyChange(tick=0, fifths=0)],
        notes=notes,
        pedals=list(pedals),
    )


def test_far_notes_get_an_ottava_even_alone():
    q = ScoreDoc.model_fields["ppq"].default
    doc = _doc([ScoreNote(id="a", pitch=64, start=0, dur=q), ScoreNote(id="b", pitch=96, start=q, dur=q)])
    assert ottava_spans(doc, 1) == [(q, q, "8va")]  # C7: 5 ledger lines, a beat long
    doc.notes[0].pitch = 72  # C5 before it reads well shifted: the bracket takes it in
    assert ottava_spans(doc, 1) == [(0, q, "8va")]
    doc.notes[0].pitch = 64
    doc.notes[1].pitch = 88  # E6, 3 ledger lines, alone: no bracket
    assert ottava_spans(doc, 1) == []
    doc.notes[1].pitch = 93  # A6, 4 ledger lines, alone: still ledger lines
    assert ottava_spans(doc, 1) == []
    doc.notes[1].pitch, doc.notes[1].dur = 96, q // 4  # far but a 16th long: no bracket for it
    assert ottava_spans(doc, 1) == []


def test_ottava_brackets_join_and_avoid_false_register_jumps():
    q = ScoreDoc.model_fields["ppq"].default
    run = [84, 88, 91, 86, 81, 83]  # six 8ths above G5, up to G6
    notes = [ScoreNote(id=f"a{i}", pitch=p, start=i * q // 2, dur=q // 2) for i, p in enumerate(run)]
    # half a bar of C5–E5 (reads well shifted) after a rest, then the same run again
    notes += [ScoreNote(id=f"m{i}", pitch=72 + i, start=4 * q + i * q, dur=q) for i in range(2)]
    notes += [ScoreNote(id=f"b{i}", pitch=p, start=6 * q + i * q // 2, dur=q // 2) for i, p in enumerate(run)]
    assert ottava_spans(_doc(notes), 1) == [(0, 6 * q + 5 * q // 2, "8va")]
    # a neighbour in the same beat that can't be shifted (E4 under it) and sits higher
    # than the bracketed notes: that beat is left out of the bracket
    clash = [ScoreNote(id="c0", pitch=64, start=6 * q + 3 * q, dur=q // 4)]
    clash += [ScoreNote(id="c1", pitch=96, start=6 * q + 3 * q, dur=q // 4)]
    clash += [ScoreNote(id="c2", pitch=79, start=6 * q + 3 * q + q // 4, dur=q // 4)]
    spans = ottava_spans(_doc(notes + clash), 1)
    assert spans and all(not (a <= 6 * q + 3 * q + q // 4 <= b) for a, b, _ in spans)


def test_level_2_grid_has_an_absolute_floor():
    doc = _doc([ScoreNote(id="a", pitch=60, start=0, dur=480)])
    doc.tempos = [TempoMark(tick=0, bpm=65.0)]
    bt = beat_ticks(doc)
    assert _floor_level(doc, bt, 2, 0.28) == 1  # 16ths at 65 bpm last 0.23 s: 8ths
    doc.tempos[0].bpm = 50.0
    assert _floor_level(doc, bt, 2, 0.28) == 2  # 16ths at 50 bpm: 0.3 s


def test_auto_clefs_follow_ledger_lines():
    assert ledger_lines(60, "treble") == 1 and ledger_lines(60, "bass") == 1
    assert ledger_lines(76, "bass") == 5 and ledger_lines(76, "treble") == 0
    q = ScoreDoc.model_fields["ppq"].default
    bar = 4 * q
    lh = []
    for b, chord in enumerate([[48], [48], [64, 67, 72], [57, 60, 67], [64, 67, 72], [43], [60], [43]]):
        for k in range(4):
            lh += [ScoreNote(id=f"n{b}{k}{p}", pitch=p, start=b * bar + k * q, dur=q, staff=2) for p in chord]
    clefs = _auto_clefs(_doc(lh))
    # one treble stretch over the high bars, kept through the in-between bar 3; a bar of
    # middle C (one ledger line in either clef) doesn't switch
    assert [(c.tick // bar, c.clef) for c in clefs] == [(2, "treble"), (5, "bass")]


def test_auto_clefs_switch_after_low_notes_inside_a_bar():
    q = ScoreDoc.model_fields["ppq"].default
    seq = [[43], [42], [67, 71, 76], [69, 72, 76]] + [[67, 72, 76]] * 8
    lh = [
        ScoreNote(id=f"n{i}{p}", pitch=p, start=i * q, dur=q, staff=2) for i, ch in enumerate(seq) for p in ch
    ]
    clefs = _auto_clefs(_doc(lh))
    # G2 / F#2 stay in the bass clef (6 ledger lines in treble); treble from beat 3
    assert [(c.tick, c.clef) for c in clefs] == [(2 * q, "treble")]


def test_held_chord_rests_rather_than_ties():
    # 9/8 bar: a three-note chord for a dotted half, the next chord on the next downbeat,
    # all under the pedal: a dotted-quarter rest instead of three tied notes
    e = ScoreDoc.model_fields["ppq"].default // 2  # an eighth
    chord = [ScoreNote(id=f"c{p}", pitch=p, start=0, dur=6 * e, staff=2) for p in (36, 43, 52)]
    nxt = [ScoreNote(id="d", pitch=38, start=9 * e, dur=6 * e, staff=2)]
    doc = _doc(chord + nxt, 9, 8, [ScorePedal(id="p", kind="sustain", start=0, end=18 * e)])
    _single_voice(doc, doc.notes, PROFILES[2], 3 * e, e)
    assert {n.dur for n in doc.notes if n.start == 0} == {6 * e}
    # a single note is held on (one tie costs what the rest would)
    one = _doc([chord[0].model_copy(), nxt[0].model_copy()], 9, 8, doc.pedals)
    _single_voice(one, one.notes, PROFILES[2], 3 * e, e)
    assert one.notes[0].dur == 9 * e


def test_rederive_keeps_a_readable_score_in_one_voice(perf, tmp_path):
    from m2s.pipeline.rederive import rederive

    doc = _run(perf, 1, tmp_path)
    for what in ("hands", "voices"):
        out = rederive(doc, what, use_svsep=False)
        assert {n.voice for n in out.notes} == {1}
        for st in (1, 2):
            notes = sorted((n for n in out.notes if n.staff == st), key=lambda n: n.start)
            starts = sorted({n.start for n in notes})
            nxt = dict(zip(starts, starts[1:]))
            assert all(n.start + n.dur <= nxt.get(n.start, 10**9) for n in notes)
    faithful = rederive(_run(perf, 0, tmp_path), "voices", use_svsep=False)
    assert any(n.voice == 2 for n in faithful.notes)  # faithful scores keep their voices
