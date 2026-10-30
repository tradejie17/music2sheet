"""Score-level regression guard on a fixed ASAP subset (needs data/eval/asap_gt_full.pkl,
created by `m2s eval-asap`). Run with: pytest -m slow tests/regression

Thresholds live in thresholds.json; raise them when a change genuinely improves things.
"""

import json
import pickle
from pathlib import Path

import pytest

from m2s.config import settings
from m2s.eval.asap import crop, evaluate_piece, summarize

pytestmark = pytest.mark.slow
THRESHOLDS = json.loads((Path(__file__).parent / "thresholds.json").read_text())


@pytest.fixture(scope="module")
def results():
    cache = settings().data_dir / "eval" / "asap_gt_full.pkl"
    if not cache.exists():
        pytest.skip("run `m2s eval-asap` once to build the ASAP cache")
    gts = pickle.loads(cache.read_bytes())
    names = sorted(gts)[:: max(1, len(gts) // 40)][:40]
    return summarize([evaluate_piece(n, crop(gts[n]), render=True) for n in names])


@pytest.mark.parametrize("metric", sorted(THRESHOLDS))
def test_metric_at_least_threshold(results, metric):
    assert results[metric] >= THRESHOLDS[metric], f"{metric}={results[metric]:.4f} < {THRESHOLDS[metric]}"
