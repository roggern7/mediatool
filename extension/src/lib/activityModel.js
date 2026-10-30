// Regras puras da aba Atividade: estados traduzidos, contadores, progresso geral e quando continuar consultando.

export const STATE_LABELS = {
  waiting: "Aguardando",
  analyzing: "Analisando publicação",
  downloading: "Baixando…",
  processing: "Processando",
  done: "Concluído",
  partial: "Concluído parcialmente",
  failed: "Falhou",
  cancelled: "Interrompido",
};

export const ACTIVE_STATES = new Set(["waiting", "analyzing", "downloading", "processing"]);

export function stateLabel(state) {
  return STATE_LABELS[state] ?? "Estado desconhecido";
}

/** Tom visual do estado: "active" | "ok" | "warn" | "err" | "muted". */
export function stateTone(state) {
  if (ACTIVE_STATES.has(state)) return "active";
  if (state === "done") return "ok";
  if (state === "partial") return "warn";
  if (state === "failed") return "err";
  return "muted";
}

export function isActive(item) {
  return Boolean(item) && ACTIVE_STATES.has(item.state);
}

const COUNTERS = [
  ["found", "encontradas"],
  ["analyzed", "analisadas"],
  ["files", "arquivos"],
  ["downloaded", "baixados"],
  ["duplicates", "duplicados"],
  ["filtered_period", "fora do período"],
  ["filtered_views", "fora das visualizações"],
  ["no_count", "sem contagem de visualizações"],
  ["failed", "falhas"],
  ["processed", "processados"],
];

/** Contadores para exibir (os zerados somem, exceto encontradas e baixados). */
export function counterList(progress) {
  const p = progress || {};
  return COUNTERS
    .filter(([key]) => key === "found" || key === "downloaded" || Number(p[key]) > 0)
    .map(([key, label]) => ({ key, label, value: Number(p[key]) || 0 }));
}

/**
 * Fração 0..1 do progresso geral, ou null quando ainda não dá para saber (nada encontrado). Itens resolvidos =
 * baixados + duplicados + falhas (+ processados/processando entram como já baixados). Estado final = 1.
 */
export function progressFraction(item) {
  if (!item) return null;
  if (!ACTIVE_STATES.has(item.state)) return 1;
  const p = item.progress || {};
  const total = Number(p.files) || Number(p.found) || 0;
  if (!total) return null;
  const done = (Number(p.downloaded) || 0) + (Number(p.duplicates) || 0) + (Number(p.failed) || 0);
  return Math.max(0, Math.min(1, done / total));
}

/**
 * Aviso do item (`message` da API: 1a página vazia, atenção, falha, pausa do lote ou do Instagram) com o tom da
 * caixa; null sem aviso.
 */
export function itemMessage(item) {
  const text = item && typeof item.message === "string" ? item.message.trim() : "";
  if (!text) return null;
  return { text, tone: item.state === "failed" ? "err" : "warn" };
}

export const ITEM_STAGE_TONES = {
  "concluído": "ok", "duplicado": "muted", "falhou": "err", "cancelado": "muted",
};

export function itemTone(stageLabel) {
  return ITEM_STAGE_TONES[stageLabel] ?? "active";
}

export const AUTO_EXPORT_LABELS = {
  waiting: "aguardando o fim do lote",
  pending: "na fila",
  queued: "na fila",
  running: "gerando ZIP",
  processing: "gerando ZIP",
  ready: "ZIP pronto",
  completed: "ZIP pronto",
  failed: "falhou",
  cancelled: "cancelada",
  expired: "expirada",
};

const AUTO_EXPORT_FINAL = new Set(["failed", "cancelled", "expired", "completed"]);

/** Texto da exportação automática do item (ou null). */
export function autoExportText(autoExport) {
  if (!autoExport) return null;
  const content = { originals: "originais", edited: "editados", both: "originais e editados" }[autoExport.content]
    ?? autoExport.content;
  const shared = Boolean(autoExport.request_id);
  const status = autoExport.download_url ? "ZIP pronto"
    : shared && autoExport.status === "waiting" ? "aguardando o fim de todo o pedido"
      : (AUTO_EXPORT_LABELS[autoExport.status] ?? "aguardando");
  const error = autoExport.status === "failed" && autoExport.error_message ? ` (${autoExport.error_message})` : "";
  // Pedido com perfis E links: um ZIP só para o pedido inteiro (os itens dele mostram o mesmo).
  return `Exportação automática${shared ? " do pedido" : ""} (${content}): ${status}${error}`;
}

/** Continua consultando enquanto algum item estiver ativo ou com exportação automática ainda sem ZIP. */
export function shouldKeepPolling(items) {
  return (items || []).some((item) => isActive(item) ||
    (item.auto_export && !item.auto_export.download_url && !AUTO_EXPORT_FINAL.has(item.auto_export.status)));
}

/* ---------- perfis de um grupo (GET /api/ext/groups/{id}/children) ---------- */

export const CHILD_PAGE = 20;
export const CHILD_PAGE_MAX = 100;

/** Quantos perfis pedir ao atualizar a lista aberta (os já carregados, entre 20 e 100). */
export function childRefreshLimit(entry) {
  const loaded = entry && Array.isArray(entry.children) ? entry.children.length : 0;
  return Math.min(CHILD_PAGE_MAX, Math.max(CHILD_PAGE, loaded));
}

/**
 * Junta uma página de perfis ao estado aberto do grupo ({children, nextAfter, total, error}). `append`: "Carregar
 * mais" (acrescenta sem repetir e avança o cursor). Senão é a atualização do começo: troca os dados dos perfis já
 * carregados e mantém os seguintes (e o cursor deles) quando a lista aberta é maior que a página.
 */
export function mergeChildPage(prev, page, append) {
  const items = Array.isArray(page?.items) ? page.items : [];
  const before = Array.isArray(prev?.children) ? prev.children : [];
  const nextAfter = page?.next_after_position ?? null;
  if (append) {
    const seen = new Set(before.map((c) => c.id));
    const children = [...before, ...items.filter((c) => !seen.has(c.id))];
    return { children, nextAfter, total: Number(page?.total ?? children.length), error: null };
  }
  if (before.length > items.length && nextAfter !== null) {
    const fresh = new Map(items.map((c) => [c.id, c]));
    return { children: before.map((c) => fresh.get(c.id) ?? c), nextAfter: prev?.nextAfter ?? null,
      total: Number(page?.total ?? before.length), error: null };
  }
  return { children: items, nextAfter, total: Number(page?.total ?? items.length), error: null };
}

/** "3 de 40 perfis" (o que está carregado da lista do grupo). */
export function childrenCountText(entry) {
  const shown = entry?.children?.length ?? 0;
  const total = Number(entry?.total) || shown;
  return `${shown} de ${total} perfi${total === 1 ? "l" : "s"}`;
}

export function mediaSetLabel(set) {
  return {
    videos: "Vídeos", photos: "Fotos", covers: "Capas", videos_covers: "Vídeos e capas", all: "Todas as mídias",
  }[set] ?? null;
}

/** Resumo dos filtros do lote ("até 50 · últimos 30 dias · mín. 10 mil"). */
export function filtersText(filters) {
  if (!filters) return "";
  const parts = [];
  if (filters.limit) parts.push(`até ${filters.limit}`);
  if (filters.posted_within_days) parts.push(`últimos ${filters.posted_within_days} dias`);
  const fmt = (n) => (n >= 1_000_000 ? `${String(n / 1_000_000).replace(".", ",")} mi`
    : n >= 1_000 ? `${String(n / 1_000).replace(".", ",")} mil` : String(n));
  if (filters.min_views) parts.push(`mín. ${fmt(filters.min_views)}`);
  if (filters.max_views) parts.push(`máx. ${fmt(filters.max_views)}`);
  return parts.join(" · ");
}
