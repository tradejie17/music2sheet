// Editor state: the ScoreDoc being edited (with undo/redo via zundo), selection,
// grid and playhead. The server revision lives outside the undo history so that
// undoing never sends a stale revision.
import { temporal } from "zundo";
import { create } from "zustand";
import type { Lockable, ScoreDoc, ScoreNote } from "../api/types";

export type SaveState = "saved" | "dirty" | "saving" | "error" | "conflict";

interface EditorState {
  jobId: string | null;
  doc: ScoreDoc | null;
  baseRev: number; // server revision the current doc is based on
  savedRev: number; // last revision the server has (what the score view shows)
  saveState: SaveState;
  selected: string[];
  gridTicks: number;
  playheadTick: number;
  playing: boolean;
  follow: boolean; // score view follows the playhead (highlight + cursor + auto-scroll)

  load: (jobId: string, doc: ScoreDoc) => void;
  edit: (fn: (doc: ScoreDoc) => void) => void;
  editNotes: (ids: string[], fn: (n: ScoreNote) => void, lock?: Lockable[]) => void;
  addNote: (n: Omit<ScoreNote, "id">) => string;
  deleteNotes: (ids: string[]) => void;
  select: (ids: string[], additive?: boolean) => void;
  setGrid: (t: number) => void;
  setPlayhead: (t: number) => void;
  setPlaying: (p: boolean) => void;
  setFollow: (f: boolean) => void;
  markSaving: () => void;
  markSaved: (rev: number) => void;
  markError: (s: "error" | "conflict") => void;
}

const FOLLOW_KEY = "m2s.follow";
function readFollow(): boolean {
  try {
    return localStorage.getItem(FOLLOW_KEY) !== "0";
  } catch {
    return true;
  }
}

let idCounter = 0;
const newId = () => `u${Date.now().toString(36)}${(idCounter++).toString(36)}`;

export const useEditor = create<EditorState>()(
  temporal(
    (set, get) => ({
      jobId: null,
      doc: null,
      baseRev: 0,
      savedRev: 0,
      saveState: "saved",
      selected: [],
      gridTicks: 240,
      playheadTick: 0,
      playing: false,
      follow: readFollow(),

      load: (jobId, doc) => {
        set({ jobId, doc, baseRev: doc.revision, savedRev: doc.revision, saveState: "saved", selected: [] });
        useEditor.temporal.getState().clear();
      },
      edit: (fn) => {
        const doc = get().doc;
        if (!doc) return;
        const next = structuredClone(doc);
        fn(next);
        set({ doc: next, saveState: "dirty" });
      },
      editNotes: (ids, fn, lock = []) => {
        const want = new Set(ids);
        get().edit((d) => {
          for (const n of d.notes) {
            if (!want.has(n.id)) continue;
            fn(n);
            for (const l of lock) if (!n.locked.includes(l)) n.locked.push(l);
          }
        });
      },
      addNote: (n) => {
        const id = newId();
        get().edit((d) => {
          d.notes.push({ ...n, id });
        });
        set({ selected: [id] });
        return id;
      },
      deleteNotes: (ids) => {
        const gone = new Set(ids);
        get().edit((d) => {
          d.notes = d.notes.filter((n) => !gone.has(n.id));
        });
        set({ selected: [] });
      },
      select: (ids, additive = false) =>
        set((s) => ({ selected: additive ? Array.from(new Set([...s.selected, ...ids])) : ids })),
      setGrid: (t) => set({ gridTicks: t }),
      setPlayhead: (t) => set({ playheadTick: t }),
      setPlaying: (p) => set({ playing: p }),
      setFollow: (f) => {
        try {
          localStorage.setItem(FOLLOW_KEY, f ? "1" : "0");
        } catch {
          /* storage unavailable: keep the in-memory value */
        }
        set({ follow: f });
      },
      markSaving: () => set({ saveState: "saving" }),
      markSaved: (rev) =>
        set((s) => ({ baseRev: rev, savedRev: rev, saveState: s.saveState === "saving" ? "saved" : s.saveState })),
      markError: (st) => set({ saveState: st }),
    }),
    {
      // only the document is undoable; selection/playhead/save status are not
      partialize: (s) => ({ doc: s.doc }),
      equality: (a, b) => a.doc === b.doc,
      limit: 200,
    },
  ),
);

export const undo = () => {
  useEditor.temporal.getState().undo();
  useEditor.setState({ saveState: "dirty" });
};
export const redo = () => {
  useEditor.temporal.getState().redo();
  useEditor.setState({ saveState: "dirty" });
};

// debug/e2e hook (Transport also sets it once audio starts)
if (typeof window !== "undefined") Object.assign(window, { __editor: useEditor });
