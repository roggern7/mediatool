import { describe, expect, it } from "vitest";
import type { LinksPreview, ProfilesPreview } from "../api/types";
import { DEFAULT_EDIT_FORM, type EditForm, NO_CHANGES_TEXT } from "./editSettings";
import { buildLinksRequest, linksCreatedMessage, linksStartBlocker } from "./importLinks";
import {
  POST_PROCESSING_DEFAULT,
  POST_PROCESSING_OPTIONS,
  type PostProcessing,
  editProblemOf,
  postProcessingOptions,
  resolveLimit,
  startLabel,
} from "./importOptions";
import { planFromPreview, startBlocker } from "./importStart";

// "Editar após baixar" na importação: corpo enviado nos três modos e escolha exclusiva com o preset.

const FORM: EditForm = { outputType: "mp4", durationMode: "15", customText: "", speed: 1.25, sharpen: true };
const SETTINGS = { duration_mode: "15", custom_seconds: null, speed: 1.25, sharpen: true, output_type: "mp4" };
const one: ProfilesPreview = { valid: ["perfil_a"], invalid: [], duplicates: 0 };
const two: ProfilesPreview = { valid: ["perfil_a", "perfil_b"], invalid: [], duplicates: 0 };
const links: LinksPreview = {
  valid: [{ shortcode: "ABCDE12345", url: "https://www.instagram.com/p/ABCDE12345/" }],
  invalid: [],
  duplicates: 0,
};

function plan(preview: ProfilesPreview, choice: PostProcessing, form: EditForm = FORM) {
  return planFromPreview(preview, {
    content: "all",
    limit: resolveLimit("last", "10"),
    ...postProcessingOptions(choice, 4, form),
  });
}

describe("escolha do pós-processamento", () => {
  it("quatro opções exclusivas; o padrão é só baixar", () => {
    expect(POST_PROCESSING_DEFAULT).toBe("none");
    expect(POST_PROCESSING_OPTIONS.map((o) => o.label)).toEqual([
      "Só baixar",
      "Processar com preset",
      "Editar após baixar",
      "Edição CapCut",
    ]);
  });

  it("preset só em 'Processar com preset' e edição só em 'Editar após baixar'", () => {
    expect(postProcessingOptions("none", 4, FORM)).toEqual({ processAfterDownload: false, presetId: null, edit: null });
    expect(postProcessingOptions("preset", 4, FORM)).toEqual({ processAfterDownload: true, presetId: 4, edit: null });
    expect(postProcessingOptions("edit", 4, FORM)).toEqual({
      processAfterDownload: false,
      presetId: null,
      edit: SETTINGS,
    });
  });
});

describe("corpo enviado", () => {
  it("um perfil: POST /api/imports com `edit` e sem preset", () => {
    expect(plan(one, "edit")).toEqual({
      kind: "single",
      body: { profile: "perfil_a", content_type: "all", limit: 10, preset_id: null, process: false, edit: SETTINGS },
    });
  });

  it("vários perfis: POST /api/import-groups com `edit` para todos", () => {
    expect(plan(two, "edit")).toEqual({
      kind: "group",
      body: {
        profiles: ["perfil_a", "perfil_b"],
        content_type: "all",
        limit: 10,
        preset_id: null,
        process: false,
        edit: SETTINGS,
      },
    });
  });

  it("links: POST /api/import-links com `edit`", () => {
    expect(buildLinksRequest(links, postProcessingOptions("edit", 4, FORM))).toEqual({
      links: ["https://www.instagram.com/p/ABCDE12345/"],
      preset_id: null,
      process: false,
      edit: SETTINGS,
    });
  });

  it("duração personalizada vai em segundos (vírgula aceita)", () => {
    const custom = { ...FORM, durationMode: "custom" as const, customText: "12,5" };
    expect(plan(one, "edit", custom)?.body).toMatchObject({ edit: { duration_mode: "custom", custom_seconds: 12.5 } });
  });

  it("sem edição o corpo é o mesmo de antes (sem a chave `edit`)", () => {
    expect(plan(one, "none")?.body).toEqual({
      profile: "perfil_a",
      content_type: "all",
      limit: 10,
      preset_id: null,
      process: false,
    });
    expect(plan(two, "preset")?.body).toEqual({
      profiles: ["perfil_a", "perfil_b"],
      content_type: "all",
      limit: 10,
      preset_id: 4,
      process: true,
    });
    expect(buildLinksRequest(links, postProcessingOptions("preset", 4, FORM))).toEqual({
      links: ["https://www.instagram.com/p/ABCDE12345/"],
      preset_id: 4,
      process: true,
    });
    for (const choice of ["none", "preset"] as const) {
      expect(plan(one, choice)?.body).not.toHaveProperty("edit");
      expect(buildLinksRequest(links, postProcessingOptions(choice, 4, FORM))).not.toHaveProperty("edit");
    }
  });
});

describe("validação da edição antes de iniciar", () => {
  const base = {
    sessionOk: true,
    paused: false,
    submitting: false,
    preview: one,
    limit: resolveLimit("last", "10"),
    process: false,
    presetId: null,
    providerProblem: null,
  };

  it("mesmas mensagens da página Editar; só vale com 'Editar após baixar'", () => {
    expect(editProblemOf("edit", { ...DEFAULT_EDIT_FORM, outputType: "auto" })).toBe(NO_CHANGES_TEXT);
    // A saída é sempre MP4: sem corte, velocidade ou nitidez, a importação não tem o que editar.
    expect(editProblemOf("edit", DEFAULT_EDIT_FORM)).toBe(NO_CHANGES_TEXT);
    expect(editProblemOf("edit", { ...DEFAULT_EDIT_FORM, sharpen: true })).toBeNull();
    expect(editProblemOf("edit", { ...FORM, durationMode: "custom", customText: "" })).toBe(
      "Informe a duração personalizada em segundos.",
    );
    expect(editProblemOf("edit", FORM)).toBeNull();
    expect(editProblemOf("none", { ...DEFAULT_EDIT_FORM, outputType: "auto" })).toBeNull();
    expect(editProblemOf("preset", { ...FORM, durationMode: "custom", customText: "" })).toBeNull();
  });

  it("formulário inválido bloqueia o início por perfil e por link", () => {
    expect(startBlocker({ ...base, editProblem: NO_CHANGES_TEXT })).toBe("edit");
    expect(startBlocker({ ...base, editProblem: null })).toBeNull();
    const linksBase = { sessionOk: true, paused: false, submitting: false, preview: links, process: false, presetId: null };
    expect(linksStartBlocker({ ...linksBase, editProblem: NO_CHANGES_TEXT })).toBe("edit");
    expect(linksStartBlocker(linksBase)).toBeNull();
  });
});

describe("textos", () => {
  it("botão e mensagem de início com edição", () => {
    expect(startLabel({ process: false, invalid: 0, edit: true })).toBe("Baixar e editar");
    expect(startLabel({ process: false, invalid: 2, edit: true })).toBe("Baixar e editar (ignorar 2 inválidas)");
    expect(startLabel({ process: true, invalid: 0 })).toBe("Baixar e processar");
    expect(linksCreatedMessage({ links: { total: 2 } }, 2, false, true)).toBe("Download e edição de 2 links iniciados.");
  });
});
