import { describe, expect, it } from "vitest";
import type { CapCutBatch, LinksPreview, ProfilesPreview } from "../api/types";
import { batchIdFromSearch, capcutForBatch } from "./batchRoute";
import {
  BATCH_STATUS_LABEL,
  type BatchConfigPlan,
  DEFAULT_BATCH_EDIT,
  type BatchExportForm,
  batchActions,
  batchSourceText,
  batchTone,
  buildImportCapcut,
  configBlocker,
} from "./capcutBatch";
import { buildLinksRequest } from "./importLinks";
import { editProblemOf, postProcessingOptions, resolveLimit } from "./importOptions";
import { planFromPreview } from "./importStart";
import { unifiedBody } from "./importUnified";
import { DEFAULT_EDIT_FORM } from "./editSettings";

// "Edição CapCut" após baixar: o campo `capcut` dos pedidos de importação, montado com as funções da página Edição
// CapCut, e o status/origem do lote vinculado.

const EXPORT: BatchExportForm = {
  resolution: "1080P",
  bitrate: "recommended",
  fps: "30",
  keep_original_audio: true,
  review_mode: "doubtful",
};
const EDIT = { ...DEFAULT_BATCH_EDIT, durationMode: "30" as const, speed: 1.2, sharpen: true };

function config(overrides: Partial<BatchConfigPlan> = {}): BatchConfigPlan {
  return { mode: "edit_caption", name: "", styleId: 7, exportForm: EXPORT, edit: EDIT, ...overrides };
}

const one: ProfilesPreview = { valid: ["perfil_a"], invalid: [], duplicates: 0 };
const two: ProfilesPreview = { valid: ["perfil_a", "perfil_b"], invalid: [], duplicates: 0 };
const links: LinksPreview = {
  valid: [{ shortcode: "ABCDE12345", url: "https://www.instagram.com/p/ABCDE12345/" }],
  invalid: [],
  duplicates: 0,
};

describe("campo capcut da importação", () => {
  it("mesmo corpo do lote da página, sem seleção nem include_done", () => {
    const body = buildImportCapcut(config({ name: "  Reels da semana " }));
    expect(body).toEqual({
      mode: "edit_caption",
      name: "Reels da semana",
      caption: { style_id: 7, overrides: EXPORT },
      edit: { preset_id: null, duration_mode: "30", start_seconds: 0, speed: 1.2, sharpen: true, mirror: false },
    });
    expect(body).not.toHaveProperty("media_ids");
    expect(body).not.toHaveProperty("include_done");
  });

  it("legenda só nos modos com legenda; edição só nos modos com edição", () => {
    const caption = buildImportCapcut(config({ mode: "caption" }));
    expect(caption).not.toHaveProperty("edit");
    expect(caption.caption).toEqual({ style_id: 7, overrides: EXPORT });
    const edit = buildImportCapcut(config({ mode: "edit" }));
    expect(edit).not.toHaveProperty("caption");
    expect(edit.edit?.duration_mode).toBe("30");
  });

  it("bloqueia com a mesma validação da edição da página", () => {
    expect(configBlocker(config())).toBeNull();
    expect(configBlocker(config({ mode: "edit", edit: DEFAULT_BATCH_EDIT }))).not.toBeNull();
    expect(configBlocker(config({ mode: "caption", edit: DEFAULT_BATCH_EDIT }))).toBeNull();
    expect(configBlocker(config({ mode: "caption", exportForm: null }))).toBe("Carregando as configurações de exportação…");
    expect(configBlocker(config({ mode: "edit", exportForm: null }))).toBeNull();
  });

  it("vai nos corpos de perfil, grupo, links e unificada, com preset e edição desligados", () => {
    const capcut = buildImportCapcut(config());
    const options = { ...postProcessingOptions("capcut", 4, DEFAULT_EDIT_FORM), capcut };
    expect(options).toMatchObject({ processAfterDownload: false, presetId: null, edit: null });
    expect(editProblemOf("capcut", DEFAULT_EDIT_FORM)).toBeNull();
    const single = planFromPreview(one, { content: "all", limit: resolveLimit("last", "10"), ...options });
    expect(single).toEqual({
      kind: "single",
      body: { profile: "perfil_a", content_type: "all", limit: 10, preset_id: null, process: false, capcut },
    });
    const group = planFromPreview(two, { content: "all", limit: resolveLimit("last", "10"), ...options });
    expect(group?.body).toMatchObject({ profiles: ["perfil_a", "perfil_b"], preset_id: null, process: false, capcut });
    expect(buildLinksRequest(links, { processAfterDownload: false, presetId: 4, capcut })).toEqual({
      links: ["https://www.instagram.com/p/ABCDE12345/"],
      preset_id: null,
      process: false,
      capcut,
    });
    const unified = unifiedBody({
      text: "@perfil",
      media: "videos",
      quantity: { ok: true, limit: 50 },
      confirmAll: false,
      periodDays: null,
      minViews: null,
      maxViews: null,
      post: "capcut",
      presetId: 4,
      edit: null,
      capcut,
    });
    expect(unified).toMatchObject({ post_processing: "capcut", capcut });
    expect(unified).not.toHaveProperty("preset_id");
    expect(unified).not.toHaveProperty("edit");
  });

  it("sem a escolha, nenhum corpo leva capcut", () => {
    const options = postProcessingOptions("none", 4, DEFAULT_EDIT_FORM);
    const single = planFromPreview(one, { content: "all", limit: resolveLimit("last", "10"), ...options, capcut: null });
    expect(single?.body).not.toHaveProperty("capcut");
    expect(buildLinksRequest(links, { processAfterDownload: false, presetId: null, capcut: null })).not.toHaveProperty(
      "capcut",
    );
  });
});

describe("lote vinculado à importação", () => {
  const base: Pick<CapCutBatch, "status" | "paused" | "cancel_requested" | "counts" | "output_exists"> = {
    status: "importing",
    paused: false,
    cancel_requested: false,
    counts: { total: 0, pending: 0, running: 0, waiting: 0, held: 0, completed: 0, failed: 0, cancelled: 0 },
    output_exists: false,
  };

  it("status Aguardando downloads e cancelamento disponível sem vídeo", () => {
    expect(BATCH_STATUS_LABEL.importing).toBe("Aguardando downloads");
    expect(batchTone("importing")).toBe("accent");
    expect(batchActions(base).cancel).toBe(true);
    expect(batchActions({ ...base, cancel_requested: true }).cancel).toBe(false);
    expect(batchActions({ ...base, status: "completed" }).cancel).toBe(false);
  });

  it("origem do lote", () => {
    expect(batchSourceText({ source: null })).toBeNull();
    expect(batchSourceText({ source: { kind: "import", batch_ids: [12], group_id: null } })).toBe("Da importação (lote 12)");
    expect(batchSourceText({ source: { kind: "import", batch_ids: [14, 15], group_id: 3 } })).toBe(
      "Da importação (grupo 3 e lotes 14, 15)",
    );
  });

  it("link da Edição CapCut abre o lote", () => {
    expect(capcutForBatch(42)).toBe("/edicao-capcut?lote=42");
    expect(batchIdFromSearch(capcutForBatch(42).split("?")[1] ?? "")).toBe(42);
  });
});
