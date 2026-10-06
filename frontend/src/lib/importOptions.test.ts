import { describe, expect, it } from "vitest";
import {
  LIMIT_OPTIONS,
  PROCESS_AFTER_DOWNLOAD_DEFAULT,
  buildImportRequest,
  canSubmitImport,
  limitAt,
} from "./importOptions";

describe("buildImportRequest", () => {
  it("o padrão é download puro, com process e preset_id explícitos", () => {
    expect(PROCESS_AFTER_DOWNLOAD_DEFAULT).toBe(false);
    const body = buildImportRequest({ profile: "perfil", content: "all", limitIndex: 1 });
    expect(body).toEqual({ profile: "perfil", content_type: "all", limit: 20, preset_id: null, process: false });
    expect(Object.keys(body)).toContain("process");
    expect(Object.keys(body)).toContain("preset_id");
  });
  it("download puro ignora o preset escolhido na tela", () => {
    const body = buildImportRequest({
      profile: "perfil",
      content: "reels",
      limitIndex: 0,
      processAfterDownload: false,
      presetId: 7,
    });
    expect(body.process).toBe(false);
    expect(body.preset_id).toBeNull();
  });
  it("com processamento ligado manda process true e o preset", () => {
    const body = buildImportRequest({
      profile: "perfil",
      content: "videos",
      limitIndex: 2,
      processAfterDownload: true,
      presetId: 7,
    });
    expect(body).toEqual({ profile: "perfil", content_type: "videos", limit: 50, preset_id: 7, process: true });
  });
  it("processamento ligado exige preset; download puro não", () => {
    expect(canSubmitImport(false, null)).toBe(true);
    expect(canSubmitImport(true, null)).toBe(false);
    expect(canSubmitImport(true, 3)).toBe(true);
  });
});

describe("limitAt", () => {
  it("mapeia cada opção para o limite da API", () => {
    expect(LIMIT_OPTIONS.map((_, i) => limitAt(i))).toEqual([10, 20, 50, 100, 10]);
  });
  it("Todos aparece desabilitado e nunca envia null", () => {
    const todos = LIMIT_OPTIONS.find((o) => o.label.startsWith("Todos"));
    expect(todos?.disabled).toBe(true);
    expect(todos?.label).toBe("Todos (indisponível por enquanto)");
    expect(LIMIT_OPTIONS.filter((o) => !o.disabled).map((o) => o.value)).toEqual([10, 20, 50, 100]);
    expect(limitAt(LIMIT_OPTIONS.indexOf(todos!))).toBe(10);
  });
});
