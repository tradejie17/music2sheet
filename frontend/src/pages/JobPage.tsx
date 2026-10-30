import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { STATUS } from "../components/JobList";
import { EditorLayout } from "../editor/EditorLayout";

const STAGES: [string, string][] = [
  ["download", "下载"],
  ["ingest", "解码音频"],
  ["transcribe", "钢琴转录"],
  ["beats", "节拍追踪"],
  ["quantize", "节奏量化"],
  ["arrange", "分手与记谱"],
  ["render", "排版"],
];

export function JobPage() {
  const { id = "" } = useParams();
  const { data: job, error } = useQuery({
    queryKey: ["job", id],
    queryFn: () => api.getJob(id),
    refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 1000 : false),
  });

  if (error) return <div className="p-6 text-red-600">任务不存在。</div>;
  if (!job) return <div className="p-6 text-slate-500">加载中…</div>;

  const header = (
    <header className="flex items-center gap-3 border-b bg-white px-4 py-2">
      <Link to="/" className="text-sm text-slate-500 hover:text-blue-600">
        ← music2sheet
      </Link>
      <h1 className="truncate font-medium">{job.title}</h1>
      <span className={`rounded px-1.5 py-0.5 text-xs ${STATUS[job.status][1]}`}>{STATUS[job.status][0]}</span>
    </header>
  );

  if (job.status === "done" && job.doc_rev !== null) {
    return (
      <div className="flex h-screen flex-col">
        {header}
        <div className="min-h-0 flex-1">
          <EditorLayout job={job} />
        </div>
      </div>
    );
  }

  const current = STAGES.findIndex(([k]) => k === job.stage);
  return (
    <div className="flex h-screen flex-col">
      {header}
      <div className="mx-auto mt-16 w-[480px] rounded-xl border bg-white p-6 shadow-sm">
        {job.status === "error" ? (
          <>
            <h2 className="mb-2 font-medium text-red-700">处理失败</h2>
            <p className="whitespace-pre-wrap text-sm text-slate-700">{job.error}</p>
            <Link to="/" className="mt-4 inline-block text-sm text-blue-600">
              返回重新上传
            </Link>
          </>
        ) : job.status === "cancelled" ? (
          <p className="text-sm text-slate-600">任务已取消。</p>
        ) : (
          <>
            <div className="mb-2 flex justify-between text-sm">
              <span>{job.status === "queued" ? "排队中…" : (STAGES[current]?.[1] ?? "处理中") + "…"}</span>
              <span className="tabular-nums text-slate-500">{Math.round(job.progress * 100)}%</span>
            </div>
            <div className="h-2 overflow-hidden rounded bg-slate-100">
              <div className="h-full bg-blue-600 transition-all" style={{ width: `${job.progress * 100}%` }} />
            </div>
            <ol className="mt-4 space-y-1 text-xs">
              {STAGES.map(([k, label], i) => (
                <li key={k} className={i < current ? "text-emerald-600" : i === current ? "font-medium text-blue-700" : "text-slate-400"}>
                  {i < current ? "✓" : i === current ? "●" : "○"} {label}
                </li>
              ))}
            </ol>
            {job.message && <p className="mt-3 text-xs text-slate-500">{job.message}</p>}
            <button className="mt-4 text-xs text-slate-500 hover:text-red-600" onClick={() => api.cancelJob(job.id)}>
              取消任务
            </button>
          </>
        )}
      </div>
    </div>
  );
}
