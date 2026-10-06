import { describe, expect, it } from "vitest";
import { DEFAULT_PARAMS, forcesReencode, validatePreset } from "./presets";

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

describe("nome do preset", () => {
  it("limita a 60 caracteres", () => {
    expect(validatePreset("a".repeat(60), DEFAULT_PARAMS)).toEqual({});
    expect(validatePreset("a".repeat(61), DEFAULT_PARAMS).name).toBeDefined();
  });
});
