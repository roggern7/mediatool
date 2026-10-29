// Filtro de período da Fila (sobre a criação do job) e separadores por dia, no fuso local do computador.

import { formatDate } from "./format";

export type PeriodId = "today" | "yesterday" | "7d" | "30d" | "all" | "custom";

export const PERIODS: { id: PeriodId; label: string }[] = [
  { id: "today", label: "Hoje" },
  { id: "yesterday", label: "Ontem" },
  { id: "7d", label: "Últimos 7 dias" },
  { id: "30d", label: "Últimos 30 dias" },
  { id: "all", label: "Tudo" },
  { id: "custom", label: "Personalizado" },
];

/** Período escolhido; `from`/`to` são as datas do "Personalizado" (AAAA-MM-DD do `<input type="date">`, ou ""). */
export interface PeriodChoice {
  period: PeriodId;
  from: string;
  to: string;
}

export const DEFAULT_PERIOD: PeriodChoice = { period: "today", from: "", to: "" };
/** Fila aberta pelo link de um lote: tudo, para não esconder jobs do lote. */
export const BATCH_DEFAULT_PERIOD: PeriodChoice = { period: "all", from: "", to: "" };

/** Intervalo [from, to) em instantes; null = sem limite daquele lado. */
export interface PeriodRange {
  from: Date | null;
  to: Date | null;
}

export type PeriodResult = { ok: true; range: PeriodRange } | { ok: false; message: string };

/** Meia-noite local do dia de `d` mais `days` dias (respeita horário de verão). */
function startOfDay(d: Date, days = 0): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + days);
}

/** "AAAA-MM-DD" -> meia-noite local daquele dia; null se vazio ou inválido. */
export function parseDateInput(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
  const date = new Date(year, month - 1, day);
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return null;
  return date;
}

/**
 * Intervalo do período em relação a `now`. "Últimos N dias" inclui hoje; no personalizado a data final é
 * inclusiva e cada data pode ficar vazia (sem limite daquele lado).
 */
export function periodRange(choice: PeriodChoice, now: Date): PeriodResult {
  switch (choice.period) {
    case "today":
      return { ok: true, range: { from: startOfDay(now), to: startOfDay(now, 1) } };
    case "yesterday":
      return { ok: true, range: { from: startOfDay(now, -1), to: startOfDay(now) } };
    case "7d":
      return { ok: true, range: { from: startOfDay(now, -6), to: startOfDay(now, 1) } };
    case "30d":
      return { ok: true, range: { from: startOfDay(now, -29), to: startOfDay(now, 1) } };
    case "all":
      return { ok: true, range: { from: null, to: null } };
    case "custom": {
      const from = choice.from ? parseDateInput(choice.from) : null;
      const last = choice.to ? parseDateInput(choice.to) : null;
      if ((choice.from && from === null) || (choice.to && last === null)) {
        return { ok: false, message: "Data inválida." };
      }
      if (from !== null && last !== null && from > last) {
        return { ok: false, message: "A data inicial é depois da data final." };
      }
      return { ok: true, range: { from, to: last === null ? null : startOfDay(last, 1) } };
    }
  }
}

/** Parâmetros de `GET /api/jobs` (instantes em UTC). */
export function periodQuery(range: PeriodRange): { created_from?: string; created_to?: string } {
  return {
    created_from: range.from?.toISOString(),
    created_to: range.to?.toISOString(),
  };
}

function localDayKey(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Dia local (AAAA-MM-DD) de uma data ISO; "" se inválida. */
export function dayKey(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : localDayKey(d);
}

/** Rótulo do separador de dia: "Hoje", "Ontem" ou "dd/mm/aaaa". */
export function dayLabel(iso: string, now: Date): string {
  const key = dayKey(iso);
  if (key === "") return "-";
  if (key === localDayKey(now)) return "Hoje";
  if (key === localDayKey(startOfDay(now, -1))) return "Ontem";
  return formatDate(iso);
}

/** Itens agrupados por dia de criação, na ordem recebida (grupos consecutivos). */
export function groupByDay<T extends { created_at: string }>(
  items: T[],
  now: Date,
): { key: string; label: string; items: T[] }[] {
  const groups: { key: string; label: string; items: T[] }[] = [];
  for (const item of items) {
    const key = dayKey(item.created_at);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.items.push(item);
    else groups.push({ key, label: dayLabel(item.created_at, now), items: [item] });
  }
  return groups;
}

/** Período em uso: na Fila geral, o escolhido (ou lembrado); no link de um lote, "Tudo" até o dono escolher outro. */
export function activePeriod(
  batchId: number | null,
  general: PeriodChoice,
  batchChoice: { batchId: number; choice: PeriodChoice } | null,
): PeriodChoice {
  if (batchId === null) return general;
  return batchChoice !== null && batchChoice.batchId === batchId ? batchChoice.choice : BATCH_DEFAULT_PERIOD;
}

const STORAGE_KEY = "media-tool.queue.period";

/** Último período escolhido na Fila geral (ou o padrão "Hoje"). */
export function loadQueuePeriod(storage: Pick<Storage, "getItem"> | null = safeLocalStorage()): PeriodChoice {
  try {
    const raw = storage?.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_PERIOD;
    const data = JSON.parse(raw) as Partial<PeriodChoice> | null;
    if (!data || !PERIODS.some((p) => p.id === data.period)) return DEFAULT_PERIOD;
    const date = (value: unknown) => (typeof value === "string" && parseDateInput(value) ? value : "");
    return { period: data.period as PeriodId, from: date(data.from), to: date(data.to) };
  } catch {
    return DEFAULT_PERIOD;
  }
}

export function saveQueuePeriod(
  choice: PeriodChoice,
  storage: Pick<Storage, "setItem"> | null = safeLocalStorage(),
): void {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify(choice));
  } catch {
    // Sem armazenamento (modo privado, cota): só não lembra.
  }
}

function safeLocalStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}
