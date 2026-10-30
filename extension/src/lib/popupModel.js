// Regras puras do popup: corpo de /api/ext/start, estados de erro e resumo do andamento.

export const LIMIT_PRESETS = [10, 50, 100, 500];
export const PERIOD_PRESETS = [7, 30, 60, 90];
export const VIEWS_PRESETS = [1_000, 10_000, 50_000, 100_000, 500_000, 1_000_000];
export const MAX_LIMIT = 1_000_000;
export const MAX_PERIOD_DAYS = 3650;

export class FormError extends Error {}

function positiveInt(text, max, label) {
  const value = Number(String(text ?? "").trim());
  if (!Number.isInteger(value) || value < 1 || (max && value > max)) {
    throw new FormError(max ? `${label}: informe um número inteiro de 1 a ${max.toLocaleString("pt-BR")}.`
      : `${label}: informe um número inteiro maior que zero.`);
  }
  return value;
}

/** "1 mil", "1 mi" etc. */
export function viewsLabel(n) {
  if (n >= 1_000_000) return `${n / 1_000_000} mi`;
  if (n >= 1_000) return `${n / 1_000} mil`;
  return String(n);
}

/** Publicação/Reel: action "videos" | "covers" | "all" -> corpo da importação unificada (um link). */
export function buildPostBody(canonicalUrl, action) {
  if (!["videos", "covers", "all"].includes(action)) throw new FormError("Ação inválida.");
  if (!canonicalUrl) throw new FormError("Link da publicação ausente.");
  return { inputs: [canonicalUrl], media: action, post_processing: "none" };
}

/**
 * Perfil: valores do formulário -> corpo da importação unificada.
 * form: {media, limit: "10"|"50"|...|"custom"|"all", limitCustom, period: "any"|"7"|...|"custom", periodCustom,
 *        minViews: ""|"1000"|...|"custom", minViewsCustom, maxViews, maxViewsCustom, confirmAll}
 */
export function buildProfileBody(username, form) {
  if (!username) throw new FormError("Perfil ausente.");
  const media = form.media ?? "videos";
  if (!["videos", "all", "covers"].includes(media)) throw new FormError("Tipo de mídia inválido.");
  const body = { inputs: [username], media, post_processing: "none" };

  if (form.limit === "all") {
    if (!form.confirmAll) throw new FormError("Confirme que quer coletar todas as publicações do perfil.");
    body.limit = null;
    body.confirm_all = true;
  } else if (form.limit === "custom") {
    body.limit = positiveInt(form.limitCustom, MAX_LIMIT, "Quantidade");
  } else {
    body.limit = positiveInt(form.limit ?? "50", MAX_LIMIT, "Quantidade");
  }

  if (form.period && form.period !== "any") {
    body.posted_within_days = form.period === "custom"
      ? positiveInt(form.periodCustom, MAX_PERIOD_DAYS, "Período (dias)")
      : positiveInt(form.period, MAX_PERIOD_DAYS, "Período (dias)");
  }

  if (media !== "all") {
    const pick = (choice, custom, label) => {
      if (!choice) return null;
      return choice === "custom" ? positiveInt(custom, null, label) : positiveInt(choice, null, label);
    };
    const min = pick(form.minViews, form.minViewsCustom, "Visualizações mínimas");
    const max = pick(form.maxViews, form.maxViewsCustom, "Visualizações máximas");
    if (min !== null && max !== null && max < min) {
      throw new FormError("O máximo de visualizações precisa ser maior ou igual ao mínimo.");
    }
    if (min !== null) body.min_views = min;
    if (max !== null) body.max_views = max;
  }
  return body;
}

/** Erro da API -> estado da tela: {state: "offline"|"unpaired"|"revoked"|"error", message}. */
export function describeError(err) {
  if (err instanceof FormError) return { state: "error", message: err.message };
  const kind = err && err.kind;
  if (kind === "offline" || kind === "timeout") return { state: "offline", message: err.message };
  if (kind === "unauthorized") return { state: err.code ? "revoked" : "unpaired", message: err.message };
  return { state: "error", message: (err && err.message) || "Erro inesperado." };
}

const STATUS_LABELS = {
  queued: "Na fila", listing: "Listando publicações", processing: "Baixando", running: "Baixando",
  completed: "Concluído", failed: "Falhou", cancelled: "Cancelado", active: "Em andamento", attention: "Precisa de atenção",
};

export function statusLabel(status) {
  return STATUS_LABELS[status] ?? status ?? "-";
}

/** Linhas do andamento para o popup (sem HTML). */
export function summarizeProgress(progress) {
  const lines = [];
  const counters = (row) => {
    const parts = [`${row.found ?? 0} encontradas`, `${row.downloaded ?? 0} baixadas`];
    if (row.duplicates) parts.push(`${row.duplicates} já existentes`);
    if (row.filtered) parts.push(`${row.filtered} filtradas`);
    if (row.failed) parts.push(`${row.failed} com falha`);
    return parts.join(" · ");
  };
  if (progress && progress.group) {
    const g = progress.group;
    lines.push({ title: `Grupo de ${g.profiles?.total ?? 0} perfis: ${statusLabel(g.status)}`, detail: counters(g),
      openUrl: g.open_url, finished: g.finished });
  }
  for (const b of (progress && progress.batches) || []) {
    const who = b.profile ? `@${b.profile}` : `Lote ${b.id}`;
    lines.push({ title: `${who}: ${statusLabel(b.status)}`, detail: b.error_message || counters(b), openUrl: b.open_url,
      finished: b.finished });
  }
  return lines;
}
