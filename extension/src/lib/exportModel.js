// Regras puras da aba Exportar: sempre UM ZIP por pedido (POST /api/ext/exports).

export const EXPORT_STATUS_LABELS = {
  pending: "Na fila",
  queued: "Na fila",
  running: "Gerando ZIP…",
  processing: "Gerando ZIP…",
  completed: "ZIP pronto",
  ready: "ZIP pronto",
  failed: "Falhou",
  cancelled: "Cancelada",
};

export const EXPORT_MAX_ITEMS = 5000;

/**
 * source: {kind: "library", mediaIds, coversOnly, coverIds} | {kind: "batch"|"group", id}
 * content: "originals" | "edited" | "both"
 */
export function buildExportBody(source, content) {
  if (!["originals", "edited", "both"].includes(content)) throw new Error("Escolha o conteúdo da exportação.");
  if (!source) throw new Error("Escolha o que exportar.");
  if (source.kind === "library") {
    const ids = source.coversOnly ? (source.coverIds ?? []) : (source.mediaIds ?? []);
    if (!ids.length) {
      throw new Error(source.coversOnly ? "Nenhuma capa na seleção." : "Selecione itens na Biblioteca primeiro.");
    }
    if (ids.length > EXPORT_MAX_ITEMS) throw new Error(`Máximo de ${EXPORT_MAX_ITEMS} itens por exportação.`);
    return { source: { kind: "library", media_ids: ids.map(Number) }, content };
  }
  if (source.kind === "batch" || source.kind === "group") {
    const id = Number(source.id);
    if (!Number.isInteger(id) || id < 1) throw new Error("Escolha um lote ou grupo.");
    return { source: { kind: source.kind, id }, content };
  }
  throw new Error("Fonte da exportação inválida.");
}

export function exportStatusText(status) {
  if (!status) return "";
  if (status.zip_ready) return "ZIP pronto";
  const label = EXPORT_STATUS_LABELS[status.status] ?? "Em andamento";
  if (status.status === "failed") return status.error_message ? `Falhou: ${status.error_message}` : "Falhou";
  if (status.total) return `${label} ${status.processed ?? 0} de ${status.total}`;
  return label;
}

export function exportFinished(status) {
  return Boolean(status) && (status.zip_ready || ["failed", "cancelled"].includes(status.status));
}

export function formatBytes(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1).replace(".", ",")} MB`;
  return `${(n / 1024 ** 3).toFixed(2).replace(".", ",")} GB`;
}
