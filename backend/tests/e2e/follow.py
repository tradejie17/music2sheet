"""Browser test of score following against a running `m2s serve` (default http://127.0.0.1:8000).

usage: uv run python tests/e2e/follow.py JOB_ID [--base URL] [--out DIR]
Checks, while playing (score synth, then original recording):
  1. the green noteheads are exactly the rendered notes whose [start, start+dur) contains
     the playhead, they change over time, and they sit inside the current-measure band;
  2. the band + cursor stay inside the visible score panel and the panel scrolls to follow;
  3. scrolling the score by hand is not undone for ~3 s, then following resumes;
  4. pause clears the green noteheads but keeps the position marker; the 跟随乐谱 toggle
     hides everything; double-clicking a note moves the playhead to it;
  5. every rendered notehead maps back to a ScoreNote (any meter, e.g. 12/8 bars longer
     than a whole note);
  6. on a fresh page (no synth engine yet) a seek made before the first 乐谱合成 play — slider,
     double-click on a note, or the position reached in 原始录音 — is where playback starts;
  7. no page errors. Screenshots go to --out.
"""

import argparse
import sys
import time
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


# One synchronous snapshot of follow state + DOM (the DOM reflects the update at `at`).
SNAP = """() => {
  const f = window.__m2sFollow.state();
  const ed = window.__editor.getState();
  const sounding = new Set(
    ed.doc.notes.filter(n => n.start <= f.at && f.at < n.start + n.dur).map(n => n.id));
  const wrong = f.painted.filter(id => !sounding.has(id));
  // sounding notes whose drawn value already ended (held past it) are not highlighted
  const missing = [...sounding].filter(id => !f.painted.includes(id));
  const sc = document.querySelector('[data-testid=score-scroll]').getBoundingClientRect();
  const bandEl = document.querySelector('[data-testid=follow-band]');
  const band = bandEl.getBoundingClientRect();
  const cur = document.querySelector('[data-testid=follow-cursor]').getBoundingClientRect();
  const heads = [...document.querySelectorAll('[data-testid=score] .vf-notehead')]
    .filter(g => g.querySelector('path[fill="#16a34a"]') || g.getAttribute('fill') === '#16a34a')
    .map(g => g.getBoundingClientRect());
  const outside = heads.filter(r => {
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    return cx < band.left - 6 || cx > band.right + 6 || cy < band.top - 60 || cy > band.bottom + 60;
  }).length;
  return {
    at: f.at, measure: f.measure, painted: f.painted.length, key: f.painted.slice().sort().join(','),
    wrong: wrong.length, missing: missing.length, unshown: f.unshown, sounding: sounding.size,
    heads: heads.length, outside,
    bandShown: getComputedStyle(bandEl).display !== 'none', bandOpacity: Number(bandEl.style.opacity || 1),
    bandVisible: band.top >= sc.top - 2 && band.bottom <= sc.bottom + 2,
    cursorInBand: cur.left >= band.left - 3 && cur.left <= band.right + 3,
    scrollTop: document.querySelector('[data-testid=score-scroll]').scrollTop,
    playing: ed.playing,
  };
}"""


def snap(page):
    return page.evaluate(SNAP)


SEEK = """(sec) => { const r = document.querySelector('input[type=range]');
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  set.call(r, String(sec)); r.dispatchEvent(new Event('input', {bubbles: true})); }"""
TICK = "Math.round(window.__editor.getState().playheadTick)"


def open_job(browser):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{a.base}/jobs/{a.job}")
    page.wait_for_selector("[data-testid=play]", timeout=60000)
    page.wait_for_function(
        "window.__m2sFollow !== undefined && window.__editor !== undefined", timeout=120000
    )
    page.wait_for_function("document.querySelector('audio').readyState >= 1", timeout=60000)
    return page


def synth_starts_at(page, want_tick, label):
    """Press play in 乐谱合成 and check playback runs on from want_tick, not from 0:00."""
    ppq = page.evaluate("window.__editor.getState().doc.ppq")
    page.click("[data-testid=play]")
    page.wait_for_function("() => window.__m2s && window.__m2s.player.isRunning", timeout=60000)
    time.sleep(1.0)
    got = page.evaluate(TICK)
    page.click("[data-testid=play]")
    # ~1 s of playback at any sane tempo is < 4 beats; starting from 0:00 is far off
    check(
        want_tick - ppq <= got <= want_tick + 4 * ppq,
        f"{label}: synth playback starts at the seek (wanted tick {want_tick}, at {got} after 1 s)",
    )


def seek_before_first_synth_play(browser):
    dur = None
    # slider
    page = open_job(browser)
    page.select_option("[data-testid=mode]", "synth")
    time.sleep(0.3)
    dur = page.evaluate("document.querySelector('audio').duration")
    page.evaluate(SEEK, round(dur * 0.4, 2))
    time.sleep(0.3)
    synth_starts_at(page, page.evaluate(TICK), "fresh page, slider")
    page.close()
    # double-click a note further down
    page = open_job(browser)
    page.select_option("[data-testid=mode]", "synth")
    time.sleep(0.3)
    heads = page.locator("[data-testid=score] .vf-notehead")
    h = heads.nth(min(400, heads.count() - 1))
    h.scroll_into_view_if_needed()
    h.dblclick()
    time.sleep(0.4)
    want = page.evaluate(TICK)
    check(want > 0, f"fresh page: double-click moved the playhead (tick {want})")
    synth_starts_at(page, want, "fresh page, double-click")
    page.close()
    # listen to the recording, pause, switch to the synth
    page = open_job(browser)
    page.evaluate(SEEK, round(dur * 0.3, 2))
    page.click("[data-testid=play]")
    page.wait_for_function("() => !document.querySelector('audio').paused", timeout=10000)
    time.sleep(1.5)
    page.click("[data-testid=play]")
    time.sleep(0.3)
    want = page.evaluate(TICK)
    page.select_option("[data-testid=mode]", "synth")
    time.sleep(0.3)
    kept = page.evaluate(TICK)
    check(abs(kept - want) < 5, f"recording → synth keeps the position ({want} -> {kept})")
    synth_starts_at(page, want, "fresh page, recording then synth")
    page.close()


def play_and_sample(page, seconds, label):
    snaps = []
    t_end = time.time() + seconds
    while time.time() < t_end:
        time.sleep(0.25)
        snaps.append(snap(page))
    live = [s for s in snaps if s["playing"]]
    with_notes = [s for s in live if s["painted"]]
    check(
        len(with_notes) >= len(live) * 0.3,
        f"{label}: notes highlighted in {len(with_notes)}/{len(live)} samples",
    )
    missing = sum(s["missing"] for s in live)
    check(
        all(s["wrong"] == 0 and s["missing"] == s["unshown"] for s in live)
        and missing <= 0.1 * sum(s["sounding"] for s in live),
        f"{label}: highlighted = sounding doc notes "
        f"(wrong {sum(s['wrong'] for s in live)}, held past their drawn value {missing}"
        f" of {sum(s['sounding'] for s in live)})",
    )
    check(
        all(s["heads"] >= s["painted"] for s in live),
        f"{label}: every highlighted note has a green notehead in the DOM",
    )
    check(
        sum(s["outside"] for s in live) == 0,
        f"{label}: green noteheads inside the measure band ({sum(s['outside'] for s in live)} outside)",
    )
    check(all(s["cursorInBand"] for s in live), f"{label}: cursor line inside the measure band")
    keys = {s["key"] for s in with_notes}
    check(
        len(keys) >= max(3, len(live) // 6),
        f"{label}: highlight changes over time ({len(keys)} distinct sets)",
    )
    measures = [s["measure"] for s in live]
    check(
        measures == sorted(measures) and measures[-1] > measures[0],
        f"{label}: band advances {measures[0]} -> {measures[-1]}",
    )
    # allow the smooth scroll ~1 s to catch up after a system change
    vis = [s["bandVisible"] for s in live[4:]]
    check(sum(vis) >= len(vis) * 0.9, f"{label}: current measure visible in {sum(vis)}/{len(vis)} samples")
    return snaps


with sync_playwright() as p:
    b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    page = b.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{a.base}/jobs/{a.job}")
    page.wait_for_selector("[data-testid=play]", timeout=60000)
    page.wait_for_function(
        "window.__m2sFollow !== undefined && window.__editor !== undefined", timeout=120000
    )

    sc = page.evaluate("window.__m2sScore")
    check(
        len(sc["unmatched"]) <= max(2, sc["pitched"] // 500),
        f"noteheads map to notes: {sc['pitched'] - len(sc['unmatched'])}/{sc['pitched']}"
        f" (unmatched {sc['unmatched'][:5]})",
    )
    s0 = snap(page)
    check(s0["bandShown"] and s0["painted"] == 0, "paused at start: position marker shown, no green notes")
    check(page.is_checked("[data-testid=follow]"), "跟随乐谱 toggle defaults to on")

    # 1) score synth from the start
    page.select_option("[data-testid=mode]", "synth")
    page.click("[data-testid=play]")
    page.wait_for_function("() => window.__m2s && window.__m2s.player.isRunning", timeout=60000)
    time.sleep(8)
    page.screenshot(path=out / "follow-synth.png")
    snaps = play_and_sample(page, 22, "synth")
    tops = sorted({s["scrollTop"] for s in snaps})
    check(len(tops) >= 2, f"synth: score panel scrolls to follow (scrollTop values {tops[:6]})")
    page.screenshot(path=out / "follow-synth-later.png")

    # 2) manual scroll is respected for ~3 s, then following resumes
    box = page.locator("[data-testid=score-scroll]").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    for _ in range(3):
        page.mouse.wheel(0, 400)
    time.sleep(0.3)
    st_user = page.evaluate("document.querySelector('[data-testid=score-scroll]').scrollTop")
    time.sleep(1.5)
    st_mid = page.evaluate("document.querySelector('[data-testid=score-scroll]').scrollTop")
    check(abs(st_mid - st_user) < 3, f"manual scroll kept for 1.5 s ({st_user} -> {st_mid})")
    time.sleep(3.0)
    s = snap(page)
    check(s["bandVisible"], f"following resumes after the grace period (scrollTop {s['scrollTop']})")

    # 3) pause
    page.click("[data-testid=play]")
    time.sleep(0.4)
    s = snap(page)
    check(
        s["painted"] == 0 and s["heads"] == 0 and s["bandShown"] and s["bandOpacity"] < 1,
        f"pause: green cleared ({s['heads']} heads), marker kept (opacity {s['bandOpacity']})",
    )
    page.screenshot(path=out / "follow-paused.png")

    # 4) double-click a note further down → playhead moves to it
    page.evaluate("document.querySelector('[data-testid=score-scroll]').scrollTop += 700")
    time.sleep(0.6)
    target = page.evaluate("""() => {
      const sc = document.querySelector('[data-testid=score-scroll]').getBoundingClientRect();
      const g = [...document.querySelectorAll('[data-testid=score] .vf-notehead')].find(e => {
        const r = e.getBoundingClientRect();
        return r.top > sc.top + sc.height * 0.4 && r.bottom < sc.bottom - 10 && r.left > sc.left + 200;
      });
      if (!g) return null;
      const r = g.getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2];
    }""")
    if target:
        page.mouse.dblclick(*target)
        time.sleep(0.5)
        r = page.evaluate("""() => {
          const ed = window.__editor.getState();
          const n = ed.doc.notes.find(n => n.id === ed.selected[0]);
          return n ? [n.start, ed.playheadTick] : null }""")
        check(r is not None and abs(r[0] - r[1]) < 2, f"double-click a note seeks to it (note {r})")
    else:
        check(False, "found a notehead to double-click")

    # 5) original recording from there
    page.select_option("[data-testid=mode]", "audio")
    time.sleep(0.3)
    if target:  # the mode switch keeps the position; re-seek to be explicit
        page.mouse.dblclick(*target)
        time.sleep(0.3)
    page.keyboard.press("Escape")  # a selected note stays blue (selection wins over green)
    st_before = page.evaluate("document.querySelector('[data-testid=score-scroll]').scrollTop")
    page.click("[data-testid=play]")
    page.wait_for_function("() => !document.querySelector('audio').paused", timeout=10000)
    snaps = play_and_sample(page, 14, "recording")
    page.screenshot(path=out / "follow-audio.png")
    check(
        any(abs(s["scrollTop"] - st_before) > 2 for s in snaps) or all(s["bandVisible"] for s in snaps),
        "recording: panel follows (or the measure was already in view)",
    )

    # 6) toggle off while playing → no marker, no green; back on → shows again
    page.uncheck("[data-testid=follow]")
    time.sleep(0.5)
    s = snap(page)
    check(not s["bandShown"] and s["heads"] == 0, "跟随乐谱 off: marker hidden and no green noteheads")
    page.check("[data-testid=follow]")
    time.sleep(0.5)
    s = snap(page)
    check(s["bandShown"] and s["painted"] >= 0 and s["wrong"] == 0, "跟随乐谱 on again: marker back")
    page.click("[data-testid=play]")

    # 7) narrower window → the score re-flows; following must use the new layout
    page.wait_for_function("!window.__editor.getState().playing", timeout=5000)
    score_before = page.evaluate("window.__m2sScore")
    page.set_viewport_size({"width": 1000, "height": 760})
    page.wait_for_function("(s) => window.__m2sScore !== s", arg=score_before, timeout=30000)
    time.sleep(0.5)
    page.click("[data-testid=play]")
    page.wait_for_function("() => !document.querySelector('audio').paused", timeout=10000)
    play_and_sample(page, 8, "after resize")
    page.screenshot(path=out / "follow-narrow.png")
    page.click("[data-testid=play]")

    page.close()

    seek_before_first_synth_play(b)

    check(not errors, f"no page errors ({errors[:3]})")
    b.close()

print("FAILED" if failures else "ALL PASSED")
sys.exit(1 if failures else 0)
