import type { Media } from "../api/types";

// Fila de envio de arquivos locais da página Editar: estados por arquivo, no máximo 2 envios ao mesmo tempo.

export const MAX_PARALLEL_UPLOADS = 2;

/**
 * Teto de tamanho por arquivo: o padrão do backend (`max_download_bytes`, 2 GB). A API de configurações não expõe
 * esse valor; se o backend usar outro teto (MEDIA_TOOL_MAX_DOWNLOAD_BYTES), ele ainda recusa com 413 e a mensagem dele.
 */
export const MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024;
/** Containers aceitos pelo backend (MP4/MOV e Matroska/WebM). GIF e outros formatos são recusados lá também. */
export const ACCEPTED_VIDEO_EXTENSIONS = [".mp4", ".m4v", ".mov", ".webm", ".mkv"] as const;
export const UPLOAD_ACCEPT = `video/mp4,video/quicktime,video/webm,video/x-matroska,${ACCEPTED_VIDEO_EXTENSIONS.join(",")}`;
export const TOO_LARGE_TEXT = "Arquivo maior que o limite de 2 GB.";
export const EMPTY_FILE_TEXT = "Arquivo vazio.";
export const UNSUPPORTED_FILE_TEXT = "Formato não suportado. Envie vídeos MP4, MOV, WebM ou MKV.";
/**
 * Esperas antes de tentar de novo quando o servidor responde 429 (as 2 vagas dele ainda ocupadas, por exemplo pela
 * análise de um envio que o usuário removeu na fase "Analisando").
 */
export const UPLOAD_RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000] as const;

export type UploadStatus = "waiting" | "uploading" | "analyzing" | "done" | "duplicate" | "error";

export const UPLOAD_STATUS_LABEL: Record<UploadStatus, string> = {
  waiting: "Aguardando",
  uploading: "Enviando",
  analyzing: "Analisando",
  done: "Pronto",
  duplicate: "Já estava na biblioteca",
  error: "Erro",
};

export interface UploadItem {
  /** Nome + tamanho: o mesmo arquivo escolhido de novo é ignorado. */
  key: string;
  name: string;
  size: number;
  status: UploadStatus;
  /** Fração enviada (0 a 1). */
  progress: number;
  error: string | null;
  media: Media | null;
}

export function fileKey(file: { name: string; size: number }): string {
  return `${file.name}\u0000${file.size}`;
}

/** Problema que impede enviar o arquivo (tamanho ou tipo), ou null. Verificado antes do envio. */
export function checkLocalFile(file: { name: string; size: number; type?: string }): string | null {
  if (file.size > MAX_UPLOAD_BYTES) return TOO_LARGE_TEXT;
  if (file.size === 0) return EMPTY_FILE_TEXT;
  const dot = file.name.lastIndexOf(".");
  const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  if (ext) return (ACCEPTED_VIDEO_EXTENSIONS as readonly string[]).includes(ext) ? null : UNSUPPORTED_FILE_TEXT;
  // Sem extensão: aceita só pelo tipo informado pelo navegador.
  const type = (file.type ?? "").toLowerCase();
  return type.startsWith("video/") ? null : UNSUPPORTED_FILE_TEXT;
}

/** Espera antes da nova tentativa (ms) para o erro `code` na tentativa `attempt` (0 = primeira), ou null. */
export function uploadRetryDelay(code: string, attempt: number): number | null {
  if (code !== "too_many_uploads") return null;
  return UPLOAD_RETRY_DELAYS_MS[attempt] ?? null;
}

/**
 * Acrescenta arquivos em "aguardando"; repetidos (nome + tamanho já na fila ou na mesma escolha) são ignorados.
 * Um arquivo que falhou e é escolhido de novo volta para a fila (nova tentativa).
 */
export function addFiles<F extends { name: string; size: number }>(
  items: UploadItem[],
  files: F[],
  validate: (file: F) => string | null = () => null,
): { items: UploadItem[]; added: string[]; ignored: number } {
  const retry = new Set(files.map(fileKey));
  const kept = items.filter((item) => !(item.status === "error" && retry.has(item.key)));
  const keys = new Set(kept.map((item) => item.key));
  const next = [...kept];
  const added: string[] = [];
  let ignored = 0;
  for (const file of files) {
    const key = fileKey(file);
    if (keys.has(key)) {
      ignored += 1;
      continue;
    }
    keys.add(key);
    const problem = validate(file);
    if (problem !== null) {
      // Recusado antes de enviar: aparece na lista com o motivo, sem ocupar vaga.
      next.push({ key, name: file.name, size: file.size, status: "error", progress: 0, error: problem, media: null });
      continue;
    }
    added.push(key);
    next.push({ key, name: file.name, size: file.size, status: "waiting", progress: 0, error: null, media: null });
  }
  return { items: next, added, ignored };
}

/** Envio ocupando uma vaga (enviando ou o servidor analisando). */
export function isRunning(item: UploadItem): boolean {
  return item.status === "uploading" || item.status === "analyzing";
}

/** Chaves que podem começar agora, na ordem da fila, sem passar de `max` envios ao mesmo tempo. */
export function nextToStart(items: UploadItem[], max = MAX_PARALLEL_UPLOADS): string[] {
  const free = max - items.filter(isRunning).length;
  if (free <= 0) return [];
  return items.filter((item) => item.status === "waiting").slice(0, free).map((item) => item.key);
}

function update(items: UploadItem[], key: string, patch: (item: UploadItem) => Partial<UploadItem>): UploadItem[] {
  return items.map((item) => (item.key === key ? { ...item, ...patch(item) } : item));
}

export function markStarted(items: UploadItem[], key: string): UploadItem[] {
  return update(items, key, () => ({ status: "uploading", progress: 0, error: null }));
}

/** Progresso do envio; com tudo enviado o arquivo passa a "analisando" (o servidor confere o vídeo). */
export function markProgress(items: UploadItem[], key: string, fraction: number): UploadItem[] {
  const progress = Math.min(1, Math.max(0, fraction));
  return update(items, key, (item) =>
    isRunning(item) ? { progress, status: progress >= 1 ? "analyzing" : "uploading" } : {},
  );
}

export function markDone(items: UploadItem[], key: string, media: Media, duplicate: boolean): UploadItem[] {
  return update(items, key, () => ({ status: duplicate ? "duplicate" : "done", progress: 1, media, error: null }));
}

export function markError(items: UploadItem[], key: string, message: string): UploadItem[] {
  return update(items, key, () => ({ status: "error", error: message }));
}

/** Remove da fila (o chamador cancela o envio, se estiver em andamento). */
export function removeItem(items: UploadItem[], key: string): UploadItem[] {
  return items.filter((item) => item.key !== key);
}

/** Algum arquivo aguardando, enviando ou em análise: o lote de edição ainda não pode começar. */
export function isBusy(items: UploadItem[]): boolean {
  return items.some((item) => item.status === "waiting" || isRunning(item));
}

/** Resumo para a interface: "2 de 5 prontos". */
export function queueSummary(items: UploadItem[]): { ready: number; failed: number; total: number } {
  return {
    ready: items.filter((item) => item.status === "done" || item.status === "duplicate").length,
    failed: items.filter((item) => item.status === "error").length,
    total: items.length,
  };
}
