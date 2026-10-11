import type { BatchStatus, ContentType, JobStage, JobStatus } from "../api/types";
import { formatCount } from "./format";

export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  pending: "Pendente",
  downloading: "Baixando",
  downloaded: "Baixado",
  processing: "Processando",
  completed: "Concluído",
  failed: "Erro",
  cancelled: "Cancelado",
};

export const JOB_STAGE_LABEL: Record<JobStage, string> = {
  queued: "Na fila",
  waiting_retry: "Aguardando nova tentativa",
  downloading: "Download",
  muxing: "Juntando vídeo e áudio",
  hashing: "Calculando hash",
  probing: "Analisando",
  thumbnail: "Miniatura",
  processing: "Processando",
  finalizing: "Finalizando",
  done: "Concluído",
  skipped: "Ignorado",
};

export const BATCH_STATUS_LABEL: Record<BatchStatus, string> = {
  listing: "Coletando lista",
  running: "Em andamento",
  completed: "Concluído",
  failed: "Falhou",
  cancelled: "Cancelado",
};

export const CONTENT_LABEL: Record<ContentType, string> = {
  reels: "Reels",
  posts: "Posts",
  videos: "Vídeos",
  images: "Imagens",
  all: "Todos os tipos",
  photos_carousels: "Publicações do feed",
};

export const TERMINAL_JOB: ReadonlySet<JobStatus> = new Set(["completed", "failed", "cancelled"]);
export const ACTIVE_BATCH: ReadonlySet<BatchStatus> = new Set(["listing", "running"]);

export type Tone = "ok" | "warn" | "err" | "muted" | "accent";

export function jobTone(status: JobStatus): Tone {
  switch (status) {
    case "completed":
      return "ok";
    case "failed":
      return "err";
    case "cancelled":
      return "muted";
    case "pending":
      return "muted";
    default:
      return "accent";
  }
}

export function batchTone(status: BatchStatus): Tone {
  switch (status) {
    case "completed":
      return "ok";
    case "failed":
      return "err";
    case "cancelled":
      return "muted";
    default:
      return "accent";
  }
}

interface BatchLike {
  status: BatchStatus;
  source: "instagram_import" | "library";
  source_profile: string | null;
  progress: { failed: number };
  listing_state?: string | null;
  paused_reason?: string | null;
  found?: number;
  group_id?: number | null;
  group_state?: string | null;
  import_mode?: string | null;
  links?: { total: number } | null;
}

/** Lote de importação por link (sem perfil de origem; quantidade e tipo de mídia não se aplicam). */
export function isLinksBatch(b: { import_mode?: string | null }): boolean {
  return b.import_mode === "links";
}

/** Origem do lote nas listas: "@perfil" ou, no lote por link, "Links (3)" ("Importação por link" sem a contagem). */
export function batchOriginLabel(b: Pick<BatchLike, "source_profile" | "import_mode" | "links">): string {
  if (!isLinksBatch(b)) return `@${b.source_profile ?? "?"}`;
  return b.links ? `Links (${formatCount(b.links.total)})` : "Importação por link";
}

export const QUEUED_LABEL = "Aguardando na fila";

/**
 * Lote filho de um grupo ainda na fila (a listagem dele não começou): não está "coletando lista". Usa
 * `group_state` da API; sem ele (resposta antiga), deriva dos demais campos.
 */
export function isQueuedInGroup(
  b: Pick<BatchLike, "status" | "listing_state" | "found" | "group_id" | "group_state">,
): boolean {
  if (b.group_state != null) return b.group_state === "waiting";
  return b.group_id != null && b.status === "listing" && b.listing_state == null && (b.found ?? 0) === 0;
}

/** Lote ativo parado: coleta precisando do dono ou pausa por falhas seguidas. */
function stoppedLabel(b: BatchLike): string | null {
  if (!ACTIVE_BATCH.has(b.status)) return null;
  if (b.listing_state === "needs_attention") return "Precisa de atenção";
  if (b.paused_reason) return "Pausado";
  return null;
}

export const RATE_LIMIT_WAIT_LABEL = "Aguardando limite de requisições";

export function batchLabel(b: BatchLike): string {
  if (b.status === "completed" && b.progress.failed > 0) return "Concluído com erros";
  const stopped = stoppedLabel(b);
  if (stopped !== null) return stopped;
  if (isQueuedInGroup(b)) return QUEUED_LABEL;
  // Espera pelo teto de páginas por hora: o lote continua ativo (mesmo tom da coleta, não é erro nem pausa).
  if (ACTIVE_BATCH.has(b.status) && b.listing_state === "waiting_rate_limit") return RATE_LIMIT_WAIT_LABEL;
  // Lote por link em "listing" está resolvendo os links, um por vez.
  if (b.status === "listing" && isLinksBatch(b)) return "Resolvendo links";
  return BATCH_STATUS_LABEL[b.status];
}

export function batchToneOf(b: BatchLike): Tone {
  if (b.status === "completed" && b.progress.failed > 0) return "warn";
  if (stoppedLabel(b) !== null) return "warn";
  if (isQueuedInGroup(b)) return "muted";
  return batchTone(b.status);
}

export function batchTitle(b: BatchLike): string {
  const active = ACTIVE_BATCH.has(b.status);
  if (b.source !== "instagram_import") return active ? "Processando lote" : "Processamento em lote";
  if (isLinksBatch(b)) return active ? "Importando por link" : "Importação por link";
  const who = `@${b.source_profile ?? "?"}`;
  return active ? `Importando ${who}` : `Importação de ${who}`;
}

/**
 * Importação somente de originais (sem processamento): a etapa de FFmpeg não existe para o lote.
 * `process` ausente (resposta antiga) conta como lote com processamento, para exibir como antes.
 */
export function isDownloadOnly(b: { source: "instagram_import" | "library"; process?: boolean }): boolean {
  return b.source === "instagram_import" && b.process === false;
}

/** Rótulo do tipo de job. */
export function jobKindLabel(kind: "ingest" | "process"): string {
  return kind === "process" ? "Processamento" : "Download";
}

/** "Duplicado" só faz sentido para downloads; reprocessar mídia existente não é duplicidade. */
export function showDuplicate(job: { kind: "ingest" | "process"; is_duplicate: boolean }): boolean {
  return job.kind === "ingest" && job.is_duplicate;
}
