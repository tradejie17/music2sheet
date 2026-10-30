"""m2s command line."""

from __future__ import annotations

import time
from pathlib import Path

import typer

from m2s.config import settings
from m2s.models.priors import Priors

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def _root() -> None:
    """music2sheet: audio → sheet music (AI draft + human fine-tuning)."""


def _progress(stage: str, frac: float, msg: str) -> None:
    typer.echo(f"  [{stage:<10}] {frac:5.0%} {msg}")


@app.command()
def transcribe(
    audio: Path = typer.Argument(..., exists=True, dir_okay=False),
    out: Path = typer.Option(Path("out"), "-o", help="output directory"),
    key: str | None = typer.Option(None, help='key prior, e.g. "Eb major"'),
    bpm: float | None = typer.Option(None, help="tempo prior"),
    time_sig: str | None = typer.Option(None, "--time-sig", help='e.g. "3/4"'),
    min_grid: str = typer.Option("1/16", "--grid"),
    triplets: bool = typer.Option(True),
    style: str = typer.Option("classical"),
    simplify: int = typer.Option(1, min=0, max=2, help="0 faithful, 1 readable, 2 simplified"),
    pdf: bool = typer.Option(True, help="render PDF with MuseScore"),
) -> None:
    """Audio file → transcription JSON, raw MIDI (and score once later stages exist)."""
    import json

    from m2s.ingest.audio import ingest
    from m2s.pipeline.stages import Ctx, s1_transcribe, s2_beats, s3_s4_quantize, s5_s6_arrange
    from m2s.render.midi_export import transcription_to_midi
    from m2s.render.musicxml import render_musicxml
    from m2s.render.pdf_mscore import musicxml_to_pdf

    priors = Priors(
        key=key,
        bpm=bpm,
        time_sig=time_sig,
        min_grid=min_grid,
        triplets=triplets,
        style=style,
        simplify=simplify,
    )
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    wav, sha, dur = ingest(audio, out, settings().max_audio_s)
    typer.echo(f"ingested {audio.name}: {dur:.1f}s sha={sha[:12]}")
    ctx = Ctx(audio=wav, audio_sha=sha, priors=priors, progress=_progress)
    tr = s1_transcribe(ctx)
    beats = s2_beats(ctx)
    (out / "transcription.json").write_text(tr.model_dump_json(indent=1))
    (out / "beats.json").write_text(beats.model_dump_json(indent=1))
    transcription_to_midi(tr, out / "raw.mid")
    typer.echo(
        f"{len(tr.notes)} notes, {len(tr.pedals)} pedals, {len(beats.beats_s)} beats (cache: {ctx.hits})"
    )

    doc, stats = s3_s4_quantize(ctx, tr, beats)
    doc = s5_s6_arrange(ctx, doc, title=audio.stem)
    (out / "doc.json").write_text(doc.model_dump_json(indent=1))
    t1 = time.perf_counter()
    xml, idmap = render_musicxml(doc)
    (out / "score.musicxml").write_text(xml)
    (out / "idmap.json").write_text(json.dumps(idmap))
    t2 = time.perf_counter()
    if pdf:
        musicxml_to_pdf(out / "score.musicxml", out / "score.pdf")
    typer.echo(f"quantize: {stats}")
    typer.echo(f"key fifths={doc.keys[0].fifths} {doc.keys[0].mode}; musicxml {t2 - t1:.2f}s")
    typer.echo(f"done in {time.perf_counter() - t0:.1f}s → {out}")


@app.command("eval-asap")
def eval_asap(
    limit: int = typer.Option(0, help="evaluate N evenly spaced scores (0 = all)"),
    seed: int = 0,
    render: bool = typer.Option(True, help="also render + validate MusicXML"),
) -> None:
    """Score-level eval on ASAP: synthetic performances of real scores → quantize/hands/key."""
    import json

    from m2s.eval.asap import run

    cfg = settings()
    out = cfg.data_dir / "eval" / f"asap_seed{seed}.csv"
    summary = run(cfg.data_dir / "datasets" / "asap", out, limit or None, seed, render)
    typer.echo(json.dumps(summary, indent=1))
    typer.echo(f"per-piece results → {out}")


@app.command("eval-maestro")
def eval_maestro(
    n: int = typer.Option(15, help="number of MAESTRO test pieces"),
    crop: float = typer.Option(90.0, help="seconds evaluated per piece"),
) -> None:
    """Transcription eval: Transkun note F1 on MAESTRO v3 test pieces (range-downloaded)."""
    from m2s.eval.maestro import evaluate

    cfg = settings()
    evaluate(
        cfg.data_dir / "datasets" / "maestro",
        cfg.data_dir / "eval" / "maestro",
        n,
        crop,
        cfg.transkun_device,
        log=typer.echo,
    )


@app.command("eval-beats")
def eval_beats(
    n: int = typer.Option(0, help="number of ASAP/MAESTRO performances (0 = all selected, 18)"),
    crop: float = typer.Option(120.0, help="seconds evaluated per performance"),
) -> None:
    """Real-audio beat eval: legacy vs tempo-DP beats on MAESTRO audio with ASAP annotations."""
    import json

    from m2s.eval.beats import run, table

    cfg = settings()
    rows, summary = run(
        cfg.data_dir / "datasets" / "asap",
        cfg.data_dir / "datasets" / "maestro",
        cfg.data_dir / "eval" / "beats",
        n,
        crop,
        (cfg.beat_device, cfg.transkun_device),
        log=lambda s: None,
    )
    typer.echo(table(rows))
    typer.echo(json.dumps(summary, indent=1))
    typer.echo(f"per-piece results → {cfg.data_dir / 'eval' / 'beats' / 'results.json'}")


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    worker: bool = typer.Option(True, help="also start the job worker process"),
) -> None:
    """Run the API (and the worker) — open the UI at http://127.0.0.1:8000 after `pnpm build`."""
    import uvicorn

    from m2s.api.app import app as api

    api.state.spawn_worker = worker
    uvicorn.run(api, host=host, port=port)


@app.command("worker")
def worker_cmd() -> None:
    """Run the job worker alone (one job at a time)."""
    from m2s.jobs.worker import main

    main()


if __name__ == "__main__":
    app()
