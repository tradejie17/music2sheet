import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { JobStatus } from "../api/types";

export const STATUS: Record<JobStatus, [string, string]> = {
  queued: ["排队中", "bg-slate-100 text-slate-600"],
  running: ["处理中", "bg-blue-100 text-blue-700"],
  done: ["完成", "bg-emerald-100 text-emerald-700"],
  error: ["失败", "bg-red-100 text-red-700"],
  cancelled: ["已取消", "bg-slate-100 text-slate-500"],
};

export function JobList() {
  const qc = useQueryClient();
  const { data: jobs = [] } = useQuery({
    queryKey: ["jobs"],
    queryFn: api.listJobs,
    refetchInterval: (q) => (q.state.data?.some((j) => j.status === "queued" || j.status === "running") ? 1000 : false),
  });
  if (!jobs.length) return <p className="text-sm text-slate-400">还没有转谱任务。</p>;
  return (
    <ul className="divide-y rounded-xl border bg-white">
      {jobs.map((j) => (
        <li key={j.id} className="flex items-center gap-3 px-4 py-2.5 text-sm">
          <span className={`rounded px-1.5 py-0.5 text-xs ${STATUS[j.status][1]}`}>{STATUS[j.status][0]}</span>
          <Link to={`/jobs/${j.id}`} className="flex-1 truncate font-medium hover:text-blue-600">
            {j.title}
          </Link>
          {j.status === "running" && <span className="text-xs text-slate-500">{Math.round(j.progress * 100)}%</span>}
          <span className="text-xs text-slate-400">{new Date(j.created_at * 1000).toLocaleString()}</span>
          <button
            className="text-xs text-slate-400 hover:text-red-600"
            onClick={async () => {
              if (!confirm(`删除「${j.title}」？`)) return;
              await api.deleteJob(j.id);
              qc.invalidateQueries({ queryKey: ["jobs"] });
            }}
          >
            删除
          </button>
        </li>
      ))}
    </ul>
  );
}
