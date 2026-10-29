import { describe, expect, it } from "vitest";
import type { Media } from "../api/types";
import * as editSettings from "./editSettings";
import {
  DEFAULT_EDIT_FORM,
  type EditForm,
  NO_CHANGES_TEXT,
  formatEditDuration,
  formatEditSpeed,
  isNoChange,
  mergeSelection,
  pickerQueryKey,
  predictedDuration,
  snapSpeed,
  parseSpeedInput,
  formatSpeedPreset,
  startLabel,
  toEditRequest,
  validateEditForm,
} from "./editSettings";

const form = (patch: Partial<EditForm> = {}): EditForm => ({ ...DEFAULT_EDIT_FORM, ...patch });

function media(id: number, type: "video" | "image" = "video"): Media {
  return { id, media_type: type } as Media;
}

describe("editSettings: padrões e validação", () => {
  it("padrão: MP4, duração original, 1,0x, sem nitidez, válido", () => {
    expect(DEFAULT_EDIT_FORM).toEqual({
      outputType: "mp4",
      durationMode: "original",
      customText: "",
      speed: 1,
      sharpen: false,
    });
    expect(validateEditForm(DEFAULT_EDIT_FORM)).toBeNull();
  });

  it("velocidade fora de 1,0x a 1,5x é recusada", () => {
    expect(validateEditForm(form({ speed: 1.6 }))).toMatch(/entre 1,0x e 1,5x/);
    expect(validateEditForm(form({ speed: 0.9 }))).toMatch(/entre 1,0x e 1,5x/);
    expect(validateEditForm(form({ speed: Number.NaN }))).not.toBeNull();
    expect(validateEditForm(form({ speed: 1.5 }))).toBeNull();
  });

  it("personalizada vazia, zero, negativa ou acima de 7200 s é recusada; vírgula é aceita", () => {
    expect(validateEditForm(form({ durationMode: "custom", customText: "" }))).toMatch(/Informe/);
    expect(validateEditForm(form({ durationMode: "custom", customText: "abc" }))).toMatch(/Informe/);
    expect(validateEditForm(form({ durationMode: "custom", customText: "0" }))).toMatch(/maior que zero/);
    expect(validateEditForm(form({ durationMode: "custom", customText: "-3" }))).toMatch(/maior que zero/);
    expect(validateEditForm(form({ durationMode: "custom", customText: "7201" }))).toMatch(/7200/);
    expect(validateEditForm(form({ durationMode: "custom", customText: "12,5" }))).toBeNull();
  });

  it("nenhuma edição: só com Automático, original, 1,0x e sem nitidez", () => {
    const none = form({ outputType: "auto" });
    expect(isNoChange(none)).toBe(true);
    expect(validateEditForm(none)).toBe(NO_CHANGES_TEXT);
    expect(isNoChange(form())).toBe(false); // MP4 sozinho normaliza para H.264/AAC
    expect(isNoChange(form({ outputType: "auto", sharpen: true }))).toBe(false);
    expect(isNoChange(form({ outputType: "auto", speed: 1.05 }))).toBe(false);
    expect(isNoChange(form({ outputType: "auto", durationMode: "15" }))).toBe(false);
  });

  it("snapSpeed prende à faixa e ao passo de 0,01", () => {
    expect(snapSpeed(1.234)).toBe(1.23);
    expect(snapSpeed(1.07)).toBe(1.07);
    expect(snapSpeed(1.0749)).toBe(1.07);
    expect(snapSpeed(0.5)).toBe(1);
    expect(snapSpeed(2)).toBe(1.5);
    expect(snapSpeed(Number.NaN)).toBe(1);
  });

  it("formatos pt-BR", () => {
    expect(formatEditSpeed(1.25)).toBe("1,25x");
    expect(formatEditSpeed(1)).toBe("1,00x");
    expect(formatEditDuration(15.03)).toBe("15,03 s");
    expect(formatEditDuration(13.3333)).toBe("13,33 s");
    expect(formatEditDuration(null)).toBe("-");
    expect(startLabel(1)).toBe("Iniciar edição (1 vídeo)");
    expect(startLabel(3)).toBe("Iniciar edição (3 vídeos)");
  });
});

describe("editSettings: pedido e duração prevista", () => {
  it("toEditRequest monta o corpo do contrato", () => {
    expect(toEditRequest([3, 4], form({ durationMode: "15", speed: 1.25, sharpen: true }))).toEqual({
      media_ids: [3, 4],
      settings: { duration_mode: "15", custom_seconds: null, speed: 1.25, sharpen: true, output_type: "mp4" },
    });
    expect(toEditRequest([1], form({ durationMode: "custom", customText: "45,5", outputType: "auto" }))).toEqual({
      media_ids: [1],
      settings: { duration_mode: "custom", custom_seconds: 45.5, speed: 1, sharpen: false, output_type: "auto" },
    });
  });

  it("predictedDuration: velocidade divide, limite + 0,03 corta, vídeo menor não é prolongado", () => {
    expect(predictedDuration(20, form({ speed: 1.5, durationMode: "15" }))).toBeCloseTo(13.333, 3);
    expect(predictedDuration(40, form({ durationMode: "15" }))).toBeCloseTo(15.03, 6);
    expect(predictedDuration(40, form({ durationMode: "custom", customText: "30" }))).toBeCloseTo(30.03, 6);
    expect(predictedDuration(40, form())).toBe(40);
    expect(predictedDuration(10, form({ durationMode: "60" }))).toBe(10);
    expect(predictedDuration(null, form())).toBeNull();
    expect(predictedDuration(0, form())).toBeNull();
  });
});

describe("editSettings: seleção", () => {
  it("só vídeos, sem repetir e até o teto", () => {
    const first = mergeSelection([], [media(1), media(2, "image"), media(1), media(3)]);
    expect(first.selection.map((m) => m.id)).toEqual([1, 3]);
    expect(first.added).toBe(2);
    const capped = mergeSelection(first.selection, [media(4), media(5), media(3)], 3);
    expect(capped.selection.map((m) => m.id)).toEqual([1, 3, 4]);
    expect(capped.added).toBe(1);
    expect(capped.overflow).toBe(1);
  });
});

describe("editSettings: recarga da lista da biblioteca", () => {
  it("a chave muda só com a versão (envio pronto), mantendo busca e página", () => {
    const before = pickerQueryKey("perfil", 120, 0);
    const after = pickerQueryKey("perfil", 120, 1);
    expect(after).not.toBe(before);
    expect(after).toContain("perfil");
    expect(after).toContain("120");
    expect(pickerQueryKey("perfil", 120, 1)).toBe(after);
    expect(pickerQueryKey("", null, 0)).toBe("edit-videos:::0");
  });
});

describe("exportação das versões editadas", () => {
  it("não há mais a opção desabilitada de exportar ao concluir; a dica aponta o painel do lote", () => {
    expect("AUTO_EXPORT_LABEL" in editSettings).toBe(false);
    expect("AUTO_EXPORT_UNAVAILABLE" in editSettings).toBe(false);
    expect(editSettings.EXPORT_AFTER_HINT).toBe("Ao terminar, baixe o ZIP das versões editadas no painel do lote.");
    expect(editSettings.AUTO_SAVE_TEXT).toContain("versões editadas são salvas automaticamente");
  });
});


describe("velocidade digitada e atalhos", () => {
  it("formatSpeedPreset é curto", () => {
    expect(formatSpeedPreset(1)).toBe("1x");
    expect(formatSpeedPreset(1.05)).toBe("1,05x");
    expect(formatSpeedPreset(1.1)).toBe("1,1x");
  });
  it("parseSpeedInput aceita vírgula, ponto e sufixo x", () => {
    expect(parseSpeedInput("1,07")).toBe(1.07);
    expect(parseSpeedInput(" 1.2 ")).toBe(1.2);
    expect(parseSpeedInput("1,15x")).toBe(1.15);
    expect(parseSpeedInput("1")).toBe(1);
  });
  it("parseSpeedInput recusa vazio, texto e fora da faixa", () => {
    expect(parseSpeedInput("")).toBeNull();
    expect(parseSpeedInput("abc")).toBeNull();
    expect(parseSpeedInput("0,9")).toBeNull();
    expect(parseSpeedInput("1,6")).toBeNull();
  });
});
