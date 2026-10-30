import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useStore } from "zustand";
import { api, urls } from "../api/client";
import { SIMPLIFY_LABELS, type Job, type Priors, type ScoreDoc, type SimplifyLevel } from "../api/types";
import { KEY_NAMES, beatTicks } from "./music";
import { redo, undo, useEditor } from "./store";

const SAVE_LABEL = {
  saved: ["已保存", "text-emerald-600"],
  dirty: ["未保存…", "text-slate-500"],
  saving: ["保存中…", "text-slate-500"],
  error: ["保存失败", "text-red-600"],
  conflict: ["版本冲突，请刷新", "text-red-600"],
} as const;

/** Tempo of the tracked beat (what Priors.bpm sets): the beat DP's level, else the
 * median beat interval of the tempo map. */
export function beatBpm(doc: ScoreDoc): number | null {
  const v = Number(doc.provenance.versions?.tempo_level_bpm);
  if (v > 0) return v;
  const a = doc.tempo_map?.anchors ?? [];
  if (a.length < 3) {
    const q = doc.tempos[0]?.bpm; // quarter notes per minute
    return q ? (q * doc.ppq) / beatTicks(doc.meters[0], doc.ppq) : null;
  }
  const d = a
    .slice(1)
    .map((x, i) => x[1] - a[i][1])
    .sort((p, q) => p - q);
  return 60 / d[d.length >> 1];
}

const BPM_MAX = 400; // Priors.bpm upper bound (exclusive)
/** ÷2 is offered only while the halved beat stays at or above this: slower beats turn a
 * ballad's sixteenths into 64ths (Call of Silence 65 → 33 was unreadable, ADR 0006). */
const HALF_MIN_BPM = 40;

/** Tooltip part for a tempo level that came from Priors.bpm: the DP treats the prior as a
 * target and settles on the nearest tempo the music supports, which may differ. */
function priorNote(prior: string | undefined, bpm: number | null): string {
  const p = Number(prior);
  if (!(p > 0) || !bpm || Math.abs(Math.log(bpm / p)) < 0.08) return "，按先验参数";
  return `，按先验参数（输入 ${p}，音乐上最接近的拍速为 ${Math.round(bpm)}）`;
}

/** Note glyph of the beat unit, for the tempo label (♩. in 6/8, 𝅗𝅥 in 2/2). */
function beatGlyph(bt: number, ppq: number): string {
  if (bt === (ppq * 3) / 2) return "♩.";
  if (bt === ppq * 2) return "𝅗𝅥";
  if (bt === ppq / 2) return "♪";
  return "♩";
}

export function Toolbar({ job, onRerun }: { job: Job; onRerun: () => void }) {
  const doc = useEditor((s) => s.doc);
  const saveState = useEditor((s) => s.saveState);
  const savedRev = useEditor((s) => s.savedRev);
  const grid = useEditor((s) => s.gridTicks);
  const pastLen = useStore(useEditor.temporal, (s) => s.pastStates.length);
  const futureLen = useStore(useEditor.temporal, (s) => s.futureStates.length);
  const { setGrid, edit, load } = useEditor.getState();
  const [busy, setBusy] = useState("");
  const qc = useQueryClient();
  if (!doc) return null;

  const level: SimplifyLevel = job.params.simplify ?? 0;
  const bpm = beatBpm(doc);
  const versions = doc.provenance.versions ?? {};
  const conf = versions.tempo_level_confidence;
  const edited = versions.edited === "1" || pastLen > 0 || futureLen > 0;

  /** Re-run S3–S6 with the job's priors changed by `patch`; the job page shows progress
   * and loads the new revision when it is done. */
  const rerunWith = async (patch: Partial<Priors>, what: string) => {
    if (saveState !== "saved") {
      alert("请等待保存完成后再操作");
      return false;
    }
    if (
      edited &&
      !window.confirm(
        `${what}会用缓存的转录与节拍重新生成乐谱（几秒钟）。\n你的手动修改会保留在历史版本中，但新版本从头生成，不包含这些修改。\n继续吗？`,
      )
    )
      return false;
    setBusy("rerun");
    try {
      await api.rerunJob(job.id, { ...job.params, ...patch });
      await qc.invalidateQueries({ queryKey: ["job", job.id] });
      return true;
    } catch (e) {
      alert(`重新生成失败：${e instanceof Error ? e.message : e}`);
      return false;
    } finally {
      setBusy("");
    }
  };
  const setTempo = (factor: number) => {
    if (!bpm) return;
    const next = Math.round(bpm * factor * 10) / 10;
    void rerunWith({ bpm: next }, factor > 1 ? "速度 ×2" : "速度 ÷2");
  };

  const ppq = doc.ppq;
  const meter = doc.meters[0];
  const bt = beatTicks(meter, ppq);
  const key = doc.keys[0];

  const doRederive = async (what: "hands" | "voices" | "spelling" | "key") => {
    if (saveState !== "saved") {
      alert("请等待保存完成后再操作");
      return;
    }
    setBusy(what);
    try {
      const d = await api.rederive(job.id, what);
      load(job.id, d);
    } finally {
      setBusy("");
    }
  };

  const [label, cls] = SAVE_LABEL[saveState];
  const btn = "rounded border bg-white px-2 py-1 hover:bg-slate-50 disabled:opacity-40";

  return (
    <div className="flex flex-wrap items-center gap-2 border-b bg-white px-3 py-2 text-xs">
      <button className={btn} onClick={undo} disabled={!pastLen} title="⌘Z">
        ↶ 撤销
      </button>
      <button className={btn} onClick={redo} disabled={!futureLen} title="⇧⌘Z">
        ↷ 重做
      </button>
      <span className="mx-1 h-5 w-px bg-slate-200" />

      <label className="flex items-center gap-1">
        网格
        <select className="rounded border px-1 py-0.5" value={grid} onChange={(e) => setGrid(Number(e.target.value))}>
          <option value={ppq}>1/4</option>
          <option value={ppq / 2}>1/8</option>
          <option value={ppq / 4}>1/16</option>
          <option value={ppq / 3}>1/8 三连音</option>
          <option value={ppq / 6}>1/16 三连音</option>
        </select>
      </label>

      <label className="flex items-center gap-1">
        拍号
        <select
          className="rounded border px-1 py-0.5"
          value={`${meter.num}/${meter.den}`}
          onChange={(e) => {
            const [num, den] = e.target.value.split("/").map(Number);
            edit((d) => {
              d.meters = [{ tick: 0, num, den }];
            });
          }}
        >
          {["2/4", "3/4", "4/4", "2/2", "3/8", "6/8", "9/8", "12/8", "5/4"].map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
      </label>

      <label className="flex items-center gap-1" title="第一小节（弱起）的长度">
        弱起
        <select
          className="rounded border px-1 py-0.5"
          value={doc.pickup_ticks}
          onChange={(e) =>
            edit((d) => {
              d.pickup_ticks = Number(e.target.value);
            })
          }
        >
          <option value={0}>无</option>
          {Array.from({ length: Math.round((ppq * 4 * meter.num) / meter.den / bt) * 2 - 1 }, (_, i) => ((i + 1) * bt) / 2).map(
            (t) => (
              <option key={t} value={t}>
                {t / bt} 拍
              </option>
            ),
          )}
        </select>
      </label>

      <label className="flex items-center gap-1">
        调号
        <select
          className="rounded border px-1 py-0.5"
          value={`${key.fifths}|${key.mode}`}
          onChange={(e) => {
            const [f, mode] = e.target.value.split("|");
            edit((d) => {
              d.keys = [{ tick: 0, fifths: Number(f), mode: mode as "major" | "minor" }];
            });
          }}
        >
          {Object.entries(KEY_NAMES)
            .sort((a, b) => Number(a[0]) - Number(b[0]))
            .flatMap(([f, [maj, min]]) => [
              <option key={`${f}M`} value={`${f}|major`}>
                {maj} 大调
              </option>,
              <option key={`${f}m`} value={`${f}|minor`}>
                {min} 小调
              </option>,
            ])}
        </select>
      </label>
      <span className="mx-1 h-5 w-px bg-slate-200" />

      <button className={btn} disabled={!!busy} onClick={() => doRederive("hands")} title="保留已锁定的音符">
        {busy === "hands" ? "…" : "重新分手"}
      </button>
      <button className={btn} disabled={!!busy} onClick={() => doRederive("spelling")}>
        {busy === "spelling" ? "…" : "重新拼写"}
      </button>
      <button className={btn} disabled={!!busy} onClick={() => doRederive("key")}>
        {busy === "key" ? "…" : "重测调号"}
      </button>
      <button className={btn} onClick={onRerun} title="修改先验参数后重新量化（转录结果会复用缓存）">
        重新生成…
      </button>
      <span className="mx-1 h-5 w-px bg-slate-200" />

      <label className="flex items-center gap-1" title={SIMPLIFY_LABELS[level][1]}>
        简化
        <select
          className="rounded border px-1 py-0.5"
          data-testid="simplify-select"
          disabled={!!busy}
          value={level}
          onChange={(e) => {
            const v = Number(e.target.value) as SimplifyLevel;
            void rerunWith({ simplify: v }, `切换到「${SIMPLIFY_LABELS[v][0]}」`).then((ok) => {
              if (!ok) e.target.value = String(level);
            });
          }}
        >
          {([0, 1, 2] as SimplifyLevel[]).map((l) => (
            <option key={l} value={l} title={SIMPLIFY_LABELS[l][1]}>
              {SIMPLIFY_LABELS[l][0]}
            </option>
          ))}
        </select>
      </label>
      <span
        className="tabular-nums text-slate-600"
        data-testid="tempo-level"
        title={`节拍速度（每分钟拍数）${
          versions.tempo_level_source === "prior"
            ? priorNote(versions.tempo_level_prior, bpm)
            : conf
              ? `，自动检测，置信度 ${conf}`
              : ""
        }。倍速/半速听起来一样，选读起来更顺的那个。`}
      >
        {beatGlyph(bt, ppq)}≈{bpm ? Math.round(bpm) : "?"}
      </span>
      <button
        className={btn}
        data-testid="tempo-x2"
        disabled={!!busy || !bpm || bpm * 2 >= BPM_MAX}
        onClick={() => setTempo(2)}
        title="按两倍速度重新划分节拍：音符时值加倍（如十六分→八分），小节数加倍"
      >
        速度 ×2
      </button>
      <button
        className={btn}
        data-testid="tempo-half"
        disabled={!!busy || !bpm || bpm / 2 < HALF_MIN_BPM}
        onClick={() => setTempo(0.5)}
        title={
          bpm && bpm / 2 < HALF_MIN_BPM
            ? `当前每分钟 ${Math.round(bpm)} 拍，减半后低于 ${HALF_MIN_BPM}，音符会细到六十四分音符，不再提供`
            : "按一半速度重新划分节拍：音符时值减半（如八分→十六分），小节数减半"
        }
      >
        速度 ÷2
      </button>
      <span className="mx-1 h-5 w-px bg-slate-200" />

      <a className={btn} href={urls.pdf(job.id, savedRev)}>
        PDF
      </a>
      <a className={btn} href={urls.musicxml(job.id, savedRev)}>
        MusicXML
      </a>
      <a className={btn} href={urls.midi(job.id, savedRev, "quantized")}>
        MIDI（量化）
      </a>
      <a className={btn} href={urls.midi(job.id, savedRev, "raw")}>
        MIDI（原始）
      </a>
      <span className={`ml-auto ${cls}`} data-testid="save-state">
        {label} · r{savedRev}
      </span>
    </div>
  );
}
