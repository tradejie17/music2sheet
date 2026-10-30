import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { Job, Priors } from "../api/types";
import { PriorsForm } from "./PriorsForm";

/** Re-run quantization/notation with new priors. Transcription and beats are cached. */
export function PriorsDialog({ job, onClose }: { job: Job; onClose: () => void }) {
  const [priors, setPriors] = useState<Priors>(job.params);
  const [busy, setBusy] = useState(false);
  const qc = useQueryClient();
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30" onClick={onClose}>
      <div className="w-[560px] rounded-lg bg-white p-5 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <h2 className="mb-1 font-medium">用新的先验参数重新生成</h2>
        <p className="mb-4 text-xs text-slate-500">
          转录与节拍结果会复用缓存，只重新量化与记谱（几秒钟）。当前的手动修改会保留在历史版本中，但新乐谱从头生成。
        </p>
        <PriorsForm value={priors} onChange={setPriors} />
        <div className="mt-5 flex justify-end gap-2 text-sm">
          <button className="rounded border px-3 py-1" onClick={onClose}>
            取消
          </button>
          <button
            className="rounded bg-blue-600 px-3 py-1 text-white disabled:opacity-50"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              await api.rerunJob(job.id, priors);
              await qc.invalidateQueries({ queryKey: ["job", job.id] });
              onClose();
            }}
          >
            重新生成
          </button>
        </div>
      </div>
    </div>
  );
}
