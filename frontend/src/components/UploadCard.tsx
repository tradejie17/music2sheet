import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api } from "../api/client";
import { DEFAULT_PRIORS, type Priors } from "../api/types";
import { PriorsForm } from "./PriorsForm";

export function UploadCard() {
  const nav = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [priors, setPriors] = useState<Priors>(DEFAULT_PRIORS);
  const [showPriors, setShowPriors] = useState(false);
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    setBusy(true);
    setErr("");
    try {
      const job = await api.createJob({ file: file ?? undefined, youtubeUrl: file ? undefined : url, priors });
      nav(`/jobs/${job.id}`);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    <div className="rounded-xl border bg-white p-5 shadow-sm">
      <h2 className="mb-1 font-medium">新建转谱</h2>
      <p className="mb-4 text-xs text-slate-500">阶段 0 仅支持钢琴独奏录音。AI 生成初稿，再在编辑器里校对。</p>
      <label
        className={`flex h-28 cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed text-sm ${
          drag ? "border-blue-500 bg-blue-50" : "border-slate-300 text-slate-500"
        }`}
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
        }}
      >
        <input
          type="file"
          accept="audio/*,.wav,.mp3,.flac,.m4a,.ogg,.aiff"
          className="hidden"
          data-testid="file-input"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        {file ? <span className="font-medium text-slate-800">{file.name}</span> : "拖入音频文件，或点击选择（≤ 15 分钟）"}
      </label>
      <div className="my-3 flex items-center gap-2 text-xs text-slate-400">
        <span className="h-px flex-1 bg-slate-200" />或<span className="h-px flex-1 bg-slate-200" />
      </div>
      <input
        className="w-full rounded border px-3 py-1.5 text-sm disabled:bg-slate-50"
        placeholder="YouTube 链接（可选；失败时请改为上传文件）"
        value={url}
        disabled={!!file}
        onChange={(e) => setUrl(e.target.value)}
      />
      <button className="mt-3 text-xs text-blue-600" onClick={() => setShowPriors((s) => !s)}>
        {showPriors ? "▾" : "▸"} 先验参数（可选，填写更准确）
      </button>
      {showPriors && (
        <div className="mt-2 rounded border bg-slate-50 p-3">
          <PriorsForm value={priors} onChange={setPriors} />
        </div>
      )}
      {err && <p className="mt-3 text-sm text-red-600">{err}</p>}
      <button
        className="mt-4 w-full rounded-lg bg-blue-600 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40"
        disabled={busy || (!file && !url.trim())}
        onClick={submit}
      >
        {busy ? "提交中…" : "开始转谱"}
      </button>
    </div>
  );
}
