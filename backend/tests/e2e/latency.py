"""Measure edit → re-rendered preview latency in the browser.

usage: uv run python tests/e2e/latency.py JOB_ID [--base URL] [--n 5]
Reports: total (keypress → score re-rendered, includes the 600 ms autosave debounce),
and the server part (PUT + MusicXML render) measured from network timings.
"""

import argparse
import statistics
import time

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("job")
ap.add_argument("--base", default="http://127.0.0.1:8000")
ap.add_argument("--n", type=int, default=5)
a = ap.parse_args()

with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1440, "height": 900})
    page.goto(f"{a.base}/jobs/{a.job}")
    page.wait_for_function("window.__m2sScore !== undefined", timeout=120000)
    info = page.evaluate("window.__m2sScore")
    print(f"noteheads={info['pitched']} unmatched={len(info['unmatched'])} notes={info['ids']}")
    page.locator("[data-testid=score] .vf-notehead").nth(10).click()
    totals, servers = [], []
    sent: dict[str, float] = {}
    page.on("request", lambda r: sent.__setitem__(r.url, time.perf_counter()) if "/doc" in r.url else None)
    for _ in range(a.n):
        rev = page.evaluate("window.__m2sScore.rev")
        t0 = time.perf_counter()
        with page.expect_response(lambda r: "/render/musicxml" in r.url) as resp:
            page.keyboard.press("ArrowUp")  # always a new, uncached state
        t_xml = time.perf_counter()
        put_t = max(sent.values())
        _ = resp.value
        page.wait_for_function(f"window.__m2sScore.rev > {rev}", timeout=60000)
        totals.append(time.perf_counter() - t0)
        servers.append(t_xml - put_t)
    med = statistics.median(totals)
    print(
        f"keypress→preview  median {med:.2f}s  (includes 0.6s debounce)  all={[round(t, 2) for t in totals]}"
    )
    print(f"server (PUT → musicxml rendered) median {statistics.median(servers):.2f}s")
    print(f"client-side (excl. debounce) ≈ {statistics.median(totals) - 0.6:.2f}s")
    b.close()
