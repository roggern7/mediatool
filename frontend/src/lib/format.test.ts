import { describe, expect, it } from "vitest";
import {
  formatBytes,
  formatDuration,
  formatSpeed,
  formatResolution,
  formatVideoCodec,
  parseDecimal,
  formatPercent,
} from "./format";

describe("formatBytes", () => {
  it("formata unidades em pt-BR", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2 KB");
    expect(formatBytes(5.5 * 1024 * 1024)).toBe("5,5 MB");
    expect(formatBytes(1.25 * 1024 ** 3)).toBe("1,25 GB");
  });
  it("trata ausente", () => {
    expect(formatBytes(null)).toBe("-");
    expect(formatBytes(-1)).toBe("-");
  });
});

describe("formatDuration", () => {
  it("usa mm:ss", () => {
    expect(formatDuration(0)).toBe("00:00");
    expect(formatDuration(5.4)).toBe("00:05");
    expect(formatDuration(75)).toBe("01:15");
  });
  it("usa h:mm:ss acima de uma hora", () => {
    expect(formatDuration(3725)).toBe("1:02:05");
  });
  it("trata ausente", () => {
    expect(formatDuration(null)).toBe("-");
  });
});

describe("outros formatadores", () => {
  it("velocidade com vírgula", () => {
    expect(formatSpeed(1.05)).toBe("1,05x");
    expect(formatSpeed(1)).toBe("1,00x");
  });
  it("resolução", () => {
    expect(formatResolution(1080, 1920)).toBe("1080×1920");
    expect(formatResolution(null, 10)).toBe("-");
  });
  it("codec de vídeo do original", () => {
    expect(formatVideoCodec("vp9")).toBe("VP9");
    expect(formatVideoCodec("h264")).toBe("H.264");
    expect(formatVideoCodec("mpeg4")).toBe("MPEG4");
    expect(formatVideoCodec(null)).toBe("-");
  });
  it("percentual limitado", () => {
    expect(formatPercent(0.456)).toBe("46%");
    expect(formatPercent(2)).toBe("100%");
  });
  it("decimal com vírgula ou ponto", () => {
    expect(parseDecimal("1,05")).toBe(1.05);
    expect(parseDecimal("0.5")).toBe(0.5);
    expect(parseDecimal("2")).toBe(2);
    expect(parseDecimal("abc")).toBeNull();
    expect(parseDecimal("")).toBeNull();
  });
});
