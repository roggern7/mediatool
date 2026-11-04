import { describe, expect, it } from "vitest";
import {
  barFractions,
  formatDecimal,
  formatLocalDateTime,
  localParts,
  mediaKindName,
  peakIndexes,
  postTimesPageUrl,
  postTimesQuery,
  postTimesTargetFromSearch,
  sourceLabel,
} from "./postTimesReport";

describe("rota dos padrões de publicação", () => {
  it("monta o endereço do lote e do grupo", () => {
    expect(postTimesPageUrl({ kind: "batch", id: 12 })).toBe("/padroes?batch_id=12");
    expect(postTimesPageUrl({ kind: "group", id: 3 })).toBe("/padroes?group_id=3");
  });

  it("lê o alvo da busca e recusa ids inválidos", () => {
    expect(postTimesTargetFromSearch("?batch_id=12")).toEqual({ kind: "batch", id: 12 });
    expect(postTimesTargetFromSearch("?group_id=3")).toEqual({ kind: "group", id: 3 });
    expect(postTimesTargetFromSearch("?batch_id=0")).toBeNull();
    expect(postTimesTargetFromSearch("?batch_id=-1")).toBeNull();
    expect(postTimesTargetFromSearch("?batch_id=abc")).toBeNull();
    expect(postTimesTargetFromSearch("")).toBeNull();
  });

  it("gera os parâmetros da API", () => {
    expect(postTimesQuery({ kind: "batch", id: 5 })).toEqual({ batch_id: 5 });
    expect(postTimesQuery({ kind: "group", id: 7 })).toEqual({ group_id: 7 });
  });
});

describe("datas no horário local do relatório", () => {
  it("usa a hora escrita na ISO, sem converter para o fuso do navegador", () => {
    expect(localParts("2026-11-03T23:50:00-03:00")).toEqual({ date: "03/11/2026", time: "23:50", weekday: "terça" });
    expect(localParts("2026-11-08T00:10:00+00:00")).toEqual({ date: "08/11/2026", time: "00:10", weekday: "domingo" });
    expect(localParts("2026-11-07T08:00:00")?.weekday).toBe("sábado");
  });

  it("devolve traço sem data válida", () => {
    expect(localParts(null)).toBeNull();
    expect(localParts("ontem")).toBeNull();
    expect(formatLocalDateTime(null)).toBe("-");
    expect(formatLocalDateTime("2026-01-02T03:04:05-03:00")).toBe("02/01/2026 03:04");
  });
});

describe("escala das barras", () => {
  it("normaliza pelo maior valor", () => {
    expect(barFractions([0, 2, 4])).toEqual([0, 0.5, 1]);
    expect(barFractions([0, 0])).toEqual([0, 0]);
    expect(barFractions([])).toEqual([]);
  });

  it("marca todos os picos e nenhum quando tudo é zero", () => {
    expect([...peakIndexes([1, 3, 3, 0])]).toEqual([1, 2]);
    expect(peakIndexes([0, 0]).size).toBe(0);
  });
});

describe("rótulos", () => {
  it("formata decimal em pt-BR", () => {
    expect(formatDecimal(2.5)).toBe("2,5");
    expect(formatDecimal(3)).toBe("3");
    expect(formatDecimal(null)).toBe("-");
  });

  it("traduz tipo e origem", () => {
    expect(mediaKindName("carousel")).toBe("Carrossel");
    expect(mediaKindName("outro")).toBe("outro");
    expect(mediaKindName(null)).toBe("-");
    expect(sourceLabel("grid")).toBe("Publicação");
    expect(sourceLabel("reels_tab")).toBe("Estimado (aba Reels)");
  });
});
