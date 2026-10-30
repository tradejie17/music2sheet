"""Real-audio beat-tracking eval: MAESTRO recordings with ASAP beat annotations.

ASAP aligns some of its performances to MAESTRO audio (metadata.csv
`maestro_audio_performance`); their `*_annotations.txt` hold hand-checked beat and
downbeat times. For each piece we crop the audio (default 120 s), run beat_this
(with frame activations) and Transkun on the crop, then compare two S3 paths:

- legacy: beat_this peak-picked beats + clean_beats (the pre-ADR-0006 pipeline)
- dp:     tempo-continuity DP over the activations (quantize/beatdp.py)

Metrics (mir_eval, 70 ms window, first 5 s ignored as usual): beat F-measure,
CMLt/AMLt, the share of successive beat intervals that jump > 25 %, the tempo level
relative to the annotation, downbeat F of the engraved bars, and the downstream
onset-position accuracy: the share of score notes that Transkun+S3–S4 put at the
right pitch *and* metrical position (measured in annotated beats, so it doesn't
depend on which beat level was chosen).
"""

from __future__ import annotations

import csv
import json
import random
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

import mir_eval
import numpy as np
import pretty_midi

from m2s.engines.runner import run_engine
from m2s.eval.datasets import fetch_maestro, maestro_index
from m2s.models.artifacts import BeatGrid, Transcription
from m2s.models.priors import Priors
from m2s.pipeline.stages import Ctx, s3_beats, s3_s4_quantize

CONDITIONS = ("legacy", "dp")
JUMP = 1.25  # successive-IBI ratio counted as a tempo jump


@dataclass(frozen=True)
class BeatPiece:
    folder: str
    audio: str  # MAESTRO-relative audio path
    annotations: str  # ASAP-relative performance annotation file
    perf_midi: str  # ASAP-relative performance MIDI
    start: float  # performance start inside the MAESTRO recording (s)

    @property
    def slug(self) -> str:
        return Path(self.annotations).name.replace("_annotations.txt", "") + "__" + Path(self.audio).stem


def select_pieces(asap: Path, maestro_root: Path, n: int = 0, seed: int = 1, per_composer: int = 2):
    """ASAP performances that are a whole MAESTRO recording (≤ 330 s), ≤ 2 per composer."""
    durations = {p.audio: p.duration for p in maestro_index(maestro_root)}
    by: dict[str, list[BeatPiece]] = {}
    with (asap / "metadata.csv").open(newline="") as f:
        for r in csv.DictReader(f):
            a = r["maestro_audio_performance"].replace("{maestro}/", "")
            if not a or a not in durations or r["end"] or (r["start"] and float(r["start"]) >= 1):
                continue
            if durations[a] > 330:
                continue
            by.setdefault(r["composer"], []).append(
                BeatPiece(
                    r["folder"],
                    a,
                    r["performance_annotations"],
                    r["midi_performance"],
                    float(r["start"] or 0),
                )
            )
    rng = random.Random(seed)
    pick: list[BeatPiece] = []
    for c in sorted(by):
        rng.shuffle(by[c])
        pick += by[c][:per_composer]
    return pick[:n] if n else pick


def _crop(src: Path, dst: Path, start: float, seconds: float) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", str(seconds)]
        + ["-ac", "1", "-ar", "44100", str(dst)],
        check=True,
    )


def _annotations(path: Path) -> tuple[np.ndarray, np.ndarray, str]:
    rows = [line.split("\t") for line in path.read_text().strip().split("\n")]
    beats = np.array([float(r[0]) for r in rows])
    downs = np.array([float(r[0]) for r in rows if r[2].startswith("db")])
    ts = next((r[2].split(",")[1] for r in rows if len(r[2].split(",")) > 1 and r[2].split(",")[1]), "")
    return beats, downs, ts


def jump_fraction(beats: np.ndarray) -> float:
    ibi = np.diff(np.asarray(beats, dtype=float))
    if len(ibi) < 2:
        return 0.0
    r = ibi[1:] / ibi[:-1]
    return float(np.mean((r > JUMP) | (r < 1 / JUMP)))


def _pos(t, beats: np.ndarray):
    i = np.clip(np.searchsorted(beats, t) - 1, 0, len(beats) - 2)
    return i + (t - beats[i]) / (beats[i + 1] - beats[i])


def _truth(asap: Path, piece: BeatPiece, gb: np.ndarray, crop_s: float):
    """perf-MIDI note index → score position (in annotated beats), via ASAP's score annotations."""
    sb, _, _ = _annotations(asap / piece.folder / "midi_score_annotations.txt")
    if len(sb) != len(gb):
        return None, None
    perf = sorted(
        (n for i in pretty_midi.PrettyMIDI(str(asap / piece.perf_midi)).instruments for n in i.notes),
        key=lambda n: (n.start, n.pitch),
    )
    score = [
        n
        for i in pretty_midi.PrettyMIDI(str(asap / piece.folder / "midi_score.mid")).instruments
        for n in i.notes
    ]
    q_by: dict[int, list[float]] = {}
    for n in score:
        q_by.setdefault(n.pitch, []).append(float(_pos(n.start, sb)))
    q = {k: np.array(sorted(v)) for k, v in q_by.items()}
    truth = {}
    for i, n in enumerate(perf):
        if not (gb[0] <= n.start <= min(gb[-1], crop_s - 1)) or n.pitch not in q:
            continue
        p = float(_pos(n.start, gb))
        j = int(np.argmin(np.abs(q[n.pitch] - p)))
        if abs(q[n.pitch][j] - p) < 0.35:
            truth[i] = q[n.pitch][j]
    return perf, truth


def _match(tr: Transcription, perf) -> dict[str, int]:
    """Transkun note id → perf note index (same pitch, onset within 50 ms, one-to-one)."""
    by_p: dict[int, list[tuple[float, int]]] = {}
    for i, n in enumerate(perf):
        by_p.setdefault(n.pitch, []).append((n.start, i))
    used: set[int] = set()
    out = {}
    for n in tr.notes:
        best = None
        for s, i in by_p.get(n.pitch, []):
            d = abs(s - n.onset_s)
            if i not in used and d < 0.05 and (best is None or d < best[0]):
                best = (d, i)
        if best:
            used.add(best[1])
            out[n.id] = best[1]
    return out


def _bar_downbeats(doc) -> np.ndarray:
    m = doc.meters[0]
    bar = m.num * 4 * doc.ppq // m.den
    last = max(n.start for n in doc.notes)
    ticks = np.arange(doc.pickup_ticks, last + bar, bar)
    return np.array([doc.tempo_map.tick_to_sec(int(t)) for t in ticks])


def evaluate_piece(asap: Path, piece: BeatPiece, grid: BeatGrid, tr: Transcription, crop_s: float) -> dict:
    gb, gdb, ts = _annotations(asap / piece.annotations)
    gb_c, gdb_c = gb[gb < crop_s], gdb[gdb < crop_s]
    ref = mir_eval.beat.trim_beats(gb_c)
    ref_down = mir_eval.beat.trim_beats(gdb_c)
    perf, truth = _truth(asap, piece, gb, crop_s)
    t2p = _match(tr, perf) if perf else {}
    row: dict = {
        "piece": piece.folder,
        "slug": piece.slug,
        "time_sig": ts,
        "gt_bpm": round(60 / float(np.median(np.diff(gb_c))), 1),
        "gt_jumps": round(jump_fraction(gb_c), 3),
    }
    ctx = Ctx(audio=Path("/dev/null"), audio_sha="0" * 64, priors=Priors())
    for cond in CONDITIONS:
        g = grid if cond == "dp" else grid.model_copy(update={"activations": None})
        b = s3_beats(ctx.priors, tr, g)
        est = b.beats[(b.beats >= 0) & (b.beats < crop_s)]
        e = mir_eval.beat.trim_beats(est)
        cmlc, cmlt, amlc, amlt = mir_eval.beat.continuity(ref, e)
        row[f"{cond}_F"] = round(float(mir_eval.beat.f_measure(ref, e)), 3)
        row[f"{cond}_CMLt"] = round(float(cmlt), 3)
        row[f"{cond}_AMLt"] = round(float(amlt), 3)
        row[f"{cond}_jumps"] = round(jump_fraction(est), 3)
        row[f"{cond}_level"] = round(float(np.median(np.diff(gb_c)) / np.median(np.diff(est))), 3)
        doc, stats = s3_s4_quantize(ctx, tr, g)
        row[f"{cond}_meter"] = stats.meter
        row[f"{cond}_bpm"] = doc.tempos[0].bpm
        db = _bar_downbeats(doc)
        row[f"{cond}_dbF"] = round(float(mir_eval.beat.f_measure(ref_down, mir_eval.beat.trim_beats(db))), 3)
        if truth:
            an = doc.tempo_map.anchors
            tk = np.array([a[0] for a in an], float)
            gpos = np.round(_pos(np.array([a[1] for a in an]), gb) * 4) / 4
            good = sum(
                1
                for n in doc.notes
                if (pi := t2p.get(n.src_ids[0])) is not None
                and pi in truth
                and abs(float(np.interp(n.start, tk, gpos)) - truth[pi]) < 0.02
            )
            row[f"{cond}_pos"] = round(good / len(truth), 3)
    return row


def run(
    asap: Path, maestro_root: Path, work: Path, n: int, crop_s: float, devices: tuple[str, str], log=print
):
    """devices: (beat_this device, transkun device). Returns per-piece rows + summary."""
    pieces = select_pieces(asap, maestro_root, n)
    idx = {p.audio: p for p in maestro_index(maestro_root)}
    work.mkdir(parents=True, exist_ok=True)
    rows = []
    for piece in pieces:
        ((audio, _),) = fetch_maestro(maestro_root, [idx[piece.audio]])
        clip = work / f"{piece.slug}.wav"
        if not clip.exists():
            _crop(audio, clip, piece.start, crop_s)
        bdir, tdir = work / f"{piece.slug}.beats", work / f"{piece.slug}.transkun"
        for d, eng, dev, need in (
            (bdir, "beat_this", devices[0], "activations.npz"),
            (tdir, "transkun", devices[1], "artifact.json"),
        ):
            d.mkdir(exist_ok=True)
            if not (d / need).exists() or not (d / "artifact.json").exists():
                run_engine(eng, clip, d, dev)
        grid = BeatGrid.model_validate_json((bdir / "artifact.json").read_text())
        grid.activations = str(bdir / grid.activations) if grid.activations else None
        tr = Transcription.model_validate_json((tdir / "artifact.json").read_text())
        row = evaluate_piece(asap, piece, grid, tr, crop_s)
        log(json.dumps(row))
        rows.append(row)
    summary = summarize(rows)
    (work / "results.json").write_text(json.dumps({"summary": summary, "pieces": rows}, indent=1))
    return rows, summary


def summarize(rows: list[dict]) -> dict:
    out: dict = {"pieces": len(rows)}
    for cond in CONDITIONS:
        for m in ("F", "CMLt", "AMLt", "jumps", "dbF", "pos"):
            vals = [r[f"{cond}_{m}"] for r in rows if f"{cond}_{m}" in r]
            if vals:
                out[f"{cond}_{m}"] = round(statistics.fmean(vals), 4)
        lv = [r[f"{cond}_level"] for r in rows]
        out[f"{cond}_same_level"] = round(sum(0.8 < x < 1.25 for x in lv) / len(rows), 3)
    out["gt_jumps"] = round(statistics.fmean(r["gt_jumps"] for r in rows), 4)
    return out


def table(rows: list[dict]) -> str:
    cols = ["F", "AMLt", "jumps", "level", "dbF", "pos"]
    head = f"{'piece':42s} {'gt bpm':>6s} " + " ".join(
        f"{c + ' ' + k[:3]:>10s}" for c in cols for k in CONDITIONS
    )
    lines = [head]
    for r in rows:
        vals = " ".join(f"{r.get(f'{k}_{c}', float('nan')):>10.3f}" for c in cols for k in CONDITIONS)
        lines.append(f"{r['piece'][:42]:42s} {r['gt_bpm']:>6.0f} {vals}")
    return "\n".join(lines)
