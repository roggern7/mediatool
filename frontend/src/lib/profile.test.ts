import { describe, expect, it } from "vitest";
import { compactProfileEntries, parseProfileEntry, parseProfileInput, parseProfileList } from "./profile";

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

describe("parseProfileList (contagem local imediata)", () => {
  it("usuario, @usuario e URLs, um por linha, em minúsculas e na ordem", () => {
    const r = parseProfileList("usuario_um\n@Usuario_Dois\nhttps://www.instagram.com/usuario.tres/?hl=pt\n");
    expect(r).toEqual({ valid: ["usuario_um", "usuario_dois", "usuario.tres"], invalid: [], duplicates: 0 });
  });
  it("linhas vazias e espaços nas pontas são ignorados; CRLF, vírgula e ponto e vírgula separam", () => {
    const r = parseProfileList("\n  a  \r\n\r\nb, c;d\n\n");
    expect(r.valid).toEqual(["a", "b", "c", "d"]);
    expect(r.invalid).toEqual([]);
  });
  it("duplicatas da própria entrada são removidas e contadas (depois de normalizar)", () => {
    const r = parseProfileList("perfil\n@Perfil\nhttps://instagram.com/perfil/\noutro");
    expect(r.valid).toEqual(["perfil", "outro"]);
    expect(r.duplicates).toBe(2);
  });
  it("inválidas ficam listadas com o motivo, sem perder as válidas", () => {
    const r = parseProfileList("bom\nhttps://www.instagram.com/p/ABCDE12345/\nnome com espaco\nhttps://example.com/x\nok.2");
    expect(r.valid).toEqual(["bom", "ok.2"]);
    expect(r.invalid).toEqual([
      { entry: "https://www.instagram.com/p/ABCDE12345/", reason: "A URL não aponta para um perfil." },
      { entry: "nome com espaco", reason: "Use apenas o @usuario ou a URL do perfil." },
      { entry: "https://example.com/x", reason: "A URL não é do Instagram." },
    ]);
  });
  it("texto vazio e lista grande", () => {
    expect(parseProfileList("")).toEqual({ valid: [], invalid: [], duplicates: 0 });
    const big = Array.from({ length: 5000 }, (_, i) => `perfil_${i}`).join("\n");
    expect(parseProfileList(`${big}\n${big}`).valid).toHaveLength(5000);
    expect(parseProfileList(`${big}\n${big}`).duplicates).toBe(5000);
  });
});

describe("parseProfileEntry (mesmas regras da lista no backend)", () => {
  it("aceita http e https, com e sem www, barra final, query e abas do perfil", () => {
    for (const entry of [
      "usuario",
      "@Usuario",
      "http://instagram.com/usuario",
      "http://www.instagram.com/usuario/",
      "HTTP://www.instagram.com/usuario/",
      "https://instagram.com/usuario",
      "https://www.instagram.com/usuario/?hl=pt#topo",
      "https://www.instagram.com/usuario/reels/",
      "https://www.instagram.com/usuario/tagged",
      "www.instagram.com/usuario/",
      "instagram.com/usuario/reels/",
    ]) {
      expect(parseProfileEntry(entry), entry).toEqual({ ok: true, username: "usuario" });
    }
  });
  it("rejeita outros caminhos, nomes reservados, outro domínio e espaço interno", () => {
    for (const entry of [
      "https://www.instagram.com/usuario/p/ABCDE12345/",
      "https://www.instagram.com/usuario/followers/",
      "https://www.instagram.com/usuario/reels/extra/",
      "https://www.instagram.com/p/ABCDE12345/",
      "https://www.instagram.com/reel/ABCDE12345/",
      "https://www.instagram.com/explore/",
      "https://www.instagram.com/",
      "https://www.instagram.com/usuario/../outro",
      "https://example.com/usuario",
      "ftp://instagram.com/usuario",
      "usuario/reels",
      "explore",
      "nome com espaco",
      "usuario..x",
      "",
    ]) {
      expect(parseProfileEntry(entry).ok, entry).toBe(false);
    }
  });
  it("a estimativa local usa essas regras e conta inválida repetida como duplicata", () => {
    const r = parseProfileList("http://instagram.com/a\nhttps://www.instagram.com/a/p/X1234/\nhttps://www.instagram.com/a/p/X1234/\nb c");
    expect(r.valid).toEqual(["a"]);
    expect(r.invalid.map((i) => i.entry)).toEqual(["https://www.instagram.com/a/p/X1234/", "b c"]);
    expect(r.duplicates).toBe(1);
  });
});

describe("compactProfileEntries (corpo menor para o preview)", () => {
  it("apara, tira linhas vazias e separa por linha, vírgula e ponto e vírgula (espaço não separa)", () => {
    expect(compactProfileEntries("  a  \r\n\r\n@b, c;d\n\n nome com espaco \n")).toEqual([
      "a",
      "@b",
      "c",
      "d",
      "nome com espaco",
    ]);
    expect(compactProfileEntries(" \n , ; \r\n")).toEqual([]);
  });
  it("tira query string e fragmento das URLs", () => {
    expect(
      compactProfileEntries(
        "https://www.instagram.com/usuario/?hl=pt&igsh=MXg0abc%3D%3D#topo\ninstagram.com/outro#x\nhttps://www.instagram.com/p/ABC12345/?x=1",
      ),
    ).toEqual(["https://www.instagram.com/usuario/", "instagram.com/outro", "https://www.instagram.com/p/ABC12345/"]);
  });
  it("não mexe no que mudaria a decisão do backend", () => {
    // Sem "/" antes do "?": não é URL; continua inválida como está.
    expect(compactProfileEntries("usuario?x=/a")).toEqual(["usuario?x=/a"]);
    expect(compactProfileEntries("@usuario#x")).toEqual(["@usuario#x"]);
    // Caractere não permitido ou entrada longa demais: inválida inteira, vai sem cortar.
    expect(compactProfileEntries("https://www.instagram.com/usuario/?q=é")).toEqual([
      "https://www.instagram.com/usuario/?q=é",
    ]);
    const long = `https://www.instagram.com/usuario/?q=${"x".repeat(600)}`;
    expect(compactProfileEntries(long)).toEqual([long]);
  });
  it("a entrada compactada dá o mesmo resultado local que a original", () => {
    const text = "https://www.instagram.com/Usuario/?hl=pt\nhttp://instagram.com/b/tagged/#x\nhttps://www.instagram.com/p/A1234567/?y";
    expect(parseProfileList(compactProfileEntries(text).join("\n"))).toEqual({
      ...parseProfileList(text),
      invalid: [{ entry: "https://www.instagram.com/p/A1234567/", reason: "A URL não aponta para um perfil." }],
    });
  });
});
