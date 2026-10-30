from fractions import Fraction as Fr

import numpy as np
import pytest
from music21 import converter

from m2s.eval.synth import make_performance
from m2s.hands.heuristic_dp import separate_hands
from m2s.hands.voices import assign_voices
from m2s.models.priors import Priors
from m2s.models.scoredoc import KeyChange, MeterChange, ScoreDoc, ScoreNote, ScorePedal
from m2s.notation.build_m21 import decompose
from m2s.notation.key_spelling import estimate_key, parse_key, spell, spelling_table
from m2s.pipeline.stages import Ctx, s3_s4_quantize, s5_s6_arrange
from m2s.render.musicxml import render_musicxml
from m2s.render.validate import check_measures


def name(pitch, key):
    sp = spell(pitch, spelling_table(key))
    return sp.step + {-2: "bb", -1: "b", 0: "", 1: "#", 2: "##"}[sp.alter] + str(sp.octave)


@pytest.mark.parametrize(
    "key,pitch,expected",
    [
        ("C major", 61, "C#4"),
        ("F major", 70, "Bb4"),
        ("A minor", 68, "G#4"),  # leading tone
        ("C minor", 71, "B4"),  # leading tone in a flat key
        ("C minor", 63, "Eb4"),
        ("Eb major", 68, "Ab4"),
        ("F# major", 65, "E#4"),
        ("Gb major", 71, "Cb5"),
        ("C major", 60, "C4"),
    ],
)
def test_spelling(key, pitch, expected):
    assert name(pitch, parse_key(key)) == expected


def test_parse_key():
    assert parse_key("Eb major").fifths == -3
    assert parse_key("c# minor").fifths == 4
    assert parse_key("d minor") == KeyChange(tick=0, fifths=-1, mode="minor")


def test_estimate_key_from_scale_and_triads():
    # G major: scale + tonic/dominant emphasis
    pcs = [67, 69, 71, 72, 74, 76, 78, 79, 67, 71, 74, 67, 71, 74, 62, 66, 69]
    notes = [ScoreNote(id=f"n{i}", pitch=p, start=i * 480, dur=480) for i, p in enumerate(pcs)]
    k = estimate_key(notes)
    assert (k.fifths, k.mode) == (1, "major")


@pytest.mark.parametrize(
    "start,end,beat,ternary,expected",
    [
        ("0", "1", "1", set(), [("0", "1")]),
        ("1/2", "3/2", "1", set(), [("1/2", "1")]),  # syncopation kept
        ("0", "5/4", "1", set(), [("0", "1"), ("1", "1/4")]),
        ("1/3", "1", "1", {0}, [("1/3", "2/3")]),
        ("5/3", "13/6", "1", {1, 2}, [("5/3", "1/3"), ("2", "1/6")]),  # triplet never crosses a beat
        ("1/6", "2/3", "1", {0}, [("1/6", "1/3"), ("1/2", "1/6")]),  # binary length in a ternary beat
        ("0", "3", "1", set(), [("0", "3")]),
    ],
)
def test_decompose(start, end, beat, ternary, expected):
    got = decompose(Fr(start), Fr(end), Fr(beat), ternary)
    assert [(str(s), str(d)) for s, d in got] == expected


def test_hands_split_melody_and_bass():
    notes = []
    for b in range(16):
        notes.append(ScoreNote(id=f"r{b}", pitch=72 + (b % 5), start=b * 960, dur=960))
        notes.append(ScoreNote(id=f"l{b}", pitch=48 - (b % 3), start=b * 960, dur=960))
        notes.append(ScoreNote(id=f"m{b}", pitch=55, start=b * 960, dur=960))  # inner voice near the split
    separate_hands(notes)
    by = {n.id: n.staff for n in notes}
    assert all(by[f"r{b}"] == 1 for b in range(16))
    assert all(by[f"l{b}"] == 2 for b in range(16))
    assert len({by[f"m{b}"] for b in range(16)}) == 1  # inner voice stays in one hand


def test_hands_respect_lock():
    n = ScoreNote(id="x", pitch=40, start=0, dur=960, staff=1, locked={"staff"})
    separate_hands([n, ScoreNote(id="y", pitch=72, start=0, dur=960)])
    assert n.staff == 1


def test_voices_held_note_goes_to_voice_two():
    held = ScoreNote(id="h", pitch=60, start=0, dur=3840, staff=1)
    run = [ScoreNote(id=f"r{i}", pitch=72 + i, start=960 * i, dur=960, staff=1) for i in range(1, 4)]
    notes = [held, *run]
    assign_voices(notes)
    assert held.voice == 1 and {n.voice for n in run} == {2}


def _doc(notes, pedals=(), pickup=0, meter=(4, 4)):
    return ScoreDoc(
        title="t",
        pickup_ticks=pickup,
        meters=[MeterChange(tick=0, num=meter[0], den=meter[1])],
        keys=[KeyChange(tick=0, fifths=-1)],
        notes=notes,
        pedals=list(pedals),
    )


def _check(doc):
    xml, idmap = render_musicxml(doc)
    assert check_measures(xml) == []
    converter.parse(xml, format="musicxml")  # music21 can read it back
    assert set(idmap.values()) == {n.id for n in doc.notes}
    return xml, idmap


def test_render_ties_triplets_pickup_pedal_two_voices():
    notes = [
        ScoreNote(id="a", pitch=72, start=0, dur=960),  # pickup beat
        ScoreNote(id="b", pitch=74, start=960, dur=320),  # triplets
        ScoreNote(id="c", pitch=76, start=1280, dur=320),
        ScoreNote(id="d", pitch=77, start=1600, dur=320),
        ScoreNote(id="e", pitch=79, start=1920, dur=3840),  # crosses barline → tie
        ScoreNote(id="f", pitch=67, start=1920, dur=960, voice=2),
        ScoreNote(id="g", pitch=41, start=960, dur=1920, staff=2),
        ScoreNote(id="h", pitch=48, start=2880, dur=480, staff=2),
    ]
    xml, idmap = _check(_doc(notes, [ScorePedal(id="p", start=960, end=3840)], pickup=960))
    assert '<measure implicit="yes" number="0"' in xml
    assert "<tied type=" in xml and "<tuplet" in xml and "<pedal" in xml
    assert idmap["m0|s1|q0|p72"] == "a"


@pytest.mark.parametrize("seed", range(8))
def test_end_to_end_synthetic_renders_valid_musicxml(seed, tmp_path):
    rng = np.random.default_rng(seed)
    score = []
    for b in range(24):
        kind = rng.integers(4)
        if kind == 0:
            score += [(int(rng.integers(64, 84)), b + Fr(i, 3), Fr(1, 3)) for i in range(3)]
        elif kind == 1:
            score += [(int(rng.integers(64, 84)), b + Fr(i, 4), Fr(1, 4)) for i in range(4)]
        elif kind == 2:
            score += [(int(rng.integers(64, 84)), Fr(b), Fr(3, 2))]
        else:
            score += [(int(rng.integers(64, 84)), b + Fr(1, 2), Fr(1, 2))]
        score.append((int(rng.integers(36, 55)), Fr(b), Fr(int(rng.integers(1, 3)))))
    perf = make_performance(score, bpm=float(rng.uniform(60, 140)), jitter_s=0.015, seed=seed)
    ctx = Ctx(audio=tmp_path / "x.wav", audio_sha="0" * 64, priors=Priors())
    doc, _ = s3_s4_quantize(ctx, perf.transcription, perf.beats)
    doc = s5_s6_arrange(ctx, doc, title="synthetic")
    _check(doc)


def test_pedal_closes_release_gaps_but_not_unpedalled_rests():
    a = ScoreNote(id="a", pitch=72, start=0, dur=300)
    b = ScoreNote(id="b", pitch=74, start=960, dur=300)
    c = ScoreNote(id="c", pitch=76, start=1920, dur=300)
    assign_voices([a, b, c], 960, pedals=[(0, 1000)])
    assert a.dur == 960  # gap under the pedal → legato
    assert b.dur == 300  # pedal released before the gap → the rest stays


def test_half_bar_rule():
    beat = Fr(1)
    # 4/4: eighth + dotted quarter (0.5→2) stays one value;
    # a half note on the off-beat across the bar's middle splits
    assert decompose(Fr(1, 2), Fr(2), beat, set(), half=Fr(2)) == [(Fr(1, 2), Fr(3, 2))]
    assert len(decompose(Fr(3, 2), Fr(7, 2), beat, set(), half=Fr(2))) > 1


def test_second_voice_rests_hidden():
    notes = [
        ScoreNote(id="h", pitch=60, start=0, dur=3840, staff=1, voice=1),
        ScoreNote(id="v2", pitch=55, start=960, dur=960, staff=1, voice=2),
    ]
    xml, _ = _check(_doc(notes))
    assert 'print-object="no"' in xml


def test_idmap_new_strike_wins_over_tied_continuation():
    # voice 2 holds D4 across the barline; voice 1 strikes D4 on the next downbeat: both
    # are drawn at m2|s1|q0|p62, and a click there must select the struck note
    notes = [
        ScoreNote(id="held", pitch=62, start=1920, dur=3840, staff=1, voice=2),
        ScoreNote(id="a", pitch=67, start=0, dur=3840, staff=1, voice=1),
        ScoreNote(id="new", pitch=62, start=3840, dur=960, staff=1, voice=1),
    ]
    for order in (notes, notes[::-1]):
        _, idmap = _check(_doc(list(order)))
        assert idmap["m2|s1|q0|p62"] == "new"
        assert idmap["m1|s1|q2|p62"] == "held"
