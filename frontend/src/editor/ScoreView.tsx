// OSMD score view. The score is re-rendered from MusicXML for each saved revision;
// clicks map back to ScoreNote ids through the backend's idmap
// ("m{measure}|s{staff}|q{offset in quarters}|p{midi}").
// During playback the view follows the playhead: sounding noteheads turn green, a band
// marks the current measure with a cursor line at the current beat, and the panel
// scrolls to keep the current system visible. That runs outside React rendering
// (store subscription, ≤ 30 Hz, only changed noteheads are repainted).
import { OpenSheetMusicDisplay, PointF2D } from "opensheetmusicdisplay";
import type { GraphicalNote } from "opensheetmusicdisplay";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { buildLayout, cursorX, glyphSpan, measureAt, type NoteGlyph, type ScoreLayout, SoundingTracker } from "./scoreFollow";
import { useEditor } from "./store";

const SELECTED = "#2563eb";
const LOW_CONF = "#ea580c";
const PLAYING = "#16a34a";
const FRAME_MS = 32; // follow updates ≤ ~30 Hz
const USER_SCROLL_GRACE_MS = 3000;
const PAGE_TURN_AT = 0.5; // in a system's last measure, bring the next system into view from here

function gcd(a: number, b: number): number {
  return b ? gcd(b, a % b) : Math.abs(a);
}

/**
 * Python-style Fraction string of quarter lengths from an OSMD whole-note fraction.
 * OSMD keeps fractions as WholeValue + proper Numerator/Denominator, so callers pass the
 * expanded numerator (bars longer than a whole note: 12/8, 6/4, 3/2…).
 */
export function quarters(num: number, den: number): string {
  let n = num * 4;
  let d = den;
  const g = gcd(n, d) || 1;
  n /= g;
  d /= g;
  return d === 1 ? `${n}` : `${n}/${d}`;
}

function keyOf(gn: GraphicalNote): string | null {
  const note = gn.sourceNote;
  if (!note || note.isRest() || !note.Pitch) return null;
  const se = note.ParentStaffEntry;
  const ts = se.Timestamp;
  const staff = se.ParentStaff.idInMusicSheet + 1;
  const measure = note.SourceMeasure.MeasureNumberXML;
  const midi = note.Pitch.getHalfTone() + 12;
  return `m${measure}|s${staff}|q${quarters(ts.GetExpandedNumerator(), ts.Denominator)}|p${midi}`;
}

const svgOf = (gn: GraphicalNote | undefined) =>
  (gn as unknown as { getSVGGElement?: () => SVGGElement | undefined } | undefined)?.getSVGGElement?.();

function paint(gn: GraphicalNote, color: string | null) {
  // VexFlowGraphicalNote exposes its SVG group; recolour noteheads in place (no re-render)
  const g = svgOf(gn);
  if (!g) return;
  g.querySelectorAll<SVGElement>(".vf-notehead path, .vf-notehead").forEach((el) => {
    if (color) {
      el.setAttribute("fill", color);
      el.setAttribute("stroke", color);
    } else {
      el.removeAttribute("fill");
      el.removeAttribute("stroke");
    }
  });
}

interface Rendered {
  byId: Map<string, GraphicalNote[]>;
  glyphs: Map<string, NoteGlyph[]>;
  idOf: Map<GraphicalNote, string>;
  layout: ScoreLayout;
  off: { x: number; y: number }; // svg origin inside the wrapper, px
  scale: number; // px per OSMD unit
}

export function ScoreView({ jobId, onSeek }: { jobId: string; onSeek?: (tick: number) => void }) {
  const scroller = useRef<HTMLDivElement>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const host = useRef<HTMLDivElement>(null);
  const band = useRef<HTMLDivElement>(null);
  const cursor = useRef<HTMLDivElement>(null);
  const osmdRef = useRef<OpenSheetMusicDisplay | null>(null);
  const [rendered, setRendered] = useState<Rendered | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState("");
  const savedRev = useEditor((s) => s.savedRev);
  const selected = useEditor((s) => s.selected);
  const notes = useEditor((s) => s.doc?.notes);
  const select = useEditor((s) => s.select);
  const idmapRef = useRef<Record<string, string>>({});
  const loadingRef = useRef(false);
  const swappingRef = useRef(false); // OSMD is replacing the SVG (empty panel): don't auto-scroll

  // colour state shared by the selection effect and the follow loop
  const selRef = useRef<Set<string>>(new Set());
  const lowRef = useRef<Set<string>>(new Set());
  const playingRef = useRef<Set<GraphicalNote>>(new Set());
  const colorOf = (id: string, gn: GraphicalNote) =>
    selRef.current.has(id)
      ? SELECTED
      : playingRef.current.has(gn)
        ? PLAYING
        : lowRef.current.has(id)
          ? LOW_CONF
          : null;

  useEffect(() => {
    if (!host.current) return;
    // autoResize off: OSMD's own resize re-render would replace every GraphicalNote and
    // leave our id map stale; we re-render and re-index on width changes ourselves
    osmdRef.current = new OpenSheetMusicDisplay(host.current, {
      backend: "svg",
      autoResize: false,
      drawTitle: true,
      drawComposer: false,
      drawCredits: false,
      drawPartNames: false,
    });
  }, []);

  /** Map rendered noteheads to ScoreNote ids and measure the layout for score following. */
  const index = (osmd: OpenSheetMusicDisplay, idmap: Record<string, string>) => {
    const ppq = useEditor.getState().doc?.ppq ?? 960;
    const byId = new Map<string, GraphicalNote[]>();
    const glyphs = new Map<string, NoteGlyph[]>();
    const idOf = new Map<GraphicalNote, string>();
    const unmatched: string[] = [];
    let pitched = 0;
    for (const staffMeasures of osmd.GraphicSheet.MeasureList) {
      for (const gm of staffMeasures) {
        if (!gm) continue;
        for (const se of gm.staffEntries) {
          for (const gve of se.graphicalVoiceEntries) {
            for (const gn of gve.notes) {
              const k = keyOf(gn);
              if (k) pitched++;
              const id = k ? idmap[k] : undefined;
              if (!id) {
                if (k) unmatched.push(k);
                continue;
              }
              byId.set(id, [...(byId.get(id) ?? []), gn]);
              const [t0, t1] = glyphSpan(gn, ppq);
              glyphs.set(id, [...(glyphs.get(id) ?? []), { gn, t0, t1 }]);
              idOf.set(gn, id);
            }
          }
        }
      }
    }
    const svg = host.current?.querySelector("svg");
    const wr = wrap.current?.getBoundingClientRect();
    const sr = svg?.getBoundingClientRect();
    const off = sr && wr ? { x: sr.left - wr.left, y: sr.top - wr.top } : { x: 0, y: 0 };
    playingRef.current = new Set(); // fresh SVG: nothing painted
    setRendered({ byId, glyphs, idOf, layout: buildLayout(osmd, ppq), off, scale: 10 * osmd.zoom });
    // debug/e2e hook: how many rendered noteheads map back to ScoreNotes
    (window as unknown as Record<string, unknown>).__m2sScore = {
      rev: useEditor.getState().savedRev,
      pitched,
      unmatched,
      ids: byId.size,
    };
  };

  // (re)load the score for the latest saved revision
  useEffect(() => {
    let cancelled = false;
    const osmd = osmdRef.current;
    if (!osmd) return;
    loadingRef.current = true;
    if (!osmd.GraphicSheet) setStatus("loading");
    (async () => {
      const sc = scroller.current;
      try {
        // the previous revision stays on screen (and followed) while the new one downloads
        const [xml, idmap] = await Promise.all([api.musicxml(jobId, savedRev), api.idmap(jobId, savedRev)]);
        if (cancelled) return;
        setStatus("loading");
        // osmd.load empties the panel, which resets its scroll position: keep the reader's place
        const top = sc?.scrollTop ?? 0;
        swappingRef.current = true;
        await osmd.load(xml);
        if (cancelled) return;
        osmd.render();
        if (sc) sc.scrollTop = top;
        idmapRef.current = idmap;
        index(osmd, idmap);
        setStatus("ready");
      } catch (e) {
        if (!cancelled) {
          setStatus("error");
          setError(String(e));
        }
      } finally {
        swappingRef.current = false;
        if (!cancelled) loadingRef.current = false;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [jobId, savedRev]); // eslint-disable-line react-hooks/exhaustive-deps

  // re-render + re-index when the panel width changes
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    let width = el.clientWidth;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const ro = new ResizeObserver(() => {
      if (Math.abs(el.clientWidth - width) < 2) return;
      width = el.clientWidth;
      clearTimeout(timer);
      timer = setTimeout(() => {
        const osmd = osmdRef.current;
        if (!osmd?.GraphicSheet || loadingRef.current) return; // a load in flight renders at the new width
        const top = el.scrollTop / Math.max(1, el.scrollHeight);
        osmd.render();
        index(osmd, idmapRef.current);
        el.scrollTop = top * el.scrollHeight;
      }, 250);
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      clearTimeout(timer);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const lowConf = useMemo(() => new Set((notes ?? []).filter((n) => n.confidence < 0.5).map((n) => n.id)), [notes]);

  // colour selection + low-confidence notes (sounding notes keep their green)
  useEffect(() => {
    selRef.current = new Set(selected);
    lowRef.current = lowConf;
    if (!rendered) return;
    for (const [id, gns] of rendered.byId) for (const gn of gns) paint(gn, colorOf(id, gn));
  }, [rendered, selected, lowConf]); // eslint-disable-line react-hooks/exhaustive-deps

  // scroll the first selected note into view when the selection changes (a re-render of
  // the same selection keeps the reader's place, and must not fight score following)
  const scrolledFor = useRef<string | undefined>(undefined);
  useEffect(() => {
    const first = selected[0];
    const g = svgOf(first ? rendered?.byId.get(first)?.[0] : undefined);
    if (!g || scrolledFor.current === first) return;
    scrolledFor.current = first;
    g.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
  }, [selected, rendered]);

  // score following: subscribe to the playhead outside React rendering
  useEffect(() => {
    const sc = scroller.current;
    const bandEl = band.current;
    const cur = cursor.current;
    // while a new revision is being laid out the previous layout keeps the marker in place
    if (!rendered || !sc || !bandEl || !cur || status === "error") return;
    const { glyphs, idOf, layout, off, scale } = rendered;
    const ppq = useEditor.getState().doc?.ppq ?? 960;
    let trackedNotes = useEditor.getState().doc?.notes;
    let tracker = new SoundingTracker(trackedNotes ?? [], ppq);
    let raf = 0;
    let lastRun = 0;
    let lastTick = NaN;
    let lastMeasure = -1;
    let shownSys = -1;
    let lastTarget = NaN;
    let lastScrollAt = 0;
    let userAt = -Infinity;
    let updates = 0;
    let runTick = NaN; // playhead used by the last update (e2e hook)
    let unshown = 0; // sounding notes with no drawn notehead at the playhead (e2e hook)
    const wrapTop = wrap.current?.offsetTop ?? 0; // constant while this layout is shown

    const setPlayingGlyphs = (want: Set<GraphicalNote>) => {
      const playing = playingRef.current;
      for (const gn of [...playing]) {
        if (want.has(gn)) continue;
        playing.delete(gn);
        paint(gn, colorOf(idOf.get(gn) ?? "", gn));
      }
      for (const gn of want) {
        if (playing.has(gn)) continue;
        playing.add(gn);
        paint(gn, colorOf(idOf.get(gn) ?? "", gn));
      }
    };

    const hide = () => {
      bandEl.style.display = "none";
      cur.style.display = "none";
      lastMeasure = -1;
      shownSys = -1;
    };

    const run = () => {
      updates++;
      const st = useEditor.getState();
      const tick = st.playheadTick;
      runTick = tick;
      if (!st.follow) {
        setPlayingGlyphs(new Set());
        hide();
        lastTick = NaN;
        return;
      }
      if (st.doc && st.doc.notes !== trackedNotes) {
        trackedNotes = st.doc.notes;
        tracker = new SoundingTracker(trackedNotes, ppq);
      }

      // 1) sounding noteheads (only while playing; a paused score stays editable-looking)
      // Only the drawn segment under the playhead lights up: a note held past the end of
      // its drawn value (voice truncation) or not rendered yet (unsaved edit) stays dark.
      const want = new Set<GraphicalNote>();
      unshown = 0;
      if (st.playing) {
        tracker.update(tick);
        for (const id of tracker.active) {
          let hit = false;
          for (const g of glyphs.get(id) ?? []) {
            if (g.t0 <= tick && tick < g.t1) {
              want.add(g.gn);
              hit = true;
            }
          }
          if (!hit) unshown++;
        }
      }
      setPlayingGlyphs(want);

      // 2) measure band + cursor line
      const mi = measureAt(layout, tick);
      if (mi < 0) return hide();
      const m = layout.measures[mi];
      const sys = layout.systems[m.sys];
      const top = off.y + (sys.top - 2.5) * scale;
      const height = (sys.bottom - sys.top + 5) * scale;
      if (mi !== lastMeasure) {
        lastMeasure = mi;
        Object.assign(bandEl.style, {
          display: "block",
          left: `${off.x + m.x * scale}px`,
          top: `${top}px`,
          width: `${m.w * scale}px`,
          height: `${height}px`,
        });
        bandEl.dataset.measure = String(mi);
        cur.style.height = `${height}px`;
      }
      cur.style.display = "block";
      cur.style.transform = `translate(${off.x + cursorX(m, tick) * scale - 1}px, ${top}px)`;
      bandEl.style.opacity = st.playing ? "1" : "0.55";
      cur.style.opacity = st.playing ? "1" : "0.55";

      // 3) keep the current system in view
      const moved = tick !== lastTick;
      lastTick = tick;
      const now = performance.now();
      if (!moved && shownSys === m.sys) return;
      if (swappingRef.current) return; // the panel is empty while OSMD swaps the SVG
      if (st.playing && now - userAt < USER_SCROLL_GRACE_MS) return; // the user is reading elsewhere
      const base = wrapTop + off.y;
      const y = (u: number) => base + u * scale;
      const vTop = sc.scrollTop;
      const vH = sc.clientHeight;
      const next = layout.measures[mi + 1];
      shownSys = m.sys;
      let target: number;
      if (st.playing && next && next.sys !== m.sys && (tick - m.tick0) >= (m.tick1 - m.tick0) * PAGE_TURN_AT) {
        // page-turn look-ahead: late in a system's last measure show as much of the next
        // system as fits while keeping the current measure (its staves) on screen
        const keepTop = y(sys.top - 2.5) - 4;
        const nextBot = y(layout.systems[next.sys].bottom + 3);
        if (keepTop >= vTop - 1 && nextBot <= vTop + vH + 1) return;
        target = Math.min(keepTop, nextBot - vH);
      } else {
        const sTop = y(Math.min(sys.inkTop, sys.top - 2.5) - 1);
        const sBot = y(sys.bottom + 3);
        // while playing, scroll before the system reaches the bottom edge so the next one is visible
        const lookahead = st.playing ? Math.min(vH * 0.25, sBot - sTop) : 0;
        if (sTop >= vTop - 1 && sBot <= vTop + vH - lookahead + 1) return;
        target = sTop - Math.min(vH * 0.05, 12);
      }
      target = Math.round(Math.max(0, Math.min(sc.scrollHeight - vH, target)));
      if (Math.abs(vTop - target) < 3) return;
      if (target === lastTarget && now - lastScrollAt < 900) return; // smooth scroll in flight
      lastTarget = target;
      lastScrollAt = now;
      // long jumps (seek, resume after reading elsewhere) land at once instead of racing past
      sc.scrollTo({ top: target, behavior: Math.abs(vTop - target) > vH * 1.5 ? "auto" : "smooth" });
    };

    const frame = (now: number) => {
      raf = 0;
      if (now - lastRun < FRAME_MS) {
        raf = requestAnimationFrame(frame);
        return;
      }
      lastRun = now;
      run();
    };
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(frame);
    };
    const unsub = useEditor.subscribe((s, p) => {
      if (s.playing && !p.playing) userAt = -Infinity; // pressing play: follow right away
      if (s.playheadTick !== p.playheadTick || s.playing !== p.playing || s.follow !== p.follow || s.doc !== p.doc)
        schedule();
    });

    // the user scrolling the score pauses auto-follow for a few seconds
    const markUser = () => (userAt = performance.now());
    const onPointer = (e: PointerEvent) => {
      // scrollbar drag: offsetX/Y are relative to the hit SVG element, not the panel, so use the panel rect
      const r = sc.getBoundingClientRect();
      if (e.clientX - r.left >= sc.clientWidth || e.clientY - r.top >= sc.clientHeight) markUser();
    };
    sc.addEventListener("wheel", markUser, { passive: true });
    sc.addEventListener("touchmove", markUser, { passive: true });
    sc.addEventListener("pointerdown", onPointer);

    (window as unknown as Record<string, unknown>).__m2sFollow = {
      state: () => ({
        tick: useEditor.getState().playheadTick,
        at: runTick,
        measure: lastMeasure,
        system: shownSys,
        active: [...tracker.active],
        unshown,
        painted: [...new Set([...playingRef.current].map((gn) => idOf.get(gn)))],
        scrollTop: sc.scrollTop,
        updates,
      }),
    };

    run();
    return () => {
      unsub();
      cancelAnimationFrame(raf);
      sc.removeEventListener("wheel", markUser);
      sc.removeEventListener("touchmove", markUser);
      sc.removeEventListener("pointerdown", onPointer);
      setPlayingGlyphs(new Set());
      hide();
    };
  }, [rendered, status]); // eslint-disable-line react-hooks/exhaustive-deps

  const nearestId = (e: React.MouseEvent): string | undefined => {
    const osmd = osmdRef.current;
    if (!osmd?.GraphicSheet) return undefined;
    const svgPt = osmd.GraphicSheet.domToSvg(new PointF2D(e.clientX, e.clientY));
    const pt = osmd.GraphicSheet.svgToOsmd(svgPt);
    const gn = osmd.GraphicSheet.GetNearestNote(pt, new PointF2D(2, 2));
    const k = gn ? keyOf(gn) : null;
    const id = k ? idmapRef.current[k] : undefined;
    if (id || !rendered) return id;
    // OSMD's nearest note can be a rest (e.g. a hidden filler rest of a second voice) or a
    // note of another voice: prefer the drawn chord under the pointer, else the nearest
    // mapped note (positions are the notehead's left edge, so x is looser than y)
    const group = (e.target as Element | null)?.closest?.(".vf-stavenote");
    let best: string | undefined;
    let bestD = Infinity;
    for (const [g, gid] of rendered.idOf) {
      const p = g.PositionAndShape.AbsolutePosition;
      const dy = p.y - pt.y;
      const onIt = group !== null && group !== undefined && svgOf(g) === group;
      const dx = onIt ? 0 : Math.max(0, Math.abs(p.x + 1.2 - pt.x) - 1.5);
      const d = dx * dx + dy * dy - (onIt ? 1000 : 0);
      if ((onIt || (Math.abs(dx) < 2.5 && Math.abs(dy) < 2.5)) && d < bestD) {
        bestD = d;
        best = gid;
      }
    }
    return best;
  };

  const onClick = (e: React.MouseEvent) => {
    const id = nearestId(e);
    if (id) select([id], e.shiftKey || e.metaKey);
    else if (!e.shiftKey) select([]);
  };

  // double-click a note: move the playhead to it
  const onDoubleClick = (e: React.MouseEvent) => {
    const id = nearestId(e);
    const n = id ? useEditor.getState().doc?.notes.find((x) => x.id === id) : undefined;
    if (n && onSeek) onSeek(n.start);
  };

  return (
    <div
      ref={scroller}
      className="relative h-full overflow-auto bg-white"
      onClick={onClick}
      onDoubleClick={onDoubleClick}
      data-testid="score-scroll"
    >
      {status !== "ready" && (
        // zero-height sticky row so the banner never shifts the score (follow geometry)
        <div className="sticky top-0 z-10 h-0 overflow-visible">
          <div className="bg-amber-50 px-3 py-1 text-xs text-amber-800">
            {status === "loading" ? "正在排版乐谱…" : `乐谱渲染失败：${error}`}
          </div>
        </div>
      )}
      <div ref={wrap} className="relative">
        <div ref={host} data-testid="score" />
        <div className="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden>
          <div
            ref={band}
            data-testid="follow-band"
            className="absolute rounded-sm"
            style={{ display: "none", background: "rgba(34,197,94,0.10)", boxShadow: "inset 0 0 0 1px rgba(22,163,74,0.25)" }}
          />
          <div
            ref={cursor}
            data-testid="follow-cursor"
            className="absolute left-0 top-0"
            style={{ display: "none", width: 2, background: "rgba(22,163,74,0.8)", willChange: "transform" }}
          />
        </div>
      </div>
    </div>
  );
}
