import type {
  Batch,
  Diagnostics,
  Health,
  ImportRequest,
  InstagramStatus,
  Job,
  JobsPage,
  JobsQuery,
  Media,
  MediaQuery,
  Paged,
  Preset,
  PresetParams,
  ProcessByFilterRequest,
  ProfileInfo,
  SessionInput,
  Settings,
  SettingsUpdate,
} from "./types";
import { errorMessageFor } from "./errors";
import type { CookiePatch } from "../lib/sessionCookies";

const CLIENT_HEADER = "X-Media-Tool-Client";
const CLIENT_VALUE = "panel";
const DEFAULT_TIMEOUT_MS = 20_000;
const SLOW_TIMEOUT_MS = 90_000;
// Maior que o prazo do backend (~40 s) para teste de sessão e consulta de perfil.
const INSTAGRAM_TIMEOUT_MS = 60_000;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  /** Texto original do backend (pode ser null). */
  readonly detail: string | null;

  constructor(status: number, code: string, message: string, detail: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

interface RequestOptions {
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  timeoutMs?: number;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const params = new URLSearchParams();
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === "") continue;
      params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return `/api${path}${qs ? `?${qs}` : ""}`;
}

function extractDetail(payload: unknown): { detail: string | null; code: string | null } {
  if (!payload || typeof payload !== "object") return { detail: null, code: null };
  const obj = payload as Record<string, unknown>;
  const code = typeof obj.code === "string" ? obj.code : null;
  let detail: string | null = null;
  if (typeof obj.detail === "string") {
    detail = obj.detail;
  } else if (Array.isArray(obj.detail)) {
    // Erros de validação do FastAPI: [{loc, msg, ...}]
    const first = obj.detail[0] as Record<string, unknown> | undefined;
    if (first && typeof first.msg === "string") detail = first.msg;
  }
  return { detail, code };
}

async function request<T>(method: Method, path: string, opts: RequestOptions = {}): Promise<T> {
  const controller = new AbortController();
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  let timedOut = false;
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const onExternalAbort = () => controller.abort();
  opts.signal?.addEventListener("abort", onExternalAbort);

  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers[CLIENT_HEADER] = CLIENT_VALUE;
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }

  let response: Response;
  try {
    response = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      body,
      signal: controller.signal,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch (err) {
    if (timedOut) {
      throw new ApiError(0, "timeout", "O servidor demorou demais para responder.");
    }
    if (opts.signal?.aborted) {
      throw new ApiError(0, "aborted", "Requisição cancelada.");
    }
    void err;
    throw new ApiError(0, "backend_offline", "Não foi possível falar com o backend local.");
  } finally {
    window.clearTimeout(timer);
    opts.signal?.removeEventListener("abort", onExternalAbort);
  }

  if (response.status === 204) return undefined as T;

  let payload: unknown = null;
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const { detail, code } = extractDetail(payload);
    const finalCode = code ?? `http_${response.status}`;
    throw new ApiError(response.status, finalCode, errorMessageFor(finalCode, response.status, detail), detail);
  }
  return payload as T;
}

export const api = {
  health: (signal?: AbortSignal) => request<Health>("GET", "/health", { timeoutMs: 5_000, signal }),
  diagnostics: () => request<Diagnostics>("GET", "/diagnostics", { timeoutMs: SLOW_TIMEOUT_MS }),

  listPresets: () => request<Preset[]>("GET", "/presets"),
  getPreset: (id: number) => request<Preset>("GET", `/presets/${id}`),
  createPreset: (name: string, params: PresetParams) =>
    request<Preset>("POST", "/presets", { body: { name, params } }),
  updatePreset: (id: number, patch: { name?: string; params?: PresetParams }) =>
    request<Preset>("PUT", `/presets/${id}`, { body: patch }),
  deletePreset: (id: number) => request<void>("DELETE", `/presets/${id}`),
  duplicatePreset: (id: number, name?: string) =>
    request<Preset>("POST", `/presets/${id}/duplicate`, { body: name ? { name } : {} }),

  lookupProfile: (profile: string) =>
    request<ProfileInfo>("POST", "/instagram/profile", { body: { profile }, timeoutMs: INSTAGRAM_TIMEOUT_MS }),
  createImport: (req: ImportRequest) => request<Batch>("POST", "/imports", { body: req }),

  listBatches: (limit = 10, offset = 0, source?: Batch["source"]) =>
    request<Paged<Batch>>("GET", "/batches", { query: { limit, offset, source } }),
  /** Resumo do lote, sem jobs (lista de jobs do lote: listJobs({ batch_id })). */
  getBatch: (id: number, includeJobs = false) =>
    request<Batch>("GET", `/batches/${id}`, { query: { include_jobs: includeJobs ? "true" : undefined } }),
  cancelBatch: (id: number) => request<Batch>("POST", `/batches/${id}/cancel`),
  retryBatchFailed: (id: number) => request<Batch>("POST", `/batches/${id}/retry-failed`),
  /** Retoma a coleta da lista de um lote parado (needs_attention) ou em pausa. */
  resumeListing: (id: number) => request<Batch>("POST", `/batches/${id}/resume-listing`),

  listMedia: (q: MediaQuery) =>
    request<Paged<Media>>("GET", "/media", {
      query: {
        view: q.view,
        media_type: q.media_type,
        profile: q.profile,
        q: q.q,
        limit: q.limit,
        offset: q.offset,
        before_id: q.before_id,
      },
    }),
  getMedia: (id: number) => request<Media>("GET", `/media/${id}`),
  processMedia: (id: number, presetId: number) =>
    request<Job>("POST", `/media/${id}/process`, { body: { preset_id: presetId } }),
  processMediaBatch: (mediaIds: number[], presetId: number) =>
    request<Batch>("POST", "/media/process", { body: { media_ids: mediaIds, preset_id: presetId } }),
  processByFilter: (body: ProcessByFilterRequest) =>
    request<Batch>("POST", "/media/process-by-filter", { body, timeoutMs: SLOW_TIMEOUT_MS }),
  deleteRendition: (id: number) => request<void>("DELETE", `/renditions/${id}`),
  deleteMedia: (id: number) =>
    request<void>("DELETE", `/media/${id}`, { query: { delete_original: "true" } }),
  openMedia: (id: number, target: "file" | "folder", renditionId?: number) =>
    request<{ ok: boolean }>("POST", `/media/${id}/open`, {
      body: renditionId !== undefined ? { target, rendition_id: renditionId } : { target },
    }),

  listJobs: (q: JobsQuery) =>
    request<JobsPage>("GET", "/jobs", {
      query: {
        status: q.status && q.status.length > 0 ? q.status.join(",") : undefined,
        batch_id: q.batch_id,
        ids: q.ids && q.ids.length > 0 ? q.ids.join(",") : undefined,
        limit: q.limit,
        offset: q.offset,
        before_id: q.before_id,
      },
    }),
  getJob: (id: number) => request<Job>("GET", `/jobs/${id}`),
  cancelJob: (id: number) => request<Job>("POST", `/jobs/${id}/cancel`),
  retryJob: (id: number) => request<Job>("POST", `/jobs/${id}/retry`),

  getSettings: () => request<Settings>("GET", "/settings"),
  updateSettings: (patch: SettingsUpdate) => request<Settings>("PUT", "/settings", { body: patch }),
  saveSession: (input: SessionInput) =>
    request<InstagramStatus>("PUT", "/settings/instagram/session", { body: input }),
  patchSessionCookies: (body: CookiePatch) =>
    request<InstagramStatus>("PATCH", "/settings/instagram/session/cookies", { body }),
  releasePause: () => request<unknown>("DELETE", "/settings/instagram/pause"),
  clearSession: () => request<InstagramStatus>("DELETE", "/settings/instagram/session"),
  testSession: () =>
    request<InstagramStatus>("POST", "/settings/instagram/session/test", { timeoutMs: INSTAGRAM_TIMEOUT_MS }),
};

export function messageOf(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return "Erro inesperado.";
}
