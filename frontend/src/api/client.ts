import type {
  Batch,
  Diagnostics,
  EditFolderList,
  EditRequest,
  ExportJob,
  ExportRequest,
  Health,
  ImportGroupDetail,
  ImportGroupList,
  BatchLinks,
  ImportGroupRequest,
  ImportLinksRequest,
  ImportRequest,
  InstagramStatus,
  Job,
  JobsPage,
  JobsQuery,
  LinksPreview,
  Media,
  MediaDeleteRequest,
  MediaDeleteResult,
  HistoryHideRequest,
  HistoryHideResult,
  MediaQuery,
  Paged,
  PickFolderResult,
  Preset,
  PresetParams,
  ProcessByFilterRequest,
  ProfileAnalysis,
  ProfileAnalysisItem,
  ProfileAnalysisPage,
  ProfileHistoryCleared,
  ProfileInfo,
  ProfilesPreview,
  SessionInput,
  Settings,
  SettingsUpdate,
  UploadResult,
  ClassifyRequest,
  ClassifyResult,
  UnifiedImportRequest,
  UnifiedImportResult,
} from "./types";
import { errorMessageFor } from "./errors";
import type { CookiePatch } from "../lib/sessionCookies";
import { PICK_FOLDER_TIMEOUT_MS } from "../lib/exportFolder";

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
  /** Corpo JSON do erro, quando há campos além de detail/code (ex.: `invalid`, `skipped`). */
  readonly payload: Record<string, unknown> | null;

  constructor(
    status: number,
    code: string,
    message: string,
    detail: string | null = null,
    payload: Record<string, unknown> | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.payload = payload;
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
    const extra = payload && typeof payload === "object" ? (payload as Record<string, unknown>) : null;
    throw new ApiError(response.status, finalCode, errorMessageFor(finalCode, response.status, detail), detail, extra);
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

  /** Pré-validação da lista de perfis: sem efeitos e sem requisição ao Instagram. */
  previewProfiles: (entries: string[], signal?: AbortSignal) =>
    request<ProfilesPreview>("POST", "/import-groups/preview", { body: { entries }, signal }),
  createImportGroup: (req: ImportGroupRequest) =>
    request<ImportGroupDetail>("POST", "/import-groups", { body: req, timeoutMs: SLOW_TIMEOUT_MS }),
  /** `history`: sem os grupos removidos do histórico de importações. */
  listImportGroups: (limit = 50, beforeId?: number, history = false) =>
    request<ImportGroupList>("GET", "/import-groups", {
      query: { limit, before_id: beforeId, history: history ? "true" : undefined },
    }),
  /** Remove do histórico de importações (só oculta; `delete_media` também apaga os vídeos). */
  hideImportHistory: (body: HistoryHideRequest) =>
    request<HistoryHideResult>("POST", "/imports/history/hide", { body, timeoutMs: 10 * 60_000 }),
  /** Grupo + uma página de filhos (ordem group_position; a seguinte é `after_position = next_after_position`). */
  getImportGroup: (id: number, opts: { childrenLimit?: number; afterPosition?: number | null } = {}) =>
    request<ImportGroupDetail>("GET", `/import-groups/${id}`, {
      query: { children_limit: opts.childrenLimit, after_position: opts.afterPosition },
    }),
  cancelImportGroup: (id: number) => request<ImportGroupDetail>("POST", `/import-groups/${id}/cancel`),

  /** Pré-validação dos links de publicações: sem efeitos e sem requisição ao Instagram. */
  previewLinks: (entries: string[], signal?: AbortSignal) =>
    request<LinksPreview>("POST", "/import-links/preview", { body: { entries }, signal }),
  /** Importação por link: `links` são as URLs normalizadas do preview. A resposta é o lote criado. */
  createLinksImport: (req: ImportLinksRequest) =>
    request<Batch & { duplicates?: number }>("POST", "/import-links", { body: req, timeoutMs: SLOW_TIMEOUT_MS }),
  /** Importação unificada: classifica as entradas (perfis, publicações, inválidas), sem efeitos. */
  classifyImport: (body: ClassifyRequest, signal?: AbortSignal) =>
    request<ClassifyResult>("POST", "/imports/classify", { body, signal }),
  /** Importação unificada: 1 perfil = lote avulso; 2+ perfis = grupo; publicações = lote de links. */
  startImport: (body: UnifiedImportRequest) =>
    request<UnifiedImportResult>("POST", "/imports/start", { body, timeoutMs: SLOW_TIMEOUT_MS }),
  /** Links de um lote por link, na ordem em que foram enviados. */
  listBatchLinks: (id: number, limit = 200, offset = 0) =>
    request<BatchLinks>("GET", `/batches/${id}/links`, { query: { limit, offset } }),

  /** `ungrouped`: só lotes avulsos e antigos (sem filhos de grupo); `groupId`: só os filhos de um grupo. */
  listBatches: (
    limit = 10,
    offset = 0,
    source?: Batch["source"],
    opts: { ungrouped?: boolean; groupId?: number; history?: boolean } = {},
  ) =>
    request<Paged<Batch>>("GET", "/batches", {
      query: {
        limit,
        offset,
        source,
        group_id: opts.groupId,
        ungrouped: opts.ungrouped ? "true" : undefined,
        history: opts.history ? "true" : undefined,
      },
    }),
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
        role: q.role,
        profile: q.profile,
        q: q.q,
        limit: q.limit,
        offset: q.offset,
        before_id: q.before_id,
      },
    }),
  getMedia: (id: number) => request<Media>("GET", `/media/${id}`),
  /** Leitura em lote (até 500 ids), na ordem pedida; ids inexistentes ficam de fora. */
  getMediaByIds: (ids: number[]) => request<Paged<Media>>("GET", "/media", { query: { ids: ids.join(",") } }),
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

  /**
   * Abre o seletor NATIVO de pasta do Windows pelo backend (sem corpo: nenhum caminho sai do painel). A resposta só
   * chega quando o usuário escolhe ou cancela, por isso o prazo longo.
   */
  pickExportFolder: () =>
    request<PickFolderResult>("POST", "/exports/pick-folder", { timeoutMs: PICK_FOLDER_TIMEOUT_MS }),
  createExport: (req: ExportRequest) =>
    request<ExportJob>("POST", "/exports", { body: req, timeoutMs: SLOW_TIMEOUT_MS }),
  getExport: (id: number) => request<ExportJob>("GET", `/exports/${id}`),
  /** Últimas exportações (mais recente primeiro) de um lote ou de um grupo. */
  listExports: (q: { batchId?: number; groupId?: number; kind?: ExportJob["kind"]; limit?: number } = {}) =>
    request<{ items: ExportJob[] }>("GET", "/exports", {
      query: { batch_id: q.batchId, group_id: q.groupId, kind: q.kind, limit: q.limit },
    }),
  cancelExport: (id: number) => request<ExportJob>("POST", `/exports/${id}/cancel`),
  /** Abre no Explorer a pasta daquela exportação (o caminho vem do registro no backend). */
  openExportFolder: (id: number) => request<{ ok: boolean }>("POST", `/exports/${id}/open`),
  /** Abre no Explorer a pasta física dos originais da Biblioteca. */
  openLibraryFolder: () => request<{ ok: boolean }>("POST", "/library/open-folder"),

  listJobs: (q: JobsQuery) =>
    request<JobsPage>("GET", "/jobs", {
      query: {
        status: q.status && q.status.length > 0 ? q.status.join(",") : undefined,
        batch_id: q.batch_id,
        ids: q.ids && q.ids.length > 0 ? q.ids.join(",") : undefined,
        limit: q.limit,
        offset: q.offset,
        before_id: q.before_id,
        created_from: q.created_from,
        created_to: q.created_to,
      },
    }),
  getJob: (id: number) => request<Job>("GET", `/jobs/${id}`),
  cancelJob: (id: number) => request<Job>("POST", `/jobs/${id}/cancel`),
  retryJob: (id: number) => request<Job>("POST", `/jobs/${id}/retry`),

  /** Análise de Perfis: `text` é a lista colada; o backend normaliza e decide o que é válido. */
  createProfileAnalysis: (text: string) => request<ProfileAnalysis>("POST", "/profile-analyses", { body: { text } }),
  listProfileAnalyses: (limit = 20, offset = 0) =>
    request<ProfileAnalysisPage>("GET", "/profile-analyses", { query: { limit, offset } }),
  getProfileAnalysis: (id: number) => request<ProfileAnalysis>("GET", `/profile-analyses/${id}`),
  listProfileAnalysisItems: (id: number, limit = 50, offset = 0) =>
    request<Paged<ProfileAnalysisItem>>("GET", `/profile-analyses/${id}/items`, { query: { limit, offset } }),
  cancelProfileAnalysis: (id: number) => request<ProfileAnalysis>("POST", `/profile-analyses/${id}/cancel`),
  resumeProfileAnalysis: (id: number) => request<ProfileAnalysis>("POST", `/profile-analyses/${id}/resume`),
  deleteProfileAnalysis: (id: number) => request<void>("DELETE", `/profile-analyses/${id}`),
  clearProfileHistory: () => request<ProfileHistoryCleared>("DELETE", "/profile-analyses"),

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

  /** Lote de edição dos vídeos (máx. 500); o progresso vem de getBatch/listJobs. */
  startEdit: (body: EditRequest) => request<Batch>("POST", "/edits", { body, timeoutMs: SLOW_TIMEOUT_MS }),
  listEditFolders: () => request<EditFolderList>("GET", "/edit/folders"),
  listEditFolderMedia: (key: string, q: { q?: string; limit?: number; before_id?: number }) =>
    request<Paged<Media>>("GET", `/edit/folders/${encodeURIComponent(key)}/media`, { query: q }),
  /** Apaga item a item no servidor: uma pasta grande (mil vídeos ou mais) pode levar minutos. */
  deleteMediaBulk: (body: MediaDeleteRequest) =>
    request<MediaDeleteResult>("POST", "/media/delete", { body, timeoutMs: 10 * 60_000 }),
  uploadLocalVideo: (file: File, onProgress?: (fraction: number) => void, signal?: AbortSignal) =>
    uploadLocalVideo(file, onProgress, signal),
};

/** Rota isenta do limite de 1 MB do corpo (o teto é o tamanho máximo de download, 2 GB por padrão). */
export const UPLOAD_URL = "/api/library/uploads";
export const UPLOAD_INTERRUPTED_TEXT =
  "O envio foi interrompido. Confira se o arquivo tem até 2 GB e se o Media Tool está aberto, e tente de novo.";

/**
 * Envia um vídeo local (multipart, campo `file`) com progresso de envio. Usa XMLHttpRequest porque fetch não
 * informa o progresso do corpo. Sem prazo fixo: arquivos grandes podem levar minutos; `signal` cancela.
 */
export function uploadLocalVideo(
  file: File,
  onProgress?: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<UploadResult> {
  return new Promise<UploadResult>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new ApiError(0, "aborted", "Requisição cancelada."));
      return;
    }
    const xhr = new XMLHttpRequest();
    const onAbort = () => xhr.abort();
    signal?.addEventListener("abort", onAbort);
    const done = () => signal?.removeEventListener("abort", onAbort);
    xhr.open("POST", UPLOAD_URL);
    xhr.setRequestHeader(CLIENT_HEADER, CLIENT_VALUE);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.responseType = "text";
    xhr.upload.onprogress = (e: ProgressEvent) => {
      if (e.lengthComputable && e.total > 0) onProgress?.(e.loaded / e.total);
    };
    xhr.onload = () => {
      done();
      let payload: unknown;
      try {
        payload = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch {
        payload = null;
      }
      if (xhr.status >= 200 && xhr.status < 300 && payload && typeof payload === "object") {
        resolve(payload as UploadResult);
        return;
      }
      const { detail, code } = extractDetail(payload);
      const finalCode = code ?? `http_${xhr.status}`;
      const extra = payload && typeof payload === "object" ? (payload as Record<string, unknown>) : null;
      // 413 aqui é o arquivo acima do teto: a mensagem do backend (com o limite) vale mais que a genérica.
      const message =
        finalCode === "payload_too_large" && detail ? detail : errorMessageFor(finalCode, xhr.status, detail);
      reject(new ApiError(xhr.status, finalCode, message, detail, extra));
    };
    xhr.onerror = () => {
      done();
      // A conexão caiu no meio do envio: backend parado ou o arquivo recusado antes do fim (ex.: acima do teto).
      reject(new ApiError(0, "upload_interrupted", UPLOAD_INTERRUPTED_TEXT));
    };
    xhr.onabort = () => {
      done();
      reject(new ApiError(0, "aborted", "Requisição cancelada."));
    };
    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

/**
 * Baixa um arquivo pequeno gerado na hora (ex.: relatório de padrões .zip). Busca primeiro para mostrar o erro do
 * backend (404 etc.) em vez de navegar para uma página de erro; com sucesso, salva pelo download do navegador.
 */
export async function downloadGeneratedFile(url: string, fallbackName: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
  } catch {
    throw new ApiError(0, "backend_offline", "Não foi possível falar com o backend local.");
  }
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    const { detail, code } = extractDetail(payload);
    const finalCode = code ?? `http_${response.status}`;
    throw new ApiError(response.status, finalCode, errorMessageFor(finalCode, response.status, detail), detail);
  }
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") ?? "";
  const name = /filename="?([^";]+)"?/i.exec(disposition)?.[1] ?? fallbackName;
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = name;
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 10_000);
}

export function messageOf(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return "Erro inesperado.";
}
