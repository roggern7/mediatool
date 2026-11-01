import { ApiError, messageOf } from "../api/client";
import { describeErrorCode } from "../api/errors";
import type {
  ProfileAnalysis,
  ProfileAnalysisStatus,
  ProfileAnalysisSummary,
  ProfileAnalysisItem,
  ProfileCheck,
} from "../api/types";
import { formatClock, formatCount } from "./format";
import type { Tone } from "./labels";
import { nextPollMs } from "./polling";
import { parseProfileInput } from "./profile";
import { ROUTES } from "./router";

// Regras de apresentação da Análise de Perfis. Regra central: Sim, Não e Não verificado são três estados
// distintos; campo ausente, nulo ou desconhecido sempre sai como Não verificado, nunca como Não.

/** Mesmo teto do backend (`MAX_PROFILES`). */
export const MAX_PROFILES = 10000;
/** Média por perfil com o intervalo entre consultas: só uma estimativa para a tela. */
const SECONDS_PER_PROFILE = 10;
/** O mesmo no ritmo normal, antes de qualquer sinal de limite. */
const FAST_SECONDS_PER_PROFILE = 4;

const UNAVAILABLE_TEXT = "Perfil não encontrado, removido ou restrito para visitantes sem login.";
const UNVERIFIED_TEXT = "Não foi possível confirmar este perfil.";
const INTERRUPTED_TEXT = "Análise interrompida: os perfis restantes não foram consultados.";

function plural(n: number, one: string, many: string): string {
  return `${formatCount(n)} ${n === 1 ? one : many}`;
}

/** Endereço da página com uma análise aberta. */
export function analysisRoute(id: number): string {
  return `${ROUTES.profiles}?id=${id}`;
}

/** Análise pedida na busca da URL (?id=N), ou null se ausente ou inválida. */
export function analysisIdFromSearch(search: string): number | null {
  const raw = new URLSearchParams(search).get("id");
  if (raw === null || !/^\d+$/.test(raw)) return null;
  const id = Number(raw);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

/** Download do ZIP: GET da mesma origem, usado direto em `<a href>`. */
export function analysisReportUrl(id: number): string {
  return `/api/profile-analyses/${id}/report`;
}

export interface LineCount {
  profiles: number;
  invalid: number;
  duplicates: number;
}

/** Contagem local da lista colada, uma entrada por linha. Só informa a tela: quem decide é o backend. */
export function countProfileLines(text: string): LineCount {
  const seen = new Set<string>();
  let invalid = 0;
  let duplicates = 0;
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    // O backend aceita `http://` do Instagram nesta lista; a tela conta igual.
    const parsed = parseProfileInput(line.trim().replace(/^http:\/\//i, "https://"));
    if (!parsed.ok) invalid += 1;
    else if (seen.has(parsed.username)) duplicates += 1;
    else seen.add(parsed.username);
  }
  return { profiles: seen.size, invalid, duplicates };
}

/** Tira de cada link o que vem depois de `?` ou `#`, que o backend descartaria: links de compartilhamento são longos e
 *  10.000 deles passariam do limite de 1 MB do corpo. Linha que não é link segue igual. */
export function compactProfileText(text: string): string {
  return text
    .split(/\r?\n/)
    .map((line) => (line.includes("instagram.com/") ? line.replace(/[?#].*$/, "") : line))
    .join("\n");
}

/** Resultado de "Limpar histórico" para o aviso da tela. */
export function clearedText({ deleted, kept_open }: { deleted: number; kept_open: number }): string {
  if (deleted === 0) return "Nenhuma análise encerrada para apagar.";
  const done = deleted === 1 ? "1 análise apagada." : `${formatCount(deleted)} análises apagadas.`;
  return kept_open > 0 ? `${done} A análise em andamento foi mantida.` : done;
}

/** "12 perfis · 2 linhas inválidas · 1 duplicata". */
export function lineCountText({ profiles, invalid, duplicates }: LineCount): string {
  const parts = [plural(profiles, "perfil", "perfis")];
  if (invalid > 0) parts.push(plural(invalid, "linha inválida", "linhas inválidas"));
  if (duplicates > 0) parts.push(plural(duplicates, "duplicata", "duplicatas"));
  return parts.join(" · ");
}

/** Aviso do contador quando a lista passa do teto; null dentro dele. */
export function overLimitText({ profiles }: LineCount): string | null {
  if (profiles <= MAX_PROFILES) return null;
  return `acima do limite de ${formatCount(MAX_PROFILES)} por análise`;
}

/** O texto padrão de `too_many_profiles` fala do teto da importação, que é outro. */
export function startErrorText(err: unknown): string {
  if (err instanceof ApiError && err.code === "payload_too_large") {
    return "A lista passa do tamanho máximo de 1 MB. Divida-a em duas análises.";
  }
  if (err instanceof ApiError && err.code === "too_many_profiles") {
    return `Perfis demais de uma vez. O máximo é ${formatCount(MAX_PROFILES)} por análise.`;
  }
  return messageOf(err);
}

export const ANALYSIS_STATUS_LABEL: Record<ProfileAnalysisStatus, string> = {
  pending: "Na fila",
  running: "Executando",
  waiting: "Pausada",
  completed: "Concluída",
  cancelled: "Cancelada",
  interrupted: "Interrompida",
};

const ANALYSIS_STATUS_TONE: Record<ProfileAnalysisStatus, Tone> = {
  pending: "muted",
  running: "accent",
  waiting: "warn",
  completed: "ok",
  cancelled: "muted",
  interrupted: "warn",
};

export function analysisTone(status: ProfileAnalysisStatus): Tone {
  return ANALYSIS_STATUS_TONE[status];
}

/** Ainda há trabalho por vir: é o que mantém o polling ligado. */
export function isAnalysisActive(status: ProfileAnalysisStatus): boolean {
  return status === "pending" || status === "running" || status === "waiting";
}

/** 2 s enquanto ativa; sem atividade o polling para (null). */
export function analysisPollMs(current: number | null, active: boolean): number | null {
  return active ? nextPollMs(current, true) : null;
}

/** Perfis com status confirmado: tudo que não é Não verificado nem cancelado. */
export function confirmedCount(summary: ProfileAnalysisSummary): number {
  return summary.active + summary.private + summary.restricted + summary.unavailable;
}

export function progressFraction(a: Pick<ProfileAnalysis, "done" | "total">): number {
  return a.total > 0 ? a.done / a.total : 0;
}

export interface Indicator {
  label: string;
  value: number;
  title?: string;
}

/** Total, Concluídos, Pendentes e Com erro: os três últimos somam o Total. */
export function analysisIndicators(a: Pick<ProfileAnalysis, "done" | "total" | "summary">): Indicator[] {
  return [
    { label: "Total", value: a.total },
    { label: "Concluídos", value: a.done - a.summary.unverified },
    { label: "Pendentes", value: a.total - a.done },
    { label: "Com erro", value: a.summary.unverified, title: "Não verificados" },
  ];
}

/** "12 de 300". */
export function progressText(a: Pick<ProfileAnalysis, "done" | "total">): string {
  return `${formatCount(a.done)} de ${formatCount(a.total)}`;
}

/** Tempo restante em texto; null quando não há estimativa honesta (encerrada ou parada em espera). */
export function estimateText(
  a: Pick<ProfileAnalysis, "status" | "done" | "total"> & Partial<Pick<ProfileAnalysis, "cautious_pace">>,
): string | null {
  if (a.status !== "pending" && a.status !== "running") return null;
  const remaining = a.total - a.done;
  if (remaining <= 0) return null;
  const seconds = remaining * (a.cautious_pace === false ? FAST_SECONDS_PER_PROFILE : SECONDS_PER_PROFILE);
  if (seconds < 60) return "menos de 1 min";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `cerca de ${minutes} min`;
  if (minutes > 24 * 60) {
    const totalHours = Math.round(minutes / 60);
    const days = plural(Math.floor(totalHours / 24), "dia", "dias");
    return totalHours % 24 === 0 ? `cerca de ${days}` : `cerca de ${days} e ${totalHours % 24} h`;
  }
  const rest = minutes % 60;
  const hours = Math.floor(minutes / 60);
  return rest === 0 ? `cerca de ${hours} h` : `cerca de ${hours} h ${rest} min`;
}

/** Página atual (base 1) e total de páginas de uma lista paginada por `offset`. */
export function pageInfo(start: number, size: number, total: number): { page: number; pages: number } {
  return { page: Math.floor(start / size) + 1, pages: Math.max(1, Math.ceil(total / size)) };
}

/** Offset de uma página, trazida para dentro da lista. */
export function pageOffset(page: number, size: number, total: number): number {
  return (Math.min(Math.max(1, page), pageInfo(0, size, total).pages) - 1) * size;
}

/** Número digitado no campo de página; null se não for inteiro. */
export function parsePage(raw: string): number | null {
  return /^\d+$/.test(raw.trim()) ? Number(raw.trim()) : null;
}

/** Aviso da espera; null fora de `waiting`. */
export function waitingText(a: Pick<ProfileAnalysis, "status" | "wait_reason" | "waiting_until">): string | null {
  if (a.status !== "waiting") return null;
  const resume = a.waiting_until ? `Retomando às ${formatClock(a.waiting_until)}.` : "Retomando em breve.";
  switch (a.wait_reason) {
    case "instagram_pause":
      return "O Instagram está em pausa no Media Tool. A análise continua quando a pausa terminar.";
    case "rate_limited":
      return `O Instagram limitou as consultas. ${resume}`;
    case "too_many_failures":
      return `Várias consultas seguidas falharam. ${resume}`;
    default:
      return `Análise em espera. ${resume}`;
  }
}

/** Motivo do fim fora do normal; null quando não há o que avisar. */
export function endNoticeText(a: Pick<ProfileAnalysis, "status" | "error">): string | null {
  return a.error ?? (a.status === "interrupted" ? INTERRUPTED_TEXT : null);
}

/** "2 linhas inválidas ignoradas · 1 duplicata ignorada"; null se nada foi ignorado. */
export function ignoredText(a: Pick<ProfileAnalysis, "invalid_count" | "duplicate_count">): string | null {
  const parts: string[] = [];
  if (a.invalid_count > 0) {
    parts.push(plural(a.invalid_count, "linha inválida ignorada", "linhas inválidas ignoradas"));
  }
  if (a.duplicate_count > 0) parts.push(plural(a.duplicate_count, "duplicata ignorada", "duplicatas ignoradas"));
  return parts.length > 0 ? parts.join(" · ") : null;
}

/** Conteúdo de uma célula. Com `tone` sai como pill; sem ele, como texto. */
export interface Cell {
  text: string;
  tone?: Tone;
  title?: string;
}

/** Célula sem dado que se aplique (perfil ainda não lido, cancelado ou indisponível). */
export const EMPTY_CELL: Cell = { text: "—" };

export interface ItemCells {
  posts: Cell;
  views: Cell;
  stories: Cell;
  highlights: Cell;
  status: Cell;
  date: Cell;
  time: Cell;
}

const UNVERIFIED_CELL: Cell = { text: "Não verificado", tone: "warn" };

/** Só `yes` e `no` afirmam algo; todo o resto é Não verificado. */
export function checkCell(value: ProfileCheck | null): Cell {
  if (value === "yes") return { text: "Sim", tone: "ok" };
  if (value === "no") return { text: "Não", tone: "muted" };
  return UNVERIFIED_CELL;
}

export function postsCell(item: ProfileAnalysisItem): Cell {
  if (item.posts_state === "unverified" || item.posts_count === null) return UNVERIFIED_CELL;
  if (item.posts_exact) return { text: formatCount(item.posts_count) };
  return {
    text: `≈ ${item.posts_text ?? formatCount(item.posts_count)}`,
    title: "Valor arredondado pelo Instagram",
  };
}

export function viewsCell(item: ProfileAnalysisItem): Cell {
  if (item.views_state === "none") return { text: "Sem Reels", tone: "muted" };
  if (item.views_state !== "known" || item.views_total === null) return UNVERIFIED_CELL;
  const reels = item.views_reels ?? 0;
  return {
    text: formatCount(item.views_total),
    title: reels === 1 ? "Soma do último Reel lido" : `Soma dos últimos ${formatCount(reels)} Reels lidos`,
  };
}

function errorText(item: ProfileAnalysisItem): string | undefined {
  if (!item.error_code && !item.error_message) return undefined;
  // O texto genérico de `internal_error` fala do backend; aqui vale o que aconteceu com este perfil.
  if (item.error_code === "internal_error") {
    return item.error_message || "Erro inesperado ao consultar este perfil. O resultado não pôde ser confirmado.";
  }
  return describeErrorCode(item.error_code, item.error_message);
}

function statusCell(item: ProfileAnalysisItem): Cell {
  const title = errorText(item);
  switch (item.account_status) {
    case "active":
      return { text: "Ativa", tone: "ok", title };
    case "private":
      return { text: "Privada", tone: "accent", title };
    case "restricted":
      return { text: "Restrita", tone: "accent", title };
    case "unavailable":
      return { text: "Indisponível", tone: "err", title: title ?? UNAVAILABLE_TEXT };
    default:
      return { ...UNVERIFIED_CELL, title: title ?? UNVERIFIED_TEXT };
  }
}

function dashes(status: Cell): ItemCells {
  return {
    posts: EMPTY_CELL,
    views: EMPTY_CELL,
    stories: EMPTY_CELL,
    highlights: EMPTY_CELL,
    status,
    date: EMPTY_CELL,
    time: EMPTY_CELL,
  };
}

/** Uma linha da tabela. Perfil ainda não lido mostra o progresso em Status e "—" no resto, nunca um resultado. */
export function itemCells(item: ProfileAnalysisItem): ItemCells {
  const title = errorText(item);
  if (item.state === "pending") return dashes({ text: "Na fila", tone: "muted", title });
  if (item.state === "running") return dashes({ text: "Consultando", tone: "accent", title });
  if (item.state === "cancelled") {
    return dashes({ text: item.error_code ? "Não consultado" : "Cancelado", tone: "muted", title });
  }
  const checked = {
    date: item.checked_date ? { text: item.checked_date } : EMPTY_CELL,
    time: item.checked_time ? { text: item.checked_time } : EMPTY_CELL,
  };
  if (item.account_status === "unavailable") return { ...dashes(statusCell(item)), ...checked };
  return {
    posts: postsCell(item),
    views: viewsCell(item),
    stories: checkCell(item.stories),
    highlights: checkCell(item.highlights),
    status: statusCell(item),
    ...checked,
  };
}
