// Musical-time helpers mirroring backend ScoreDoc semantics (ticks at doc.ppq).
import type { MeterChange, ScoreDoc, ScoreNote, Spelling, Step, TempoMap } from "../api/types";

export function measureLen(m: MeterChange, ppq: number): number {
  return (ppq * 4 * m.num) / m.den;
}

export function isCompound(m: MeterChange): boolean {
  return m.den === 8 && m.num % 3 === 0 && m.num > 3;
}

export function beatTicks(m: MeterChange, ppq: number): number {
  return isCompound(m) ? (ppq * 3) / 2 : (ppq * 4) / m.den;
}

/** Ticks where each measure starts (measure 1 is the pickup when pickup_ticks > 0). */
export function barlines(doc: ScoreDoc, until: number): number[] {
  const meters = [...doc.meters].sort((a, b) => a.tick - b.tick);
  const out = [0];
  let t = doc.pickup_ticks || measureLen(meters[0], doc.ppq);
  while (out[out.length - 1] < until) {
    out.push(t);
    const cur = [...meters].reverse().find((m) => m.tick <= t) ?? meters[0];
    t += measureLen(cur, doc.ppq);
  }
  return out;
}

function interp(x: number, xs: number[], ys: number[]): number {
  const n = xs.length;
  if (n < 2) return ys[0] ?? 0;
  let i = 0;
  if (x <= xs[0]) i = 0;
  else if (x >= xs[n - 1]) i = n - 2;
  else {
    let lo = 0;
    let hi = n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (xs[mid] <= x) lo = mid;
      else hi = mid;
    }
    i = lo;
  }
  const [x0, x1, y0, y1] = [xs[i], xs[i + 1], ys[i], ys[i + 1]];
  return y0 + ((x - x0) * (y1 - y0)) / (x1 - x0);
}

export class TimeMap {
  private ticks: number[];
  private secs: number[];
  constructor(tm: TempoMap | null, bpm = 100, ppq = 960) {
    if (tm && tm.anchors.length >= 2) {
      this.ticks = tm.anchors.map((a) => a[0]);
      this.secs = tm.anchors.map((a) => a[1]);
    } else {
      this.ticks = [0, ppq];
      this.secs = [0, 60 / bpm];
    }
  }
  tickToSec(t: number): number {
    return interp(t, this.ticks, this.secs);
  }
  secToTick(s: number): number {
    return interp(s, this.secs, this.ticks);
  }
}

const STEP_PC: Record<Step, number> = { C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 };
const SHARP_NAMES = ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"];
const ACC = { [-2]: "𝄫", [-1]: "♭", 0: "", 1: "♯", 2: "𝄪" } as Record<number, string>;

export function pitchName(n: Pick<ScoreNote, "pitch" | "spelling">): string {
  if (n.spelling) return `${n.spelling.step}${ACC[n.spelling.alter]}${n.spelling.octave}`;
  return `${SHARP_NAMES[n.pitch % 12]}${Math.floor(n.pitch / 12) - 1}`;
}

/** All (step, alter) spellings of a MIDI pitch with |alter| ≤ 2. */
export function enharmonics(pitch: number): Spelling[] {
  const out: Spelling[] = [];
  for (const step of Object.keys(STEP_PC) as Step[]) {
    for (const alter of [-2, -1, 0, 1, 2]) {
      const pc = (STEP_PC[step] + alter + 12) % 12;
      if (pc !== pitch % 12) continue;
      const octave = Math.floor((pitch - STEP_PC[step] - alter) / 12) - 1;
      out.push({ step, alter, octave });
    }
  }
  return out.sort((a, b) => Math.abs(a.alter) - Math.abs(b.alter));
}

export function spellingLabel(s: Spelling): string {
  return `${s.step}${ACC[s.alter]}${s.octave}`;
}

/** "m:b" position label (1-based beat), for the inspector. */
export function positionLabel(doc: ScoreDoc, tick: number): string {
  const bars = barlines(doc, tick + 1);
  let mi = 0;
  for (let i = 0; i < bars.length; i++) if (bars[i] <= tick) mi = i;
  const meter = [...doc.meters].reverse().find((m) => m.tick <= tick) ?? doc.meters[0];
  const bt = beatTicks(meter, doc.ppq);
  const firstNo = doc.pickup_ticks ? 0 : 1;
  let rel = tick - bars[mi];
  if (mi === 0 && doc.pickup_ticks) rel += measureLen(meter, doc.ppq) - doc.pickup_ticks;
  const beat = rel / bt + 1;
  return `${mi + firstNo}:${Number.isInteger(beat) ? beat : beat.toFixed(2)}`;
}

export const KEY_NAMES: Record<number, [string, string]> = {
  [-7]: ["C♭", "a♭"], [-6]: ["G♭", "e♭"], [-5]: ["D♭", "b♭"], [-4]: ["A♭", "f"], [-3]: ["E♭", "c"],
  [-2]: ["B♭", "g"], [-1]: ["F", "d"], 0: ["C", "a"], 1: ["G", "e"], 2: ["D", "b"], 3: ["A", "f♯"],
  4: ["E", "c♯"], 5: ["B", "g♯"], 6: ["F♯", "d♯"], 7: ["C♯", "a♯"],
};
