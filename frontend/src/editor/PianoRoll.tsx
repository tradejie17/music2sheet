// Piano roll editor (Canvas 2D, virtual scrolling). Edits go to the ScoreDoc store:
// drag = move (snapped to grid, Alt = free), right edge = resize, double-click = add,
// drag on empty space = box select, click the ruler = seek.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ScoreNote } from "../api/types";
import { barlines, beatTicks } from "./music";
import { useEditor } from "./store";

const KEY_W = 44;
const RULER_H = 22;
const ROW_H = 8;
const LO = 21;
const HI = 108;
const EDGE_PX = 6;

const COLORS = {
  rh: "#3b82f6",
  lh: "#10b981",
  sel: "#1e3a8a",
  low: "#f97316",
  ghost: "rgba(100,116,139,0.28)",
  bar: "#94a3b8",
  beat: "#e2e8f0",
  black: "#f1f5f9",
  playhead: "#ef4444",
};

export interface Ghost {
  pitch: number;
  start: number;
  end: number;
}

type Drag =
  | { kind: "move" | "resize"; x0: number; y0: number; anchor: ScoreNote; ids: string[]; dt: number; dp: number; dd: number }
  | { kind: "box"; x0: number; y0: number; x1: number; y1: number; additive: boolean };

const isBlack = (p: number) => [1, 3, 6, 8, 10].includes(p % 12);

export function PianoRoll({ ghosts, onSeek }: { ghosts: Ghost[]; onSeek: (tick: number) => void }) {
  const doc = useEditor((s) => s.doc);
  const selected = useEditor((s) => s.selected);
  const grid = useEditor((s) => s.gridTicks);
  const playhead = useEditor((s) => s.playheadTick);
  const playing = useEditor((s) => s.playing);
  const { select, editNotes, addNote } = useEditor.getState();

  const scroller = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [view, setView] = useState({ x: 0, y: 0, w: 800, h: 300 });
  const [pxPerQuarter, setPxPerQuarter] = useState(60);
  const [drag, setDrag] = useState<Drag | null>(null);
  const [hoverCursor, setHoverCursor] = useState("default");

  const ppq = doc?.ppq ?? 960;
  const ppx = pxPerQuarter / ppq; // px per tick
  const endTick = useMemo(() => Math.max(ppq * 8, ...(doc?.notes ?? []).map((n) => n.start + n.dur)), [doc, ppq]);
  const bars = useMemo(() => (doc ? barlines(doc, endTick + ppq * 8) : []), [doc, endTick, ppq]);
  const totalW = KEY_W + (endTick + ppq * 8) * ppx;
  const totalH = RULER_H + (HI - LO + 1) * ROW_H;
  const sel = useMemo(() => new Set(selected), [selected]);

  const xOf = useCallback((t: number) => KEY_W + t * ppx - view.x, [ppx, view.x]);
  const yOf = useCallback((p: number) => RULER_H + (HI - p) * ROW_H - view.y, [view.y]);
  const tickAt = (x: number) => (x - KEY_W + view.x) / ppx;
  const pitchAt = (y: number) => Math.round(HI - (y - RULER_H + view.y - ROW_H / 2) / ROW_H);
  const snap = (t: number, free = false) => (free ? Math.round(t) : Math.round(t / grid) * grid);

  // initial vertical scroll: centre around middle C
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = RULER_H + (HI - 76) * ROW_H;
  }, []);

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setView((v) => ({ ...v, w: el.clientWidth, h: el.clientHeight })));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // follow the playhead while playing
  useEffect(() => {
    const el = scroller.current;
    if (!el || !playing) return;
    const x = KEY_W + playhead * ppx;
    if (x < el.scrollLeft + KEY_W || x > el.scrollLeft + el.clientWidth - 80) el.scrollLeft = x - KEY_W - 80;
  }, [playhead, playing, ppx]);

  const noteRect = useCallback(
    (n: ScoreNote) => {
      let { start, dur, pitch } = n;
      if (drag && (drag.kind === "move" || drag.kind === "resize") && sel.has(n.id)) {
        start += drag.dt;
        pitch += drag.dp;
        dur = Math.max(grid / 2, dur + drag.dd);
      }
      return { x: xOf(start), y: yOf(pitch), w: Math.max(2, dur * ppx - 1), h: ROW_H - 1 };
    },
    [drag, sel, grid, xOf, yOf, ppx],
  );

  // ---- drawing ----
  useEffect(() => {
    const c = canvas.current;
    if (!c || !doc) return;
    const dpr = window.devicePixelRatio || 1;
    const [cw, ch] = [Math.round(view.w * dpr), Math.round(view.h * dpr)];
    if (c.width !== cw || c.height !== ch) {
      // resizing reallocates the backing store: only when the size really changed
      c.width = cw;
      c.height = ch;
      c.style.width = `${view.w}px`;
      c.style.height = `${view.h}px`;
    }
    const g = c.getContext("2d")!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, view.w, view.h);

    // pitch rows
    for (let p = LO; p <= HI; p++) {
      const y = yOf(p);
      if (y < RULER_H - ROW_H || y > view.h) continue;
      if (isBlack(p)) {
        g.fillStyle = COLORS.black;
        g.fillRect(KEY_W, y, view.w, ROW_H);
      }
      if (p % 12 === 0) {
        g.fillStyle = "#cbd5e1";
        g.fillRect(KEY_W, y + ROW_H - 0.5, view.w, 0.5);
      }
    }
    // beats + bars
    const t0 = tickAt(KEY_W);
    const t1 = tickAt(view.w);
    for (let i = 0; i < bars.length - 1; i++) {
      const [b0, b1] = [bars[i], bars[i + 1]];
      if (b1 < t0 || b0 > t1) continue;
      const meter = [...doc.meters].reverse().find((m) => m.tick <= b0) ?? doc.meters[0];
      const bt = beatTicks(meter, ppq);
      g.fillStyle = COLORS.beat;
      for (let t = b1 - bt; t > b0; t -= bt) g.fillRect(xOf(t), RULER_H, 1, view.h);
      g.fillStyle = COLORS.bar;
      g.fillRect(xOf(b0), RULER_H, 1, view.h);
    }
    // raw transcription ghosts
    g.fillStyle = COLORS.ghost;
    for (const gh of ghosts) {
      if (gh.end < t0 || gh.start > t1) continue;
      g.fillRect(xOf(gh.start), yOf(gh.pitch) + 2, Math.max(2, (gh.end - gh.start) * ppx), ROW_H - 4);
    }
    // notes
    for (const n of doc.notes) {
      if (n.start + n.dur < t0 - (drag ? ppq * 8 : 0) || n.start > t1 + (drag ? ppq * 8 : 0)) continue;
      const r = noteRect(n);
      g.globalAlpha = n.voice > 1 ? 0.7 : 1;
      g.fillStyle = sel.has(n.id) ? COLORS.sel : n.staff === 1 ? COLORS.rh : COLORS.lh;
      g.fillRect(r.x, r.y, r.w, r.h);
      g.globalAlpha = 1;
      if (n.confidence < 0.5) {
        g.strokeStyle = COLORS.low;
        g.lineWidth = 2;
        g.strokeRect(r.x + 1, r.y + 1, r.w - 2, r.h - 2);
      }
    }
    // box selection
    if (drag?.kind === "box") {
      g.strokeStyle = COLORS.sel;
      g.setLineDash([4, 3]);
      g.strokeRect(drag.x0, drag.y0, drag.x1 - drag.x0, drag.y1 - drag.y0);
      g.setLineDash([]);
    }
    // ruler
    g.fillStyle = "#f8fafc";
    g.fillRect(0, 0, view.w, RULER_H);
    g.fillStyle = "#475569";
    g.font = "11px ui-sans-serif, system-ui";
    const firstNo = doc.pickup_ticks ? 0 : 1;
    for (let i = 0; i < bars.length - 1; i++) {
      const x = xOf(bars[i]);
      if (x < KEY_W - 40 || x > view.w) continue;
      g.fillRect(x, 8, 1, RULER_H - 8);
      g.fillText(String(i + firstNo), x + 3, 13);
    }
    // sustain pedal lane (bottom of ruler)
    g.fillStyle = "rgba(16,185,129,0.6)";
    for (const p of doc.pedals) {
      if (p.kind !== "sustain" || p.end < t0 || p.start > t1) continue;
      g.fillRect(xOf(p.start), RULER_H - 4, Math.max(2, (p.end - p.start) * ppx - 2), 3);
    }
    // keyboard
    for (let p = LO; p <= HI; p++) {
      const y = yOf(p);
      if (y < RULER_H - ROW_H || y > view.h) continue;
      g.fillStyle = isBlack(p) ? "#334155" : "#ffffff";
      g.fillRect(0, y, KEY_W, ROW_H);
      g.fillStyle = "#e2e8f0";
      g.fillRect(0, y + ROW_H - 0.5, KEY_W, 0.5);
      if (p % 12 === 0) {
        g.fillStyle = "#64748b";
        g.font = "8px ui-sans-serif";
        g.fillText(`C${p / 12 - 1}`, 22, y + ROW_H - 1);
      }
    }
    g.fillStyle = "#f8fafc";
    g.fillRect(0, 0, KEY_W, RULER_H);
    // playhead
    const px = xOf(playhead);
    if (px >= KEY_W) {
      g.fillStyle = COLORS.playhead;
      g.fillRect(px, 0, 1.5, view.h);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [doc, view, ghosts, bars, sel, drag, playhead, ppx, noteRect]);

  // ---- interaction ----
  const local = (e: React.PointerEvent | React.MouseEvent) => {
    const r = canvas.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  const hit = (x: number, y: number): { note: ScoreNote; edge: boolean } | null => {
    if (!doc) return null;
    let best: { note: ScoreNote; edge: boolean } | null = null;
    for (const n of doc.notes) {
      const r = noteRect(n);
      if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) {
        const cand = { note: n, edge: x >= r.x + r.w - EDGE_PX };
        if (!best || sel.has(n.id)) best = cand;
      }
    }
    return best;
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (!doc || e.button !== 0) return;
    const { x, y } = local(e);
    if (y < RULER_H) {
      onSeek(Math.max(0, tickAt(x)));
      return;
    }
    if (x < KEY_W) return;
    canvas.current!.setPointerCapture(e.pointerId);
    const h = hit(x, y);
    if (h) {
      let ids = selected;
      if (e.shiftKey || e.metaKey) {
        ids = sel.has(h.note.id) ? selected.filter((i) => i !== h.note.id) : [...selected, h.note.id];
        select(ids);
      } else if (!sel.has(h.note.id)) {
        ids = [h.note.id];
        select(ids);
      }
      setDrag({ kind: h.edge ? "resize" : "move", x0: x, y0: y, anchor: h.note, ids, dt: 0, dp: 0, dd: 0 });
    } else {
      setDrag({ kind: "box", x0: x, y0: y, x1: x, y1: y, additive: e.shiftKey || e.metaKey });
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const { x, y } = local(e);
    if (!drag) {
      const h = y > RULER_H && x > KEY_W ? hit(x, y) : null;
      setHoverCursor(y < RULER_H ? "pointer" : h ? (h.edge ? "ew-resize" : "grab") : "crosshair");
      return;
    }
    if (drag.kind === "box") {
      setDrag({ ...drag, x1: x, y1: y });
      return;
    }
    const rawDt = (x - drag.x0) / ppx;
    if (drag.kind === "move") {
      const dt = snap(drag.anchor.start + rawDt, e.altKey) - drag.anchor.start;
      const dp = pitchAt(y) - pitchAt(drag.y0);
      setDrag({ ...drag, dt: Math.max(dt, -drag.anchor.start), dp });
    } else {
      const end = snap(drag.anchor.start + drag.anchor.dur + rawDt, e.altKey);
      setDrag({ ...drag, dd: Math.max(grid / 2, end - drag.anchor.start) - drag.anchor.dur });
    }
  };

  const onPointerUp = () => {
    if (!drag || !doc) return;
    if (drag.kind === "box") {
      const [xa, xb] = [Math.min(drag.x0, drag.x1), Math.max(drag.x0, drag.x1)];
      const [ya, yb] = [Math.min(drag.y0, drag.y1), Math.max(drag.y0, drag.y1)];
      if (xb - xa > 3 || yb - ya > 3) {
        const ids = doc.notes
          .filter((n) => {
            const r = noteRect(n);
            return r.x + r.w >= xa && r.x <= xb && r.y + r.h >= ya && r.y <= yb;
          })
          .map((n) => n.id);
        select(ids, drag.additive);
      } else if (!drag.additive) {
        select([]);
      }
    } else if (drag.kind === "move" && (drag.dt || drag.dp)) {
      const { dt, dp } = drag;
      const locks = [...(dt ? (["time"] as const) : []), ...(dp ? (["pitch"] as const) : [])];
      editNotes(
        drag.ids,
        (n) => {
          n.start = Math.max(0, n.start + dt);
          if (dp) {
            n.pitch = Math.min(108, Math.max(21, n.pitch + dp));
            if (!n.locked.includes("spelling")) n.spelling = null;
          }
        },
        locks,
      );
    } else if (drag.kind === "resize" && drag.dd) {
      const dd = drag.dd;
      editNotes(drag.ids, (n) => (n.dur = Math.max(grid / 2, n.dur + dd)), ["time"]);
    }
    setDrag(null);
  };

  const onDoubleClick = (e: React.MouseEvent) => {
    if (!doc) return;
    const { x, y } = local(e);
    if (y < RULER_H || x < KEY_W || hit(x, y)) return;
    const pitch = pitchAt(y);
    addNote({
      pitch,
      start: Math.max(0, Math.floor(tickAt(x) / grid) * grid),
      dur: grid,
      staff: pitch >= 60 ? 1 : 2,
      voice: 1,
      spelling: null,
      tie_next: false,
      velocity: 80,
      confidence: 1,
      src_ids: [],
      locked: ["pitch", "time", "staff"],
    });
  };

  const onWheel = (e: React.WheelEvent) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    setPxPerQuarter((z) => Math.min(400, Math.max(10, z * (e.deltaY < 0 ? 1.15 : 1 / 1.15))));
  };

  return (
    <div className="relative h-full">
      <div className="absolute right-3 top-1 z-10 flex items-center gap-1 rounded bg-white/90 px-1 text-xs shadow-sm">
        <button className="px-1" onClick={() => setPxPerQuarter((z) => Math.max(10, z / 1.25))} title="缩小">
          −
        </button>
        <span className="w-10 text-center tabular-nums">{Math.round(pxPerQuarter)}</span>
        <button className="px-1" onClick={() => setPxPerQuarter((z) => Math.min(400, z * 1.25))} title="放大">
          ＋
        </button>
      </div>
      <div
        ref={scroller}
        className="h-full overflow-auto"
        onScroll={(e) => {
          // read now: React nulls e.currentTarget after dispatch, and the updater may run later
          const { scrollLeft: x, scrollTop: y } = e.currentTarget;
          setView((v) => (v.x === x && v.y === y ? v : { ...v, x, y }));
        }}
        onWheel={onWheel}
      >
        <div style={{ width: totalW, height: totalH, position: "relative" }}>
          <canvas
            ref={canvas}
            data-testid="pianoroll"
            style={{ position: "sticky", left: 0, top: 0, cursor: drag ? "grabbing" : hoverCursor, display: "block" }}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onDoubleClick={onDoubleClick}
          />
        </div>
      </div>
    </div>
  );
}
