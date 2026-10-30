import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../api/client";
import type { Job } from "../api/types";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { PriorsDialog } from "../components/PriorsDialog";
import { Inspector } from "./Inspector";
import { TimeMap } from "./music";
import { type Ghost, PianoRoll } from "./PianoRoll";
import { ScoreView } from "./ScoreView";
import { redo, undo, useEditor } from "./store";
import { Toolbar } from "./Toolbar";
import { Transport, type TransportHandle } from "./Transport";

/** Debounced autosave: PUT the doc 600 ms after the last edit, one request at a time. */
function useAutosave() {
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let chain: Promise<void> = Promise.resolve();
    const save = () => {
      chain = chain.then(async () => {
        const { doc, baseRev, jobId, saveState } = useEditor.getState();
        if (!doc || !jobId || saveState !== "dirty") return;
        useEditor.getState().markSaving();
        try {
          const r = await api.putDoc(jobId, { ...doc, revision: baseRev });
          useEditor.getState().markSaved(r.revision);
        } catch (e) {
          useEditor.getState().markError(e instanceof ApiError && e.status === 409 ? "conflict" : "error");
        }
      });
    };
    const unsub = useEditor.subscribe((s, prev) => {
      // fire on any doc change while dirty, and when something (e.g. undo) marks it dirty
      if (s.saveState === "dirty" && (s.doc !== prev.doc || prev.saveState !== "dirty")) {
        clearTimeout(timer);
        timer = setTimeout(save, 600);
      }
    });
    return () => {
      unsub();
      clearTimeout(timer);
    };
  }, []);
}

function useShortcuts(transport: React.RefObject<TransportHandle | null>) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName)) return;
      const st = useEditor.getState();
      const ids = st.selected;
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
        return;
      }
      if (e.key === " ") {
        e.preventDefault();
        transport.current?.toggle();
        return;
      }
      if (!ids.length) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        st.deleteNotes(ids);
      } else if (e.key === "s" || e.key === "S") {
        st.editNotes(ids, (n) => (n.staff = n.staff === 1 ? 2 : 1), ["staff"]);
      } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
        e.preventDefault();
        const d = (e.key === "ArrowUp" ? 1 : -1) * (e.shiftKey ? 12 : 1);
        st.editNotes(
          ids,
          (n) => {
            n.pitch = Math.min(108, Math.max(21, n.pitch + d));
            if (!n.locked.includes("spelling")) n.spelling = null;
          },
          ["pitch"],
        );
      } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        const d = (e.key === "ArrowRight" ? 1 : -1) * st.gridTicks;
        st.editNotes(ids, (n) => (n.start = Math.max(0, n.start + d)), ["time"]);
      } else if (e.key === "Escape") {
        st.select([]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [transport]);
}

export function EditorLayout({ job }: { job: Job }) {
  const doc = useEditor((s) => s.doc);
  const loadedFor = useEditor((s) => s.jobId);
  const transport = useRef<TransportHandle>(null);
  const [split, setSplit] = useState(0.55);
  const [rerunOpen, setRerunOpen] = useState(false);
  useAutosave();
  useShortcuts(transport);

  const docQuery = useQuery({ queryKey: ["doc", job.id, job.doc_rev], queryFn: () => api.getDoc(job.id) });
  const trQuery = useQuery({ queryKey: ["tr", job.id, job.audio_sha], queryFn: () => api.transcription(job.id) });

  useEffect(() => {
    if (docQuery.data && (loadedFor !== job.id || docQuery.data.revision > useEditor.getState().baseRev)) {
      useEditor.getState().load(job.id, docQuery.data);
    }
  }, [docQuery.data, job.id, loadedFor]);

  const time = useMemo(() => new TimeMap(doc?.tempo_map ?? null, doc?.tempos[0]?.bpm, doc?.ppq), [doc?.tempo_map, doc?.tempos, doc?.ppq]);
  const ghosts: Ghost[] = useMemo(
    () =>
      (trQuery.data?.notes ?? []).map((n) => ({
        pitch: n.pitch,
        start: time.secToTick(n.onset_s),
        end: time.secToTick(n.offset_s),
      })),
    [trQuery.data, time],
  );

  if (!doc || loadedFor !== job.id) return <div className="p-6 text-sm text-slate-500">加载乐谱…</div>;

  return (
    <div className="flex h-full flex-col">
      <Toolbar job={job} onRerun={() => setRerunOpen(true)} />
      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col">
          <div style={{ height: `${split * 100}%` }} className="min-h-0 border-b">
            <ErrorBoundary name="乐谱视图">
              <ScoreView jobId={job.id} onSeek={(t) => transport.current?.seekTick(t)} />
            </ErrorBoundary>
          </div>
          <div
            className="h-1.5 cursor-row-resize bg-slate-200 hover:bg-blue-300"
            onPointerDown={(e) => {
              const parent = (e.currentTarget.parentElement as HTMLElement).getBoundingClientRect();
              const move = (ev: PointerEvent) =>
                setSplit(Math.min(0.85, Math.max(0.15, (ev.clientY - parent.top) / parent.height)));
              const up = () => {
                window.removeEventListener("pointermove", move);
                window.removeEventListener("pointerup", up);
              };
              window.addEventListener("pointermove", move);
              window.addEventListener("pointerup", up);
            }}
          />
          <div className="min-h-0 flex-1">
            <ErrorBoundary name="卷帘">
              <PianoRoll ghosts={ghosts} onSeek={(t) => transport.current?.seekTick(t)} />
            </ErrorBoundary>
          </div>
        </div>
        <aside className="w-64 shrink-0 overflow-y-auto border-l bg-white">
          <ErrorBoundary name="检查器">
            <Inspector />
          </ErrorBoundary>
        </aside>
      </div>
      <Transport ref={transport} jobId={job.id} time={time} />
      {rerunOpen && <PriorsDialog job={job} onClose={() => setRerunOpen(false)} />}
    </div>
  );
}
