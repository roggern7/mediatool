import { describe, expect, it } from "vitest";
import type { BatchLink, LinksPreview } from "../api/types";
import {
  type LinksStartState,
  buildLinksRequest,
  compactLinkEntries,
  failedLinks,
  failedLinksTitle,
  interpretLinksStartError,
  linkEntriesSummary,
  linksCountText,
  linksCreatedMessage,
  linksStartBlocker,
  linksStartSummary,
  linksSummaryText,
  parseLinkEntry,
  parseLinkList,
} from "./importLinks";
import { entriesKey, settledPreview } from "./importStart";

const preview: LinksPreview = {
  valid: [
    { shortcode: "AAA", url: "https://www.instagram.com/p/AAA/" },
    { shortcode: "BBB", url: "https://www.instagram.com/reel/BBB/" },
  ],
  invalid: [{ entry: "https://example.com/p/x/", reason: "O link não é do Instagram." }],
  duplicates: 1,
};
const none: LinksPreview = { valid: [], invalid: preview.invalid, duplicates: 0 };

describe("parseLinkEntry (estimativa local, mesmas regras do backend)", () => {
  it("aceita /p/, /reel/ e /reels/, com ou sem esquema e www, e normaliza", () => {
    expect(parseLinkEntry("https://www.instagram.com/p/Cabc-1_Z/")).toEqual({
      ok: true,
      shortcode: "Cabc-1_Z",
      url: "https://www.instagram.com/p/Cabc-1_Z/",
    });
    expect(parseLinkEntry("http://instagram.com/reel/XYZ12")).toEqual({
      ok: true,
      shortcode: "XYZ12",
      url: "https://www.instagram.com/reel/XYZ12/",
    });
    expect(parseLinkEntry("  www.instagram.com/reels/XYZ12/  ")).toMatchObject({
      ok: true,
      url: "https://www.instagram.com/reel/XYZ12/",
    });
    expect(parseLinkEntry("HTTPS://WWW.INSTAGRAM.COM/P/AAAAA/")).toMatchObject({
      ok: true,
      url: "https://www.instagram.com/p/AAAAA/",
    });
    expect(parseLinkEntry("https://www.instagram.com:443/p/AAAAA")).toMatchObject({ ok: true, shortcode: "AAAAA" });
  });
  it("descarta parâmetros e fragmento", () => {
    expect(parseLinkEntry("https://www.instagram.com/p/AAAAA/?igsh=abc&img_index=2#x")).toMatchObject({
      ok: true,
      url: "https://www.instagram.com/p/AAAAA/",
    });
    expect(parseLinkEntry("instagram.com/reel/BBBBB?utm_source=ig")).toMatchObject({ ok: true, shortcode: "BBBBB" });
  });
  it("aceita o usuário na frente de /p/, /reel/ e /reels/, se for um usuário válido", () => {
    expect(parseLinkEntry("https://www.instagram.com/usuario/p/AAAAA/")).toMatchObject({
      ok: true,
      url: "https://www.instagram.com/p/AAAAA/",
    });
    expect(parseLinkEntry("https://www.instagram.com/usuario.um/reels/AAAAA/")).toMatchObject({
      ok: true,
      url: "https://www.instagram.com/reel/AAAAA/",
    });
    for (const first of ["share", "explore", "stories", "tv", "accounts", "@usuario", "usu..ario", "..."]) {
      expect(parseLinkEntry(`https://www.instagram.com/${first}/p/AAAAA/`), first).toEqual({
        ok: false,
        error: "A URL não aponta para uma publicação.",
      });
    }
  });
  it("código de 5 a 64 caracteres, só letras, números, _ e -", () => {
    expect(parseLinkEntry("https://www.instagram.com/p/AAAA/")).toEqual({
      ok: false,
      error: "Código da publicação inválido.",
    });
    expect(parseLinkEntry(`https://www.instagram.com/p/${"A".repeat(64)}/`).ok).toBe(true);
    expect(parseLinkEntry(`https://www.instagram.com/p/${"A".repeat(65)}/`).ok).toBe(false);
    expect(parseLinkEntry("https://www.instagram.com/p/AAA.AA/").ok).toBe(false);
  });
  it("recusa o que não é link de publicação do Instagram", () => {
    for (const entry of [
      "",
      "usuario",
      "p/AAAAA",
      "m.instagram.com/p/AAAAA/",
      "https://m.instagram.com/p/AAAAA/",
      "https://example.com/p/AAAAA/",
      "https://instagram.com.example.com/p/AAAAA/",
      "https://user@www.instagram.com/p/AAAAA/",
      "https://www.instagram.com:8443/p/AAAAA/",
      "https://www.instagram.com/usuario/",
      "https://www.instagram.com/p/",
      "https://www.instagram.com/stories/usuario/12345/",
      "https://www.instagram.com/p/AAAAA/extra/mais/",
      "https://www.instagram.com/tv/AAAAA/",
      "https://www.instagram.com/p/../AAAAA/",
      "https://www.instagram.com/p/AAA%41A/",
      "https://www.instagram.com/p/AA AAA/",
      "https://www.instagram.com/p/AAAAÁ/",
      "https://www.instagram.com\\p\\AAAAA",
      "ftp://www.instagram.com/p/AAAAA/",
      `https://www.instagram.com/p/AAAAA/?x=${"a".repeat(512)}`,
    ]) {
      expect(parseLinkEntry(entry).ok, entry).toBe(false);
    }
  });
});

describe("lista colada", () => {
  const text = [
    "https://www.instagram.com/p/AAAAA/",
    "",
    "  instagram.com/reel/BBBBB/?igsh=1  ",
    "https://www.instagram.com/reels/AAAAA/ https://www.instagram.com/p/CCCCC/,nada;nada",
    "\thttps://www.instagram.com/p/DDDDD/\t",
  ].join("\r\n");
  it("compactLinkEntries separa por linha, vírgula, ponto e vírgula e espaço, com os parâmetros intactos", () => {
    expect(compactLinkEntries(text)).toEqual([
      "https://www.instagram.com/p/AAAAA/",
      "instagram.com/reel/BBBBB/?igsh=1",
      "https://www.instagram.com/reels/AAAAA/",
      "https://www.instagram.com/p/CCCCC/",
      "nada",
      "nada",
      "https://www.instagram.com/p/DDDDD/",
    ]);
    expect(compactLinkEntries(" \n ,; \n")).toEqual([]);
  });
  it("parseLinkList: repetido é o mesmo código em qualquer forma de URL; inválida repetida também conta", () => {
    expect(parseLinkList(text)).toEqual({ valid: 4, invalid: 1, duplicates: 2 });
    expect(parseLinkList("")).toEqual({ valid: 0, invalid: 0, duplicates: 0 });
  });
});

describe("textos da lista", () => {
  it("contagem e resumo das entradas", () => {
    expect(linksCountText(1)).toBe("1 link");
    expect(linksCountText(1500)).toBe("1.500 links");
    expect(linkEntriesSummary({ valid: 3, duplicates: 0, invalid: 0 })).toBe("3 links válidos");
    expect(linkEntriesSummary({ valid: 1, duplicates: 1, invalid: 1 })).toBe(
      "1 link válido • 1 repetido removido • 1 entrada inválida",
    );
    expect(linkEntriesSummary({ valid: 0, duplicates: 2, invalid: 3 })).toBe(
      "0 links válidos • 2 repetidos removidos • 3 entradas inválidas",
    );
  });
  it("resumo antes de iniciar só com link válido", () => {
    expect(linksStartSummary(null)).toBeNull();
    expect(linksStartSummary(none)).toBeNull();
    expect(linksStartSummary(preview)).toBe("2 links • Cada publicação inteira (foto, Reel, vídeo ou carrossel)");
    expect(linksStartSummary({ ...preview, valid: preview.valid.slice(0, 1) })).toBe(
      "1 link • A publicação inteira (foto, Reel, vídeo ou carrossel)",
    );
  });
});

describe("pode iniciar por link?", () => {
  const base: LinksStartState = {
    sessionOk: true,
    paused: false,
    submitting: false,
    preview,
    process: false,
    presetId: null,
  };
  it("tudo certo: pode, mesmo com inválidos (seguem só os válidos)", () => {
    expect(linksStartBlocker(base)).toBeNull();
    expect(linksStartBlocker({ ...base, process: true, presetId: 4 })).toBeNull();
  });
  it("cada condição bloqueia sozinha", () => {
    expect(linksStartBlocker({ ...base, submitting: true })).toBe("submitting");
    expect(linksStartBlocker({ ...base, sessionOk: false })).toBe("no_session");
    expect(linksStartBlocker({ ...base, paused: true })).toBe("paused");
    expect(linksStartBlocker({ ...base, preview: null })).toBe("no_preview");
    expect(linksStartBlocker({ ...base, preview: none })).toBe("no_valid");
    expect(linksStartBlocker({ ...base, process: true, presetId: null })).toBe("preset");
  });
  it("o preview assentado genérico serve para os links", () => {
    const key = entriesKey(["a", "b"]);
    const ok = { currentKey: key, requestedKey: key, typing: false, loading: false, result: { key, data: preview } };
    expect(settledPreview(ok)).toBe(preview);
    expect(settledPreview({ ...ok, loading: true })).toBeNull();
  });
});

describe("pedido e resposta da criação", () => {
  it("manda só as URLs normalizadas do preview; download puro zera o preset", () => {
    expect(buildLinksRequest(preview, { processAfterDownload: false, presetId: 4 })).toEqual({
      links: ["https://www.instagram.com/p/AAA/", "https://www.instagram.com/reel/BBB/"],
      preset_id: null,
      process: false,
    });
    expect(buildLinksRequest(preview, { processAfterDownload: true, presetId: 4 })).toMatchObject({
      preset_id: 4,
      process: true,
    });
    expect(buildLinksRequest(null, { processAfterDownload: false, presetId: null })).toBeNull();
    expect(buildLinksRequest(none, { processAfterDownload: false, presetId: null })).toBeNull();
  });
  it("mensagem de sucesso usa o total do lote ou, sem ele, o que foi enviado", () => {
    expect(linksCreatedMessage({ links: { total: 3 } }, 5, false)).toBe("Download de 3 links iniciado.");
    expect(linksCreatedMessage({}, 1, false)).toBe("Download de 1 link iniciado.");
    expect(linksCreatedMessage({ links: null }, 2, true)).toBe("Download e processamento de 2 links iniciados.");
  });
  it("erro: invalid_links refaz a pré-validação; os demais só mostram a mensagem", () => {
    expect(interpretLinksStartError({ status: 422, code: "invalid_links", message: "Há links inválidos." })).toEqual({
      error: "Há links inválidos.",
      refreshPreview: true,
    });
    expect(interpretLinksStartError({ status: 409, code: "instagram_paused", message: "Em pausa." })).toEqual({
      error: "Em pausa.",
      refreshPreview: false,
    });
    expect(interpretLinksStartError(null)).toEqual({ error: "Erro inesperado.", refreshPreview: false });
    expect(interpretLinksStartError({ status: 413, code: "http_413", message: "x" }).error).toContain(
      "grande demais",
    );
  });
});

describe("lote por link", () => {
  it("resumo dos links: só as partes que existem", () => {
    expect(linksSummaryText(null)).toBeNull();
    expect(linksSummaryText(undefined)).toBeNull();
    expect(linksSummaryText({ total: 3, pending: 3, resolved: 0, failed: 0 })).toBe("3 links • 3 pendentes");
    expect(linksSummaryText({ total: 4, pending: 1, resolved: 2, failed: 1 })).toBe(
      "4 links • 2 resolvidos • 1 com falha • 1 pendente",
    );
    expect(linksSummaryText({ total: 1, pending: 0, resolved: 1, failed: 0 })).toBe("1 link • 1 resolvido");
  });
  it("links que falharam, com o motivo em texto", () => {
    const link = (over: Partial<BatchLink>): BatchLink => ({
      position: 1,
      url: "https://www.instagram.com/p/AAA/",
      shortcode: "AAA",
      status: "resolved",
      error_code: null,
      error_message: null,
      items_found: 1,
      profile: "perfil",
      ...over,
    });
    const items = [
      link({}),
      link({ position: 2, status: "failed", error_code: "content_unavailable", error_message: "Publicação removida." }),
      link({ position: 3, status: "pending" }),
      link({ position: 4, status: "failed", error_code: "codigo_novo", error_message: null }),
      link({ position: 5, status: "cancelled" }),
    ];
    expect(failedLinks(items)).toEqual([
      { position: 2, url: "https://www.instagram.com/p/AAA/", reason: "Publicação removida." },
      { position: 4, url: "https://www.instagram.com/p/AAA/", reason: "Erro (codigo_novo)." },
    ]);
    expect(failedLinksTitle(1)).toBe("1 link não pôde ser importado");
    expect(failedLinksTitle(3)).toBe("3 links não puderam ser importados");
  });
});
