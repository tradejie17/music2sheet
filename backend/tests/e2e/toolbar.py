"""Browser test of the toolbar controls that re-generate the score: 简化 (simplify level),
速度 ×2 / ÷2 (tempo level) and the 重新生成… dialog's 乐谱简化 field.

usage: uv run python tests/e2e/toolbar.py JOB_ID [--base URL] [--out DIR]

Each control re-runs S3–S6 from the cached transcription and beats, so the server needs a
job worker (`m2s serve` starts one). Use a COPY job: the test replaces its score several
times. To keep the user's database untouched, run a server on its own data directory:

    M2S_DATA_DIR=/tmp/m2s-e2e uv run m2s serve --port 8401
    curl -F file=@audio.wav -F 'priors={"simplify":1}' http://127.0.0.1:8401/api/jobs

Checks, for every change: the editor loads a new revision without a page reload, the job's
params and the control show the new value, and the doc changed as expected:
  * level 2: one voice per staff, ≤ 2 notes per right-hand chord, fewer notes than level 1,
    no pedal marks, a single meter (compound rewrite recorded in provenance);
  * level 0: the faithful score (no simplify marker, pedal marks on, more notes);
  * ×2 / ÷2: tempos[0].bpm and the tracked beat double / halve (beat level from the prior),
    the meter stays a single consistent one, the bar count roughly halves / doubles;
  * a hand-edited doc asks for confirmation before a re-run replaces it;
  * no page errors.
"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("job")
ap.add_argument("--base", default="http://127.0.0.1:8000")
ap.add_argument("--out", default="../data/e2e")
a = ap.parse_args()
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def job_params():
    with urllib.request.urlopen(f"{a.base}/api/jobs/{a.job}") as r:
        return json.loads(r.read())["params"]


# Summary of the doc the editor holds (what the score view, piano roll and player use).
STATE = """() => {
  const ed = window.__editor.getState();
  const d = ed.doc;
  const voices = {1: new Set(), 2: new Set()};
  const rh = {};
  for (const n of d.notes) {
    voices[n.staff].add(n.voice);
    if (n.staff === 1 && !(n.locked || []).length) rh[n.start] = (rh[n.start] || 0) + 1;
  }
  const end = Math.max(...d.notes.map(n => n.start + n.dur));
  const m = d.meters[0];
  const bar = d.ppq * 4 * m.num / m.den;
  return {
    rev: d.revision, saved: ed.saveState, notes: d.notes.length,
    v1: voices[1].size, v2: voices[2].size, rhMax: Math.max(0, ...Object.values(rh)),
    meters: d.meters.map(x => `${x.num}/${x.den}`), bars: Math.ceil((end - d.pickup_ticks) / bar),
    bpm: d.tempos[0].bpm, versions: d.provenance.versions || {}, engraving: d.engraving || null,
    select: document.querySelector('[data-testid=simplify-select]').value,
    label: document.querySelector('[data-testid=tempo-level]').textContent,
  };
}"""


def state(page):
    return page.evaluate(STATE)


def wait_new_rev(page, rev, what, timeout=120):
    """Wait until the editor holds a revision newer than `rev` and the score re-rendered."""
    t0 = time.time()
    score = page.evaluate("window.__m2sScore")
    page.wait_for_function(
        "(r) => window.__editor.getState().doc && window.__editor.getState().doc.revision > r",
        arg=rev,
        timeout=timeout * 1000,
    )
    try:
        page.wait_for_function("(s) => window.__m2sScore !== s", arg=score, timeout=60000)
    except Exception:
        pass  # the score may already have re-rendered before we read the marker
    page.wait_for_selector("[data-testid=simplify-select]", timeout=60000)
    time.sleep(0.5)
    s = state(page)
    print(
        f"  {what}: rev {rev} -> {s['rev']} in {time.time() - t0:.1f}s, {s['notes']} notes, "
        f"{s['meters']} ♩={s['bpm']}, bars {s['bars']}, voices {s['v1']}/{s['v2']}",
        flush=True,
    )
    return s


def bpm_of(s):
    return float(s["versions"].get("tempo_level_bpm") or s["bpm"])


with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1440, "height": 900})
    errors, dialogs = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def on_dialog(d):
        dialogs.append(d.message)
        d.accept()

    page.on("dialog", on_dialog)
    page.goto(f"{a.base}/jobs/{a.job}")
    page.wait_for_selector("[data-testid=simplify-select]", timeout=120000)
    page.wait_for_function("window.__editor !== undefined && window.__m2sScore !== undefined", timeout=120000)
    time.sleep(1)

    # start from level 1 (the default for new jobs)
    s = state(page)
    if s["select"] != "1":
        page.select_option("[data-testid=simplify-select]", "1")
        s = wait_new_rev(page, s["rev"], "to level 1")
    s1 = s
    check(
        s1["select"] == "1" and job_params().get("simplify") == 1,
        "level 1 selected and stored in the job params",
    )
    check(s1["versions"].get("simplify") == "1", "level-1 doc carries provenance simplify=1")
    check(s1["v1"] <= 1 and s1["v2"] <= 1, f"level 1: one voice per staff ({s1['v1']}/{s1['v2']})")
    check(
        s1["engraving"] and not s1["engraving"]["pedal_marks"], "level 1: pedal marks folded into 'con Ped.'"
    )
    page.screenshot(path=out / "toolbar-level1.png")

    # 简化 → level 2
    page.select_option("[data-testid=simplify-select]", "2")
    s2 = wait_new_rev(page, s1["rev"], "简化 → 2")
    check(
        s2["select"] == "2" and job_params().get("simplify") == 2,
        "level 2 selected and stored in the job params",
    )
    check(s2["versions"].get("simplify") == "2", "level-2 doc carries provenance simplify=2")
    check(s2["v1"] <= 1 and s2["v2"] <= 1, f"level 2: ≤ 1 voice per staff ({s2['v1']}/{s2['v2']})")
    check(s2["rhMax"] <= 2, f"level 2: right-hand chords ≤ 2 notes (max {s2['rhMax']})")
    check(s2["notes"] < s1["notes"], f"level 2 has fewer notes than level 1 ({s2['notes']} < {s1['notes']})")
    check(
        len(s2["meters"]) == 1 and (s2["meters"] == s1["meters"] or "simplify_meter" in s2["versions"]),
        f"level 2: one meter, same as level 1 unless rewritten in compound ({s2['meters']})",
    )
    page.screenshot(path=out / "toolbar-level2.png")

    # 简化 → level 0 (faithful)
    page.select_option("[data-testid=simplify-select]", "0")
    s0 = wait_new_rev(page, s2["rev"], "简化 → 0")
    check(
        s0["select"] == "0" and job_params().get("simplify") == 0,
        "level 0 selected and stored in the job params",
    )
    check("simplify" not in s0["versions"], "level-0 doc has no simplify marker")
    check(s0["engraving"] is None or s0["engraving"]["pedal_marks"], "level 0: every pedal mark printed")
    check(s0["notes"] >= s1["notes"], f"level 0 keeps every note ({s0['notes']} ≥ {s1['notes']})")

    # back to level 1 through the 重新生成… dialog (PriorsForm 乐谱简化)
    page.click("text=重新生成…")
    page.wait_for_selector("[data-testid=priors-simplify]", timeout=10000)
    check(page.input_value("[data-testid=priors-simplify]") == "0", "dialog shows the job's current level")
    page.select_option("[data-testid=priors-simplify]", "1")
    page.click("button:text-is('重新生成')")
    sd = wait_new_rev(page, s0["rev"], "dialog → 1")
    check(sd["select"] == "1" and sd["versions"].get("simplify") == "1", "dialog re-run applied level 1")
    check(sd["notes"] == s1["notes"], f"level 1 again: same score as before ({sd['notes']} vs {s1['notes']})")

    # 速度 ×2
    bpm0 = bpm_of(sd)
    page.click("[data-testid=tempo-x2]")
    sx = wait_new_rev(page, sd["rev"], "速度 ×2")
    r = bpm_of(sx) / bpm0
    check(abs(r - 2) < 0.02, f"×2: beat tempo {bpm0} → {bpm_of(sx)} (×{r:.3f})")
    check(abs(sx["bpm"] / sd["bpm"] - 2) < 0.04, f"×2: tempos[0].bpm {sd['bpm']} → {sx['bpm']}")
    check(sx["versions"].get("tempo_level_source") == "prior", "×2: beat level taken from the prior")
    check(job_params().get("bpm") == round(bpm0 * 2, 1), f"×2: job prior bpm = {job_params().get('bpm')}")
    check(len(sx["meters"]) == 1, f"×2: a single consistent meter ({sx['meters']})")
    check(1.6 <= sx["bars"] / sd["bars"] <= 2.5, f"×2: bar count {sd['bars']} → {sx['bars']}")
    check(str(round(bpm_of(sx))) in sx["label"], f"×2: toolbar shows {sx['label']}")
    check(sx["select"] == "1" and sx["versions"].get("simplify") == "1", "×2 keeps the simplify level")
    page.screenshot(path=out / "toolbar-x2.png")

    # 速度 ÷2 → back to the original beat level
    page.click("[data-testid=tempo-half]")
    sh = wait_new_rev(page, sx["rev"], "速度 ÷2")
    r = bpm_of(sh) / bpm_of(sx)
    check(abs(r - 0.5) < 0.01, f"÷2: beat tempo {bpm_of(sx)} → {bpm_of(sh)} (×{r:.3f})")
    check(
        abs(bpm_of(sh) - bpm0) <= 0.2, f"÷2 after ×2 returns to the original level ({bpm_of(sh)} vs {bpm0})"
    )
    check(
        len(sh["meters"]) == 1 and abs(sh["bars"] - sd["bars"]) <= max(2, sd["bars"] // 20),
        f"÷2: meter {sh['meters']}, bars {sh['bars']} (was {sd['bars']})",
    )
    half_off = page.is_disabled("[data-testid=tempo-half]")
    check(
        half_off == (bpm_of(sh) / 2 < 40),
        f"÷2 {'disabled' if half_off else 'enabled'} at beat tempo {bpm_of(sh)} (off below 40 after halving)",
    )

    # a hand-edited doc: the re-run asks first
    page.evaluate("""() => window.__editor.getState().edit(d => {
      const n = d.notes.find(n => n.staff === 1); n.pitch += 1; })""")
    page.wait_for_function(
        "(r) => window.__editor.getState().saveState === 'saved' && window.__editor.getState().savedRev > r",
        arg=sh["rev"],
        timeout=20000,
    )
    n_dialogs = len(dialogs)
    rev_edit = page.evaluate("window.__editor.getState().savedRev")
    page.select_option("[data-testid=simplify-select]", "2")
    se = wait_new_rev(page, rev_edit, "edited → 2")
    check(
        len(dialogs) == n_dialogs + 1 and "手动修改" in dialogs[-1],
        "edited doc: confirmation asked before re-run",
    )
    check(
        se["versions"].get("simplify") == "2" and "edited" not in se["versions"], "re-run replaced the edit"
    )

    check(not errors, f"no page errors ({errors[:3]})")
    b.close()

print("FAILED" if failures else "ALL PASSED")
sys.exit(1 if failures else 0)
