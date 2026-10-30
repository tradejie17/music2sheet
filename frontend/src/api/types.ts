// Mirrors backend/src/m2s/models/*.py and the jobs table.

export type JobStatus = "queued" | "running" | "done" | "error" | "cancelled";

export interface Priors {
  key: string | null;
  bpm: number | null;
  time_sig: string | null;
  min_grid: "1/8" | "1/16" | "1/32";
  triplets: boolean;
  style: "pop" | "classical";
  engine: "transkun";
  /** 0 原样 (faithful) · 1 易读 (readable) · 2 简化 (simplified arrangement); absent on old jobs = 0 */
  simplify?: SimplifyLevel;
}

export type SimplifyLevel = 0 | 1 | 2;

export const SIMPLIFY_LABELS: Record<SimplifyLevel, [string, string]> = {
  0: ["原样", "转录到的每个音、每个踏板都照写"],
  1: ["易读", "保留所有旋律与和声音，去掉装饰音和重复的和弦音；一行一个声部、少连音线、不用六连音；踏板只标一次，高音区用 8va"],
  2: ["简化", "右手只留旋律（最多加一个和声音），左手每个和声换成一个柱式和弦，节奏网格更粗、无连音"],
};

// New jobs default to the readable score (docs/adr/0007-simplify.md); the backend's
// model default stays 0 because jobs created before the option existed were faithful.
export const DEFAULT_PRIORS: Priors = {
  key: null,
  bpm: null,
  time_sig: null,
  min_grid: "1/16",
  triplets: true,
  style: "classical",
  engine: "transkun",
  simplify: 1,
};

export interface Job {
  id: string;
  title: string;
  source: string;
  status: JobStatus;
  stage: string;
  progress: number;
  message: string;
  params: Priors;
  audio_sha: string | null;
  duration_s: number | null;
  doc_rev: number | null;
  error: string | null;
  created_at: number;
  updated_at: number;
}

export type Lockable = "pitch" | "time" | "staff" | "voice" | "spelling";
export type Step = "C" | "D" | "E" | "F" | "G" | "A" | "B";

export interface Spelling {
  step: Step;
  alter: number;
  octave: number;
}

export interface ScoreNote {
  id: string;
  pitch: number;
  start: number;
  dur: number;
  staff: 1 | 2;
  voice: number;
  spelling: Spelling | null;
  tie_next: boolean;
  velocity: number;
  confidence: number;
  src_ids: string[];
  locked: Lockable[];
}

export interface ScorePedal {
  id: string;
  kind: "sustain" | "soft" | "sostenuto";
  start: number;
  end: number;
  locked: boolean;
}

export interface MeterChange {
  tick: number;
  num: number;
  den: number;
}

export interface KeyChange {
  tick: number;
  fifths: number;
  mode: "major" | "minor";
}

export interface TempoMap {
  anchors: [number, number][];
}

export interface ScoreDoc {
  schema_version: number;
  revision: number;
  ppq: number;
  title: string;
  pickup_ticks: number;
  meters: MeterChange[];
  keys: KeyChange[];
  tempos: { tick: number; bpm: number; text: string | null }[];
  clefs: { tick: number; staff: 1 | 2; clef: string }[];
  notes: ScoreNote[];
  pedals: ScorePedal[];
  tempo_map: TempoMap | null;
  engraving?: { pedal_marks: boolean; ottava: boolean; whole_beats: boolean };
  provenance: { audio_sha?: string | null; versions?: Record<string, string>; [k: string]: unknown };
}

export interface NoteEvent {
  id: string;
  pitch: number;
  onset_s: number;
  offset_s: number;
  velocity: number;
}

export interface Transcription {
  duration_s: number;
  notes: NoteEvent[];
}
