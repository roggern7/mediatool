import { describe, expect, it } from "vitest";
import {
  batchNotice,
  isBatchPaused,
  listingDropped,
  listingDroppedNotice,
  listingDroppedSuffix,
  needsAttention,
  rateLimitRemainingText,
  rateLimitWaitText,
} from "./batchState";

const now = new Date(2026, 9, 3, 10, 0, 0).getTime();
const later = new Date(2026, 9, 3, 14, 30, 0).toISOString();
const before = new Date(2026, 9, 3, 9, 0, 0).toISOString();

describe("estado do lote", () => {
  it("sem campos novos não mostra aviso", () => {
    expect(batchNotice({}, now)).toBeNull();
    expect(batchNotice({ listing_state: "running" }, now)).toBeNull();
  });
  it("needs_attention mostra motivo e permite retomar", () => {
    const b = { listing_state: "needs_attention", error_code: "access_refused", error_message: null };
    expect(needsAttention(b)).toBe(true);
    const n = batchNotice(b, now);
    expect(n?.kind).toBe("needs_attention");
    expect(n?.canResume).toBe(true);
    expect(n?.text).toContain("Motivo: O Instagram recusou a chamada usada pelo Media Tool.");
  });
  it("lote por link parado por falhas seguidas (links_failing): mensagem do backend e retomar", () => {
    const b = {
      listing_state: "needs_attention",
      error_code: "links_failing",
      error_message: "3 links seguidos falharam",
    };
    const n = batchNotice(b, now);
    expect(n?.kind).toBe("needs_attention");
    expect(n?.canResume).toBe(true);
    expect(n?.text).toContain("Motivo: 3 links seguidos falharam.");
    // Sem a mensagem do backend, o texto fixo em pt-BR entra no lugar (nunca o código cru).
    const fallback = batchNotice({ ...b, error_message: null }, now);
    expect(fallback?.canResume).toBe(true);
    expect(fallback?.text).toContain("Motivo: Vários links seguidos não puderam ser importados.");
    expect(fallback?.text).not.toContain("links_failing");
  });
  it("needs_attention sem motivo continua claro", () => {
    const n = batchNotice({ listing_state: "needs_attention" }, now);
    expect(n?.text.startsWith("A importação parou e precisa de você.")).toBe(true);
    expect(n?.text).not.toContain("Motivo");
  });
  it("pausa por lote mostra retomada prevista", () => {
    const b = { paused_reason: "content_unavailable", paused_until: later };
    expect(isBatchPaused(b, now)).toBe(true);
    const n = batchNotice(b, now);
    expect(n?.kind).toBe("batch_paused");
    expect(n?.canResume).toBe(false);
    expect(n?.text).toContain("(Conteúdo indisponível)");
    expect(n?.text).toContain("Retomada automática prevista às 14:30");
  });
  it("pausa vencida não aparece", () => {
    expect(isBatchPaused({ paused_reason: "x", paused_until: before }, now)).toBe(false);
    expect(batchNotice({ paused_reason: "x", paused_until: before }, now)).toBeNull();
  });
  it("coleta em pausa avisa que retoma sozinha", () => {
    expect(batchNotice({ listing_state: "paused" }, now)?.kind).toBe("listing_paused");
  });
});

describe("espera pelo limite de requisições", () => {
  const resumeAt = new Date(2026, 9, 3, 10, 18, 0).toISOString();
  const waiting = {
    listing_state: "waiting_rate_limit",
    rate_limit_resume_at: resumeAt,
    rate_limit_pages_used: 300,
    rate_limit_pages_max: 300,
  };
  it("informa retomada, tempo restante e páginas usadas, sem parecer erro nem pausa", () => {
    const n = batchNotice(waiting, now);
    expect(n?.kind).toBe("rate_limit_wait");
    expect(n?.canResume).toBe(false);
    expect(n?.text).toBe(
      "Aguardando limite de requisições. O lote continua ativo e retoma automaticamente às 10:18 " +
        "(em cerca de 18 min). Páginas usadas na última hora: 300 de 300.",
    );
    expect(n?.text).not.toMatch(/paus|erro|falh/i);
  });
  it("o tempo restante acompanha o relógio", () => {
    const at = (min: number, sec = 0) => new Date(2026, 9, 3, 10, min, sec).getTime();
    expect(rateLimitRemainingText(resumeAt, at(0))).toBe("em cerca de 18 min");
    expect(rateLimitRemainingText(resumeAt, at(10, 20))).toBe("em cerca de 8 min");
    expect(rateLimitRemainingText(resumeAt, at(16, 45))).toBe("em cerca de 1 min");
    expect(rateLimitRemainingText(resumeAt, at(17, 30))).toBe("em menos de 1 min");
    expect(rateLimitRemainingText(resumeAt, at(18))).toBe("em instantes");
    expect(rateLimitRemainingText(resumeAt, at(25))).toBe("em instantes");
    expect(rateLimitRemainingText("inválido", at(0))).toBe("em instantes");
    expect(rateLimitWaitText(waiting, at(10, 20))).toContain("às 10:18 (em cerca de 8 min)");
  });
  it("sem horário ou sem contagem o texto continua claro", () => {
    expect(rateLimitWaitText({ listing_state: "waiting_rate_limit" }, now)).toBe(
      "O lote continua ativo e retoma automaticamente.",
    );
    expect(rateLimitWaitText({ ...waiting, rate_limit_pages_used: null }, now)).toBe(
      "O lote continua ativo e retoma automaticamente às 10:18 (em cerca de 18 min).",
    );
  });
  it("quando a API volta a running o aviso some", () => {
    expect(batchNotice({ ...waiting, listing_state: "running", rate_limit_resume_at: null }, now)).toBeNull();
  });
  it("atenção e pausa por falhas têm precedência", () => {
    expect(batchNotice({ ...waiting, paused_reason: "content_unavailable", paused_until: later }, now)?.kind).toBe(
      "batch_paused",
    );
  });
});

describe("mídias descartadas durante a listagem", () => {
  const one = { listing_dropped: { unexpected_total: 1, reasons: { image_no_url: 1 } } };
  it("zero descartes ou lote antigo sem o campo: nenhum aviso", () => {
    expect(listingDropped({})).toBeNull();
    expect(listingDropped({ listing_dropped: null })).toBeNull();
    expect(listingDropped({ listing_dropped: { unexpected_total: 0, reasons: {} } })).toBeNull();
    expect(listingDropped({ listing_dropped: { unexpected_total: 0, reasons: { image_no_url: 0 } } })).toBeNull();
    expect(listingDroppedSuffix({})).toBe("");
    expect(listingDroppedSuffix({ listing_dropped: { unexpected_total: 0, reasons: {} } })).toBe("");
  });
  it("singular", () => {
    const d = listingDropped(one);
    expect(d?.text).toBe("1 mídia não pôde ser adicionada durante a listagem");
    expect(d?.summary).toBe("1 mídia descartada durante a listagem");
    expect(d?.detail).toBe("imagem sem URL utilizável: 1");
    expect(d?.atLeast).toBe(false);
  });
  it("plural com detalhe por motivo", () => {
    const d = listingDropped({
      listing_dropped: {
        unexpected_total: 5,
        reasons: { image_no_url: 1, carousel_child_invalid: 2, video_no_url: 1, carousel_children_missing: 1 },
      },
    });
    expect(d?.text).toBe("5 mídias não puderam ser adicionadas durante a listagem");
    expect(d?.summary).toBe("5 mídias descartadas durante a listagem");
    expect(d?.detail).toBe(
      "imagem sem URL utilizável: 1; item de carrossel inválido: 2; vídeo sem URL utilizável: 1; " +
        "item faltando em carrossel: 1",
    );
  });
  it("post inteiro descartado: a contagem é um mínimo e motivos irmãos somam", () => {
    const d = listingDropped({
      listing_dropped: { unexpected_total: 4, reasons: { node_null: 1, edge_null: 2, node_no_code: 1 } },
    });
    expect(d?.atLeast).toBe(true);
    expect(d?.text).toBe("Pelo menos 4 mídias não puderam ser adicionadas durante a listagem");
    expect(d?.summary).toBe("pelo menos 4 mídias descartadas durante a listagem");
    expect(d?.detail).toBe("post sem dados na resposta: 3; post sem identificador: 1");
    expect(listingDropped({ listing_dropped: { unexpected_total: 1, reasons: { node_no_code: 1 } } })?.text).toBe(
      "Pelo menos 1 mídia não pôde ser adicionada durante a listagem",
    );
  });
  it("motivo desconhecido e total sem motivos não quebram", () => {
    expect(listingDropped({ listing_dropped: { unexpected_total: 2, reasons: { algo_novo: 2 } } })?.detail).toBe(
      "outro motivo: 2",
    );
    const bare = listingDropped({ listing_dropped: { unexpected_total: 3 } });
    expect(bare?.text).toBe("3 mídias não puderam ser adicionadas durante a listagem");
    expect(bare?.detail).toBe("");
    expect(listingDropped({ listing_dropped: { reasons: { image_no_url: 2 } } })?.total).toBe(2);
  });
  it("resumo final: N concluídos • M mídia(s) descartada(s) durante a listagem, com milhar pt-BR", () => {
    expect(`10.000 concluídos${listingDroppedSuffix(one)}`).toBe(
      "10.000 concluídos • 1 mídia descartada durante a listagem",
    );
    const many = { listing_dropped: { unexpected_total: 1234, reasons: { image_no_url: 1234 } } };
    expect(`10.000 concluídos${listingDroppedSuffix(many)}`).toBe(
      "10.000 concluídos • 1.234 mídias descartadas durante a listagem",
    );
    expect(listingDropped(many)?.detail).toBe("imagem sem URL utilizável: 1.234");
  });
  it("descarte não cria aviso de lote parado nem muda o estado", () => {
    expect(batchNotice({ ...one, listing_state: "running" }, now)).toBeNull();
  });
});

describe("fecho do aviso de mídias descartadas", () => {
  const dropped = { listing_dropped: { unexpected_total: 1, reasons: { image_no_url: 1 }, pending: 1 } };
  const base = "1 mídia não pôde ser adicionada durante a listagem (imagem sem URL utilizável: 1).";
  it("lote ativo: os demais itens seguem", () => {
    for (const status of ["listing", "running"]) {
      expect(listingDroppedNotice({ ...dropped, status })).toBe(`${base} Os demais itens do lote seguem normalmente.`);
    }
  });
  it("lote concluído: os demais itens não foram afetados", () => {
    expect(listingDroppedNotice({ ...dropped, status: "completed" })).toBe(
      `${base} Os demais itens do lote não foram afetados.`,
    );
  });
  it("lote cancelado, falho ou sem status: sem fecho", () => {
    for (const status of ["cancelled", "failed", undefined]) {
      expect(listingDroppedNotice({ ...dropped, status })).toBe(base);
    }
  });
  it("sem motivo conhecido e sem descarte", () => {
    expect(listingDroppedNotice({ listing_dropped: { unexpected_total: 2 }, status: "completed" })).toBe(
      "2 mídias não puderam ser adicionadas durante a listagem. Os demais itens do lote não foram afetados.",
    );
    expect(listingDroppedNotice({ status: "running" })).toBeNull();
  });
});
