// Filtro opcional de visualizações mínimas da importação por perfil (só vídeos: conteúdo Reels ou Vídeos; só o
// método web). Funções puras: a tela só liga os estados.

import type { ContentType, ViewsFilter } from "../api/types";
import { formatCount } from "./format";

/**
 * Interruptor ÚNICO do filtro de visualizações na interface (o backend tem o seu: import_service.MIN_VIEWS_ENABLED).
 * Desligado: o campo some da tela Importar e os contadores de visualizações não aparecem; o período continua.
 * A paginação da aba Reels sem login recebeu 429 no teste real (2026-10-29).
 */
export const VIEWS_FILTER_ENABLED = false;

/** O que a tela mostra de `views_filter`: com o filtro de visualizações desligado, só o período (ou nada). */
export function visibleFilter(vf: ViewsFilter | null | undefined): ViewsFilter | null {
  if (!vf) return null;
  if (VIEWS_FILTER_ENABLED) return vf;
  return typeof vf.posted_within_days === "number" ? { ...vf, min_views: null } : null;
}

/** Atalhos do seletor (o "Personalizado…" aceita qualquer inteiro de 1 a MAX_MIN_VIEWS). */
export const MIN_VIEWS_PRESETS = [100_000, 250_000, 500_000, 1_000_000] as const;
/** Teto de sanidade da API. */
export const MAX_MIN_VIEWS = 10_000_000_000;
/** Conteúdos em que o filtro vale (a API recusa os demais com 422). */
export const MIN_VIEWS_CONTENT: readonly ContentType[] = ["reels", "videos"];

/** Valor do seletor: "off" (Sem filtro), um atalho ("500000") ou "custom" (Personalizado…). */
export type MinViewsMode = "off" | "custom" | `${number}`;

export const MIN_VIEWS_DISABLED_HINT = "Escolha Reels para usar o filtro de visualizações (ele só vale para vídeos).";
export const MIN_VIEWS_HINT =
  "Vídeos abaixo do mínimo ou sem contagem de visualizações disponível são ignorados antes do download. Com o " +
  "filtro, “Últimos N” conta apenas os vídeos aprovados.";

export function minViewsAllowed(content: ContentType): boolean {
  return MIN_VIEWS_CONTENT.includes(content);
}

/** Texto digitado no mínimo personalizado (aceita "500000" e "500.000"); null se não for inteiro válido. */
export function parseMinViewsText(text: string): number | null {
  const t = text.trim();
  if (!/^\d+$/.test(t) && !/^\d{1,3}(\.\d{3})+$/.test(t)) return null;
  const n = Number(t.replace(/\./g, ""));
  return Number.isSafeInteger(n) && n >= 1 && n <= MAX_MIN_VIEWS ? n : null;
}

export type MinViewsChoice = { ok: true; minViews: number | null } | { ok: false; error: string };

/**
 * Mínimo enviado à API: null (sem filtro) quando desligado ou quando o conteúdo não é de vídeo (o seletor fica
 * desabilitado e o filtro nunca vai no corpo); o atalho ou o inteiro digitado no personalizado.
 */
export function resolveMinViews(content: ContentType, mode: MinViewsMode, text: string): MinViewsChoice {
  if (mode === "off" || !minViewsAllowed(content)) return { ok: true, minViews: null };
  if (mode !== "custom") return { ok: true, minViews: Number(mode) };
  const n = parseMinViewsText(text);
  if (n === null) return { ok: false, error: "Informe um número inteiro de visualizações a partir de 1." };
  return { ok: true, minViews: n };
}

/**
 * Linha do lote/grupo: "Mín. 500.000 visualizações: 42 analisados • 7 aprovados • 5 baixados • 30 abaixo do
 * mínimo • 5 sem contagem disponível". `downloaded` é a contagem já existente do lote (progress.downloaded);
 * analisados = aprovados + abaixo + sem contagem. Sem filtro, null (nada é exibido).
 */
export function viewsFilterText(vf: ViewsFilter | null | undefined, downloaded?: number | null): string | null {
  if (!vf) return null;
  const views = typeof vf.min_views === "number";
  const days = typeof vf.posted_within_days === "number" ? vf.posted_within_days : null;
  const parts = [
    `${formatCount(vf.analyzed)} ${vf.analyzed === 1 ? "analisado" : "analisados"}`,
    `${formatCount(vf.approved)} ${vf.approved === 1 ? "aprovado" : "aprovados"}`,
  ];
  if (typeof downloaded === "number") parts.push(`${formatCount(downloaded)} ${downloaded === 1 ? "baixado" : "baixados"}`);
  if (views) parts.push(`${formatCount(vf.below)} abaixo do mínimo`, `${formatCount(vf.no_count)} sem contagem disponível`);
  if (days !== null) parts.push(`${formatCount(vf.out_of_period ?? 0)} fora do período`);
  const labels = [
    ...(views ? [`Mín. ${formatCount(vf.min_views as number)} visualizações`] : []),
    ...(days !== null ? [periodSummary(days)] : []),
  ];
  return `${labels.join(" • ")}: ${parts.join(" • ")}`;
}

// ---------------------------------------------------------------- período de postagem

/** Atalhos do seletor de período ("custom" = N dias digitado, de 1 a MAX_PERIOD_DAYS). */
export const PERIOD_PRESETS = [7, 30, 60, 90] as const;
export const MAX_PERIOD_DAYS = 3650;
export type PeriodMode = "any" | "custom" | `${number}`;
export type PeriodChoice = { ok: true; days: number | null } | { ok: false; error: string };
export const PERIOD_HINT =
  "Só publicações postadas dentro do período são baixadas. A data vem da listagem do perfil; o corte é fixado ao " +
  "iniciar a importação.";

/** "Últimos 30 dias" / "Último dia". */
export function periodSummary(days: number): string {
  return days === 1 ? "Último dia" : `Últimos ${formatCount(days)} dias`;
}

/** Dias digitados no período personalizado (inteiro de 1 a 3650; aceita "1.000"); null se inválido. */
export function parsePeriodDays(text: string): number | null {
  const t = text.trim();
  if (!/^\d+$/.test(t) && !/^\d{1,3}(\.\d{3})+$/.test(t)) return null;
  const n = Number(t.replace(/\./g, ""));
  return Number.isSafeInteger(n) && n >= 1 && n <= MAX_PERIOD_DAYS ? n : null;
}

/** Dias enviados à API: null em "Qualquer data"; o atalho; ou o inteiro digitado no personalizado. */
export function resolvePeriod(mode: PeriodMode, text: string): PeriodChoice {
  if (mode === "any") return { ok: true, days: null };
  if (mode !== "custom") return { ok: true, days: Number(mode) };
  const n = parsePeriodDays(text);
  if (n === null) return { ok: false, error: `Informe um número inteiro de dias de 1 a ${formatCount(MAX_PERIOD_DAYS)}.` };
  return { ok: true, days: n };
}
