"""Tune hand-separation constants on ASAP ground-truth notes (no quantization noise).

usage: python scripts/sweep_hands.py ../data/eval/asap_gt_full.pkl
"""

import itertools
import pickle
import statistics
import sys

import m2s.hands.heuristic_dp as h
from m2s.eval.asap import crop
from m2s.models.scoredoc import ScoreNote

gts = pickle.load(open(sys.argv[1], "rb"))
pieces = []
for name, gt in sorted(gts.items()):
    g = crop(gt)
    notes = [(p, int(s * 960), max(1, int(d * 960)), st) for p, s, d, st in g.notes]
    pieces.append((name, notes))


def score(**params) -> tuple[float, float]:
    for k, v in params.items():
        setattr(h, k, v)
    accs = []
    for _, notes in pieces:
        sn = [ScoreNote(id=str(i), pitch=p, start=s, dur=d) for i, (p, s, d, _) in enumerate(notes)]
        h.separate_hands(sn)
        accs.append(sum(n.staff == gt[3] for n, gt in zip(sn, notes)) / len(notes))
    return statistics.fmean(accs), statistics.median(accs)


base = {k: getattr(h, k) for k in ("ZONE_W", "CONT_DIV", "CROSS", "ALPHA", "LH_SOFT_MAX", "RH_SOFT_MIN")}
mean, median = score(**base)
print(f"baseline {base} → mean {mean:.4f} median {median:.4f}", flush=True)
grid = {
    "CONT_DIV": [3.0, 6.0, 12.0],
    "ALPHA": [0.15, 0.3, 0.5],
    "ZONE_W": [0.05, 0.15, 0.4],
    "CROSS": [0.5, 2.0, 5.0],
}
best = (0, None)
for vals in itertools.product(*grid.values()):
    params = {**base, **dict(zip(grid, vals))}
    m, med = score(**params)
    if m > best[0]:
        best = (m, params)
        print(f"  {dict(zip(grid, vals))} → mean {m:.4f} median {med:.4f}", flush=True)
print("best", best)
