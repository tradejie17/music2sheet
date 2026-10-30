import type { Lockable, ScoreNote } from "../api/types";
import { enharmonics, pitchName, positionLabel, spellingLabel } from "./music";
import { useEditor } from "./store";

const LOCK_LABELS: Record<Lockable, string> = {
  pitch: "音高",
  time: "时值",
  staff: "谱表",
  voice: "声部",
  spelling: "拼写",
};

export function Inspector() {
  const doc = useEditor((s) => s.doc);
  const selected = useEditor((s) => s.selected);
  const { editNotes, deleteNotes } = useEditor.getState();
  if (!doc) return null;
  const notes = doc.notes.filter((n) => selected.includes(n.id));

  if (notes.length === 0) {
    const low = doc.notes.filter((n) => n.confidence < 0.5).length;
    return (
      <div className="space-y-2 p-3 text-xs text-slate-500">
        <p>点击乐谱或卷帘中的音符进行编辑。</p>
        <p>
          {doc.notes.length} 个音符，其中 <span className="font-medium text-orange-600">{low}</span> 个低置信度（橙色）。
        </p>
        <p>
          分手模型：
          {doc.provenance.versions?.hands === "piano_svsep"
            ? "piano_svsep（GNN）"
            : doc.provenance.versions?.hands === "heuristic"
              ? "启发式（piano_svsep 未安装或失败）"
              : "未记录（旧版本生成）"}
        </p>
        <ul className="list-disc space-y-0.5 pl-4">
          <li>拖动：移动（按住 Alt 不吸附）</li>
          <li>拖右边缘：改时值；双击空白：添加</li>
          <li>Del 删除 · S 切换谱表 · ↑↓ 半音 · ⇧↑↓ 八度</li>
          <li>←→ 按网格移动 · ⌘Z / ⇧⌘Z 撤销/重做</li>
        </ul>
      </div>
    );
  }

  const one: ScoreNote | null = notes.length === 1 ? notes[0] : null;
  const ids = notes.map((n) => n.id);
  const allStaff = notes.every((n) => n.staff === notes[0].staff) ? notes[0].staff : null;
  const allVoice = notes.every((n) => n.voice === notes[0].voice) ? notes[0].voice : null;

  return (
    <div className="space-y-3 p-3 text-sm">
      <div className="font-medium">
        {one ? pitchName(one) : `已选 ${notes.length} 个音符`}
        {one && <span className="ml-2 text-xs text-slate-500">位置 {positionLabel(doc, one.start)}</span>}
      </div>
      {one && (
        <div className="grid grid-cols-2 gap-x-2 text-xs text-slate-600">
          <span>MIDI {one.pitch}</span>
          <span>时值 {(one.dur / doc.ppq).toFixed(3).replace(/\.?0+$/, "")} 拍</span>
          <span>力度 {one.velocity}</span>
          <span className={one.confidence < 0.5 ? "text-orange-600" : ""}>置信度 {one.confidence.toFixed(2)}</span>
        </div>
      )}

      <label className="block text-xs">
        谱表
        <select
          className="mt-0.5 block w-full rounded border px-1 py-0.5"
          value={allStaff ?? ""}
          onChange={(e) => editNotes(ids, (n) => (n.staff = Number(e.target.value) as 1 | 2), ["staff"])}
        >
          {allStaff === null && <option value="">（混合）</option>}
          <option value={1}>高音谱表（右手）</option>
          <option value={2}>低音谱表（左手）</option>
        </select>
      </label>

      <label className="block text-xs">
        声部
        <select
          className="mt-0.5 block w-full rounded border px-1 py-0.5"
          value={allVoice ?? ""}
          onChange={(e) => editNotes(ids, (n) => (n.voice = Number(e.target.value)), ["voice"])}
        >
          {allVoice === null && <option value="">（混合）</option>}
          <option value={1}>声部 1（符干向上）</option>
          <option value={2}>声部 2（符干向下）</option>
        </select>
      </label>

      {one && (
        <label className="block text-xs">
          音名拼写
          <select
            className="mt-0.5 block w-full rounded border px-1 py-0.5"
            value={one.spelling ? spellingLabel(one.spelling) : "auto"}
            onChange={(e) => {
              const v = e.target.value;
              const sp = enharmonics(one.pitch).find((s) => spellingLabel(s) === v) ?? null;
              editNotes([one.id], (n) => (n.spelling = sp), sp ? ["spelling"] : []);
            }}
          >
            <option value="auto">自动（按调号）</option>
            {enharmonics(one.pitch).map((s) => (
              <option key={spellingLabel(s)} value={spellingLabel(s)}>
                {spellingLabel(s)}
              </option>
            ))}
          </select>
        </label>
      )}

      {one && (
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={one.tie_next}
            onChange={(e) => editNotes([one.id], (n) => (n.tie_next = e.target.checked))}
          />
          连到下一个同音高音符（延音线）
        </label>
      )}

      <div className="text-xs">
        <div className="mb-1 text-slate-500">锁定（重新推导时保留）</div>
        <div className="flex flex-wrap gap-1">
          {(Object.keys(LOCK_LABELS) as Lockable[]).map((l) => {
            const on = notes.every((n) => n.locked.includes(l));
            return (
              <button
                key={l}
                className={`rounded border px-1.5 py-0.5 ${on ? "border-blue-600 bg-blue-50 text-blue-700" : "text-slate-500"}`}
                onClick={() =>
                  editNotes(ids, (n) => {
                    n.locked = on ? n.locked.filter((x) => x !== l) : Array.from(new Set([...n.locked, l]));
                  })
                }
              >
                {on ? "🔒" : "🔓"} {LOCK_LABELS[l]}
              </button>
            );
          })}
        </div>
      </div>

      <button className="w-full rounded bg-red-50 py-1 text-xs text-red-700 hover:bg-red-100" onClick={() => deleteNotes(ids)}>
        删除所选（Del）
      </button>
    </div>
  );
}
