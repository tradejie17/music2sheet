#!/usr/bin/env bash
# Install piano_svsep (CPJKU, ISMIR 2024, MIT) into its own venv: it pins torch 2.8 /
# partitura 1.8 / scikit-learn 1.6 and needs torch-scatter, which conflict with the main
# environment. The pipeline calls it as a subprocess (see m2s/hands/svsep.py) and falls
# back to the heuristic hand separator when this environment is missing.
#
# usage: backend/scripts/setup_svsep.sh [DATA_DIR]   (default: <repo>/data)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DATA_DIR="${1:-$REPO_ROOT/data}"
DEST="$DATA_DIR/envs/svsep"
COMMIT="1462e7c28d7adaae033150883a7cb00238ee364a"

mkdir -p "$DEST"
if [ ! -d "$DEST/src/.git" ]; then
  git clone -q https://github.com/CPJKU/piano_svsep "$DEST/src"
fi
git -C "$DEST/src" fetch -q --depth 1 origin "$COMMIT" 2>/dev/null || true
git -C "$DEST/src" checkout -q "$COMMIT"

uv venv -q -p 3.11 "$DEST/.venv"
export VIRTUAL_ENV="$DEST/.venv"
uv pip install -q "torch==2.8.0"
uv pip install -q torch-scatter --find-links https://data.pyg.org/whl/torch-2.8.0+cpu.html --no-build-isolation
uv pip install -q torch_geometric "partitura==1.8.0" torchmetrics scipy "scikit-learn==1.6.0" \
  pytorch_lightning verovio joblib gitpython tqdm pandas
uv pip install -q --no-deps "$DEST/src"
"$DEST/.venv/bin/python" -c "import torch_scatter, torch_geometric, piano_svsep; print('piano_svsep ready')"
