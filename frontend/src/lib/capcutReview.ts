// Revisão das legendas (docs/CAPCUT.md, "Fidelidade das legendas"): frases, edições, corpo do POST e filtros.
// Funções puras, testadas em capcutReview.test.ts.

import type { CapCutJob, CapCutReviewApprove, CapCutReviewSegment, CapCutReviewWord } from "../api/types";
import { needsReview } from "./capcut";
import { formatCount } from "./format";

/** Uma frase na tela: o texto original (para saber se mudou) e o texto do campo. */
export interface ReviewRow {
  index: number;
  start: number;
  end: number;
  original: string;
  text: string;
  doubt: boolean;
  words: CapCutReviewWord[];
}

/** Espaços repetidos e nas pontas não contam como edição (o servidor compara do mesmo jeito). */
export function normalizeReviewText(text: string): string {
  return text.split(/\s+/).filter(Boolean).join(" ");
}

export function reviewRows(segments: readonly CapCutReviewSegment[]): ReviewRow[] {
  return segments.map((s) => ({
    index: s.index,
    start: s.start,
    end: s.end,
    original: s.text,
    text: s.text,
    doubt: s.doubt,
    words: s.words,
  }));
}

export function isRowEdited(row: Pick<ReviewRow, "original" | "text">): boolean {
  return normalizeReviewText(row.text) !== normalizeReviewText(row.original);
}

export function editedCount(rows: readonly ReviewRow[]): number {
  return rows.filter(isRowEdited).length;
}

/** Troca o texto de uma frase (pelo índice da frase). */
export function setRowText(rows: readonly ReviewRow[], index: number, text: string): ReviewRow[] {
  return rows.map((r) => (r.index === index ? { ...r, text } : r));
}

/** Corpo do POST: só as frases alteradas (texto vazio remove a frase); nenhuma = aprovar como está. */
export function approveBody(rows: readonly ReviewRow[]): CapCutReviewApprove {
  return { segments: rows.filter(isRowEdited).map((r) => ({ index: r.index, text: normalizeReviewText(r.text) })) };
}

/** Frases com trecho duvidoso. */
export function doubtCount(rows: readonly Pick<ReviewRow, "doubt">[]): number {
  return rows.filter((r) => r.doubt).length;
}

/** "Só trechos duvidosos" começa ligado quando há dúvidas. */
export function defaultOnlyDoubtful(rows: readonly Pick<ReviewRow, "doubt">[]): boolean {
  return doubtCount(rows) > 0;
}

/** Frases mostradas: com o filtro, as duvidosas (e as já editadas, para a edição não sumir da tela). */
export function visibleRows(rows: readonly ReviewRow[], onlyDoubtful: boolean): ReviewRow[] {
  return onlyDoubtful ? rows.filter((r) => r.doubt || isRowEdited(r)) : [...rows];
}

/** "1 trecho para conferir" / "3 trechos para conferir" / "Nenhum trecho duvidoso". */
export function reviewCountText(n: number): string {
  if (n <= 0) return "Nenhum trecho duvidoso";
  return `${formatCount(n)} ${n === 1 ? "trecho para conferir" : "trechos para conferir"}`;
}

/** "1 vídeo aguardando revisão" / "2 vídeos aguardando revisão". */
export function reviewWaitingText(n: number): string {
  return `${formatCount(n)} ${n === 1 ? "vídeo aguardando revisão" : "vídeos aguardando revisão"}`;
}

/** Tempo da frase: "0:04,2". */
export function formatReviewTime(seconds: number): string {
  const safe = Math.max(0, Number.isFinite(seconds) ? seconds : 0);
  const minutes = Math.floor(safe / 60);
  const rest = safe - minutes * 60;
  const tenths = Math.floor(rest * 10 + 1e-6);
  const whole = String(Math.floor(tenths / 10)).padStart(2, "0");
  return `${minutes}:${whole},${tenths % 10}`;
}

/** Índice da frase seguinte na lista visível (Enter); null na última. */
export function nextRowIndex(rows: readonly Pick<ReviewRow, "index">[], current: number): number | null {
  const at = rows.findIndex((r) => r.index === current);
  const next = at < 0 ? undefined : rows[at + 1];
  return next ? next.index : null;
}

/** Primeiro job da lista que espera a revisão ("Revisar próximo"). */
export function nextReviewJob<T extends Pick<CapCutJob, "id" | "status" | "needs_review">>(
  jobs: readonly T[],
  skipId?: number | null,
): T | null {
  return jobs.find((j) => needsReview(j) && j.id !== skipId) ?? null;
}
