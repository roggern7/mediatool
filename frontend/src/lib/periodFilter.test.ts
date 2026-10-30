import { describe, expect, it } from "vitest";
import type { ProfilesPreview, ViewsFilter } from "../api/types";
import { buildImportGroupRequest, buildImportRequest, resolveLimit } from "./importOptions";
import { PERIOD_WEB_ONLY_MESSAGE, planFromPreview, providerBlocker, startBlocker, startSummary } from "./importStart";
import {
  PERIOD_PRESETS,
  VIEWS_FILTER_ENABLED,
  parsePeriodDays,
  periodSummary,
  resolveMinViews,
  resolvePeriod,
  viewsFilterText,
  visibleFilter,
} from "./viewsFilter";

const preview = (valid: string[]): ProfilesPreview => ({ valid, invalid: [], duplicates: 0 });
const base = { content: "reels" as const, limit: 10, processAfterDownload: false, presetId: null };

describe("período de postagem: valores", () => {
  it("atalhos de 7, 30, 60 e 90 dias", () => {
    expect(PERIOD_PRESETS).toEqual([7, 30, 60, 90]);
    expect(periodSummary(30)).toBe("Últimos 30 dias");
    expect(periodSummary(1)).toBe("Último dia");
  });

  it("personalizado aceita inteiro de 1 a 3650 (com ponto de milhar)", () => {
    expect(parsePeriodDays(" 45 ")).toBe(45);
    expect(parsePeriodDays("1")).toBe(1);
    expect(parsePeriodDays("3.650")).toBe(3650);
    for (const bad of ["", "0", "3651", "1,5", "-3", "dez", "30 dias", "1.5"]) expect(parsePeriodDays(bad)).toBeNull();
  });

  it("resolvePeriod: qualquer data, atalho e personalizado", () => {
    expect(resolvePeriod("any", "999")).toEqual({ ok: true, days: null });
    expect(resolvePeriod("60", "")).toEqual({ ok: true, days: 60 });
    expect(resolvePeriod("custom", "120")).toEqual({ ok: true, days: 120 });
    const bad = resolvePeriod("custom", "0");
    expect(bad.ok).toBe(false);
  });
});

describe("período de postagem: corpo enviado", () => {
  it("sem período a chave não aparece (avulso, grupo e com edição)", () => {
    expect("posted_within_days" in buildImportRequest({ ...base, profile: "p" })).toBe(false);
    expect("posted_within_days" in buildImportGroupRequest({ ...base, profiles: ["a", "b"], postedWithinDays: null }))
      .toBe(false);
  });

  it("com período o corpo leva posted_within_days junto do min_views, também em Editar após baixar", () => {
    const single = buildImportRequest({ ...base, profile: "p", postedWithinDays: 30, minViews: 10_000 });
    expect(single.posted_within_days).toBe(30);
    expect(single.min_views).toBe(10_000);
    const edit = { mute: true } as unknown as Parameters<typeof buildImportRequest>[0]["edit"];
    const edited = buildImportRequest({ ...base, profile: "p", edit, postedWithinDays: 7, minViews: 5 });
    expect([edited.posted_within_days, edited.min_views, edited.process]).toEqual([7, 5, false]);
  });

  it("planFromPreview: personalizado vai no corpo; inválido não monta pedido", () => {
    const options = {
      content: "all" as const,
      limit: resolveLimit("all", ""),
      processAfterDownload: false,
      presetId: null,
      minViews: resolveMinViews("all", "off", ""),
    };
    const group = planFromPreview(preview(["a", "b"]), { ...options, period: resolvePeriod("custom", "45") });
    expect(group?.kind === "group" && group.body.posted_within_days).toBe(45);
    expect(planFromPreview(preview(["a"]), { ...options, period: resolvePeriod("custom", "x") })).toBeNull();
    const any = planFromPreview(preview(["a"]), { ...options, period: resolvePeriod("any", "") });
    expect(any && "posted_within_days" in any.body).toBe(false);
  });

  it("bloqueia o início com o personalizado inválido e resume o período", () => {
    const period = resolvePeriod("custom", "");
    const state = {
      sessionOk: true,
      paused: false,
      submitting: false,
      preview: preview(["a"]),
      limit: resolveLimit("last", "10"),
      period,
      process: false,
      presetId: null,
      providerProblem: null,
    };
    expect(startBlocker(state)).toBe("period");
    expect(startBlocker({ ...state, period: resolvePeriod("30", "") })).toBeNull();
    const summary = startSummary({ preview: preview(["a"]), limit: resolveLimit("last", "10"), content: "all",
      period: resolvePeriod("30", "") });
    expect(summary?.kind === "summary" && summary.text.endsWith("• Últimos 30 dias")).toBe(true);
    expect(providerBlocker("gallery_dl", "all", false, null, 30)).toBe(PERIOD_WEB_ONLY_MESSAGE);
    expect(providerBlocker("web", "all", false, null, 30)).toBeNull();
  });
});

describe("período de postagem: contadores", () => {
  it("linha do lote mostra fora do período; sem visualizações não mostra abaixo/sem contagem", () => {
    const vf: ViewsFilter = { min_views: null, posted_within_days: 30, posted_after: "2026-09-29T00:00:00Z",
      analyzed: 12, approved: 9, below: 0, no_count: 0, out_of_period: 3, pending: 0 };
    expect(viewsFilterText(vf, 8)).toBe("Últimos 30 dias: 12 analisados • 9 aprovados • 8 baixados • 3 fora do período");
    const both: ViewsFilter = { ...vf, min_views: 500_000, below: 1, no_count: 2 };
    expect(viewsFilterText(both)).toBe(
      "Mín. 500.000 visualizações • Últimos 30 dias: 12 analisados • 9 aprovados • 1 abaixo do mínimo • " +
        "2 sem contagem disponível • 3 fora do período",
    );
  });
});

describe("filtro de visualizações ligado na interface", () => {
  it("o interruptor está ligado (2026-10-29) e a tela mostra visualizações e período", () => {
    expect(VIEWS_FILTER_ENABLED).toBe(true);
    const onlyViews: ViewsFilter = { min_views: 500_000, analyzed: 5, approved: 1, below: 2, no_count: 2, pending: 0 };
    expect(visibleFilter(onlyViews)).toBe(onlyViews);
    expect(viewsFilterText(visibleFilter(onlyViews))).toBe(
      "Mín. 500.000 visualizações: 5 analisados • 1 aprovado • 2 abaixo do mínimo • 2 sem contagem disponível",
    );
    const both: ViewsFilter = { ...onlyViews, posted_within_days: 30, out_of_period: 4, analyzed: 9 };
    expect(visibleFilter(both)?.min_views).toBe(500_000);
    expect(visibleFilter(null)).toBeNull();
  });
});
