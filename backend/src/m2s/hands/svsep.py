"""Staff (hand) refinement with piano_svsep (CPJKU, ISMIR 2024 GNN, MIT).

On 227 ASAP scores it beats our heuristic DP: staff accuracy 0.928 vs 0.886 (0.927 vs
0.892 on composers absent from its training data); see docs/adr/0003. It lives in its
own venv (scripts/setup_svsep.sh) and runs as a subprocess on a MusicXML rendering of
the heuristic result. Missing or failing → the heuristic result stands.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path

from m2s.config import settings
from m2s.models.scoredoc import ScoreDoc, ScoreNote

log = logging.getLogger("m2s.svsep")


def svsep_paths() -> tuple[Path, Path] | None:
    root = settings().data_dir / "envs" / "svsep"
    py, src = root / ".venv" / "bin" / "python", root / "src"
    return (py, src) if py.exists() and (src / "pretrained_models" / "model.ckpt").exists() else None


def predict_staffs(doc: ScoreDoc, timeout_s: float = 300) -> dict[str, int]:
    """note id → staff, from piano_svsep. Raises on any failure.

    Predictions come back per written fragment as (onset in quarters, pitch); a note is
    identified by its first fragment, which starts at note.start."""
    from m2s.render.musicxml import render_musicxml

    paths = svsep_paths()
    if paths is None:
        raise RuntimeError("piano_svsep is not installed (backend/scripts/setup_svsep.sh)")
    py, src = paths
    script = Path(__file__).resolve().parents[1] / "engines" / "svsep_predict.py"
    xml, _ = render_musicxml(doc)
    with tempfile.TemporaryDirectory() as td:
        xml_path, out_path = Path(td) / "in.musicxml", Path(td) / "out.json"
        xml_path.write_text(xml)
        r = subprocess.run(
            [str(py), "-I", str(script), str(src), str(xml_path), str(out_path)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        if r.returncode != 0 or not out_path.exists():
            raise RuntimeError(f"piano_svsep failed (rc={r.returncode}): {r.stderr[-800:]}")
        frags = json.loads(out_path.read_text())
    # partitura counts quarters from the first downbeat (pickup notes are negative)
    by_pos = {(round(q * doc.ppq) + doc.pickup_ticks, p): st for q, p, st in frags}
    return {n.id: by_pos[(n.start, n.pitch)] for n in doc.notes if (n.start, n.pitch) in by_pos}


def refine_staffs(doc: ScoreDoc) -> bool:
    """Overwrite unlocked notes' staff with piano_svsep's prediction. Returns success."""
    if svsep_paths() is None:
        return False
    try:
        pred = predict_staffs(doc)
    except Exception as e:  # never fail a job because the optional refiner failed
        log.warning("svsep refinement skipped: %s", e)
        return False
    notes: list[ScoreNote] = doc.notes
    changed = 0
    for n in notes:
        st = pred.get(n.id)
        if st in (1, 2) and "staff" not in n.locked and st != n.staff:
            n.staff = st
            changed += 1
    log.info("svsep: %d/%d notes changed staff", changed, len(notes))
    return True
