import { describe, expect, it } from "vitest";
import type { TopVideoItem, TopVideosSearch } from "../api/types";
import {
  buildTopDownloadRequest,
  scanTimeText,
  buildTopRequest,
  formatViews,
  resolveTopCount,
  scanProgressText,
  topDownloadBlocker,
  topDownloadHint,
  topDownloadMessage,
  topResultSummary,
  topResultView,
} from "./topVideos";
import { DEFAULT_EDIT_FORM } from "./editSettings";
import { postProcessingOptions } from "./importOptions";

function item(code: string, views = 10): TopVideoItem {
  return {
    code,
    url: `https://www.instagram.com/reel/${code}/`,
    views,
    posted_at: null,
    pinned: false,
    thumbnail_url: null,
  };
}

describe("buildTopRequest", () => {
  it.each([
    ["@Perfil.Teste", "perfil.teste"],
    ["perfil_teste", "perfil_teste"],
    ["https://www.instagram.com/Perfil_Teste/", "perfil_teste"],
    ["instagram.com/perfil_teste/reels/", "perfil_teste"],
  ])("aceita %s", (text, username) => {
    expect(buildTopRequest(text, "10", 120)).toEqual({
      ok: true,
      body: { profile: username, top: 10, scan_limit: 120 },
    });
  });

  it.each(["", "https://www.instagram.com/p/ABCDEF/", "perfil com espaço"])("recusa o perfil %j", (text) => {
    expect(buildTopRequest(text, "10", 120).ok).toBe(false);
  });

  it("recusa quantidade fora de 1 a 50", () => {
    expect(resolveTopCount("0").ok).toBe(false);
    expect(resolveTopCount("51").ok).toBe(false);
    expect(resolveTopCount("abc").ok).toBe(false);
    expect(resolveTopCount(" 50 ")).toEqual({ ok: true, top: 50 });
    expect(buildTopRequest("perfil", "1,5", 60)).toEqual({ ok: false, error: "Informe um número de 1 a 50." });
  });
});

describe("formatViews", () => {
  it.each([
    [0, "0"],
    [980, "980"],
    [1_000, "1 mil"],
    [45_349, "45,3 mil"],
    [999_999, "999,9 mil"],
    [1_000_000, "1 mi"],
    [1_250_000, "1,2 mi"],
    [123_456_789, "123,4 mi"],
    [2_000_000_000, "2 bi"],
  ])("%d -> %s", (n, text) => {
    expect(formatViews(n)).toBe(text);
  });

  it("progresso no singular e no plural", () => {
    expect(scanProgressText(1)).toBe("1 Reel analisado");
    expect(scanProgressText(1200)).toBe("1.200 Reels analisados");
  });
});

describe("download dos escolhidos", () => {
  const items = [item("CODEA1"), item("CODEB2"), item("CODEA1")];

  it("só baixar manda as URLs sem repetição", () => {
    expect(buildTopDownloadRequest(items, { processAfterDownload: false, presetId: 3 })).toEqual({
      links: ["https://www.instagram.com/reel/CODEA1/", "https://www.instagram.com/reel/CODEB2/"],
      preset_id: null,
      process: false,
    });
  });

  it("preset, edição e Edição CapCut vão como na aba Por link", () => {
    expect(buildTopDownloadRequest([item("CODEA1")], { processAfterDownload: true, presetId: 3 })).toMatchObject({
      preset_id: 3,
      process: true,
    });
    const edit = postProcessingOptions("edit", 3, DEFAULT_EDIT_FORM).edit;
    expect(buildTopDownloadRequest([item("CODEA1")], { processAfterDownload: false, presetId: 3, edit })).toEqual({
      links: ["https://www.instagram.com/reel/CODEA1/"],
      preset_id: null,
      process: false,
      edit,
    });
    const capcut = { mode: "caption" } as never;
    expect(buildTopDownloadRequest([item("CODEA1")], { processAfterDownload: false, presetId: 3, capcut })).toEqual({
      links: ["https://www.instagram.com/reel/CODEA1/"],
      preset_id: null,
      process: false,
      capcut,
    });
  });

  it("nenhum vídeo escolhido não monta pedido", () => {
    expect(buildTopDownloadRequest([], { processAfterDownload: false, presetId: null })).toBeNull();
  });

  it("bloqueios iguais aos da aba Por link, com aviso", () => {
    const base = { sessionOk: true, paused: false, submitting: false, items, process: false, presetId: null };
    expect(topDownloadBlocker(base)).toBeNull();
    expect(topDownloadBlocker({ ...base, sessionOk: false })).toBe("no_session");
    expect(topDownloadBlocker({ ...base, paused: true })).toBe("paused");
    expect(topDownloadBlocker({ ...base, items: [] })).toBe("no_valid");
    expect(topDownloadBlocker({ ...base, process: true })).toBe("preset");
    expect(topDownloadBlocker({ ...base, editProblem: "x" })).toBe("edit");
    expect(topDownloadHint("no_session")).toContain("sessão");
    expect(topDownloadHint("no_valid")).toBeNull();
  });

  it("mensagens de sucesso", () => {
    expect(topDownloadMessage(1, "none")).toBe("Download de 1 vídeo iniciado.");
    expect(topDownloadMessage(10, "capcut")).toBe("Download de 10 vídeos iniciado; os vídeos vão para a Edição CapCut.");
    expect(topDownloadMessage(2, "preset")).toBe("Download e processamento de 2 vídeos iniciados.");
  });
});

describe("topResultSummary", () => {
  const search: TopVideosSearch = {
    id: "a".repeat(32),
    profile: "perfil",
    status: "completed",
    scanned: 120,
    pages: 10,
    items: [item("CODEA1")],
    error_code: null,
    error_message: null,
  };
  it("resume o resultado", () => {
    expect(topResultSummary(search)).toBe("120 Reels analisados • 1 vídeo com mais visualizações");
    expect(topResultSummary({ ...search, items: [] })).toBe("120 Reels analisados • nenhum Reel com contagem de visualizações");
    expect(topResultSummary({ ...search, status: "cancelled" })).toBe("Busca cancelada • ranking PARCIAL: só os 120 Reels mais recentes foram analisados, os mais vistos podem não estar aqui");
  });
});

describe("topResultView", () => {
  const base: TopVideosSearch = {
    id: "b".repeat(32),
    profile: "perfil",
    status: "failed",
    scanned: 24,
    pages: 2,
    items: [item("CODEA1", 900), item("CODEB2", 30)],
    error_code: "rate_limited",
    error_message: "O Instagram limitou as consultas.",
  };

  it("falha no meio mostra o aviso e a lista parcial, que continua baixável", () => {
    expect(topResultView(base)).toEqual({ error: "O Instagram limitou as consultas.", showList: true, showEmpty: false });
    expect(topResultSummary(base)).toBe("Busca interrompida • ranking PARCIAL: só os 24 Reels mais recentes foram analisados, os mais vistos podem não estar aqui");
    expect(
      topDownloadBlocker({ sessionOk: true, paused: false, submitting: false, items: base.items, process: false, presetId: null }),
    ).toBeNull();
  });

  it("falha sem nada lido mostra só o aviso", () => {
    expect(topResultView({ ...base, items: [], error_message: null })).toEqual({
      error: "A busca falhou.",
      showList: false,
      showEmpty: false,
    });
  });

  it("concluída sem itens mostra o vazio; rodando não mostra nada", () => {
    expect(topResultView({ ...base, status: "completed", items: [] })).toEqual({ error: null, showList: false, showEmpty: true });
    expect(topResultView({ ...base, status: "running" })).toEqual({ error: null, showList: false, showEmpty: false });
    expect(topResultView(null).showList).toBe(false);
  });
});

describe("tempo da busca", () => {
  it("estima pelo ritmo de ~7,5 s por página", () => {
    expect(scanTimeText(12)).toBe("menos de 1 min");
    expect(scanTimeText(60)).toBe("menos de 1 min");
    expect(scanTimeText(120)).toBe("~1 min");
    expect(scanTimeText(600)).toBe("~6 min");
  });
});
