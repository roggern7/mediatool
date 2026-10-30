import { describe, expect, it } from "vitest";
import type { ClassifyResult, UnifiedImportResult } from "../api/types";
import { LIBRARY_TYPE_FILTERS, libraryTypeQuery, mediaKindLabel } from "./importCovers";
import {
  MEDIA_CHOICES,
  autoExportText,
  autoExportNeedsProcessing,
  AUTO_EXPORT_NEEDS_PROCESSING_TEXT,
  classificationText,
  createdText,
  entriesOf,
  needsConfirmAll,
  postProcessingApplies,
  progressExtrasText,
  resolveQuantity,
  unifiedBody,
  viewsAllowed,
  type UnifiedForm,
} from "./importUnified";
import { VIEWS_FILTER_ENABLED, VIEWS_PRESETS, resolveViewsRange, viewsFilterText, viewsPresetLabel } from "./viewsFilter";

const classified = (profiles: number, links: number, invalid = 0, duplicates = 0): ClassifyResult => ({
  profiles: Array.from({ length: profiles }, (_, i) => `perfil_${i}`),
  links: Array.from({ length: links }, (_, i) => ({ shortcode: `CODE${i}`, url: `https://www.instagram.com/p/CODE${i}/`, kind: "p" as const })),
  invalid: Array.from({ length: invalid }, (_, i) => ({ entry: `x${i}`, reason: "inválida" })),
  duplicates,
});

const form = (over: Partial<UnifiedForm> = {}): UnifiedForm => ({
  text: "@perfil\nhttps://www.instagram.com/p/ABC/\n\n",
  media: "videos",
  quantity: { ok: true, limit: 50 },
  confirmAll: false,
  periodDays: null,
  minViews: null,
  maxViews: null,
  post: "none",
  presetId: null,
  edit: null,
  ...over,
});

describe("classificação", () => {
  it("resume perfis, publicações, inválidas e repetidas", () => {
    expect(classificationText(classified(2, 1, 1, 3))).toBe("2 perfis • 1 publicação • 1 inválida • 3 repetidas");
    expect(classificationText(classified(1, 0))).toBe("1 perfil • 0 publicações");
  });
  it("envia uma entrada por linha não vazia", () => {
    expect(entriesOf(" a \n\n b\r\nc ")).toEqual(["a", "b", "c"]);
  });
});

describe("quantidade e confirmação", () => {
  it("atalhos, personalizada e Todas", () => {
    expect(resolveQuantity("10", "")).toEqual({ ok: true, limit: 10 });
    expect(resolveQuantity("custom", "1.500")).toEqual({ ok: true, limit: 1500 });
    expect(resolveQuantity("custom", "0").ok).toBe(false);
    expect(resolveQuantity("custom", "abc").ok).toBe(false);
    expect(resolveQuantity("all", "")).toEqual({ ok: true, limit: null });
  });
  it("Todas só pede confirmação quando há perfil", () => {
    expect(needsConfirmAll(classified(1, 0), { ok: true, limit: null })).toBe(true);
    expect(needsConfirmAll(classified(0, 3), { ok: true, limit: null })).toBe(false);
    expect(needsConfirmAll(classified(2, 0), { ok: true, limit: 50 })).toBe(false);
  });
});

describe("corpo enviado", () => {
  it("padrão: vídeos, sem filtros nem chaves extras", () => {
    expect(unifiedBody(form())).toEqual({
      inputs: ["@perfil", "https://www.instagram.com/p/ABC/"],
      media: "videos",
      limit: 50,
      confirm_all: false,
      post_processing: "none",
    });
  });
  it("Todas confirmada, período, mín/máx e preset", () => {
    expect(
      unifiedBody(
        form({ quantity: { ok: true, limit: null }, confirmAll: true, periodDays: 30, minViews: 1000, maxViews: 50000, post: "preset", presetId: 3 }),
      ),
    ).toMatchObject({ limit: null, confirm_all: true, posted_within_days: 30, min_views: 1000, max_views: 50000, preset_id: 3 });
  });
  it("visualizações nunca vão com Todas as mídias", () => {
    const body = unifiedBody(form({ media: "all", minViews: 1000, maxViews: 5000 }));
    expect(body).not.toHaveProperty("min_views");
    expect(body).not.toHaveProperty("max_views");
    expect(viewsAllowed("covers")).toBe(true);
    expect(viewsAllowed("all")).toBe(false);
  });
  it("quantidade inválida não gera corpo", () => {
    expect(unifiedBody(form({ quantity: { ok: false, error: "x" } }))).toBeNull();
  });
  it("aviso do que foi criado junto", () => {
    const result: UnifiedImportResult = {
      batch_ids: [9], group_id: 4, links_batch_id: 9, profiles: 2, links: 3, invalid: [{ entry: "x", reason: "y" }], duplicates: 0,
    };
    expect(createdText(result)).toBe("Criados: grupo com 2 perfis e lote de 3 publicações. 1 entrada inválida ignorada.");
  });
  it("avisos da API (notes) entram no fim", () => {
    const result: UnifiedImportResult = {
      batch_ids: [9], group_id: null, links_batch_id: 9, profiles: 0, links: 1, invalid: [], duplicates: 0,
      notes: ["Os filtros valem só para perfis."],
    };
    expect(createdText(result)).toBe("Criados: lote de 1 publicação. Os filtros valem só para perfis.");
  });
});

describe("visualizações mínimo/máximo", () => {
  const off = { mode: "off" as const, text: "" };
  it("filtro ligado e atalhos", () => {
    expect(VIEWS_FILTER_ENABLED).toBe(true);
    expect([...VIEWS_PRESETS]).toEqual([1000, 10000, 50000, 100000, 500000, 1000000]);
    expect(viewsPresetLabel(1000)).toBe("1 mil");
    expect(viewsPresetLabel(1_000_000)).toBe("1 mi");
  });
  it("mínimo, máximo, personalizado e máximo menor que o mínimo", () => {
    expect(resolveViewsRange(true, { mode: "10000", text: "" }, { mode: "custom", text: "20.000" })).toEqual({
      ok: true, minViews: 10000, maxViews: 20000,
    });
    expect(resolveViewsRange(true, { mode: "50000", text: "" }, { mode: "10000", text: "" }).ok).toBe(false);
    expect(resolveViewsRange(false, { mode: "50000", text: "" }, off)).toEqual({ ok: true, minViews: null, maxViews: null });
  });
  it("linha do lote mostra o máximo e os acima dele", () => {
    const text = viewsFilterText({ min_views: 1000, max_views: 5000, analyzed: 4, approved: 1, below: 1, above: 1, no_count: 1, pending: 0 }, 1);
    expect(text).toBe(
      "Mín. 1.000 visualizações • Máx. 5.000 visualizações: 4 analisados • 1 aprovado • 1 baixado • 1 abaixo do mínimo • 1 acima do máximo • 1 sem contagem disponível",
    );
  });
});

describe("Biblioteca: capas", () => {
  it("filtro Capas usa role e os demais o tipo", () => {
    expect(LIBRARY_TYPE_FILTERS.map(([v]) => v)).toEqual(["all", "video", "image", "cover"]);
    expect(libraryTypeQuery("cover")).toEqual({ role: "cover" });
    expect(libraryTypeQuery("video")).toEqual({ media_type: "video" });
    expect(libraryTypeQuery("all")).toEqual({});
  });
  it("selo Capa", () => {
    expect(mediaKindLabel({ role: "cover", media_type: "image" })).toBe("Capa");
    expect(mediaKindLabel({ role: "original", media_type: "image" })).toBe("Imagem");
    expect(mediaKindLabel({ media_type: "video" })).toBe("Vídeo");
  });
});

describe("contadores Duplicados e Filtrados", () => {
  it("mostra os dois quando o backend manda, inclusive zero", () => {
    expect(progressExtrasText({ duplicates: 2, filtered: 1500 })).toBe("Duplicados: 2 · Filtrados: 1.500");
    expect(progressExtrasText({ duplicates: 0, filtered: 0 })).toBe("Duplicados: 0 · Filtrados: 0");
  });
  it("backend antigo sem os campos: nada", () => {
    expect(progressExtrasText({})).toBeNull();
    expect(progressExtrasText({ filtered: 3 })).toBe("Filtrados: 3");
  });
});

describe("tipos de mídia e exportação automática", () => {
  it("cinco tipos, na ordem da tela", () => {
    expect(MEDIA_CHOICES.map((c) => c.value)).toEqual(["videos", "photos", "covers", "videos_covers", "all"]);
  });
  it("visualizações só com vídeos, capas ou vídeos e capas", () => {
    expect(["videos", "covers", "videos_covers"].every((m) => viewsAllowed(m as never))).toBe(true);
    expect(viewsAllowed("photos")).toBe(false);
    const body = unifiedBody(form({ media: "photos", minViews: 10 }));
    expect(body).not.toHaveProperty("min_views");
    expect(unifiedBody(form({ media: "videos_covers", minViews: 10 }))).toMatchObject({ min_views: 10 });
  });
  it("pós-processamento não vale para somente fotos/capas", () => {
    expect(postProcessingApplies("photos")).toBe(false);
    expect(postProcessingApplies("covers")).toBe(false);
    expect(postProcessingApplies("all")).toBe(true);
  });
  it("auto_export só vai quando ligado", () => {
    expect(unifiedBody(form())).not.toHaveProperty("auto_export");
    expect(unifiedBody(form({ autoExport: "off" }))).not.toHaveProperty("auto_export");
    expect(unifiedBody(form({ autoExport: "both" }))).toMatchObject({ auto_export: { content: "both" } });
  });
  it("editados/ambos exigem pós-processamento", () => {
    expect(autoExportNeedsProcessing("edited")).toBe(true);
    expect(autoExportNeedsProcessing("both")).toBe(true);
    expect(autoExportNeedsProcessing("originals")).toBe(false);
    expect(autoExportNeedsProcessing("off")).toBe(false);
    expect(AUTO_EXPORT_NEEDS_PROCESSING_TEXT).toContain("originais");
  });
  it("texto do painel da exportação automática", () => {
    const base = { content: "originals" as const, export_id: null, error_message: null, zip_ready: false, download_url: null };
    expect(autoExportText(null)).toBeNull();
    expect(autoExportText({ ...base, status: "waiting" })).toContain("aguardando");
    expect(autoExportText({ ...base, status: "waiting", request_id: 4 })).toContain("ZIP único");
    expect(autoExportText({ ...base, status: "completed", export_id: 3, zip_ready: true, download_url: "/x" })).toContain(
      "ZIP pronto",
    );
    expect(autoExportText({ ...base, status: "failed", error_message: "Nada para exportar." })).toContain(
      "falhou: Nada para exportar.",
    );
  });
});
