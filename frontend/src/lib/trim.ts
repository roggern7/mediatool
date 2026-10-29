import type { EditTrim } from "../api/types";
import { formatSeconds } from "./format";

// Recorte manual de um vídeo (editor de recorte da página Editar): tempos, limites, passo de quadro e régua.
// Tempos em segundos da linha do tempo do original. O recorte é [início, fim): o fim é exclusivo (o quadro que
// começa no fim já fica de fora) e a duração do trecho é fim - início.

export interface Trim {
  start: number;
  end: number;
}

/** Duração mínima do recorte (mesmo mínimo do backend). */
export const MIN_TRIM = 0.1;
/** Passo de quadro quando o fps da mídia é desconhecido. */
export const DEFAULT_FPS = 30;
export const ZOOM_LEVELS = [1, 2, 4, 8, 16] as const;
export const FILMSTRIP_MIN = 10;
export const FILMSTRIP_MAX = 20;
export const TIMECODE_INVALID_TEXT = "Use mm:ss.mmm (ex.: 01:05.250) ou segundos (ex.: 65,25).";

const EPS = 1e-6;

/** fps válido da mídia, ou null. */
export function knownFps(fps: number | null | undefined): number | null {
  return fps !== null && fps !== undefined && Number.isFinite(fps) && fps > 0 && fps <= 1000 ? fps : null;
}

/** Duração de um quadro (s): 1/fps da mídia, ou 1/30 s quando desconhecido. */
export function frameDuration(fps: number | null | undefined): number {
  return 1 / (knownFps(fps) ?? DEFAULT_FPS);
}

export function clampTime(t: number, duration: number): number {
  if (!Number.isFinite(t)) return 0;
  return Math.min(Math.max(t, 0), Math.max(duration, 0));
}

/** Milissegundo para baixo (com folga de ponto flutuante: 0,29 vira 0,29 e não 0,289). */
export function floorMs(t: number): number {
  return Math.floor(t * 1000 + EPS) / 1000;
}

export function roundMs(t: number): number {
  return Math.round(t * 1000) / 1000;
}

/** Leva ao início de quadro mais próximo (fps conhecido) ou ao milissegundo; sempre dentro de [0, duração]. */
export function snapTime(t: number, fps: number | null | undefined, duration: number): number {
  const rate = knownFps(fps);
  const snapped = rate ? Math.round(t * rate) / rate : roundMs(t);
  return clampTime(snapped, duration);
}

/** Início do quadro que está na tela no instante `t` (o vídeo tocando mostra o quadro que começou antes de `t`). */
export function frameStartAt(t: number, fps: number | null | undefined, duration: number): number {
  const rate = knownFps(fps);
  return clampTime(rate ? Math.floor(t * rate + EPS) / rate : floorMs(t), duration);
}

/** Um quadro para trás (-1) ou para frente (+1), na grade de quadros quando o fps é conhecido. */
export function stepFrame(t: number, direction: number, fps: number | null | undefined, duration: number): number {
  const rate = knownFps(fps);
  if (rate) return clampTime((Math.round(t * rate) + direction) / rate, duration);
  return clampTime(roundMs(t + direction / DEFAULT_FPS), duration);
}

/** Pula `delta` segundos (ex.: ±1 s), mantendo a grade de quadros. */
export function stepSeconds(t: number, delta: number, fps: number | null | undefined, duration: number): number {
  return snapTime(t + delta, fps, duration);
}

/**
 * Instante para onde o `<video>` é levado para mostrar o quadro certo, longe da borda entre dois quadros (o navegador
 * mostra o quadro que começou antes do instante pedido):
 * - `at`: o quadro na tela na agulha;
 * - `start`: o primeiro quadro que fica no recorte (começa no início ou logo depois);
 * - `end`: o último quadro que fica no recorte (começa antes do fim).
 */
export function previewTime(
  t: number,
  fps: number | null | undefined,
  duration: number,
  mode: "at" | "start" | "end" = "at",
): number {
  const rate = knownFps(fps);
  const last = Math.max(duration - 0.001, 0);
  if (!rate) return Math.min(clampTime(mode === "end" ? t - 0.001 : t, duration), last);
  let frame: number;
  if (mode === "start") frame = Math.ceil(t * rate - EPS);
  else if (mode === "end") frame = Math.ceil(t * rate - EPS) - 1;
  else frame = Math.floor(t * rate + EPS);
  return Math.min(clampTime((Math.max(frame, 0) + 0.5) / rate, duration), last);
}

/** Recorte inteiro (sem corte): de 0 até a duração. */
export function fullTrim(duration: number): Trim {
  return { start: 0, end: Math.max(duration, 0) };
}

/** Recorte que não corta nada (começa no 0 e termina na duração, com folga de 1 ms). */
export function isFullTrim(trim: Trim, duration: number): boolean {
  return trim.start <= 0.0005 && trim.end >= duration - 0.0005;
}

/** Recorte válido para a duração conhecida: preso a [0, duração] e com pelo menos MIN_TRIM. */
export function normalizeTrim(trim: Trim, duration: number): Trim {
  if (duration <= MIN_TRIM) return fullTrim(duration);
  const end = clampTime(Math.max(trim.end, MIN_TRIM), duration);
  const start = clampTime(Math.min(trim.start, end - MIN_TRIM), duration);
  return { start, end };
}

/** Novo início: nunca passa do fim - 0,1 s nem fica antes de 0 (os puxadores não se cruzam). */
export function setTrimStart(trim: Trim, value: number, duration: number): Trim {
  const start = Math.min(clampTime(value, duration), Math.max(trim.end - MIN_TRIM, 0));
  return { start, end: trim.end };
}

/** Novo fim: nunca antes do início + 0,1 s nem depois da duração. */
export function setTrimEnd(trim: Trim, value: number, duration: number): Trim {
  const end = Math.max(clampTime(value, duration), Math.min(trim.start + MIN_TRIM, duration));
  return { start: trim.start, end };
}

export function trimLength(trim: Trim): number {
  return Math.max(trim.end - trim.start, 0);
}

/** "mm:ss.mmm" (ou "h:mm:ss.mmm" a partir de 1 hora), milissegundo para baixo. */
export function formatTimecode(seconds: number): string {
  const totalMs = Math.max(0, Math.floor((Number.isFinite(seconds) ? seconds : 0) * 1000 + EPS));
  const ms = totalMs % 1000;
  const totalSeconds = Math.floor(totalMs / 1000);
  const s = totalSeconds % 60;
  const m = Math.floor(totalSeconds / 60) % 60;
  const h = Math.floor(totalSeconds / 3600);
  const tail = `${String(s).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${tail}` : `${String(m).padStart(2, "0")}:${tail}`;
}

/**
 * Tempo digitado -> segundos (milissegundo), ou null. Aceita "mm:ss.mmm", "m:ss", "h:mm:ss.mmm" e só segundos
 * ("65,25" ou "65.25"); vírgula ou ponto decimal; com minutos, os segundos ficam abaixo de 60.
 */
export function parseTimecode(text: string): number | null {
  const parts = text.trim().replace(/s$/i, "").trim().split(":");
  if (parts.length > 3 || parts.some((p) => p.trim() === "")) return null;
  const secText = (parts[parts.length - 1] ?? "").trim().replace(",", ".");
  if (!/^\d+(\.\d+)?$|^\.\d+$/.test(secText)) return null;
  const seconds = Number(secText);
  const units = parts.slice(0, -1).map((p) => p.trim());
  if (units.some((u) => !/^\d+$/.test(u))) return null;
  if (units.length > 0 && seconds >= 60) return null;
  if (units.length === 2 && Number(units[1]) >= 60) return null;
  const hours = units.length === 2 ? Number(units[0]) : 0;
  const minutes = units.length > 0 ? Number(units[units.length - 1]) : 0;
  const total = hours * 3600 + minutes * 60 + seconds;
  return Number.isFinite(total) ? roundMs(total) : null;
}

/** Duração curta com vírgula ("9,55 s"). */
export function formatTrimLength(seconds: number): string {
  return formatSeconds(Math.round(seconds * 100) / 100);
}

/** "Recorte 00:03.250 → 00:12.800 (9,55 s)". */
export function trimLabel(trim: Trim): string {
  return `Recorte ${formatTimecode(trim.start)} → ${formatTimecode(trim.end)} (${formatTrimLength(trimLength(trim))})`;
}

/** Item de `trims` em POST /api/edits: milissegundo para baixo (o início não passa do quadro marcado e o fim não
 * inclui o quadro seguinte). */
export function toTrimRequest(mediaId: number, trim: Trim): EditTrim {
  return { media_id: mediaId, start: floorMs(trim.start), end: floorMs(trim.end) };
}

/** Posição (0..1) de um tempo na linha do tempo, e o inverso. */
export function timeToFraction(t: number, duration: number): number {
  return duration > 0 ? Math.min(Math.max(t / duration, 0), 1) : 0;
}

export function fractionToTime(fraction: number, duration: number): number {
  return clampTime(fraction * duration, duration);
}

const RULER_STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];

/** Intervalo entre marcas com rótulo: o menor que deixa pelo menos `minPx` pixels entre elas. */
export function rulerStep(duration: number, widthPx: number, minPx = 80): number {
  if (duration <= 0 || widthPx <= 0) return 1;
  const pxPerSecond = widthPx / duration;
  return RULER_STEPS.find((step) => step * pxPerSecond >= minPx) ?? 3600;
}

/** Marcas da régua: tempos múltiplos de `step` dentro de [0, duração]. */
export function rulerTicks(duration: number, step: number): number[] {
  if (duration <= 0 || step <= 0) return [];
  const ticks: number[] = [];
  for (let i = 0; i * step <= duration + EPS && ticks.length < 5000; i += 1) ticks.push(Number((i * step).toFixed(3)));
  return ticks;
}

/** Rótulo curto da régua: "0:05", "1:30" ou, com passo abaixo de 1 s, "0:05.5". */
export function rulerLabel(t: number, step: number): string {
  const decimals = step >= 1 ? 0 : 1;
  const totalSeconds = Math.floor(t + EPS);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  const base = `${m}:${String(s).padStart(2, "0")}`;
  if (decimals === 0) return base;
  const frac = Math.round((t - totalSeconds) * 10 ** decimals);
  return `${base}.${String(Math.min(frac, 10 ** decimals - 1)).padStart(decimals, "0")}`;
}

/** Quantos quadros na tira de miniaturas para a largura da linha do tempo (10 a 20). */
export function filmstripCount(widthPx: number): number {
  return Math.min(FILMSTRIP_MAX, Math.max(FILMSTRIP_MIN, Math.round(widthPx / 90)));
}

/** Instantes das miniaturas: o meio de cada fatia da linha do tempo. */
export function filmstripTimes(duration: number, count: number): number[] {
  if (duration <= 0 || count <= 0) return [];
  return Array.from({ length: count }, (_, i) => ((i + 0.5) * duration) / count);
}
