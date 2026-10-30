from fractions import Fraction as Fr

import numpy as np
import pytest

from m2s.eval.synth import make_performance
from m2s.models.artifacts import PedalEvent
from m2s.models.priors import Priors
from m2s.quantize.beatmap import clean_beats
from m2s.quantize.meter import infer_meter
from m2s.quantize.quantizer import quantize


def run(perf, **priors):
    p = Priors(**priors)
    tr = perf.transcription
    beats = clean_beats(perf.beats, p.bpm, tr.notes[0].onset_s, max(n.offset_s for n in tr.notes))
    return quantize(tr, beats, perf.beats.downbeats_s, p)


def onset_accuracy(doc, score, beat_ticks=960, offset_beats=0):
    """Fraction of ground-truth (pitch, start) pairs reproduced exactly.

    Scored under the best whole-beat shift, so a single stray leading note that moves
    tick 0 doesn't count as getting every note wrong."""
    want = {(p, int((Fr(s) - offset_beats) * beat_ticks)) for p, s, _ in score}
    got = [(n.pitch, n.start) for n in doc.notes]
    best = max(sum((p, t + sh * beat_ticks) in want for p, t in got) for sh in (-1, 0, 1))
    return best / len(want)


def melody(n_bars=8, bpb=4):
    """Mixed quarters / eighths / sixteenths, plus a bass note per beat."""
    out, rng = [], np.random.default_rng(1)
    patterns = [[Fr(0)], [Fr(0), Fr(1, 2)], [Fr(0), Fr(1, 4), Fr(1, 2), Fr(3, 4)], [Fr(0), Fr(3, 4)]]
    for b in range(n_bars * bpb):
        pat = patterns[int(rng.integers(len(patterns)))]
        for i, f in enumerate(pat):
            nxt = pat[i + 1] if i + 1 < len(pat) else Fr(1)
            out.append((int(rng.integers(64, 80)), b + f, nxt - f))
        out.append((int(rng.integers(40, 52)), Fr(b), Fr(1)))
    return out


@pytest.mark.parametrize("seed", range(5))
def test_binary_rhythms_with_drift_and_jitter(seed):
    score = melody()
    perf = make_performance(score, bpm=100, drift=0.10, jitter_s=0.015, seed=seed)
    doc, stats = run(perf)
    assert onset_accuracy(doc, score) >= 0.95, stats
    assert (doc.meters[0].num, doc.meters[0].den) == (4, 4)
    assert doc.pickup_ticks == 0


def test_triplets_are_detected():
    score = []
    for b in range(16):
        if b % 4 in (1, 2):  # triplet beats
            score += [(72 + i, b + Fr(i, 3), Fr(1, 3)) for i in range(3)]
        else:
            score += [(70, Fr(b), Fr(1, 2)), (71, b + Fr(1, 2), Fr(1, 2))]
    perf = make_performance(score, bpm=90, drift=0.05, jitter_s=0.012, seed=3)
    doc, stats = run(perf)
    assert onset_accuracy(doc, score) >= 0.95, stats
    assert 3 in stats.subdiv_hist


def test_no_triplets_prior_forbids_them():
    score = [(72 + i, b + Fr(i, 3), Fr(1, 3)) for b in range(8) for i in range(3)]
    perf = make_performance(score, bpm=80, jitter_s=0.005, seed=4)
    _, stats = run(perf, triplets=False, time_sig="4/4")  # simple meter forced: no tuplets allowed
    assert 3 not in stats.subdiv_hist and 6 not in stats.subdiv_hist


def test_three_four_meter_from_noisy_downbeats():
    score = melody(n_bars=12, bpb=3)
    perf = make_performance(score, bpm=120, beats_per_bar=3, downbeat_dropout=0.3, seed=5)
    doc, stats = run(perf)
    assert (doc.meters[0].num, doc.meters[0].den) == (3, 4), stats


def test_time_sig_prior_overrides():
    score = melody(n_bars=8, bpb=4)
    perf = make_performance(score, bpm=100, seed=6)
    doc, _ = run(perf, time_sig="2/4")
    assert (doc.meters[0].num, doc.meters[0].den) == (2, 4)


def test_compound_six_eight():
    # tracker beats = dotted quarters; each beat divided in three eighths
    score = []
    for b in range(16):
        score += [(67, Fr(b), Fr(1, 3)), (69, b + Fr(1, 3), Fr(1, 3)), (71, b + Fr(2, 3), Fr(1, 3))]
    perf = make_performance(score, bpm=60, beats_per_bar=2, jitter_s=0.01, seed=7)
    doc, stats = run(perf)
    assert (doc.meters[0].num, doc.meters[0].den) == (6, 8), stats
    assert onset_accuracy(doc, score, beat_ticks=1440) >= 0.95


def test_pickup_detected():
    # music starts on beat 4 of a 4/4 bar: one-beat pickup
    score = [(72, Fr(3), Fr(1))] + [(70 + (b % 5), Fr(b), Fr(1)) for b in range(4, 20)]
    perf = make_performance(score, bpm=100, first_downbeat=0, jitter_s=0.01, seed=8)
    doc, stats = run(perf)
    assert doc.pickup_ticks == 960, stats
    assert onset_accuracy(doc, score, offset_beats=3) >= 0.95


def test_bpm_prior_fixes_double_tempo_tracking():
    score = melody(n_bars=8)
    perf = make_performance(score, bpm=60, seed=9)
    # simulate a tracker locked at double tempo: insert midpoints
    b = np.asarray(perf.beats.beats_s)
    perf.beats.beats_s = np.sort(np.concatenate([b, (b[:-1] + b[1:]) / 2])).tolist()
    doc, stats = run(perf, bpm=60)
    assert onset_accuracy(doc, score) >= 0.95, stats


def test_pedals_snap_and_abut_on_repedal():
    score = [(60, Fr(b), Fr(1)) for b in range(8)]
    perf = make_performance(score, bpm=60, jitter_s=0.0, beat_jitter_s=0.0, seed=10)
    bt = perf.beat_times
    perf.transcription.pedals = [
        PedalEvent(id="p0", kind="sustain", start_s=bt[0] + 0.05, end_s=bt[2] - 0.05),
        PedalEvent(id="p1", kind="sustain", start_s=bt[2] + 0.03, end_s=bt[4] - 0.02),
    ]
    doc, _ = run(perf)
    assert [(p.start, p.end) for p in doc.pedals] == [(0, 1920), (1920, 3840)]


def test_meter_agreement_prefers_true_bar_length():
    downbeats = list(range(0, 40, 4))
    assert infer_meter(downbeats, 40, None, compound=False).beats_per_bar == 4
    assert infer_meter(list(range(0, 40, 2)), 40, None, compound=False).beats_per_bar == 2


def test_repair_gaps_fills_missed_beats_evenly():
    from m2s.quantize.beatmap import repair_gaps

    beats = np.array([0.0, 1.0, 2.0, 3.0, 8.0, 9.0, 10.0])  # 4 missed beats
    out = repair_gaps(beats, np.array([]))
    assert np.allclose(out, np.arange(11.0))
