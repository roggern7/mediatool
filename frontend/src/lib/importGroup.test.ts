import { describe, expect, it } from "vitest";
import {
  type GroupChildLike,
  type GroupLike,
  childState,
  groupAttentionText,
  groupDroppedText,
  groupMediaText,
  groupOptionsText,
  groupProfileCounts,
  groupStatusLabel,
  groupSummary,
  groupTitle,
  mergeRecent,
  profileEndText,
  profileProgressText,
  profileStateDetail,
  profileStateLabel,
  skippedFrom,
  skippedNotice,
} from "./importGroup";

const now = new Date(2026, 9, 6, 10, 0, 0).getTime();
const at1030 = new Date(2026, 9, 6, 10, 30, 0).toISOString();

function child(
  over: Omit<Partial<GroupChildLike>, "progress"> & { progress?: Partial<GroupChildLike["progress"]> } = {},
): GroupChildLike {
  const { progress, ...rest } = over;
  return {
    status: "listing",
    process: false,
    requested_limit: null,
    listing_state: "running",
    listing_end: null,
    group_state: "active",
    ...rest,
    progress: { found: 0, downloaded: 0, failed: 0, ...progress },
  };
}

function group(over: Partial<GroupLike> = {}): GroupLike {
  return {
    content_type: "reels",
    requested_limit: 1000,
    preset_name: null,
    process: false,
    status: "active",
    attention: false,
    profiles: { total: 37, waiting: 20, active: 1, completed: 12, attention: 2, failed: 1, cancelled: 1 },
    progress: { found: 20000, downloaded: 15300, processed: 0 },
    listing_dropped_total: 0,
    ...over,
  };
}

describe("estado por perfil", () => {
  it("na fila: aguardando, mesmo com listing_state nulo", () => {
    const b = child({ group_state: "waiting", listing_state: null });
    expect(profileStateLabel(b, now)).toEqual({ label: "Aguardando", tone: "muted" });
    expect(profileStateDetail(b, now)).toBe("na fila");
    expect(profileProgressText(b)).toBe("");
  });
  it("listando e baixando", () => {
    expect(profileStateLabel(child(), now)).toEqual({ label: "Listando", tone: "accent" });
    expect(profileStateLabel(child({ status: "running", listing_state: "done" }), now)).toEqual({
      label: "Baixando",
      tone: "accent",
    });
    expect(profileStateLabel(child({ status: "running", listing_state: "done", process: true }), now).label).toBe(
      "Baixando e processando",
    );
    expect(profileStateDetail(child(), now)).toBeNull();
  });
  it("aguardando downloads (backpressure) não parece erro", () => {
    const b = child({ listing_state: "waiting_backpressure" });
    expect(profileStateLabel(b, now)).toEqual({ label: "Aguardando downloads", tone: "accent" });
    expect(profileStateDetail(b, now)).toBe("a listagem espera a fila de downloads esvaziar");
  });
  it("aguardando limite de requisições mostra o horário de retomada", () => {
    const b = child({ listing_state: "waiting_rate_limit", rate_limit_resume_at: at1030 });
    expect(profileStateLabel(b, now)).toEqual({ label: "Aguardando limite de requisições", tone: "accent" });
    expect(profileStateDetail(b, now)).toBe("retoma às 10:30 (em cerca de 30 min)");
    expect(profileStateDetail(child({ listing_state: "waiting_rate_limit" }), now)).toBe("retoma automaticamente");
  });
  it("pausado: pela listagem ou pelo disjuntor do lote", () => {
    expect(profileStateLabel(child({ listing_state: "paused" }), now)).toEqual({ label: "Pausado", tone: "warn" });
    expect(profileStateDetail(child({ listing_state: "paused" }), now)).toBe("tenta de novo sozinho");
    const b = child({ status: "running", paused_reason: "content_unavailable", paused_until: at1030 });
    expect(profileStateLabel(b, now).label).toBe("Pausado");
    expect(profileStateDetail(b, now)).toBe("retomada automática prevista às 10:30");
  });
  it("atenção traz o motivo", () => {
    const b = child({
      group_state: "attention",
      listing_state: "needs_attention",
      error_code: "contract_changed",
      error_message: "O Instagram interrompeu a paginação do perfil antes do fim.",
    });
    expect(profileStateLabel(b, now)).toEqual({ label: "Atenção", tone: "warn" });
    expect(profileStateDetail(b, now)).toBe("O Instagram interrompeu a paginação do perfil antes do fim.");
    expect(profileStateDetail(child({ group_state: "attention" }), now)).toBeNull();
  });
  it("concluído, falhou e cancelado", () => {
    expect(profileStateLabel(child({ group_state: "completed", status: "completed" }), now)).toEqual({
      label: "Concluído",
      tone: "ok",
    });
    expect(
      profileStateLabel(child({ group_state: "completed", status: "completed", progress: { failed: 2 } }), now),
    ).toEqual({ label: "Concluído com erros", tone: "warn" });
    const failed = child({ group_state: "failed", status: "failed", error_code: "profile_not_found" });
    expect(profileStateLabel(failed, now)).toEqual({ label: "Falhou", tone: "err" });
    expect(profileStateDetail(failed, now)).toBe("Perfil não encontrado.");
    expect(profileStateLabel(child({ group_state: "cancelled", status: "cancelled" }), now)).toEqual({
      label: "Cancelado",
      tone: "muted",
    });
  });
  it("sem group_state (resposta antiga) deriva de status e listing_state", () => {
    expect(childState(child({ group_state: null, status: "completed" }))).toBe("completed");
    expect(childState(child({ group_state: null, listing_state: "needs_attention" }))).toBe("attention");
    expect(childState(child({ group_state: undefined }))).toBe("active");
  });
});

describe("quantidade por perfil", () => {
  it("concluído: N mídias", () => {
    const done = { group_state: "completed" as const, status: "completed" as const, listing_end: "limit" as const };
    expect(profileProgressText(child({ ...done, progress: { found: 1240, downloaded: 1240 } }))).toBe("1.240 mídias");
    expect(profileProgressText(child({ ...done, progress: { found: 1, downloaded: 1 } }))).toBe("1 mídia");
  });
  it("em andamento: baixadas / pedido, ou baixadas / encontradas em Todos", () => {
    expect(profileProgressText(child({ requested_limit: 2000, progress: { found: 900, downloaded: 830 } }))).toBe(
      "830 / 2.000",
    );
    expect(profileProgressText(child({ requested_limit: null, progress: { found: 3120, downloaded: 3051 } }))).toBe(
      "3.051 / 3.120",
    );
    expect(profileProgressText(child())).toBe("");
  });
  it("listagem encerrada: o total passa a ser o encontrado", () => {
    const b = child({ status: "running", listing_state: "done", requested_limit: 2000, listing_end: "limit" });
    expect(profileProgressText({ ...b, progress: { found: 2000, downloaded: 1500, failed: 0 } })).toBe("1.500 / 2.000");
  });
  it("concluído conta baixadas + já existentes, sem os itens com erro, e diz quantas já existiam", () => {
    const done = { group_state: "completed" as const, status: "completed" as const, listing_end: "limit" as const };
    expect(
      profileProgressText(child({ ...done, progress: { found: 1240, downloaded: 930, skipped: 300, failed: 10 } })),
    ).toBe("1.230 mídias · 300 já existentes · 10 com erro");
    expect(profileProgressText(child({ ...done, progress: { found: 1, downloaded: 0, skipped: 1 } }))).toBe(
      "1 mídia · 1 já existente",
    );
    expect(profileProgressText(child({ ...done, progress: { found: 5, downloaded: 5, skipped: 0 } }))).toBe("5 mídias");
    expect(
      profileProgressText(child({ requested_limit: 2000, progress: { found: 900, downloaded: 500, skipped: 330 } })),
    ).toBe("830 / 2.000 · 330 já existentes");
  });
  it("perfil chegou ao fim antes da quantidade pedida", () => {
    const ended = child({
      group_state: "completed",
      status: "completed",
      requested_limit: 100,
      listing_end: "profile_end",
      progress: { found: 37, downloaded: 37 },
    });
    expect(profileEndText(ended)).toBe("perfil chegou ao fim: 37 de 100");
    expect(profileProgressText(ended)).toBe("perfil chegou ao fim: 37 de 100");
    const downloading = { ...ended, group_state: "active" as const, status: "running" as const };
    expect(profileProgressText({ ...downloading, progress: { found: 37, downloaded: 12, failed: 0 } })).toBe(
      "12 / 37 · perfil chegou ao fim: 37 de 100",
    );
  });
  it("fim do perfil sem falta não vira aviso", () => {
    expect(profileEndText(child({ listing_end: "profile_end", requested_limit: null, progress: { found: 37 } }))).toBeNull();
    expect(profileEndText(child({ listing_end: "profile_end", requested_limit: 37, progress: { found: 37 } }))).toBeNull();
    expect(profileEndText(child({ listing_end: "limit", requested_limit: 100, progress: { found: 100 } }))).toBeNull();
    expect(profileEndText(child({ listing_end: null, requested_limit: 100, progress: { found: 5 } }))).toBeNull();
  });
  it("erros e descartes de listagem aparecem compactos na linha", () => {
    const b = child({
      group_state: "completed",
      status: "completed",
      progress: { found: 50, downloaded: 48, failed: 2 },
      listing_dropped: { unexpected_total: 3, reasons: { image_no_url: 3 } },
    });
    expect(profileProgressText(b)).toBe("48 mídias · 2 com erro · 3 descartadas na listagem");
    const whole = child({
      status: "running",
      progress: { found: 10, downloaded: 4 },
      listing_dropped: { unexpected_total: 1, reasons: { node_null: 1 } },
    });
    expect(profileProgressText(whole)).toBe("4 / 10 · pelo menos 1 descartada na listagem");
  });
});

describe("cabeçalho do grupo", () => {
  it("título com singular e milhar", () => {
    expect(groupTitle(group())).toBe("Importação • 37 perfis");
    expect(groupTitle({ profiles: { ...group().profiles, total: 1 } })).toBe("Importação • 1 perfil");
    expect(groupTitle({ profiles: { ...group().profiles, total: 1240 } })).toBe("Importação • 1.240 perfis");
  });
  it("perfis por estado, só os que existem", () => {
    expect(groupProfileCounts(group().profiles)).toEqual([
      "12 concluídos",
      "1 ativo",
      "20 aguardando",
      "2 com atenção",
      "1 falhou",
      "1 cancelado",
    ]);
    expect(
      groupProfileCounts({ total: 9, waiting: 0, active: 2, completed: 1, attention: 0, failed: 3, cancelled: 3 }),
    ).toEqual(["1 concluído", "2 ativos", "3 falharam", "3 cancelados"]);
  });
  it("mídias baixadas e descartes somados", () => {
    expect(groupMediaText(group())).toBe("15.300 de 20.000 mídias baixadas");
    expect(groupMediaText(group({ progress: { found: 1, downloaded: 1, processed: 0 } }))).toBe("1 de 1 mídia baixada");
    expect(groupMediaText(group({ process: true, progress: { found: 10, downloaded: 8, processed: 1 } }))).toBe(
      "8 de 10 mídias baixadas · 1 processada",
    );
    expect(groupMediaText(group({ progress: { found: 0, downloaded: 0, processed: 0 } }))).toBeNull();
    expect(groupMediaText(group({ progress: { found: 100, downloaded: 60, processed: 0, skipped: 30 } }))).toBe(
      "60 de 100 mídias baixadas · 30 já existentes",
    );
    expect(groupDroppedText(group())).toBeNull();
    expect(groupDroppedText(group({ listing_dropped_total: 1 }))).toBe("1 mídia descartada durante a listagem");
    expect(groupDroppedText(group({ listing_dropped_total: 1200 }))).toBe("1.200 mídias descartadas durante a listagem");
  });
  it("resumo em uma linha", () => {
    expect(groupSummary(group({ listing_dropped_total: 3 }))).toBe(
      "12 concluídos • 1 ativo • 20 aguardando • 2 com atenção • 1 falhou • 1 cancelado • " +
        "15.300 de 20.000 mídias baixadas • 3 mídias descartadas durante a listagem",
    );
    const fresh = group({
      profiles: { total: 2, waiting: 2, active: 0, completed: 0, attention: 0, failed: 0, cancelled: 0 },
      progress: { found: 0, downloaded: 0, processed: 0 },
    });
    expect(groupSummary(fresh)).toBe("2 aguardando");
  });
  it("opções do grupo", () => {
    expect(groupOptionsText(group())).toBe("Reels · Últimos 1.000 por perfil · Somente originais");
    expect(
      groupOptionsText(group({ content_type: "photos_carousels", requested_limit: null, process: true, preset_name: "Anti-duplicidade" })),
    ).toBe("Fotos e carrosséis · Todos · Anti-duplicidade");
  });
  it("estado geral", () => {
    expect(groupStatusLabel({ status: "active", attention: false })).toEqual({ label: "Em andamento", tone: "accent" });
    expect(groupStatusLabel({ status: "active", attention: true })).toEqual({ label: "Em andamento", tone: "accent" });
    expect(groupStatusLabel({ status: "attention", attention: true })).toEqual({
      label: "Precisa de atenção",
      tone: "warn",
    });
    expect(groupStatusLabel({ status: "completed", attention: false })).toEqual({ label: "Concluído", tone: "ok" });
    expect(groupStatusLabel({ status: "completed", attention: true })).toEqual({
      label: "Concluído com pendências",
      tone: "warn",
    });
    expect(groupStatusLabel({ status: "cancelled", attention: false })).toEqual({ label: "Cancelado", tone: "muted" });
  });
  it("attention: true fica evidente, com o grupo ativo ou concluído", () => {
    expect(groupAttentionText(group())).toBeNull();
    expect(groupAttentionText(group({ attention: true }))).toBe(
      "2 perfis precisam de atenção e 1 perfil falhou. Os demais perfis seguem normalmente.",
    );
    const done = group({
      status: "completed",
      attention: true,
      profiles: { total: 5, waiting: 0, active: 0, completed: 2, attention: 0, failed: 3, cancelled: 0 },
    });
    expect(groupAttentionText(done)).toBe("Importação concluída com pendências: 3 perfis falharam.");
    const one = group({
      status: "attention",
      attention: true,
      profiles: { total: 2, waiting: 0, active: 0, completed: 1, attention: 1, failed: 0, cancelled: 0 },
    });
    expect(groupAttentionText(one)).toBe("1 perfil precisa de atenção. Os demais perfis seguem normalmente.");
  });
});

describe("perfis pulados na criação", () => {
  const reason = "profile_already_importing";
  it("nenhum pulado: sem aviso", () => {
    expect(skippedNotice([])).toBeNull();
    expect(skippedNotice(null)).toBeNull();
    expect(skippedFrom({ skipped: null })).toEqual([]);
    expect(skippedFrom(null)).toEqual([]);
    expect(skippedFrom({ skipped: "x" })).toEqual([]);
  });
  it("lê a lista da resposta e do corpo do erro 409", () => {
    const body = { code: reason, detail: "x", skipped: [{ profile: "a", reason }, { nope: 1 }, null, { profile: "b" }] };
    expect(skippedFrom(body)).toEqual([
      { profile: "a", reason },
      { profile: "b", reason: null },
    ]);
  });
  it("diz quais foram pulados e por quê", () => {
    expect(skippedNotice([{ profile: "usuario_dois", reason }])).toBe(
      "1 perfil foi pulado porque já tem uma importação em andamento: @usuario_dois.",
    );
    expect(skippedNotice([{ profile: "a", reason }, { profile: "b", reason }])).toBe(
      "2 perfis foram pulados porque já têm uma importação em andamento: @a, @b.",
    );
    expect(skippedNotice([{ profile: "a", reason: "outro" }])).toBe(
      "1 perfil foi pulado e não entrou nesta importação: @a.",
    );
  });
  it("lista longa é resumida", () => {
    const many = Array.from({ length: 25 }, (_, i) => ({ profile: `p${i}`, reason }));
    const text = skippedNotice(many) ?? "";
    expect(text.startsWith("25 perfis foram pulados porque já têm uma importação em andamento: @p0, @p1,")).toBe(true);
    expect(text.endsWith("@p19 e mais 5.")).toBe(true);
  });
});

describe("lista de recentes", () => {
  it("junta grupos e lotes avulsos do mais novo para o mais antigo, sem passar do limite", () => {
    const groups = [
      { id: 2, created_at: "2026-10-06T12:00:00.000Z" },
      { id: 1, created_at: "2026-10-01T12:00:00.000Z" },
    ];
    const batches = [
      { id: 90, created_at: "2026-10-06T13:00:00.000Z" },
      { id: 80, created_at: "2026-10-03T12:00:00.000Z" },
      { id: 70, created_at: "2026-09-01T12:00:00.000Z" },
    ];
    const merged = mergeRecent(groups, batches, 4);
    expect(merged.map((e) => (e.kind === "group" ? `g${e.group.id}` : `b${e.batch.id}`))).toEqual([
      "b90",
      "g2",
      "b80",
      "g1",
    ]);
    expect(mergeRecent(groups, [], 8)).toHaveLength(2);
    expect(mergeRecent([], [], 8)).toEqual([]);
  });
});
