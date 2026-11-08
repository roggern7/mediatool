// Aba "Top vídeos" do Importar: busca dos Reels com mais visualizações de um perfil (POST /api/top-videos) e
// download dos escolhidos como uma importação por links comum (POST /api/import-links, o mesmo pedido da aba "Por link").

import type {
  CapCutImportSettings,
  EditSettings,
  ImportLinksRequest,
  TopVideoItem,
  TopVideosRequest,
  TopVideosSearch,
} from "../api/types";
import { formatCount } from "./format";
import { buildLinksRequest, linksStartBlocker } from "./importLinks";
import type { PostProcessing } from "./importOptions";
import type { StartBlocker } from "./importStart";
import { parseProfileInput } from "./profile";

export const TOP_DEFAULT = 10;
export const TOP_MAX = 50;
export const SCAN_DEFAULT = 120;
export const SCAN_OPTIONS = [60, 120, 300, 600] as const;
/** Intervalo entre as leituras do andamento da busca. */
export const TOP_POLL_MS = 1_000;

export type TopRequestChoice = { ok: true; body: TopVideosRequest } | { ok: false; error: string };

/** "Quantos": inteiro de 1 a 50. */
export function resolveTopCount(text: string): { ok: true; top: number } | { ok: false; error: string } {
  const value = text.trim();
  if (!/^\d{1,3}$/.test(value)) return { ok: false, error: `Informe um número de 1 a ${TOP_MAX}.` };
  const top = Number(value);
  if (top < 1 || top > TOP_MAX) return { ok: false, error: `Informe um número de 1 a ${TOP_MAX}.` };
  return { ok: true, top };
}

/** Corpo da busca: perfil normalizado (@usuario, usuario ou link do perfil), quantos e quantos Reels analisar. */
export function buildTopRequest(profileText: string, topText: string, scanLimit: number): TopRequestChoice {
  const profile = parseProfileInput(profileText);
  if (!profile.ok) return { ok: false, error: profile.error };
  const top = resolveTopCount(topText);
  if (!top.ok) return top;
  return { ok: true, body: { profile: profile.username, top: top.top, scan_limit: scanLimit } };
}

function shortNumber(value: number, unit: string): string {
  // Uma casa decimal, truncada (999.999 vira "999,9 mil", nunca "1.000 mil"); ",0" some.
  const tenths = Math.floor(value * 10) / 10;
  const text = Number.isInteger(tenths) ? formatCount(tenths) : tenths.toFixed(1).replace(".", ",");
  return `${text} ${unit}`;
}

/** Visualizações curtas: "980", "45,3 mil", "1,2 mi", "2 bi". */
export function formatViews(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "-";
  if (n >= 1_000_000_000) return shortNumber(n / 1_000_000_000, "bi");
  if (n >= 1_000_000) return shortNumber(n / 1_000_000, "mi");
  if (n >= 1_000) return shortNumber(n / 1_000, "mil");
  return formatCount(Math.floor(n));
}

export function viewsLabel(n: number): string {
  return `${formatViews(n)} ${n === 1 ? "visualização" : "visualizações"}`;
}

export function scanProgressText(scanned: number): string {
  return scanned === 1 ? "1 Reel analisado" : `${formatCount(scanned)} Reels analisados`;
}

/** Linha de resumo do resultado. */
export function topResultSummary(search: TopVideosSearch): string {
  const found = search.items.length;
  const base = scanProgressText(search.scanned);
  if (search.status === "cancelled") return `Busca cancelada • ${base} • ${found} no ranking parcial`;
  if (search.status === "failed") return `Busca interrompida • ${base} • ${found} no ranking parcial`;
  if (found === 0) return `${base} • nenhum Reel com contagem de visualizações`;
  return `${base} • ${found === 1 ? "1 vídeo" : `${found} vídeos`} com mais visualizações`;
}

/**
 * O que a tela mostra de uma busca: o aviso de erro (falha) e a lista. Uma falha no meio da busca mantém o ranking do
 * que já foi lido, que continua podendo ser baixado; `showList` é falso só enquanto roda ou se nada foi lido.
 */
export function topResultView(search: TopVideosSearch | null): {
  error: string | null;
  showList: boolean;
  showEmpty: boolean;
} {
  if (search === null || search.status === "running") return { error: null, showList: false, showEmpty: false };
  const error = search.status === "failed" ? (search.error_message ?? "A busca falhou.") : null;
  const showList = search.items.length > 0;
  return { error, showList, showEmpty: !showList && search.status !== "failed" };
}

export function isRunning(search: TopVideosSearch | null | undefined): boolean {
  return search?.status === "running";
}

function asPreview(items: TopVideoItem[]) {
  const seen = new Set<string>();
  const valid = items
    .filter((item) => (seen.has(item.url) ? false : (seen.add(item.url), true)))
    .map((item) => ({ shortcode: item.code, url: item.url }));
  return { valid, invalid: [], duplicates: 0 };
}

/**
 * Corpo de POST /api/import-links para os vídeos escolhidos: o mesmo da aba "Por link" (só baixar, preset, edição ou
 * Edição CapCut). null sem vídeo.
 */
export function buildTopDownloadRequest(
  items: TopVideoItem[],
  options: {
    processAfterDownload: boolean;
    presetId: number | null;
    edit?: EditSettings | null;
    capcut?: CapCutImportSettings | null;
  },
): ImportLinksRequest | null {
  return buildLinksRequest(asPreview(items), options);
}

/** Por que o download não pode começar agora; null quando pode. As regras são as da aba "Por link". */
export function topDownloadBlocker(s: {
  sessionOk: boolean;
  paused: boolean;
  submitting: boolean;
  items: TopVideoItem[];
  process: boolean;
  presetId: number | null;
  editProblem?: string | null;
}): StartBlocker | null {
  return linksStartBlocker({ ...s, preview: asPreview(s.items) });
}

/** Aviso curto ao lado dos botões de download; null quando não há o que dizer. */
export function topDownloadHint(blocker: StartBlocker | null): string | null {
  switch (blocker) {
    case "no_session":
      return "Para baixar, configure a sessão do Instagram em Configurações.";
    case "paused":
      return "Instagram em pausa: o download fica disponível quando a pausa terminar.";
    case "preset":
      return "Escolha um preset no Processamento.";
    case "edit":
      return "Corrija as configurações do Processamento para baixar.";
    default:
      return null;
  }
}

/** Mensagem de sucesso do download dos escolhidos. */
export function topDownloadMessage(count: number, post: PostProcessing): string {
  const what = count === 1 ? "1 vídeo" : `${count} vídeos`;
  if (post === "capcut") return `Download de ${what} iniciado; os vídeos vão para a Edição CapCut.`;
  if (post === "edit") return `Download e edição de ${what} iniciados.`;
  if (post === "preset") return `Download e processamento de ${what} iniciados.`;
  return `Download de ${what} iniciado.`;
}
