"""Subprocess entrypoint: python -m m2s.engines.runner ENGINE AUDIO OUT_DIR --device D

Also provides `run_engine()`, the parent-side helper that spawns this entrypoint,
relays progress lines and enforces an RSS ceiling.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

ENGINES = {
    "transkun": "m2s.engines.transkun_eng:TranskunEngine",
    "beat_this": "m2s.engines.beatthis_eng:BeatThisEngine",
}


class EngineError(RuntimeError):
    pass


def _load(name: str):
    mod, cls = ENGINES[name].split(":")
    module = __import__(mod, fromlist=[cls])
    return getattr(module, cls)()


def engine_version(name: str) -> str:
    from importlib.metadata import version

    return {"transkun": version("transkun"), "beat_this": version("beat-this")}[name]


def run_engine(
    name: str,
    audio: Path,
    out_dir: Path,
    device: str,
    on_progress: Callable[[float, str], None] | None = None,
    rss_limit_gb: float = 9.0,
    timeout_s: float = 1800,
) -> None:
    import psutil

    cmd = [sys.executable, "-m", "m2s.engines.runner", name, str(audio), str(out_dir), "--device", device]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    killed: list[str] = []

    def watchdog() -> None:
        try:
            p = psutil.Process(proc.pid)
            while proc.poll() is None:
                if p.memory_info().rss / 1e9 > rss_limit_gb:
                    killed.append(f"RSS exceeded {rss_limit_gb} GB")
                    proc.kill()
                    return
                threading.Event().wait(0.5)
        except psutil.NoSuchProcess:
            pass

    threading.Thread(target=watchdog, daemon=True).start()
    stderr_lines: list[str] = []
    threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True).start()
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if on_progress:  # may raise (e.g. job cancelled): kill the engine with it
                on_progress(msg.get("progress", 0.0), msg.get("message", ""))
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    try:
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise EngineError(f"{name} timed out after {timeout_s}s") from None
    if killed:
        raise EngineError(f"{name} killed: {killed[0]}")
    if rc != 0:
        tail = "".join(stderr_lines[-30:])
        raise EngineError(f"{name} failed (rc={rc}):\n{tail}")


def main() -> None:
    import warnings

    warnings.filterwarnings("ignore")
    ap = argparse.ArgumentParser()
    ap.add_argument("engine", choices=list(ENGINES))
    ap.add_argument("audio", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--device", default="cpu")
    a = ap.parse_args()
    _load(a.engine).run(a.audio, a.out_dir, a.device)


if __name__ == "__main__":
    main()
