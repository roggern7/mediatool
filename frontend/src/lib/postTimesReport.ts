import type { Tone } from "./labels";
import type { PostTimesItem, PostTimesSegmentKind } from "../api/types";
import { ROUTES } from "./router";

export type PostTimesTarget = { kind: "batch" | "group"; id: number };

/** Endereço da página "Padrões de publicação" do lote ou do grupo. */
export function postTimesPageUrl(target: PostTimesTarget): string {
  return `${ROUTES.postTimes}?${target.kind === "group" ? "group_id" : "batch_id"}=${target.id}`;
}

function positiveId(raw: string | null): number | null {
  if (raw === null || !/^\d+$/.test(raw)) return null;
  const id = Number(raw);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

/** Lote ou grupo pedido na busca da URL (?batch_id=N ou ?group_id=N); null se ausente ou inválido. */
export function postTimesTargetFromSearch(search: string): PostTimesTarget | null {
  const params = new URLSearchParams(search);
  const group = positiveId(params.get("group_id"));
  if (group !== null) return { kind: "group", id: group };
  const batch = positiveId(params.get("batch_id"));
  return batch !== null ? { kind: "batch", id: batch } : null;
}

/** Parâmetros de api.postTimesReport para o alvo. */
export function postTimesQuery(target: PostTimesTarget): { batch_id: number } | { group_id: number } {
  return target.kind === "group" ? { group_id: target.id } : { batch_id: target.id };
}

const WEEKDAYS = ["domingo", "segunda", "terça", "quarta", "quinta", "sexta", "sábado"];

export interface LocalParts {
  /** "03/11/2026" */
  date: string;
  /** "14:05" */
  time: string;
  /** "terça" */
  weekday: string;
}

/**
 * Data, hora e dia da semana como estão escritos na ISO (horário local do relatório), sem converter para o fuso do
 * navegador: "2026-11-03T14:05:00-03:00" vira 03/11/2026, 14:05, terça.
 */
export function localParts(iso: string | null | undefined): LocalParts | null {
  if (!iso) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(iso);
  if (!m) return null;
  const [, y, mo, d, h, mi] = m;
  const weekday = WEEKDAYS[new Date(Date.UTC(Number(y), Number(mo) - 1, Number(d))).getUTCDay()];
  if (weekday === undefined) return null;
  return { date: `${d}/${mo}/${y}`, time: `${h}:${mi}`, weekday };
}

/** "03/11/2026 14:05" no horário local da ISO; "-" sem data. */
export function formatLocalDateTime(iso: string | null | undefined): string {
  const p = localParts(iso);
  return p ? `${p.date} ${p.time}` : "-";
}

/** Número com vírgula decimal ("2,5"); "-" para null. */
export function formatDecimal(value: number | null | undefined, places = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  return value.toLocaleString("pt-BR", { maximumFractionDigits: places, minimumFractionDigits: 0 });
}

/** "14h" */
export function hourLabel(hour: number): string {
  return `${String(hour).padStart(2, "0")}h`;
}

/** Altura de cada barra como fração (0 a 1) do maior valor; tudo 0 quando não há contagem. */
export function barFractions(counts: number[]): number[] {
  const max = Math.max(0, ...counts);
  return counts.map((c) => (max > 0 && c > 0 ? c / max : 0));
}

/** Índices com a maior contagem (vazio quando tudo é 0). */
export function peakIndexes(counts: number[]): Set<number> {
  const max = Math.max(0, ...counts);
  return new Set(max > 0 ? counts.flatMap((c, i) => (c === max ? [i] : [])) : []);
}

/** "1 publicação" / "3 publicações". */
export function postsText(n: number): string {
  return `${n.toLocaleString("pt-BR")} ${n === 1 ? "publicação" : "publicações"}`;
}

export const SEGMENT_KIND_LABELS: Record<PostTimesSegmentKind, string> = {
  ritmo: "Ritmo",
  rajada: "Rajada",
  pausa: "Pausa",
};

const MEDIA_KIND_NAMES: Record<string, string> = { photo: "Foto", video: "Vídeo", reel: "Reel", carousel: "Carrossel" };

/** Tipo da publicação em português; texto desconhecido passa como veio. */
export function mediaKindName(kind: string | null | undefined): string {
  if (!kind) return "-";
  return MEDIA_KIND_NAMES[kind] ?? kind;
}

export function sourceLabel(source: PostTimesItem["source"]): string {
  return source === "reels_tab" ? "Estimado (aba Reels)" : "Publicação";
}

export function regularityTone(regularity: string | null | undefined): Tone {
  if (regularity === "regular") return "ok";
  if (regularity === "variável") return "warn";
  if (regularity === "irregular") return "err";
  return "muted";
}
