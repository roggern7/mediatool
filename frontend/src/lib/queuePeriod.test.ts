import { describe, expect, it } from "vitest";
import {
  BATCH_DEFAULT_PERIOD,
  DEFAULT_PERIOD,
  type PeriodChoice,
  activePeriod,
  dayLabel,
  groupByDay,
  loadQueuePeriod,
  parseDateInput,
  periodQuery,
  periodRange,
  saveQueuePeriod,
} from "./queuePeriod";

// Datas montadas no fuso local (como no navegador): os testes valem em qualquer fuso.
const NOW = new Date(2026, 9, 29, 15, 42, 10); // 29/10/2026 15:42 local
const local = (y: number, m: number, d: number, h = 0, min = 0) => new Date(y, m - 1, d, h, min);
const choice = (period: PeriodChoice["period"], from = "", to = ""): PeriodChoice => ({ period, from, to });

function rangeOf(c: PeriodChoice, now = NOW) {
  const result = periodRange(c, now);
  if (!result.ok) throw new Error(result.message);
  return result.range;
}

describe("período da Fila: intervalos", () => {
  it("Hoje vai da meia-noite local até a meia-noite seguinte", () => {
    expect(rangeOf(choice("today"))).toEqual({ from: local(2026, 10, 29), to: local(2026, 10, 30) });
  });

  it("Ontem é o dia local anterior inteiro", () => {
    expect(rangeOf(choice("yesterday"))).toEqual({ from: local(2026, 10, 28), to: local(2026, 10, 29) });
    // Virada de mês e de ano.
    expect(rangeOf(choice("yesterday"), local(2026, 1, 1, 0, 5))).toEqual({
      from: local(2025, 12, 31),
      to: local(2026, 1, 1),
    });
  });

  it("Últimos 7 e 30 dias incluem hoje", () => {
    expect(rangeOf(choice("7d"))).toEqual({ from: local(2026, 10, 23), to: local(2026, 10, 30) });
    expect(rangeOf(choice("30d"))).toEqual({ from: local(2026, 9, 30), to: local(2026, 10, 30) });
    expect(rangeOf(choice("7d"), local(2026, 3, 2, 23, 59))).toEqual({
      from: local(2026, 2, 24),
      to: local(2026, 3, 3),
    });
  });

  it("Tudo não tem limites e não manda parâmetros", () => {
    const range = rangeOf(choice("all"));
    expect(range).toEqual({ from: null, to: null });
    expect(periodQuery(range)).toEqual({ created_from: undefined, created_to: undefined });
  });

  it("Personalizado: data final inclusiva, limites opcionais", () => {
    expect(rangeOf(choice("custom", "2026-10-01", "2026-10-15"))).toEqual({
      from: local(2026, 10, 1),
      to: local(2026, 10, 16),
    });
    expect(rangeOf(choice("custom", "2026-10-15", "2026-10-15"))).toEqual({
      from: local(2026, 10, 15),
      to: local(2026, 10, 16),
    });
    expect(rangeOf(choice("custom", "2026-10-01", ""))).toEqual({ from: local(2026, 10, 1), to: null });
    expect(rangeOf(choice("custom", "", "2026-10-31"))).toEqual({ from: null, to: local(2026, 11, 1) });
    expect(rangeOf(choice("custom"))).toEqual({ from: null, to: null });
  });

  it("Personalizado inválido não vira consulta", () => {
    expect(periodRange(choice("custom", "2026-10-15", "2026-10-01"), NOW)).toEqual({
      ok: false,
      message: "A data inicial é depois da data final.",
    });
    expect(periodRange(choice("custom", "2026-02-30", ""), NOW).ok).toBe(false);
    expect(parseDateInput("2026-02-30")).toBeNull();
    expect(parseDateInput("29/10/2026")).toBeNull();
    expect(parseDateInput("2026-10-29")).toEqual(local(2026, 10, 29));
  });

  it("manda os limites em UTC (ISO com Z)", () => {
    const range = rangeOf(choice("today"));
    const query = periodQuery(range);
    expect(query.created_from).toBe(local(2026, 10, 29).toISOString());
    expect(query.created_to).toBe(local(2026, 10, 30).toISOString());
    expect(query.created_from?.endsWith("Z")).toBe(true);
  });
});

describe("período da Fila: separadores por dia", () => {
  const iso = (d: Date) => d.toISOString();

  it("rótulos Hoje, Ontem e dd/mm/aaaa pelo dia local", () => {
    expect(dayLabel(iso(local(2026, 10, 29, 0, 0)), NOW)).toBe("Hoje");
    expect(dayLabel(iso(local(2026, 10, 29, 23, 59)), NOW)).toBe("Hoje");
    expect(dayLabel(iso(local(2026, 10, 28, 23, 59)), NOW)).toBe("Ontem");
    expect(dayLabel(iso(local(2026, 10, 27, 12, 0)), NOW)).toBe("27/10/2026");
    expect(dayLabel(iso(local(2025, 12, 31, 8, 0)), local(2026, 1, 1, 9))).toBe("Ontem");
    expect(dayLabel("não é data", NOW)).toBe("-");
  });

  it("agrupa itens consecutivos do mesmo dia, mantendo a ordem", () => {
    const jobs = [
      { id: 5, created_at: iso(local(2026, 10, 29, 10)) },
      { id: 4, created_at: iso(local(2026, 10, 29, 0, 1)) },
      { id: 3, created_at: iso(local(2026, 10, 28, 23, 59)) },
      { id: 2, created_at: iso(local(2026, 10, 20, 9)) },
      { id: 1, created_at: iso(local(2026, 10, 20, 8)) },
    ];
    const groups = groupByDay(jobs, NOW);
    expect(groups.map((g) => g.label)).toEqual(["Hoje", "Ontem", "20/10/2026"]);
    expect(groups.map((g) => g.items.map((j) => j.id))).toEqual([[5, 4], [3], [2, 1]]);
    expect(groupByDay([], NOW)).toEqual([]);
  });
});

describe("período da Fila: padrão e memória", () => {
  it("Fila geral começa em Hoje; link de lote começa em Tudo", () => {
    expect(DEFAULT_PERIOD.period).toBe("today");
    expect(activePeriod(null, DEFAULT_PERIOD, null).period).toBe("today");
    expect(activePeriod(7, DEFAULT_PERIOD, null)).toEqual(BATCH_DEFAULT_PERIOD);
    expect(BATCH_DEFAULT_PERIOD.period).toBe("all");
    // Mesmo com outro período lembrado na Fila geral.
    expect(activePeriod(7, choice("yesterday"), null).period).toBe("all");
  });

  it("escolha no lote vale só para aquele lote", () => {
    const picked = { batchId: 7, choice: choice("7d") };
    expect(activePeriod(7, DEFAULT_PERIOD, picked).period).toBe("7d");
    expect(activePeriod(8, DEFAULT_PERIOD, picked).period).toBe("all");
    expect(activePeriod(null, DEFAULT_PERIOD, picked).period).toBe("today");
  });

  it("lembra o último período e tolera armazenamento ausente ou corrompido", () => {
    const data = new Map<string, string>();
    const storage = {
      getItem: (k: string) => data.get(k) ?? null,
      setItem: (k: string, v: string) => void data.set(k, v),
    };
    expect(loadQueuePeriod(storage)).toEqual(DEFAULT_PERIOD);
    saveQueuePeriod(choice("custom", "2026-10-01", "2026-10-15"), storage);
    expect(loadQueuePeriod(storage)).toEqual(choice("custom", "2026-10-01", "2026-10-15"));
    for (const raw of ["{", "null", '{"period":"semana"}', "42"]) {
      data.set([...data.keys()][0] ?? "", raw);
      expect(loadQueuePeriod(storage)).toEqual(DEFAULT_PERIOD);
    }
    data.set([...data.keys()][0] ?? "", '{"period":"custom","from":"lixo","to":"2026-10-15"}');
    expect(loadQueuePeriod(storage)).toEqual(choice("custom", "", "2026-10-15"));
    const broken = {
      getItem: () => {
        throw new Error("bloqueado");
      },
      setItem: () => {
        throw new Error("cota");
      },
    };
    expect(loadQueuePeriod(broken)).toEqual(DEFAULT_PERIOD);
    expect(() => saveQueuePeriod(DEFAULT_PERIOD, broken)).not.toThrow();
    expect(loadQueuePeriod(null)).toEqual(DEFAULT_PERIOD);
  });
});
