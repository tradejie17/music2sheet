import { JobList } from "../components/JobList";
import { UploadCard } from "../components/UploadCard";

export function HomePage() {
  return (
    <div className="mx-auto max-w-3xl space-y-6 p-6">
      <header>
        <h1 className="text-xl font-semibold">music2sheet</h1>
        <p className="text-sm text-slate-500">音频 → 五线谱 · AI 初稿 + 人工微调</p>
      </header>
      <UploadCard />
      <section>
        <h2 className="mb-2 text-sm font-medium text-slate-600">最近的任务</h2>
        <JobList />
      </section>
    </div>
  );
}
