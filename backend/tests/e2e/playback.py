"""Browser test of playback against a running `m2s serve` (default http://127.0.0.1:8000).

usage: uv run python tests/e2e/playback.py JOB_ID [--base URL]
Checks:
  1. crash regression: narrow viewport, play the recording ~20 s (forces piano-roll auto
     scroll) + rapid wheel scrolling — the page must not error or lose the <audio> element;
  2. score synth: notes get scheduled, pause stops them, edits while paused are honoured;
  3. overlay mode: synth stays in sync with the recording (< 50 ms drift).
"""

import argparse
import sys
import time

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("job")
ap.add_argument("--base", default="http://127.0.0.1:8000")
a = ap.parse_args()

# count sampler voice starts
INIT = """
window.__starts = 0;
const orig = AudioBufferSourceNode.prototype.start;
AudioBufferSourceNode.prototype.start = function (...args) {
  window.__starts++;
  return orig.apply(this, args);
};
"""
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


with sync_playwright() as p:
    b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    page = b.new_page(viewport={"width": 900, "height": 800})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(INIT)
    page.goto(f"{a.base}/jobs/{a.job}")
    page.wait_for_selector("[data-testid=play]", timeout=60000)
    page.wait_for_function("window.__m2sScore !== undefined", timeout=120000)

    # 1) crash regression
    page.click("[data-testid=play]")
    time.sleep(20)
    roll = page.locator("[data-testid=pianoroll]").bounding_box()
    page.mouse.move(roll["x"] + roll["width"] / 2, roll["y"] + roll["height"] / 2)
    for _ in range(20):
        page.mouse.wheel(150, 0)
    time.sleep(1)
    alive = page.evaluate(
        "document.querySelectorAll('audio').length === 1 && !document.querySelector('audio').paused"
    )
    check(alive and not errors, f"20 s playback + wheel scrolling: still playing, no errors ({errors[:2]})")
    page.click("[data-testid=play]")

    # 2) score synth
    page.select_option("[data-testid=mode]", "synth")
    page.click("[data-testid=play]")
    try:
        page.wait_for_function("() => window.__m2s && window.__m2s.player.isRunning", timeout=30000)
    except Exception:
        print(
            "DEBUG",
            page.evaluate(
                "() => ({btn: document.querySelector('[data-testid=play]').innerText,"
                " mode: document.querySelector('[data-testid=mode]').value,"
                " paused: document.querySelector('audio').paused, m2s: !!window.__m2s,"
                " run: window.__m2s && window.__m2s.player.isRunning,"
                " load: window.__m2s && window.__m2s.player.loadProgress,"
                " ctx: window.__m2s && window.__m2s.ctx.state})"
            ),
        )
        raise
    # the synth starts where the recording was paused (~20 s), not at 0:00
    start = page.evaluate("window.__m2s.player['anchorPos']")
    check(start > 10, f"synth starts where the recording stopped ({start:.1f} s)")
    time.sleep(6)
    got, want = page.evaluate(
        "([s]) => { const p = window.__m2s.player, pos = p.position();"
        " return [p.stats.started, p['events'].filter(e => e.t >= s && e.t < pos).length] }",
        [start],
    )
    check(
        want > 0 and abs(got - want) <= 2,
        f"synth plays every scheduled note ({got} started, {want} expected)",
    )
    page.click("[data-testid=play]")
    time.sleep(0.4)
    p0 = page.evaluate("window.__starts")
    time.sleep(1.5)
    check(page.evaluate("window.__starts") == p0, "no new notes after pause")
    r = page.evaluate("""() => {
      const p = window.__m2s.player, ed = window.__editor.getState();
      const pos = p.position(), clk = p['clockRef'];
      const t0 = clk.secToTick(pos), t1 = clk.secToTick(pos + 3);
      ed.deleteNotes(ed.doc.notes.filter(n => n.start >= t0 && n.start < t1).map(n => n.id));
      return pos }""")
    time.sleep(0.3)
    left = page.evaluate(
        "([a]) => window.__m2s.player['events'].filter(e => e.t >= a && e.t < a + 3).length", [r]
    )
    check(left == 0, "edit while paused is reflected in the synth events")
    page.keyboard.press("Meta+z")  # restore the deleted notes

    # 3) overlay drift
    page.select_option("[data-testid=mode]", "mix")
    time.sleep(0.3)
    page.click("[data-testid=play]")
    time.sleep(1.5)
    drifts = []
    for _ in range(8):
        time.sleep(0.5)
        drifts.append(
            page.evaluate(
                "() => Math.abs(window.__m2s.player.position() - document.querySelector('audio').currentTime)"
            )
        )
    page.click("[data-testid=play]")
    check(max(drifts) < 0.05, f"overlay drift max {max(drifts) * 1000:.0f} ms")
    check(not errors, f"no page errors ({errors[:3]})")
    b.close()

print("FAILED" if failures else "ALL PASSED")
sys.exit(1 if failures else 0)
