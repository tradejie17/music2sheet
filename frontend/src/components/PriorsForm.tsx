import { SIMPLIFY_LABELS, type Priors, type SimplifyLevel } from "../api/types";

const KEYS = [
  "C major", "G major", "D major", "A major", "E major", "B major", "F# major", "C# major",
  "F major", "Bb major", "Eb major", "Ab major", "Db major", "Gb major", "Cb major",
  "a minor", "e minor", "b minor", "f# minor", "c# minor", "g# minor", "d# minor",
  "d minor", "g minor", "c minor", "f minor", "bb minor", "eb minor",
];

/** Optional hints. Leaving a field empty means "detect automatically". */
export function PriorsForm({ value, onChange }: { value: Priors; onChange: (p: Priors) => void }) {
  const set = <K extends keyof Priors>(k: K, v: Priors[K]) => onChange({ ...value, [k]: v });
  const field = "mt-0.5 block w-full rounded border px-2 py-1 text-sm";
  return (
    <div className="grid grid-cols-2 gap-3 text-xs text-slate-600 sm:grid-cols-3">
      <label>
        调号
        <select className={field} value={value.key ?? ""} onChange={(e) => set("key", e.target.value || null)}>
          <option value="">自动检测</option>
          {KEYS.map((k) => (
            <option key={k}>{k}</option>
          ))}
        </select>
      </label>
      <label>
        速度 BPM
        <input
          className={field}
          type="number"
          min={30}
          max={300}
          placeholder="自动检测"
          value={value.bpm ?? ""}
          onChange={(e) => set("bpm", e.target.value ? Number(e.target.value) : null)}
        />
      </label>
      <label>
        拍号
        <select className={field} value={value.time_sig ?? ""} onChange={(e) => set("time_sig", e.target.value || null)}>
          <option value="">自动检测</option>
          {["2/4", "3/4", "4/4", "2/2", "6/8", "9/8", "12/8"].map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
      </label>
      <label>
        最小量化粒度
        <select className={field} value={value.min_grid} onChange={(e) => set("min_grid", e.target.value as Priors["min_grid"])}>
          <option value="1/8">八分音符</option>
          <option value="1/16">十六分音符</option>
          <option value="1/32">三十二分音符</option>
        </select>
      </label>
      <label>
        风格
        <select className={field} value={value.style} onChange={(e) => set("style", e.target.value as Priors["style"])}>
          <option value="classical">古典</option>
          <option value="pop">流行</option>
        </select>
      </label>
      <label className="flex items-end gap-2 pb-1.5">
        <input type="checkbox" checked={value.triplets} onChange={(e) => set("triplets", e.target.checked)} />
        检测三连音
      </label>
      <label className="col-span-2 sm:col-span-3">
        乐谱简化
        <select
          className={field}
          data-testid="priors-simplify"
          value={value.simplify ?? 0}
          onChange={(e) => set("simplify", Number(e.target.value) as SimplifyLevel)}
        >
          {([0, 1, 2] as SimplifyLevel[]).map((l) => (
            <option key={l} value={l}>
              {SIMPLIFY_LABELS[l][0]}：{SIMPLIFY_LABELS[l][1]}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}
