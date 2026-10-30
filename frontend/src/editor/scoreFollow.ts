// Score following: geometry of the rendered OSMD score in musical time, and an
// incremental "which notes are sounding" tracker. Pure helpers, no React; ScoreView
// wires them to the playhead outside of React rendering.
import type { GraphicalNote, OpenSheetMusicDisplay } from "opensheetmusicdisplay";
import type { ScoreNote } from "../api/types";

/** One measure column of the rendered score (all staves), in OSMD units (×10×zoom = px). */
export interface MeasureGeom {
  tick0: number;
  tick1: number;
  x: number; // left edge of the measure's notes (after clef/key/time signatures)
  w: number;
  sys: number; // index into ScoreLayout.systems
  /** (tick, x) pairs of staff-entry positions across staves, ascending in both. */
  anchors: [number, number][];
}

export interface SystemGeom {
  top: number; // top line of the first staff
  bottom: number; // bottom line of the last staff
  inkTop: number; // highest notation above the first staff (ledger lines, beams), capped
}

export interface ScoreLayout {
  measures: MeasureGeom[];
  systems: SystemGeom[];
}

/** A rendered notehead group with the musical span it shows (tied notes have several). */
export interface NoteGlyph {
  gn: GraphicalNote;
  t0: number;
  t1: number;
}

const wholeToTicks = (realValue: number, ppq: number) => Math.round(realValue * 4 * ppq);

/**
 * Absolute tick span of a rendered note (OSMD timestamps are in whole notes). The first
 * note of a tie carries the whole tied Length, so use the drawn length, clipped to its bar.
 */
export function glyphSpan(gn: GraphicalNote, ppq: number): [number, number] {
  const n = gn.sourceNote;
  const m = n.SourceMeasure;
  const t0 = wholeToTicks(m.AbsoluteTimestamp.RealValue + n.ParentStaffEntry.Timestamp.RealValue, ppq);
  const len = wholeToTicks((gn.graphicalNoteLength ?? n.Length).RealValue, ppq);
  const barEnd = wholeToTicks(m.AbsoluteTimestamp.RealValue + m.Duration.RealValue, ppq);
  return [t0, Math.max(t0 + 1, Math.min(t0 + len, barEnd))];
}

export function buildLayout(osmd: OpenSheetMusicDisplay, ppq: number): ScoreLayout {
  const measures: MeasureGeom[] = [];
  const systems: SystemGeom[] = [];
  const sysIndex = new Map<unknown, number>();
  for (const column of osmd.GraphicSheet.MeasureList) {
    const gms = column.filter((g) => g && g.ParentMusicSystem);
    if (!gms.length) continue;
    const first = gms[0];
    const sm = first.parentSourceMeasure;
    const tick0 = wholeToTicks(sm.AbsoluteTimestamp.RealValue, ppq);
    const tick1 = tick0 + wholeToTicks(sm.Duration.RealValue, ppq);
    const ms = first.ParentMusicSystem;
    let sys = sysIndex.get(ms);
    if (sys === undefined) {
      const y = ms.PositionAndShape.AbsolutePosition.y;
      const lines = ms.StaffLines;
      const last = lines[lines.length - 1];
      const top = y + (lines[0]?.PositionAndShape.RelativePosition.y ?? 0);
      let sky = 0;
      for (const v of lines[0]?.SkyLine ?? []) if (Number.isFinite(v) && v < sky) sky = v;
      systems.push({
        top,
        bottom: y + (last ? last.PositionAndShape.RelativePosition.y + last.StaffHeight : 4),
        inkTop: top + Math.max(sky, -12),
      });
      sys = systems.length - 1;
      sysIndex.set(ms, sys);
    }
    const x = first.PositionAndShape.AbsolutePosition.x;
    const w = first.PositionAndShape.Size.width;
    // staff entries of all staves → earliest x per tick
    const byTick = new Map<number, number>();
    for (const gm of gms) {
      for (const se of gm.staffEntries) {
        const t = tick0 + wholeToTicks(se.relInMeasureTimestamp.RealValue, ppq);
        const sx = se.PositionAndShape.AbsolutePosition.x;
        byTick.set(t, Math.min(byTick.get(t) ?? Infinity, sx));
      }
    }
    // band starts after clef/key/time signatures, but never right of the first note
    const firstX = Math.min(...byTick.values());
    const contentLeft = Math.min(
      x + Math.max(...gms.map((g) => g.beginInstructionsWidth || 0)),
      Number.isFinite(firstX) ? firstX - 1.5 : Infinity,
    );
    if (!byTick.has(tick0)) byTick.set(tick0, contentLeft + 1);
    const anchors = [...byTick.entries()].sort((a, b) => a[0] - b[0]);
    anchors.push([tick1, x + w - 0.5]);
    // keep x monotonic (grace notes / cross-staff oddities)
    for (let i = 1; i < anchors.length; i++) if (anchors[i][1] < anchors[i - 1][1]) anchors[i][1] = anchors[i - 1][1];
    measures.push({ tick0, tick1, x: contentLeft, w: x + w - contentLeft, sys, anchors });
  }
  measures.sort((a, b) => a.tick0 - b.tick0);
  // a multi-measure rest is drawn as one measure standing for several bars: let it span
  // up to the next drawn measure so the band and cursor stay on it the whole time
  for (let i = 0; i + 1 < measures.length; i++) {
    const m = measures[i];
    const t1 = measures[i + 1].tick0;
    if (t1 > m.tick1) {
      m.tick1 = t1;
      m.anchors[m.anchors.length - 1][0] = t1;
    }
  }
  return { measures, systems };
}

/** Index of the measure containing tick (clamped to the first/last measure); -1 if none. */
export function measureAt(layout: ScoreLayout, tick: number): number {
  const ms = layout.measures;
  if (!ms.length) return -1;
  let lo = 0;
  let hi = ms.length - 1;
  if (tick <= ms[0].tick0) return 0;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ms[mid].tick0 <= tick) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

/** Cursor x (OSMD units) inside a measure, interpolated between staff entries. */
export function cursorX(m: MeasureGeom, tick: number): number {
  const a = m.anchors;
  if (tick <= a[0][0]) return a[0][1];
  for (let i = 1; i < a.length; i++) {
    if (tick < a[i][0]) {
      const [t0, x0] = a[i - 1];
      const [t1, x1] = a[i];
      return x0 + ((tick - t0) / (t1 - t0)) * (x1 - x0);
    }
  }
  return a[a.length - 1][1];
}

/**
 * Notes sounding at a tick ([start, start+dur) contains it). Forward steps are
 * incremental — O(notes started + active) — and jumps fall back to a bounded rescan.
 */
export class SoundingTracker {
  private notes: ScoreNote[];
  private maxDur: number;
  private next = 0; // index of the first note with start > last tick
  private last = -Infinity;
  readonly active = new Set<string>();
  private ends = new Map<string, number>();
  private jump: number;

  constructor(notes: readonly ScoreNote[], ppq = 960) {
    this.jump = 8 * ppq;
    this.notes = [...notes].sort((a, b) => a.start - b.start);
    this.maxDur = this.notes.reduce((m, n) => Math.max(m, n.dur), 0);
  }

  private upper(tick: number): number {
    let lo = 0;
    let hi = this.notes.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.notes[mid].start <= tick) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  /** Move to tick; returns true when the active set changed. */
  update(tick: number): boolean {
    let changed = false;
    if (tick >= this.last && tick - this.last < this.jump) {
      while (this.next < this.notes.length && this.notes[this.next].start <= tick) {
        const n = this.notes[this.next++];
        if (n.start + n.dur > tick) {
          this.active.add(n.id);
          this.ends.set(n.id, n.start + n.dur);
          changed = true;
        }
      }
      for (const [id, end] of this.ends) {
        if (end <= tick) {
          this.ends.delete(id);
          this.active.delete(id);
          changed = true;
        }
      }
    } else {
      const before = this.active.size;
      this.active.clear();
      this.ends.clear();
      this.next = this.upper(tick);
      for (let i = this.next - 1; i >= 0 && this.notes[i].start > tick - this.maxDur; i--) {
        const n = this.notes[i];
        if (n.start + n.dur > tick) {
          this.active.add(n.id);
          this.ends.set(n.id, n.start + n.dur);
        }
      }
      changed = before > 0 || this.active.size > 0;
    }
    this.last = tick;
    return changed;
  }

  reset() {
    this.last = -Infinity;
    this.active.clear();
    this.ends.clear();
    this.next = 0;
  }
}
