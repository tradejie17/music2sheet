"""Tune quantizer.COMPOUND_BIAS on cached ASAP scores (run `m2s eval-asap` once first).

usage: python scripts/sweep_compound_bias.py ../data/eval/asap_gt_full.pkl 0,0.15,0.3
"""

import pickle
import sys
import warnings

import m2s.quantize.quantizer as q
from m2s.eval.asap import crop, evaluate_piece, summarize

warnings.filterwarnings("ignore")
gts = pickle.load(open(sys.argv[1], "rb"))


def is_compound(ts: str) -> bool:
    num, den = (int(x) for x in ts.split("/"))
    return den == 8 and num % 3 == 0 and num > 3


for bias in [float(x) for x in sys.argv[2].split(",")]:
    q.COMPOUND_BIAS = bias
    rs = [evaluate_piece(n, crop(g), render=False) for n, g in sorted(gts.items())]
    s = summarize(rs)
    comp = [r for r in rs if is_compound(r.gt_meter)]
    false_comp = sum(is_compound(r.our_meter) for r in rs if not is_compound(r.gt_meter))
    print(
        f"bias {bias}: meter {s['meter_ok']:.3f} onset mean {s['onset_acc_mean']:.3f} "
        f"median {s['onset_acc_median']:.3f} | compound ok {sum(r.meter_ok for r in comp)}/{len(comp)} "
        f"| simple→compound errors {false_comp}",
        flush=True,
    )
