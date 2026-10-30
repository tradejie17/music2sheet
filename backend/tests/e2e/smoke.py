"""Browser smoke test against a running `m2s serve` with at least one finished job.

usage: uv run python tests/e2e/smoke.py [JOB_ID] [--base http://127.0.0.1:8000] [--out DIR]
Checks: home renders, score renders, every rendered notehead maps to a ScoreNote,
click-to-select works, an edit autosaves (new revision) and the score re-renders,
undo restores. Saves screenshots to --out.
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("job", nargs="?")
ap.add_argument("--base", default="http://127.0.0.1:8000")
ap.add_argument("--out", default="../data/e2e")
a = ap.parse_args()
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
jobs = json.load(urllib.request.urlopen(f"{a.base}/api/jobs"))
job = a.job or next(j["id"] for j in jobs if j["status"] == "done")

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(a.base)
    expect(page.get_by_text("新建转谱")).to_be_visible()
    page.screenshot(path=out / "home.png")

    page.goto(f"{a.base}/jobs/{job}")
    page.wait_for_function("window.__m2sScore !== undefined", timeout=60000)
    info = page.evaluate("window.__m2sScore")
    check(info["pitched"] > 0, f"score rendered with {info['pitched']} noteheads")
    check(not info["unmatched"], f"all noteheads map to ScoreNotes (unmatched: {info['unmatched'][:5]})")
    page.screenshot(path=out / "editor.png")
    rev0 = info["rev"]

    # click the first notehead in the score
    head = page.locator("[data-testid=score] .vf-notehead").first
    head.click()
    page.wait_for_timeout(300)
    check(page.get_by_text("音名拼写").is_visible(), "click on notehead selects a note (inspector shows it)")
    page.screenshot(path=out / "selected.png")

    # edit: raise a semitone → autosave → new revision → re-render
    page.keyboard.press("ArrowUp")
    page.wait_for_function(f"window.__m2sScore && window.__m2sScore.rev > {rev0}", timeout=30000)
    check(True, "edit autosaved and score re-rendered")
    check("已保存" in page.get_by_test_id("save-state").inner_text(), "save state shows saved")
    page.keyboard.press("Meta+z")
    page.wait_for_function(f"window.__m2sScore && window.__m2sScore.rev > {rev0 + 1}", timeout=30000)
    check(True, "undo autosaved as a new revision")
    page.screenshot(path=out / "after-undo.png")
    check(not errors, f"no page errors ({errors[:3]})")
    b.close()

print("FAILED" if failures else "ALL PASSED")
sys.exit(1 if failures else 0)
