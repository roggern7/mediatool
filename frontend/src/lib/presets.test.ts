import { describe, expect, it } from "vitest";
import {
  DEFAULT_PARAMS,
  OPTIONAL_ADJUSTMENTS,
  compactParams,
  forcesReencode,
  hasActiveTransforms,
  isOptionalActive,
  optionalSummary,
  optionalValue,
  validatePreset,
} from "./presets";

describe("validatePreset", () => {
  it("aceita os padrões", () => {
    expect(validatePreset("Meu preset", DEFAULT_PARAMS)).toEqual({});
  });
  it("exige nome", () => {
    expect(validatePreset("   ", DEFAULT_PARAMS).name).toBeDefined();
  });
  it("limita corte e velocidade", () => {
    expect(validatePreset("a", { ...DEFAULT_PARAMS, trim_start: 30 })).toEqual({});
    expect(validatePreset("a", { ...DEFAULT_PARAMS, trim_start: 30.1 }).trim_start).toBeDefined();
    expect(validatePreset("a", { ...DEFAULT_PARAMS, trim_start: -0.1 }).trim_start).toBeDefined();
    expect(validatePreset("a", { ...DEFAULT_PARAMS, speed: 0.5 })).toEqual({});
    expect(validatePreset("a", { ...DEFAULT_PARAMS, speed: 2 })).toEqual({});
    expect(validatePreset("a", { ...DEFAULT_PARAMS, speed: 2.01 }).speed).toBeDefined();
    expect(validatePreset("a", { ...DEFAULT_PARAMS, speed: Number.NaN }).speed).toBeDefined();
  });
});

describe("forcesReencode", () => {
  const plain = { metadata_cleanup: true, trim_start: 0, mirror: false, speed: 1, auto_adjust: false, reencode: false };
  it("só limpeza não força reencode", () => {
    expect(forcesReencode(plain)).toBe(false);
  });
  it("transformação ativa com reencode desligado força", () => {
    expect(forcesReencode({ ...plain, mirror: true })).toBe(true);
    expect(forcesReencode({ ...plain, speed: 1.05 })).toBe(true);
    expect(forcesReencode({ ...plain, trim_start: 0.2 })).toBe(true);
  });
  it("reencode ligado nunca mostra a nota", () => {
    expect(forcesReencode({ ...DEFAULT_PARAMS, reencode: true })).toBe(false);
  });
});

describe("ajustes opcionais", () => {
  const plain = { metadata_cleanup: true, trim_start: 0, mirror: false, speed: 1, auto_adjust: false, reencode: false };
  const neutral = { zoom: 0, brightness: 0, contrast: 1, saturation: 1, trim_end: 0, pitch_shift: 0, grain: 0 };
  const antiDuplicate = { ...DEFAULT_PARAMS, zoom: 3, brightness: 0.01, contrast: 1.02, saturation: 1.03, trim_end: 0.3 };

  it("preset novo é o Padrão: só as 6 chaves originais", () => {
    expect(Object.keys(DEFAULT_PARAMS).sort()).toEqual(
      ["auto_adjust", "metadata_cleanup", "mirror", "reencode", "speed", "trim_start"],
    );
    expect(compactParams({ ...DEFAULT_PARAMS, ...neutral })).toEqual(DEFAULT_PARAMS);
  });
  it("preset antigo (sem as chaves novas) vale como neutro", () => {
    for (const adjustment of OPTIONAL_ADJUSTMENTS) {
      expect(optionalValue(DEFAULT_PARAMS, adjustment)).toBe(adjustment.neutral);
      expect(isOptionalActive(DEFAULT_PARAMS, adjustment)).toBe(false);
    }
    expect(optionalSummary(DEFAULT_PARAMS)).toEqual([]);
    expect(validatePreset("a", DEFAULT_PARAMS)).toEqual({});
  });
  it("compactParams só mantém os ajustes ligados", () => {
    expect(compactParams({ ...plain, ...neutral, zoom: 2, grain: 1 })).toEqual({ ...plain, zoom: 2, grain: 1 });
    expect(compactParams(antiDuplicate)).toEqual(antiDuplicate);
  });
  it("valida as faixas", () => {
    expect(validatePreset("a", antiDuplicate)).toEqual({});
    for (const adjustment of OPTIONAL_ADJUSTMENTS) {
      const { key, min, max } = adjustment;
      expect(validatePreset("a", { ...plain, [key]: min })).toEqual({});
      expect(validatePreset("a", { ...plain, [key]: max })).toEqual({});
      expect(validatePreset("a", { ...plain, [key]: max + 0.01 })[key]).toBe(adjustment.error);
      expect(validatePreset("a", { ...plain, [key]: min - 0.01 })[key]).toBe(adjustment.error);
      expect(validatePreset("a", { ...plain, [key]: Number.NaN })[key]).toBe(adjustment.error);
      expect(adjustment.suggested).not.toBe(adjustment.neutral);
      expect(validatePreset("a", { ...plain, [key]: adjustment.suggested })).toEqual({});
    }
  });
  it("qualquer ajuste ligado conta como transformação e força reencode", () => {
    expect(hasActiveTransforms({ ...plain, ...neutral })).toBe(false);
    for (const adjustment of OPTIONAL_ADJUSTMENTS) {
      const params = { ...plain, [adjustment.key]: adjustment.suggested };
      expect(hasActiveTransforms(params)).toBe(true);
      expect(forcesReencode(params)).toBe(true);
      expect(forcesReencode({ ...params, reencode: true })).toBe(false);
    }
  });
  it("resume os ajustes ligados", () => {
    expect(optionalSummary(antiDuplicate)).toEqual(["Zoom 3%", "Cor", "Corta 0,3 s do final"]);
    expect(optionalSummary({ ...plain, pitch_shift: 1.5, grain: 2 })).toEqual(["Tom +1,5%", "Granulação"]);
    expect(optionalSummary({ ...plain, pitch_shift: -2, saturation: 1.1 })).toEqual(["Cor", "Tom -2%"]);
    expect(optionalSummary({ ...plain, ...neutral })).toEqual([]);
  });
});

describe("nome do preset", () => {
  it("limita a 60 caracteres", () => {
    expect(validatePreset("a".repeat(60), DEFAULT_PARAMS)).toEqual({});
    expect(validatePreset("a".repeat(61), DEFAULT_PARAMS).name).toBeDefined();
  });
});
