import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import type { ProfileAnalysisItem, ProfileAnalysisStatus } from "../api/types";
import { formatClock } from "./format";
import {
  EMPTY_CELL,
  ANALYSIS_STATUS_LABEL,
  MAX_PROFILES,
  analysisIdFromSearch,
  analysisIndicators,
  analysisPollMs,
  analysisReportUrl,
  analysisRoute,
  analysisTone,
  checkCell,
  accountCards,
  clearedText,
  compactProfileText,
  countProfileLines,
  endNoticeText,
  estimateText,
  ignoredText,
  isAnalysisActive,
  itemCells,
  lineCountText,
  overLimitText,
  pageInfo,
  pageOffset,
  parsePage,
  postsCell,
  progressFraction,
  progressText,
  startErrorText,
  viewsCell,
  waitingText,
} from "./profileAnalysis";

function item(patch: Partial<ProfileAnalysisItem> = {}): ProfileAnalysisItem {
  return {
    position: 1,
    username: "perfil",
    state: "done",
    account_status: "active",
    display_name: null,
    posts_state: "known",
    posts_count: 8630,
    posts_exact: 1,
    posts_text: null,
    views_state: "known",
    views_total: 1500,
    views_reels: 12,
    stories: "yes",
    highlights: "no",
    error_code: null,
    error_message: null,
    checked_at: "2026-10-31T23:58:01.123Z",
    checked_date: "31/10/2026",
    checked_time: "20:58:01",
    attempts: 1,
    ...patch,
  };
}

describe("contagem local da lista", () => {
  it("conta uma entrada por linha, normaliza e ignora linhas em branco", () => {
    const text = "@a\n\nA\r\nhttps://instagram.com/a/\ninstagram.com/b?igsh=1\n  www.instagram.com/c/  \n";
    expect(countProfileLines(text)).toEqual({ profiles: 3, invalid: 0, duplicates: 2 });
  });

  it("linha inválida conta à parte e vírgula não separa entradas", () => {
    expect(countProfileLines("nome com espaço\nhttps://exemplo.com/a\nb")).toEqual({
      profiles: 1,
      invalid: 2,
      duplicates: 0,
    });
    expect(countProfileLines("a,b").invalid).toBe(1);
    expect(countProfileLines("http://instagram.com/alfa\nhttp://exemplo.com/a")).toEqual({
      profiles: 1,
      invalid: 1,
      duplicates: 0,
    });
  });

  it("texto vazio não tem perfis", () => {
    expect(countProfileLines("  \n\n")).toEqual({ profiles: 0, invalid: 0, duplicates: 0 });
  });

  it("descreve a contagem em texto curto", () => {
    expect(lineCountText({ profiles: 1, invalid: 0, duplicates: 0 })).toBe("1 perfil");
    expect(lineCountText({ profiles: 1200, invalid: 2, duplicates: 1 })).toBe(
      "1.200 perfis · 2 linhas inválidas · 1 duplicata",
    );
  });

  it("avisa só quando a lista passa do limite", () => {
    expect(MAX_PROFILES).toBe(10000);
    expect(overLimitText({ profiles: 10000, invalid: 0, duplicates: 0 })).toBeNull();
    expect(overLimitText({ profiles: 10001, invalid: 0, duplicates: 0 })).toBe(
      "acima do limite de 10.000 por análise",
    );
  });

  it("conta 10.000 linhas sem repetir nem perder perfis", () => {
    const lines = Array.from({ length: 10000 }, (_, i) => `https://www.instagram.com/perfil${i}/`);
    expect(countProfileLines(lines.join("\n"))).toEqual({ profiles: 10000, invalid: 0, duplicates: 0 });
  });
});

describe("endereços", () => {
  it("monta e lê o ?id= da análise", () => {
    expect(analysisRoute(42)).toBe("/analise-perfis?id=42");
    expect(analysisIdFromSearch(analysisRoute(42).slice("/analise-perfis".length))).toBe(42);
    expect(analysisReportUrl(42)).toBe("/api/profile-analyses/42/report");
  });

  it("aceita só ids inteiros positivos", () => {
    for (const search of ["", "?id=", "?id=0", "?id=-3", "?id=1.5", "?id=abc", "?id=99999999999999999999"]) {
      expect(analysisIdFromSearch(search)).toBeNull();
    }
  });
});

describe("estado da análise", () => {
  const ALL: ProfileAnalysisStatus[] = ["pending", "running", "waiting", "completed", "cancelled", "interrupted"];

  it("só fila, andamento e espera mantêm o polling", () => {
    expect(ALL.filter(isAnalysisActive)).toEqual(["pending", "running", "waiting"]);
  });

  it("usa os nomes combinados para cada estado", () => {
    expect(ALL.map((status) => ANALYSIS_STATUS_LABEL[status])).toEqual([
      "Na fila",
      "Executando",
      "Pausada",
      "Concluída",
      "Cancelada",
      "Interrompida",
    ]);
  });

  it("polling de 2 s enquanto ativa e parado quando ociosa", () => {
    expect(analysisPollMs(null, true)).toBe(2_000);
    expect(analysisPollMs(2_000, true)).toBe(2_000);
    expect(analysisPollMs(2_000, false)).toBeNull();
  });

  it("interrompida e cancelada não usam o tom de sucesso", () => {
    expect(analysisTone("completed")).toBe("ok");
    expect(analysisTone("interrupted")).toBe("warn");
    expect(analysisTone("cancelled")).toBe("muted");
  });

  it("resume o que Limpar histórico apagou", () => {
    expect(clearedText({ deleted: 0, kept_open: 1 })).toBe("Nenhuma análise encerrada para apagar.");
    expect(clearedText({ deleted: 1, kept_open: 0 })).toBe("1 análise apagada.");
    expect(clearedText({ deleted: 1200, kept_open: 1 })).toBe(
      "1.200 análises apagadas. A análise em andamento foi mantida.",
    );
  });

  it("encurta links de compartilhamento e não mexe no resto", () => {
    const share = "https://www.instagram.com/alfa/?utm_source=ig_web&igsh=abc#x";
    expect(compactProfileText(`${share}\r\n@beta?\nhttps://exemplo.com/a?b`)).toBe(
      "https://www.instagram.com/alfa/\n@beta?\nhttps://exemplo.com/a?b",
    );
    expect(compactProfileText(Array(10_000).fill(share).join("\n")).length).toBeLessThan(1_000_000);
  });

  it("resumo da consulta soma ativas, inativas e não verificadas", () => {
    const summary = {
      active: 5,
      private: 2,
      restricted: 1,
      unavailable: 10,
      unverified: 2,
      cancelled: 0,
      stories_yes: 0,
      highlights_yes: 0,
      views_total: 0,
    };
    const accounts = { active: 8, inactive: 10, unverified: 2, not_consulted: 0 };
    const cards = accountCards({ total: 20, summary, accounts });
    expect(cards.map((card) => [card.label, card.value, card.share])).toEqual([
      ["Contas ativas", 8, 40],
      ["Contas inativas", 10, 50],
      ["Não verificadas", 2, 10],
      ["Views totais", 0, null],
    ]);
    expect(cards[0]?.detail).toBe("5 públicas · 2 privadas · 1 restritas");
    const cancelled = accountCards({ total: 20, summary, accounts: { ...accounts, not_consulted: 3 } });
    expect(cancelled.at(-2)?.label).toBe("Não consultadas");
  });
});

describe("indicadores", () => {
  const summary = {
    active: 0,
    private: 0,
    restricted: 0,
    unavailable: 0,
    unverified: 3,
    cancelled: 0,
    stories_yes: 0,
    highlights_yes: 0,
    views_total: 0,
  };

  it("Concluídos, Pendentes e Com erro somam o Total", () => {
    const indicators = analysisIndicators({ total: 100, done: 40, summary });
    expect(indicators).toEqual([
      { label: "Total", value: 100 },
      { label: "Concluídos", value: 37 },
      { label: "Pendentes", value: 60 },
      { label: "Com erro", value: 3, title: "Não verificados" },
    ]);
    expect(indicators.slice(1).reduce((sum, indicator) => sum + indicator.value, 0)).toBe(100);
  });
});

describe("páginas", () => {
  it("mostra a página atual e o total", () => {
    expect(pageInfo(0, 50, 10000)).toEqual({ page: 1, pages: 200 });
    expect(pageInfo(9950, 50, 10000)).toEqual({ page: 200, pages: 200 });
    expect(pageInfo(50, 50, 101)).toEqual({ page: 2, pages: 3 });
    expect(pageInfo(0, 50, 0)).toEqual({ page: 1, pages: 1 });
  });

  it("vai direto para uma página, sempre dentro da lista", () => {
    expect(pageOffset(1, 50, 10000)).toBe(0);
    expect(pageOffset(120, 50, 10000)).toBe(5950);
    expect(pageOffset(200, 50, 10000)).toBe(9950);
    expect(pageOffset(9999, 50, 10000)).toBe(9950);
    expect(pageOffset(0, 50, 10000)).toBe(0);
  });

  it("só aceita inteiro no campo de página", () => {
    expect(parsePage(" 12 ")).toBe(12);
    for (const raw of ["", "abc", "1.5", "-3"]) expect(parsePage(raw)).toBeNull();
  });
});

describe("progresso e estimativa", () => {
  it("mostra o andamento sem dividir por zero", () => {
    expect(progressText({ done: 12, total: 300 })).toBe("12 de 300");
    expect(progressFraction({ done: 3, total: 12 })).toBe(0.25);
    expect(progressFraction({ done: 0, total: 0 })).toBe(0);
  });

  it("estima cerca de 10 s por perfil restante", () => {
    expect(estimateText({ status: "running", done: 0, total: 3 })).toBe("menos de 1 min");
    expect(estimateText({ status: "running", done: 0, total: 360, cautious_pace: false })).toBe("cerca de 24 min");
    expect(estimateText({ status: "running", done: 0, total: 360, cautious_pace: true })).toBe("cerca de 1 h");
    expect(estimateText({ status: "running", done: 12, total: 42 })).toBe("cerca de 5 min");
    expect(estimateText({ status: "pending", done: 0, total: 360 })).toBe("cerca de 1 h");
    expect(estimateText({ status: "running", done: 0, total: 1000 })).toBe("cerca de 2 h 47 min");
  });

  it("passa a falar em dias acima de 24 h", () => {
    expect(estimateText({ status: "running", done: 0, total: 8640 })).toBe("cerca de 24 h");
    expect(estimateText({ status: "running", done: 0, total: 8700 })).toBe("cerca de 1 dia");
    expect(estimateText({ status: "running", done: 0, total: 9000 })).toBe("cerca de 1 dia e 1 h");
    expect(estimateText({ status: "pending", done: 0, total: 10000 })).toBe("cerca de 1 dia e 4 h");
    expect(estimateText({ status: "running", done: 0, total: 17280 })).toBe("cerca de 2 dias");
    expect(estimateText({ status: "running", done: 0, total: 18000 })).toBe("cerca de 2 dias e 2 h");
  });

  it("não estima em espera, encerrada ou sem nada restante", () => {
    expect(estimateText({ status: "waiting", done: 1, total: 5 })).toBeNull();
    expect(estimateText({ status: "completed", done: 5, total: 5 })).toBeNull();
    expect(estimateText({ status: "running", done: 5, total: 5 })).toBeNull();
  });
});

describe("avisos", () => {
  const until = new Date(2026, 10, 1, 14, 5).toISOString();

  it("formata o horário local", () => {
    expect(formatClock(until)).toBe("14:05");
    expect(formatClock(null)).toBe("-");
    expect(formatClock("não é data")).toBe("-");
  });

  it("explica cada espera", () => {
    const waiting = (wait_reason: string | null, waiting_until: string | null = until) =>
      waitingText({ status: "waiting", wait_reason, waiting_until });
    expect(waiting("rate_limited")).toBe("O Instagram limitou as consultas. Retomando às 14:05.");
    expect(waiting("too_many_failures")).toBe("Várias consultas seguidas falharam. Retomando às 14:05.");
    expect(waiting("instagram_pause", null)).toContain("pausa");
    expect(waiting(null, null)).toBe("Análise em espera. Retomando em breve.");
  });

  it("não avisa de espera fora de waiting", () => {
    expect(waitingText({ status: "running", wait_reason: "rate_limited", waiting_until: until })).toBeNull();
  });

  it("explica o fim interrompido e prefere a mensagem do backend", () => {
    expect(endNoticeText({ status: "completed", error: null })).toBeNull();
    expect(endNoticeText({ status: "interrupted", error: null })).toContain("não foram consultados");
    expect(endNoticeText({ status: "interrupted", error: "Limite do Instagram." })).toBe("Limite do Instagram.");
  });

  it("resume as linhas ignoradas", () => {
    expect(ignoredText({ invalid_count: 0, duplicate_count: 0 })).toBeNull();
    expect(ignoredText({ invalid_count: 1, duplicate_count: 3 })).toBe(
      "1 linha inválida ignorada · 3 duplicatas ignoradas",
    );
  });

  it("usa o teto da análise, não o da importação, no erro de lista grande", () => {
    const tooMany = new ApiError(400, "too_many_profiles", "O máximo é 10.000 por importação.");
    expect(startErrorText(tooMany)).toBe("Perfis demais de uma vez. O máximo é 10.000 por análise.");
    expect(startErrorText(new ApiError(400, "no_profiles", "Informe ao menos um perfil."))).toBe(
      "Informe ao menos um perfil.",
    );
  });
});

describe("Sim, Não e Não verificado são estados distintos", () => {
  it("só `no` vira Não", () => {
    expect(checkCell("yes")).toMatchObject({ text: "Sim", tone: "ok" });
    expect(checkCell("no")).toMatchObject({ text: "Não", tone: "muted" });
    expect(checkCell("unverified")).toMatchObject({ text: "Não verificado", tone: "warn" });
    expect(checkCell(null)).toMatchObject({ text: "Não verificado", tone: "warn" });
  });

  it("uma falha nunca aparece como Não em nenhuma coluna", () => {
    const failed = item({
      account_status: "unverified",
      posts_state: "unverified",
      posts_count: null,
      posts_exact: null,
      views_state: "unverified",
      views_total: null,
      views_reels: null,
      stories: "unverified",
      highlights: "unverified",
      error_code: "timeout",
      error_message: "O Instagram demorou a responder.",
    });
    const cells = itemCells(failed);
    for (const key of ["posts", "views", "stories", "highlights", "status"] as const) {
      expect(cells[key].text).toBe("Não verificado");
      expect(cells[key].tone).toBe("warn");
    }
    expect(cells.status.title).toBe("O Instagram demorou a responder.");
    expect(cells.date.text).toBe("31/10/2026");
    expect(cells.time.text).toBe("20:58:01");
  });

  it("item concluído sem dados sai como Não verificado", () => {
    const cells = itemCells(
      item({
        account_status: null,
        posts_state: null,
        posts_count: null,
        views_state: null,
        views_total: null,
        stories: null,
        highlights: null,
      }),
    );
    for (const key of ["posts", "views", "stories", "highlights", "status"] as const) {
      expect(cells[key].text).toBe("Não verificado");
    }
    expect(cells.status.title).toBe("Não foi possível confirmar este perfil.");
  });
});

describe("Posts e Views", () => {
  it("Posts exato, aproximado e não verificado", () => {
    expect(postsCell(item())).toEqual({ text: "8.630" });
    const approx = postsCell(item({ posts_count: 33000, posts_exact: 0, posts_text: "33K" }));
    expect(approx.text).toBe("≈ 33K");
    expect(approx.title).toBeDefined();
    expect(postsCell(item({ posts_count: 33000, posts_exact: 0, posts_text: null })).text).toBe("≈ 33.000");
    expect(postsCell(item({ posts_state: "unverified" })).text).toBe("Não verificado");
    expect(postsCell(item({ posts_count: 0 })).text).toBe("0");
  });

  it("Views mostra a soma com o tooltip dos Reels lidos", () => {
    expect(viewsCell(item())).toEqual({ text: "1.500", title: "Soma dos últimos 12 Reels lidos" });
    expect(viewsCell(item({ views_reels: 1 })).title).toBe("Soma do último Reel lido");
  });

  it("Sem Reels é neutro e diferente de Não verificado", () => {
    expect(viewsCell(item({ views_state: "none", views_total: null }))).toEqual({ text: "Sem Reels", tone: "muted" });
    expect(viewsCell(item({ views_state: "unverified", views_total: null })).text).toBe("Não verificado");
    expect(viewsCell(item({ views_state: "known", views_total: null })).text).toBe("Não verificado");
  });
});

describe("linhas da tabela", () => {
  it("perfil ativo mostra os resultados e sem tooltip de erro", () => {
    const cells = itemCells(item());
    expect(cells.status).toMatchObject({ text: "Ativa", tone: "ok" });
    expect(cells.status.title).toBeUndefined();
    expect(cells.stories.text).toBe("Sim");
    expect(cells.highlights.text).toBe("Não");
    expect(cells.posts.text).toBe("8.630");
  });

  it("perfil privado usa o tom de destaque e mantém o que não foi verificado", () => {
    const cells = itemCells(item({ account_status: "private", stories: "unverified", views_state: "unverified" }));
    expect(cells.status).toMatchObject({ text: "Privada", tone: "accent" });
    expect(cells.stories.text).toBe("Não verificado");
    expect(cells.views.text).toBe("Não verificado");
  });

  it("indisponível mostra travessão nos campos, com data e motivo", () => {
    const cells = itemCells(item({ account_status: "unavailable", stories: "unverified", highlights: "unverified" }));
    expect(cells.status).toMatchObject({ text: "Indisponível", tone: "err" });
    expect(cells.status.title).toBe("Perfil não encontrado, removido ou restrito para visitantes sem login.");
    for (const key of ["posts", "views", "stories", "highlights"] as const) {
      expect(cells[key]).toBe(EMPTY_CELL);
    }
    expect(cells.date.text).toBe("31/10/2026");
  });

  it("progresso aparece na própria linha, sem resultado", () => {
    const blank = {
      account_status: null,
      stories: null,
      highlights: null,
      checked_date: null,
      checked_time: null,
    } as const;
    expect(itemCells(item({ ...blank, state: "pending" })).status).toMatchObject({ text: "Na fila", tone: "muted" });
    const running = itemCells(item({ ...blank, state: "running" }));
    expect(running.status).toMatchObject({ text: "Consultando", tone: "accent" });
    expect(running.stories).toBe(EMPTY_CELL);
    expect(running.posts).toBe(EMPTY_CELL);
    expect(running.date).toBe(EMPTY_CELL);
    expect(itemCells(item({ ...blank, state: "cancelled" })).status.text).toBe("Cancelado");
    expect(itemCells(item({ ...blank, state: "cancelled", error_code: "rate_limited" })).status.text)
      .toBe("Não consultado");
  });

  it("perfil não consultado por interrupção mostra o motivo no tooltip", () => {
    const blank = { account_status: null, checked_date: null, checked_time: null } as const;
    const message = "Não consultado: muitos perfis seguidos sem resposta confirmada; a análise foi interrompida.";
    const cells = itemCells(
      item({ ...blank, state: "cancelled", error_code: "too_many_failures", error_message: message }),
    );
    expect(cells.status).toMatchObject({ text: "Não consultado", title: message });
    expect(cells.date).toBe(EMPTY_CELL);
  });

  it("internal_error mostra a mensagem da análise, não o texto genérico do backend", () => {
    const message = "Erro inesperado ao consultar este perfil. O resultado não pôde ser confirmado.";
    const failed = item({ account_status: "unverified", error_code: "internal_error", error_message: message });
    expect(itemCells(failed).status.title).toBe(message);
    expect(itemCells({ ...failed, error_message: null }).status.title).toBe(message);
  });
});
