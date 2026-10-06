import { describe, expect, it } from "vitest";
import { parseProfileInput } from "./profile";

function ok(v: string) {
  const r = parseProfileInput(v);
  return r.ok ? r.username : null;
}

describe("parseProfileInput", () => {
  it("aceita username simples e com @", () => {
    expect(ok("natgeo")).toBe("natgeo");
    expect(ok("@nat.geo_1")).toBe("nat.geo_1");
    expect(ok("  @NatGeo  ")).toBe("natgeo");
  });
  it("aceita URLs de perfil", () => {
    expect(ok("https://www.instagram.com/natgeo/")).toBe("natgeo");
    expect(ok("instagram.com/natgeo?hl=pt")).toBe("natgeo");
    expect(ok("https://instagram.com/natgeo/reels/")).toBe("natgeo");
  });
  it("rejeita entradas inválidas", () => {
    expect(ok("")).toBeNull();
    expect(ok("nome com espaço")).toBeNull();
    expect(ok("a..b")).toBeNull();
    expect(ok("x".repeat(31))).toBeNull();
    expect(ok("https://www.instagram.com/p/ABC123/")).toBeNull();
    expect(ok("https://www.instagram.com/reel/ABC123/")).toBeNull();
    expect(ok("https://example.com/natgeo")).toBeNull();
    expect(ok("https://www.instagram.com/")).toBeNull();
  });
});

describe("parseProfileInput alinhado ao backend", () => {
  it("exige https em URLs", () => {
    expect(ok("http://www.instagram.com/natgeo/")).toBeNull();
  });
  it("aceita só instagram.com e www.instagram.com", () => {
    expect(ok("https://m.instagram.com/natgeo/")).toBeNull();
    expect(ok("www.instagram.com/natgeo")).toBe("natgeo");
  });
  it("rejeita nomes reservados e sem letra ou número", () => {
    expect(ok("explore")).toBeNull();
    expect(ok("@stories")).toBeNull();
    expect(ok("___")).toBeNull();
  });
  it("rejeita caracteres fora de ASCII imprimível", () => {
    expect(ok("joão")).toBeNull();
  });
});
