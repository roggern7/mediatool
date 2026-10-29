import { describe, expect, it } from "vitest";
import type { ContentType, ProfilesPreview, ViewsFilter } from "../api/types";
import { groupViewsText } from "./importGroup";
import { buildImportGroupRequest, buildImportRequest, planImport, resolveLimit } from "./importOptions";
import { MIN_VIEWS_WEB_ONLY_MESSAGE, planFromPreview, providerBlocker, startBlocker, startSummary } from "./importStart";
import {
  MIN_VIEWS_PRESETS,
  minViewsAllowed,
  parseMinViewsText,
  resolveMinViews,
  viewsFilterText,
} from "./viewsFilter";

const vf: ViewsFilter = { min_views: 500_000, analyzed: 42, approved: 7, below: 30, no_count: 5, pending: 0 };

describe("filtro de visualizações: valores", () => {
  it("atalhos e conteúdos permitidos", () => {
    expect(MIN_VIEWS_PRESETS).toEqual([100_000, 250_000, 500_000, 1_000_000]);
    expect((["reels", "videos"] as ContentType[]).every(minViewsAllowed)).toBe(true);
    expect((["all", "posts", "images", "photos_carousels"] as ContentType[]).some(minViewsAllowed)).toBe(false);
  });

  it("personalizado aceita inteiro com ou sem pontos de milhar e recusa o resto", () => {
    expect(parseMinViewsText("1.000.000")).toBe(1_000_000);
    expect(parseMinViewsText(" 500000 ")).toBe(500_000);
    expect(parseMinViewsText("1")).toBe(1);
    for (const bad of ["", "0", "-5", "1,5", "1.5", "5e5", "abc", "1.00", "10000000001"]) {
      expect(parseMinViewsText(bad)).toBeNull();
    }
  });

  it("resolveMinViews: desligado, atalho, personalizado e conteúdo que não é vídeo", () => {
    expect(resolveMinViews("reels", "off", "")).toEqual({ ok: true, minViews: null });
    expect(resolveMinViews("reels", "500000", "")).toEqual({ ok: true, minViews: 500_000 });
    expect(resolveMinViews("videos", "custom", "1.234")).toEqual({ ok: true, minViews: 1234 });
    expect(resolveMinViews("reels", "custom", "0").ok).toBe(false);
    // Conteúdo que não é vídeo: o filtro nunca vai no corpo, mesmo com um valor esquecido no seletor.
    expect(resolveMinViews("all", "500000", "")).toEqual({ ok: true, minViews: null });
    expect(resolveMinViews("photos_carousels", "custom", "lixo")).toEqual({ ok: true, minViews: null });
  });

  it("linha do lote com os rótulos pedidos; sem filtro nada é exibido", () => {
    expect(viewsFilterText(vf, 5)).toBe(
      "Mín. 500.000 visualizações: 42 analisados • 7 aprovados • 5 baixados • 30 abaixo do mínimo • " +
        "5 sem contagem disponível",
    );
    expect(viewsFilterText({ ...vf, analyzed: 1, approved: 1, below: 0, no_count: 0 }, 1)).toContain(
      "1 analisado • 1 aprovado • 1 baixado",
    );
    expect(viewsFilterText(vf)).not.toContain("baixado");
    expect(viewsFilterText(null, 3)).toBeNull();
    expect(viewsFilterText(undefined)).toBeNull();
  });

  it("linha agregada do grupo usa os baixados do grupo", () => {
    expect(groupViewsText({ views_filter: vf, progress: { found: 9, downloaded: 6, processed: 0 } })).toContain(
      "6 baixados",
    );
    expect(groupViewsText({ views_filter: null, progress: { found: 9, downloaded: 6, processed: 0 } })).toBeNull();
  });
});

describe("filtro de visualizações: corpo e início", () => {
  const base = { content: "reels" as ContentType, limit: 10, processAfterDownload: false, presetId: null };
  const preview: ProfilesPreview = { valid: ["um", "dois"], invalid: [], duplicates: 0 };

  it("sem filtro o corpo é idêntico ao de antes (a chave nem aparece)", () => {
    for (const minViews of [undefined, null]) {
      const single = buildImportRequest({ ...base, profile: "p", minViews });
      expect(single).toEqual({ profile: "p", content_type: "reels", limit: 10, preset_id: null, process: false });
      expect("min_views" in single).toBe(false);
      expect("min_views" in buildImportGroupRequest({ ...base, profiles: ["a", "b"], minViews })).toBe(false);
    }
  });

  it("com filtro o corpo leva min_views (avulso e grupo)", () => {
    expect(buildImportRequest({ ...base, profile: "p", minViews: 250_000 }).min_views).toBe(250_000);
    const plan = planImport({ ...base, profiles: ["a", "b"], minViews: 1 });
    expect(plan?.kind === "group" && plan.body.min_views).toBe(1);
  });

  it("personalizado inválido bloqueia o início e o resumo explica", () => {
    const minViews = resolveMinViews("reels", "custom", "");
    const state = {
      sessionOk: true,
      paused: false,
      submitting: false,
      preview,
      limit: resolveLimit("last", "10"),
      minViews,
      process: false,
      presetId: null,
      providerProblem: null,
    };
    expect(startBlocker(state)).toBe("min_views");
    expect(startBlocker({ ...state, minViews: resolveMinViews("reels", "500000", "") })).toBeNull();
    expect(startBlocker({ ...state, minViews: undefined })).toBeNull();
    expect(startSummary({ preview, limit: state.limit, content: "reels", minViews })?.kind).toBe("problem");
    expect(planFromPreview(preview, { ...base, limit: state.limit, minViews })).toBeNull();
  });

  it("resumo e plano com o filtro ligado", () => {
    const minViews = resolveMinViews("reels", "1000000", "");
    const summary = startSummary({ preview, limit: resolveLimit("last", "10"), content: "reels", minViews });
    expect(summary?.kind === "summary" && summary.text).toContain("Mín. 1.000.000 visualizações");
    expect(summary?.kind === "summary" && summary.hints.join(" ")).toContain("“Últimos N” conta só os aprovados");
    const plan = planFromPreview(preview, { ...base, limit: resolveLimit("last", "10"), minViews });
    expect(plan?.body.min_views).toBe(1_000_000);
    const off = planFromPreview(preview, { ...base, limit: resolveLimit("last", "10") });
    expect(off && "min_views" in off.body).toBe(false);
  });

  it("método gallery-dl recusa o filtro antes de enviar", () => {
    expect(providerBlocker("gallery_dl", "reels", false, 500_000)).toBe(MIN_VIEWS_WEB_ONLY_MESSAGE);
    expect(providerBlocker("gallery_dl", "reels", false, null)).toBeNull();
    expect(providerBlocker("gallery_dl", "reels", false)).toBeNull();
    expect(providerBlocker("auto", "reels", false, 500_000)).toBeNull();
  });
});
