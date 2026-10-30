import type { Job, Priors, ScoreDoc, Transcription } from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    super(typeof body === "object" && body && "detail" in body ? String((body as { detail: unknown }).detail) : `HTTP ${status}`);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api${path}`, init);
  const ct = r.headers.get("content-type") ?? "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new ApiError(r.status, body);
  return body as T;
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  health: () => call<Record<string, boolean | string>>("/health"),
  listJobs: () => call<Job[]>("/jobs"),
  getJob: (id: string) => call<Job>(`/jobs/${id}`),
  createJob: (input: { file?: File; youtubeUrl?: string; priors: Priors }) => {
    const fd = new FormData();
    if (input.file) fd.append("file", input.file);
    if (input.youtubeUrl) fd.append("youtube_url", input.youtubeUrl);
    fd.append("priors", JSON.stringify(input.priors));
    return call<Job>("/jobs", { method: "POST", body: fd });
  },
  deleteJob: (id: string) => call<{ ok: boolean }>(`/jobs/${id}`, { method: "DELETE" }),
  cancelJob: (id: string) => call<Job>(`/jobs/${id}/cancel`, { method: "POST" }),
  rerunJob: (id: string, priors: Priors) => call<Job>(`/jobs/${id}/rerun`, json("POST", priors)),
  getDoc: (id: string) => call<ScoreDoc>(`/jobs/${id}/doc`),
  putDoc: (id: string, doc: ScoreDoc) => call<{ revision: number; doc_hash: string }>(`/jobs/${id}/doc`, json("PUT", { doc })),
  rederive: (id: string, what: "hands" | "voices" | "spelling" | "key") =>
    call<ScoreDoc>(`/jobs/${id}/doc/rederive`, json("POST", { what })),
  transcription: (id: string) => call<Transcription>(`/jobs/${id}/transcription`),
  musicxml: (id: string, rev: number) => call<string>(`/jobs/${id}/render/musicxml?rev=${rev}`),
  idmap: (id: string, rev: number) => call<Record<string, string>>(`/jobs/${id}/render/idmap?rev=${rev}`),
};

export const urls = {
  audio: (id: string) => `/api/jobs/${id}/audio`,
  pdf: (id: string, rev: number) => `/api/jobs/${id}/render/pdf?rev=${rev}&download=true`,
  musicxml: (id: string, rev: number) => `/api/jobs/${id}/render/musicxml?rev=${rev}&download=true`,
  midi: (id: string, rev: number, kind: "quantized" | "raw") => `/api/jobs/${id}/midi?kind=${kind}&rev=${rev}`,
};
