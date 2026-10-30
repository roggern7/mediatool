// Importação de vários perfis (grupo): rótulos por perfil, agregados do cabeçalho e lista de recentes.
// Contrato: docs/CONTRACTS.md, seção 8.1.

import { describeErrorCode } from "../api/errors";
import type {
  BatchProgress,
  BatchStatus,
  ContentType,
  GroupChildState,
  GroupProfiles,
  ViewsFilter,
} from "../api/types";
import { type BatchStateLike, isBatchPaused, listingDropped, rateLimitRemainingText } from "./batchState";
import { formatCount } from "./format";
import { profilesCountText } from "./importOptions";
import { CONTENT_LABEL, RATE_LIMIT_WAIT_LABEL, type Tone } from "./labels";
import { formatPauseTime } from "./pause";
import { viewsFilterText } from "./viewsFilter";

/** Campos de um lote filho usados aqui. */
export interface GroupChildLike extends BatchStateLike {
  status: BatchStatus;
  process?: boolean;
  requested_limit: number | null;
  progress: Pick<BatchProgress, "found" | "downloaded" | "failed"> & { skipped?: number };
  listing_end?: "profile_end" | "limit" | "period_end" | "empty_first_page" | null;
  group_state?: GroupChildState | null;
}

/** Estado geral do filho: `group_state` da API; sem ele (resposta antiga), deriva de status/listing_state. */
export function childState(b: GroupChildLike): GroupChildState {
  if (b.group_state) return b.group_state;
  if (b.status === "completed" || b.status === "failed" || b.status === "cancelled") return b.status;
  return b.listing_state === "needs_attention" ? "attention" : "active";
}

/** Rótulo e tom do estado de um perfil do grupo (espera, pausa e fila não usam tom de erro). */
export function profileStateLabel(b: GroupChildLike, now: number): { label: string; tone: Tone } {
  switch (childState(b)) {
    case "waiting":
      return { label: "Aguardando", tone: "muted" };
    case "attention":
      return { label: "Atenção", tone: "warn" };
    case "completed":
      return b.progress.failed > 0
        ? { label: "Concluído com erros", tone: "warn" }
        : { label: "Concluído", tone: "ok" };
    case "failed":
      return { label: "Falhou", tone: "err" };
    case "cancelled":
      return { label: "Cancelado", tone: "muted" };
    default:
      break;
  }
  if (isBatchPaused(b, now) || b.listing_state === "paused") return { label: "Pausado", tone: "warn" };
  if (b.listing_state === "waiting_rate_limit") return { label: RATE_LIMIT_WAIT_LABEL, tone: "accent" };
  if (b.listing_state === "waiting_backpressure") return { label: "Aguardando downloads", tone: "accent" };
  if (b.status === "listing") return { label: "Listando", tone: "accent" };
  return { label: b.process ? "Baixando e processando" : "Baixando", tone: "accent" };
}

function validTime(iso: string | null | undefined): iso is string {
  return typeof iso === "string" && Number.isFinite(Date.parse(iso));
}

/** Linha de apoio do estado: horário de retomada, motivo da atenção/falha ou posição na fila; null se não há. */
export function profileStateDetail(b: GroupChildLike, now: number): string | null {
  const state = childState(b);
  if (state === "waiting") return "na fila";
  if (state === "attention" || state === "failed") {
    return b.error_code || b.error_message ? describeErrorCode(b.error_code ?? null, b.error_message ?? null) : null;
  }
  if (state !== "active") return null;
  if (isBatchPaused(b, now)) {
    return validTime(b.paused_until)
      ? `retomada automática prevista às ${formatPauseTime(b.paused_until, now)}`
      : "retoma automaticamente";
  }
  if (b.listing_state === "paused") return "tenta de novo sozinho";
  if (b.listing_state === "waiting_rate_limit") {
    const at = b.rate_limit_resume_at;
    return validTime(at)
      ? `retoma às ${formatPauseTime(at, now)} (${rateLimitRemainingText(at, now)})`
      : "retoma automaticamente";
  }
  if (b.listing_state === "waiting_backpressure") return "a listagem espera a fila de downloads esvaziar";
  return null;
}

/** Lote que terminou porque a 1a página do perfil veio vazia e sem próxima (listing_end "empty_first_page"). */
export const EMPTY_FIRST_PAGE_TEXT =
  "o Instagram não devolveu nenhuma publicação deste perfil agora (perfil sem posts, privado ou limite temporário); " +
  "tente de novo mais tarde";

/** "perfil chegou ao fim: 37 de 100" quando o perfil acabou antes da quantidade pedida; null nos demais casos. */
export function profileEndText(b: GroupChildLike): string | null {
  if (b.listing_end !== "profile_end" || b.requested_limit === null) return null;
  if (b.progress.found >= b.requested_limit) return null;
  return `perfil chegou ao fim: ${formatCount(b.progress.found)} de ${formatCount(b.requested_limit)}`;
}

function existingText(n: number): string {
  return `${formatCount(n)} já ${n === 1 ? "existente" : "existentes"}`;
}

function mediaCountText(n: number): string {
  return `${formatCount(n)} ${n === 1 ? "mídia" : "mídias"}`;
}

/**
 * Quantidade do perfil na linha do grupo: "1.240 mídias" (concluído), "830 / 2.000" (concluídas / pedido, ou
 * / encontradas quando é "Todos" ou a listagem já terminou), "perfil chegou ao fim: 37 de 100", mais já
 * existentes, erros e descartes de listagem de forma compacta. Vazio para perfil ainda na fila.
 * "Concluídas" = baixadas agora + já existentes na Biblioteca (`skipped`); itens com erro não entram.
 */
export function profileProgressText(b: GroupChildLike): string {
  const state = childState(b);
  if (state === "waiting") return "";
  const p = b.progress;
  const parts: string[] = [];
  const end = profileEndText(b);
  const existing = Math.max(0, p.skipped ?? 0);
  const done = p.downloaded + existing;
  if (state === "completed" && b.listing_end === "empty_first_page") {
    parts.push(EMPTY_FIRST_PAGE_TEXT);
  } else if (state === "completed") {
    parts.push(end ?? mediaCountText(done));
  } else {
    const target = b.status === "listing" ? (b.requested_limit ?? p.found) : p.found;
    if (target > 0 || done > 0) parts.push(`${formatCount(done)} / ${formatCount(target)}`);
    if (end) parts.push(end);
  }
  if (existing > 0) parts.push(existingText(existing));
  if (p.failed > 0) parts.push(`${formatCount(p.failed)} com erro`);
  const dropped = listingDropped(b);
  if (dropped) {
    parts.push(
      `${dropped.atLeast ? "pelo menos " : ""}${formatCount(dropped.total)} ` +
        `${dropped.total === 1 ? "descartada" : "descartadas"} na listagem`,
    );
  }
  return parts.join(" · ");
}

/** Campos do grupo usados aqui. */
export interface GroupLike {
  content_type?: ContentType | null;
  requested_limit: number | null;
  preset_name?: string | null;
  process: boolean;
  status: "active" | "attention" | "completed" | "cancelled";
  attention: boolean;
  profiles: GroupProfiles;
  progress: Pick<BatchProgress, "found" | "downloaded" | "processed"> & { skipped?: number };
  listing_dropped_total: number;
  views_filter?: ViewsFilter | null;
}

/** "Importação • 37 perfis". */
export function groupTitle(g: Pick<GroupLike, "profiles">): string {
  return `Importação • ${profilesCountText(g.profiles.total)}`;
}

export function groupStatusLabel(g: Pick<GroupLike, "status" | "attention">): { label: string; tone: Tone } {
  switch (g.status) {
    case "attention":
      return { label: "Precisa de atenção", tone: "warn" };
    case "completed":
      return g.attention ? { label: "Concluído com pendências", tone: "warn" } : { label: "Concluído", tone: "ok" };
    case "cancelled":
      return { label: "Cancelado", tone: "muted" };
    default:
      return { label: "Em andamento", tone: "accent" };
  }
}

/** Perfis por estado, só os que existem: ["12 concluídos", "1 ativo", "20 aguardando", "2 com atenção", ...]. */
export function groupProfileCounts(profiles: GroupProfiles): string[] {
  const n = formatCount;
  const parts: string[] = [];
  if (profiles.completed > 0) parts.push(`${n(profiles.completed)} ${profiles.completed === 1 ? "concluído" : "concluídos"}`);
  if (profiles.active > 0) parts.push(`${n(profiles.active)} ${profiles.active === 1 ? "ativo" : "ativos"}`);
  if (profiles.waiting > 0) parts.push(`${n(profiles.waiting)} aguardando`);
  if (profiles.attention > 0) parts.push(`${n(profiles.attention)} com atenção`);
  if (profiles.failed > 0) parts.push(`${n(profiles.failed)} ${profiles.failed === 1 ? "falhou" : "falharam"}`);
  if (profiles.cancelled > 0) parts.push(`${n(profiles.cancelled)} ${profiles.cancelled === 1 ? "cancelado" : "cancelados"}`);
  return parts;
}

/** "15.300 de 20.000 mídias baixadas · 300 já existentes" (soma dos perfis); null enquanto nada foi encontrado. */
export function groupMediaText(g: Pick<GroupLike, "progress" | "process">): string | null {
  const p = g.progress;
  if (p.found <= 0) return null;
  const downloaded = `${formatCount(p.downloaded)} de ${formatCount(p.found)} ${p.found === 1 ? "mídia baixada" : "mídias baixadas"}`;
  const existing = Math.max(0, p.skipped ?? 0);
  const base = existing > 0 ? `${downloaded} · ${existingText(existing)}` : downloaded;
  return g.process ? `${base} · ${formatCount(p.processed)} ${p.processed === 1 ? "processada" : "processadas"}` : base;
}

/** "3 mídias descartadas durante a listagem" (soma dos perfis); null sem descarte. */
export function groupDroppedText(g: Pick<GroupLike, "listing_dropped_total">): string | null {
  const total = g.listing_dropped_total;
  if (!(total > 0)) return null;
  return `${mediaCountText(total)} ${total === 1 ? "descartada" : "descartadas"} durante a listagem`;
}

/** Resumo do grupo em uma linha: perfis por estado, mídias baixadas e descartes (se houver). */
export function groupSummary(g: GroupLike): string {
  return [...groupProfileCounts(g.profiles), groupMediaText(g), groupDroppedText(g)]
    .filter((part): part is string => part !== null)
    .join(" • ");
}

/** Linha do filtro de visualizações do grupo (soma dos perfis, com os baixados do grupo); null sem filtro. */
export function groupViewsText(g: Pick<GroupLike, "views_filter" | "progress">): string | null {
  return viewsFilterText(g.views_filter, g.progress.downloaded);
}

/** Opções do grupo: "Reels · Últimos 1.000 por perfil · Somente originais". */
export function groupOptionsText(g: GroupLike): string {
  return [
    g.content_type ? CONTENT_LABEL[g.content_type] : null,
    g.requested_limit === null ? "Todos" : `Últimos ${formatCount(g.requested_limit)} por perfil`,
    g.process && g.preset_name ? g.preset_name : g.process ? "Com processamento" : "Somente originais",
  ]
    .filter(Boolean)
    .join(" · ");
}

/**
 * Aviso do cabeçalho quando algum perfil está em atenção ou falhou (`attention: true`), inclusive com o grupo
 * ainda ativo ou já concluído. null quando não há pendência.
 */
export function groupAttentionText(g: Pick<GroupLike, "status" | "attention" | "profiles">): string | null {
  if (!g.attention) return null;
  const { attention, failed } = g.profiles;
  const parts: string[] = [];
  if (attention > 0) {
    parts.push(attention === 1 ? "1 perfil precisa de atenção" : `${formatCount(attention)} perfis precisam de atenção`);
  }
  if (failed > 0) parts.push(failed === 1 ? "1 perfil falhou" : `${formatCount(failed)} perfis falharam`);
  if (parts.length === 0) return null;
  const head = g.status === "completed" ? "Importação concluída com pendências: " : "";
  const body = parts.join(" e ");
  const tail = g.status === "completed" ? "" : " Os demais perfis seguem normalmente.";
  return `${head}${head ? body : body.charAt(0).toUpperCase() + body.slice(1)}.${tail}`;
}

export interface SkippedLike {
  profile: string;
  reason?: string | null;
}

const SKIPPED_LIST_MAX = 20;

/** Perfis pulados na criação do grupo, lidos com segurança de uma resposta ou do corpo de um erro. */
export function skippedFrom(value: unknown): SkippedLike[] {
  const list = value && typeof value === "object" ? (value as { skipped?: unknown }).skipped : null;
  if (!Array.isArray(list)) return [];
  const out: SkippedLike[] = [];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const { profile, reason } = item as { profile?: unknown; reason?: unknown };
    if (typeof profile === "string" && profile) out.push({ profile, reason: typeof reason === "string" ? reason : null });
  }
  return out;
}

/**
 * Aviso (não é erro) dos perfis pulados na criação: "2 perfis foram pulados porque já têm uma importação em
 * andamento: @a, @b." null quando nenhum foi pulado.
 */
export function skippedNotice(skipped: SkippedLike[] | null | undefined): string | null {
  if (!skipped || skipped.length === 0) return null;
  const n = skipped.length;
  const known = skipped.every((s) => s.reason === "profile_already_importing");
  const why = known
    ? n === 1
      ? "porque já tem uma importação em andamento"
      : "porque já têm uma importação em andamento"
    : n === 1
      ? "e não entrou nesta importação"
      : "e não entraram nesta importação";
  const names = skipped.slice(0, SKIPPED_LIST_MAX).map((s) => `@${s.profile}`).join(", ");
  const more = n > SKIPPED_LIST_MAX ? ` e mais ${formatCount(n - SKIPPED_LIST_MAX)}` : "";
  return `${n === 1 ? "1 perfil foi pulado" : `${formatCount(n)} perfis foram pulados`} ${why}: ${names}${more}.`;
}

export type RecentEntry<G, B> = { kind: "group"; group: G } | { kind: "batch"; batch: B };

/**
 * Lista de importações recentes: grupos (uma linha cada) e lotes avulsos/antigos (`?ungrouped=true`) juntos, do
 * mais novo para o mais antigo, no máximo `limit` linhas.
 */
export function mergeRecent<G extends { id: number; created_at: string }, B extends { id: number; created_at: string }>(
  groups: G[],
  batches: B[],
  limit: number,
): RecentEntry<G, B>[] {
  const entries: RecentEntry<G, B>[] = [
    ...groups.map((group) => ({ kind: "group" as const, group })),
    ...batches.map((batch) => ({ kind: "batch" as const, batch })),
  ];
  const time = (e: RecentEntry<G, B>) => {
    const t = Date.parse(e.kind === "group" ? e.group.created_at : e.batch.created_at);
    return Number.isFinite(t) ? t : 0;
  };
  const id = (e: RecentEntry<G, B>) => (e.kind === "group" ? e.group.id : e.batch.id);
  return entries.sort((a, b) => time(b) - time(a) || id(b) - id(a)).slice(0, Math.max(0, limit));
}
