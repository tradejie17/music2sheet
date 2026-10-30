// Plays the ScoreDoc with a sampled piano (smplr SplendidGrandPiano) through Web Audio.
// The timeline unit is "position seconds": seconds of the original recording when the
// clock is the doc's TimeMap, or seconds of a steady-tempo rendition (SteadyClock).
// A small lookahead scheduler hands notes to the sampler ~150 ms before they sound,
// so pause/seek/rate changes/edits take effect almost immediately.
import { SplendidGrandPiano, type Smplr } from "smplr";
import type { ScoreDoc } from "../api/types";

export interface Clock {
  tickToSec(t: number): number;
  secToTick(s: number): number;
}

/** Score time at a constant tempo (what the MIDI export does), with a short lead-in. */
export class SteadyClock implements Clock {
  private spt: number;
  constructor(bpm: number, ppq: number, private lead = 0.3) {
    this.spt = 60 / (bpm * ppq);
  }
  tickToSec(t: number) {
    return this.lead + t * this.spt;
  }
  secToTick(s: number) {
    return (s - this.lead) / this.spt;
  }
}

export interface SynthEvent {
  t: number; // onset, position seconds
  end: number; // sounding end incl. sustain pedal, position seconds
  pitch: number;
  vel: number;
}

// Decoding every velocity layer of every key takes ~560 MB in the tab: load only the
// middle layers and the pitches the score uses (others fall back to the nearest key).
const VEL_RANGE: [number, number] = [40, 105];
const MAX_RING_S = 8; // samples decay anyway; bounds simultaneous voices under long pedals

/** ScoreDoc → sorted synth events. Ties merged, sustain pedal extends releases, re-strikes cut. */
export function buildEvents(doc: ScoreDoc, clock: Clock): SynthEvent[] {
  const notes = [...doc.notes].sort((a, b) => a.start - b.start || a.pitch - b.pitch);
  const sus = doc.pedals.filter((p) => p.kind === "sustain").sort((a, b) => a.start - b.start);
  const soft = doc.pedals.filter((p) => p.kind === "soft");
  const inSoft = (t: number) => soft.some((p) => p.start <= t && t < p.end);
  // pedal-up tick for a release at tick r (r itself if the pedal is up)
  const pedalUp = (r: number) => {
    let lo = 0;
    let hi = sus.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (sus[mid].start <= r) lo = mid + 1;
      else hi = mid;
    }
    const p = sus[lo - 1];
    return p && r < p.end ? p.end : r;
  };

  // merge explicit ties into one sounding note
  const merged: { start: number; end: number; pitch: number; vel: number }[] = [];
  const openTie = new Map<number, (typeof merged)[number]>(); // pitch → note waiting for its tie
  for (const n of notes) {
    const prev = openTie.get(n.pitch);
    if (prev && prev.end === n.start) {
      prev.end = n.start + n.dur;
      if (!n.tie_next) openTie.delete(n.pitch);
      continue;
    }
    const m = { start: n.start, end: n.start + n.dur, pitch: n.pitch, vel: n.velocity };
    merged.push(m);
    if (n.tie_next) openTie.set(n.pitch, m);
    else openTie.delete(n.pitch);
  }

  // next onset per pitch, to damp a pedalled note when the same key is struck again
  const nextSame = new Array<number>(merged.length).fill(Infinity);
  const last = new Map<number, number>();
  for (let i = merged.length - 1; i >= 0; i--) {
    const k = last.get(merged[i].pitch);
    if (k !== undefined) nextSame[i] = merged[k].start;
    last.set(merged[i].pitch, i);
  }

  const out = merged.map((m, i) => {
    const endTick = Math.min(pedalUp(m.end), Math.max(m.end, nextSame[i]));
    const t = clock.tickToSec(m.start);
    const end = Math.min(t + MAX_RING_S, Math.max(t + 0.05, clock.tickToSec(endTick)));
    const vel = Math.max(VEL_RANGE[0], Math.min(VEL_RANGE[1], Math.round(m.vel * (inSoft(m.start) ? 0.75 : 1))));
    return { t, end, pitch: m.pitch, vel };
  });
  // the tempo map is not guaranteed monotonic (see repair_gaps bug), so sort by time, not tick
  return out.sort((a, b) => a.t - b.t);
}

function lowerBound(ev: SynthEvent[], t: number): number {
  let lo = 0;
  let hi = ev.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (ev[mid].t < t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** setInterval in a Worker: not throttled to 1 Hz when the tab is in the background. */
function workerTicker(ms: number, cb: () => void): () => void {
  try {
    const src = `setInterval(() => postMessage(0), ${ms});`;
    const url = URL.createObjectURL(new Blob([src], { type: "text/javascript" }));
    const w = new Worker(url);
    w.onmessage = cb;
    return () => {
      w.terminate();
      URL.revokeObjectURL(url);
    };
  } catch {
    const id = setInterval(cb, ms);
    return () => clearInterval(id);
  }
}

const LOOKAHEAD_S = 0.15; // < smplr's own 200 ms lookahead, so starts dispatch immediately
const TICK_MS = 25;
// served (and cached on first use) by the backend: offline after the first load
export const PIANO_BASE_URL = "/api/assets/piano";


export class ScorePlayer {
  readonly ctx: AudioContext;
  readonly bus: GainNode; // synth volume
  private piano: Smplr;
  readonly ready: Promise<void>;
  loadProgress = { loaded: 0, total: 0 };
  private events: SynthEvent[] = [];
  private key: unknown = null;
  private clockRef: Clock | null = null;
  private cursor = 0;
  private anchorCtx = 0; // ctx time at which the timeline was at anchorPos
  private anchorPos = 0;
  private rate = 1;
  private running = false;
  private scheduledTo = 0; // position up to which events have been handed to the sampler
  private stopTicker: (() => void) | null = null;
  stats = { started: 0, dropped: 0, resyncs: 0 };

  constructor(ctx: AudioContext, pitches?: number[]) {
    this.ctx = ctx;
    this.bus = ctx.createGain();
    this.bus.connect(ctx.destination);
    this.piano = SplendidGrandPiano(ctx, {
      baseUrl: PIANO_BASE_URL,
      destination: this.bus,
      decayTime: 0.4,
      onLoadProgress: (p) => (this.loadProgress = p),
      notesToLoad: { notes: pitches, velocityRange: VEL_RANGE, fallback: "nearest" },
    });
    this.ready = this.piano.ready;
  }

  /** (Re)build the event list; cheap enough (~3 ms for 2.5k notes) to call on every edit. */
  setScore(doc: ScoreDoc, clock: Clock) {
    if (this.key === doc && this.clockRef === clock) return;
    this.key = doc;
    this.clockRef = clock;
    this.events = buildEvents(doc, clock);
    this.cursor = lowerBound(this.events, this.scheduledTo);
  }

  get isRunning() {
    return this.running;
  }

  position(): number {
    if (!this.running) return this.anchorPos;
    return this.anchorPos + Math.max(0, this.ctx.currentTime - this.anchorCtx) * this.rate;
  }

  start(pos: number, rate: number, at = this.ctx.currentTime + 0.03) {
    this.silence();
    this.anchorCtx = at;
    this.anchorPos = pos;
    this.rate = rate;
    this.running = true;
    this.scheduledTo = pos;
    this.cursor = lowerBound(this.events, pos);
    this.stopTicker ??= workerTicker(TICK_MS, () => this.pump());
    this.pump();
  }

  stop() {
    if (!this.running) return;
    this.anchorPos = this.position();
    this.running = false;
    this.stopTicker?.();
    this.stopTicker = null;
    this.silence();
  }

  /** Set the position while stopped. */
  cue(pos: number) {
    if (this.running) this.start(pos, this.rate);
    else this.anchorPos = pos;
  }

  get lastEnd(): number {
    return this.events.reduce((m, e) => Math.max(m, e.end), 0);
  }

  /** Move the clock without cutting sounding notes (rate change, drift correction). */
  reanchor(pos: number, rate = this.rate) {
    const now = this.ctx.currentTime;
    this.anchorCtx = now;
    this.anchorPos = pos;
    this.rate = rate;
    this.stats.resyncs++;
  }

  setRate(rate: number) {
    if (this.running) this.reanchor(this.position(), rate);
    else this.rate = rate;
  }

  dispose() {
    this.stop();
    this.piano.dispose();
    this.bus.disconnect();
  }

  private silence() {
    this.piano.stop();
    this.piano.scheduler.stop();
  }

  private pump() {
    if (!this.running) return;
    const now = this.ctx.currentTime;
    const horizon = this.anchorPos + (now + LOOKAHEAD_S - this.anchorCtx) * this.rate;
    const ev = this.events;
    while (this.cursor < ev.length && ev[this.cursor].t < horizon) {
      const e = ev[this.cursor++];
      const when = this.anchorCtx + (e.t - this.anchorPos) / this.rate;
      if (when < now - 0.03) {
        this.stats.dropped++; // late (tab frozen, or re-anchored forward): skip, don't burst
        continue;
      }
      this.piano.start({
        note: e.pitch,
        velocity: e.vel,
        time: Math.max(when, now),
        duration: (e.end - e.t) / this.rate,
      });
      this.stats.started++;
    }
    this.scheduledTo = Math.max(this.scheduledTo, horizon);
  }
}
