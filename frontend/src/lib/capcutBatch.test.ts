import { describe, expect, it } from "vitest";
import type { CapCutBatch, CapCutBatchCounts, CapCutSettings, PresetParams } from "../api/types";
import {
  DEFAULT_BATCH_EDIT,
  EMPTY_SELECTION,
  type BatchPlan,
  addIds,
  batchActions,
  batchCountsText,
  batchEtaText,
  batchJobActions,
  batchJobStep,
  batchPollMs,
  batchProgress,
  batchSummaryText,
  buildBatchRequest,
  captionedText,
  cleanFilter,
  editFormFromPreset,
  editSummaryText,
  estimateText,
  exportFormFrom,
  folderProfiles,
  formatEta,
  isSelected,
  needsCaptionFallback,
  openCount,
  pageState,
  parseStart,
  planBlocker,
  planLines,
  resolveEstimate,
  selectAllFiltered,
  selectionBlocker,
  selectionCount,
  selectionTarget,
  skippedSummary,
  snapBatchSpeed,
  toggleOne,
  togglePage,
  validateBatchEdit,
} from "./capcutBatch";

const SETTINGS = {
  resolution: "1080P",
  fps: "60",
  bitrate: "recommended",
  keep_original_audio: true,
} as CapCutSettings;

const COUNTS: CapCutBatchCounts = {
  total: 10,
  pending: 0,
  running: 0,
  waiting: 0,
  held: 0,
  completed: 0,
  failed: 0,
  cancelled: 0,
};

function counts(patch: Partial<CapCutBatchCounts>): CapCutBatchCounts {
  return { ...COUNTS, ...patch };
}

function batch(patch: Partial<CapCutBatch>): CapCutBatch {
  return {
    id: 1,
    name: "Lote 1",
    mode: "caption",
    status: "running",
    paused: false,
    cancel_requested: false,
    created_at: "2026-11-07T15:30:00Z",
    finished_at: null,
    output_dir: "C:\\saida\\Lote 1",
    output_exists: true,
    counts: COUNTS,
    progress: 0,
    per_video_seconds: null,
    eta_seconds: null,
    elapsed_seconds: null,
    current_job: null,
    summary: { style_name: "Amarelo", edit: null, resolution: "1080P", fps: "60", bitrate: "high", keep_audio: true, highlight: true },
    options: {},
    ...patch,
  };
}

function plan(patch: Partial<BatchPlan>): BatchPlan {
  return {
    mode: "caption",
    selection: { ids: new Set([3, 1, 2]), all: null },
    name: "",
    includeDone: false,
    styleId: null,
    exportForm: exportFormFrom(SETTINGS),
    edit: DEFAULT_BATCH_EDIT,
    ...patch,
  };
}

describe("lote: seleção (página vs todos os filtrados)", () => {
  it("marca e desmarca um a um", () => {
    let sel = toggleOne(EMPTY_SELECTION, 5);
    expect(isSelected(sel, 5)).toBe(true);
    expect(selectionCount(sel)).toBe(1);
    sel = toggleOne(sel, 5);
    expect(selectionCount(sel)).toBe(0);
  });

  it("marcar a página soma aos já marcados; de novo, desmarca só os da página", () => {
    const start = toggleOne(EMPTY_SELECTION, 99);
    const marked = togglePage(start, [1, 2, 3]);
    expect(Array.from(marked.ids).sort()).toEqual([1, 2, 3, 99]);
    expect(pageState(marked, [1, 2, 3])).toBe("all");
    expect(pageState(toggleOne(marked, 2), [1, 2, 3])).toBe("some");
    const unmarked = togglePage(marked, [1, 2, 3]);
    expect(Array.from(unmarked.ids)).toEqual([99]);
    expect(pageState(unmarked, [1, 2, 3])).toBe("none");
  });

  it("parcial: marcar a página completa os que faltam", () => {
    const sel = togglePage(toggleOne(EMPTY_SELECTION, 2), [1, 2, 3]);
    expect(Array.from(sel.ids).sort()).toEqual([1, 2, 3]);
  });

  it("todos os filtrados vai como filter com o total, não como a página", () => {
    const sel = selectAllFiltered({ q: "  gato ", profile: "", folder: "batch:4", captioned: "no" }, 1234 - 300);
    expect(selectionCount(sel)).toBe(934);
    expect(isSelected(sel, 777)).toBe(true);
    expect(pageState(sel, [1, 2])).toBe("all");
    expect(selectionTarget(sel)).toEqual({ filter: { q: "gato", folder: "batch:4", captioned: "no" } });
    // Com o filtro ativo, marcar um ou a página não muda nada.
    expect(toggleOne(sel, 1)).toBe(sel);
    expect(togglePage(sel, [1, 2])).toBe(sel);
  });

  it("ids vão em ordem crescente, sem filtro", () => {
    expect(selectionTarget({ ids: new Set([9, 2, 5]), all: null })).toEqual({ media_ids: [2, 5, 9] });
  });

  it("cleanFilter tira vazios e o 'todos' de já legendados", () => {
    expect(cleanFilter({ q: " ", profile: " perfil ", folder: "", captioned: "any" })).toEqual({ profile: "perfil" });
    expect(cleanFilter({ captioned: "yes" })).toEqual({ captioned: "yes" });
  });

  it("vídeo enviado do computador entra na seleção; com todos os filtrados, a seleção vira a lista de ids", () => {
    const plain = addIds(toggleOne(EMPTY_SELECTION, 1), [2]);
    expect(plain.leftFilter).toBe(false);
    expect(Array.from(plain.selection.ids).sort()).toEqual([1, 2]);
    const fromFilter = addIds(selectAllFiltered({ q: "x" }, 50), [8]);
    expect(fromFilter.leftFilter).toBe(true);
    expect(fromFilter.selection).toEqual({ ids: new Set([8]), all: null });
    expect(addIds(EMPTY_SELECTION, []).selection).toBe(EMPTY_SELECTION);
  });

  it("bloqueia seleção vazia e acima de 1000", () => {
    expect(selectionBlocker(EMPTY_SELECTION)).toBe("Selecione ao menos um vídeo.");
    expect(selectionBlocker(selectAllFiltered({}, 1000))).toBeNull();
    expect(selectionBlocker(selectAllFiltered({}, 1001))).toBe("Máximo de 1.000 vídeos por lote (1.001 selecionados).");
  });

  it("marcação de já legendado e perfis das pastas", () => {
    expect(captionedText({ capcut: { done: 2, last_output_name: "a.mp4", open: false } })).toBe("Legendado (2)");
    expect(captionedText({ capcut: { done: 0, last_output_name: null, open: true } })).toBeNull();
    expect(folderProfiles([{ profiles: ["zeca", "ana"] }, { profiles: ["ana"] }, { profiles: [] }])).toEqual([
      "ana",
      "zeca",
    ]);
  });
});

describe("lote: pedido por modo", () => {
  it("só legendas: caption com modelo e overrides da exportação, sem edit", () => {
    const body = buildBatchRequest(plan({ styleId: 4, name: "  Meu lote " }));
    expect(body).toEqual({
      mode: "caption",
      media_ids: [1, 2, 3],
      include_done: false,
      name: "Meu lote",
      caption: {
        style_id: 4,
        overrides: { resolution: "1080P", fps: "60", bitrate: "recommended", keep_original_audio: true },
      },
    });
  });

  it("só edição: edit sem caption; nome vazio não vai", () => {
    const edit = { ...DEFAULT_BATCH_EDIT, durationMode: "30" as const, speed: 1.2, sharpen: true, startText: "2,5" };
    const body = buildBatchRequest(plan({ mode: "edit", edit, includeDone: true }));
    expect(body).toEqual({
      mode: "edit",
      media_ids: [1, 2, 3],
      include_done: true,
      edit: { preset_id: null, duration_mode: "30", speed: 1.2, sharpen: true, start_seconds: 2.5 },
    });
    expect(body.caption).toBeUndefined();
  });

  it("edição + legendas: os dois; personalizada manda custom_seconds; modelo padrão = null", () => {
    const edit = { ...DEFAULT_BATCH_EDIT, presetId: 7, durationMode: "custom" as const, customText: "45,5" };
    const body = buildBatchRequest(plan({ mode: "edit_caption", edit, selection: selectAllFiltered({ profile: "p" }, 40) }));
    expect(body.filter).toEqual({ profile: "p" });
    expect(body.media_ids).toBeUndefined();
    expect(body.caption?.style_id).toBeNull();
    expect(body.edit).toEqual({
      preset_id: 7,
      duration_mode: "custom",
      custom_seconds: 45.5,
      speed: 1,
      sharpen: false,
      start_seconds: 0,
    });
  });

  it("sem as configurações carregadas, caption vai sem overrides", () => {
    expect(buildBatchRequest(plan({ exportForm: null })).caption).toEqual({ style_id: null });
  });

  it("validação da edição só nos modos com edição", () => {
    expect(planBlocker(plan({ mode: "caption" }))).toBeNull();
    expect(planBlocker(plan({ mode: "edit" }))).toBe("Nenhuma edição selecionada.");
    expect(planBlocker(plan({ mode: "edit_caption", edit: { ...DEFAULT_BATCH_EDIT, presetId: 1 } }))).toBeNull();
    expect(validateBatchEdit({ ...DEFAULT_BATCH_EDIT, speed: 2.5 })).toBe("A velocidade deve ficar entre 0,5x e 2,0x.");
    expect(validateBatchEdit({ ...DEFAULT_BATCH_EDIT, speed: 0.5 })).toBeNull();
    expect(validateBatchEdit({ ...DEFAULT_BATCH_EDIT, durationMode: "custom", customText: "" })).toBe(
      "Informe a duração personalizada em segundos.",
    );
    expect(validateBatchEdit({ ...DEFAULT_BATCH_EDIT, startText: "abc" })).toBe(
      "Informe o início do corte em segundos (0 a 86.400).",
    );
    expect(validateBatchEdit({ ...DEFAULT_BATCH_EDIT, startText: "45" })).toBeNull();
  });

  it("início do corte: vazio = 0, passa de 30 s, teto 86400", () => {
    expect(parseStart("")).toBe(0);
    expect(parseStart("90,5")).toBe(90.5);
    expect(parseStart("86401")).toBeNull();
    expect(parseStart("-1")).toBeNull();
  });

  it("velocidade presa a 0,5..2,0 no passo de 0,01", () => {
    expect(snapBatchSpeed(0.1)).toBe(0.5);
    expect(snapBatchSpeed(3)).toBe(2);
    expect(snapBatchSpeed(1.234)).toBe(1.23);
  });

  it("preset pré-preenche velocidade, nitidez, duração e início", () => {
    const params = { speed: 1.1, sharpen: true, max_duration: 30.03, trim_start: 0.5 } as PresetParams;
    expect(editFormFromPreset(DEFAULT_BATCH_EDIT, { id: 3, params })).toEqual({
      presetId: 3,
      durationMode: "30",
      customText: "",
      startText: "0,5",
      speed: 1.1,
      sharpen: true,
    });
    const custom = editFormFromPreset(DEFAULT_BATCH_EDIT, { id: 4, params: { speed: 1, trim_start: 0, max_duration: 45 } as PresetParams });
    expect(custom).toMatchObject({ durationMode: "custom", customText: "45", startText: "", sharpen: false });
    const cleared = editFormFromPreset(custom, null);
    expect(cleared).toEqual({ ...custom, presetId: null });
  });

  it("resumo do que será feito", () => {
    const lines = planLines(
      plan({ mode: "edit_caption", edit: { ...DEFAULT_BATCH_EDIT, durationMode: "15", speed: 1.2, sharpen: true } }),
      "Amarelo",
      "Rápido",
    );
    expect(lines).toEqual([
      "3 vídeos · Edição + legendas",
      "Edição: Preset Rápido · 15 s · 1,20x · nitidez",
      "Legendas: modelo Amarelo",
      "Exportação: 1080p · 60 fps · qualidade Recomendada · com áudio original",
      "Vídeos já processados neste modo são pulados.",
    ]);
    expect(planLines(plan({ mode: "edit", includeDone: true, selection: toggleOne(EMPTY_SELECTION, 1) }), "x", null)).toEqual([
      "1 vídeo · Só edição",
      "Edição: Sem alterações",
      "Vídeos já processados neste modo são feitos de novo.",
    ]);
    expect(editSummaryText({ ...DEFAULT_BATCH_EDIT, startText: "5" }, null)).toBe("início 5 s");
  });
});

describe("lote: estimativa e formatação", () => {
  it("formatEta arredonda para minutos e horas", () => {
    expect(formatEta(null)).toBe("—");
    expect(formatEta(30)).toBe("menos de 1 min");
    expect(formatEta(12 * 60 + 20)).toBe("~12 min");
    expect(formatEta(3600)).toBe("~1 h");
    expect(formatEta(3600 + 5 * 60)).toBe("~1 h 05 min");
  });

  it("texto da barra de seleção", () => {
    expect(estimateText(undefined)).toBe("calculando estimativa…");
    expect(estimateText({ per_video_seconds: null, total_seconds: null, samples: 0 })).toBe("sem histórico para estimar");
    expect(estimateText({ per_video_seconds: 90, total_seconds: 900, samples: 4 })).toBe("estimativa ~15 min");
  });

  it("pulados com motivo", () => {
    expect(skippedSummary([])).toBeNull();
    expect(
      skippedSummary([
        { media_id: 1, reason: "already_done" },
        { media_id: 2, reason: "already_done" },
        { media_id: 3, reason: "already_queued" },
      ]),
    ).toBe("3 vídeos pulados: 2 já processados neste modo, 1 já estavam na fila.");
  });

  it("resumo das configurações do lote no histórico", () => {
    expect(batchSummaryText(batch({}))).toBe("modelo Amarelo · 1080p · 60 fps · qualidade Maior · com áudio original");
    expect(batchSummaryText(batch({ mode: "edit", summary: { ...batch({}).summary, edit: "30 s · 1,2x" } }))).toBe(
      "30 s · 1,2x",
    );
  });
});

describe("lote: status derivado e ações", () => {
  it("abertos = total menos os terminais (held conta como aberto)", () => {
    expect(openCount(counts({ completed: 4, failed: 1, cancelled: 1, pending: 3, held: 1 }))).toBe(4);
  });

  it("contagens em texto mostram só o que existe", () => {
    expect(batchCountsText(counts({ completed: 3, pending: 4, waiting: 1, running: 1, failed: 2, held: 1 }))).toBe(
      "3 de 10 concluídos · 5 na fila · 1 em andamento · 2 com erro · 1 pausado",
    );
    expect(batchCountsText(counts({ completed: 10 }))).toBe("10 de 10 concluídos");
  });

  it("progresso: concluído = 100%, senão o do servidor", () => {
    expect(batchProgress({ status: "completed", progress: 97 })).toBe(1);
    expect(batchProgress({ status: "running", progress: 40 })).toBe(0.4);
    expect(batchProgress({ status: "running", progress: 140 })).toBe(1);
  });

  it("polling rápido só com lote em andamento", () => {
    expect(batchPollMs([{ status: "running" }], 30_000)).toBe(2_000);
    expect(batchPollMs([{ status: "paused" }, { status: "completed" }], 2_000)).toBe(5_000);
  });

  it("em andamento: pausar e cancelar; sem retomar nem remover", () => {
    const can = batchActions(batch({ counts: counts({ pending: 5, running: 1, completed: 4 }) }));
    expect(can).toEqual({ pause: true, resume: false, cancel: true, retryFailed: false, zip: true, reveal: true, remove: false });
  });

  it("pausado: retomar; concluído com erros: reprocessar falhas e remover", () => {
    expect(batchActions(batch({ status: "paused", paused: true, counts: counts({ pending: 10 }) }))).toMatchObject({
      pause: false,
      resume: true,
      cancel: true,
      zip: false,
    });
    expect(
      batchActions(batch({ status: "completed_with_errors", counts: counts({ completed: 8, failed: 2 }), output_exists: false })),
    ).toEqual({ pause: false, resume: false, cancel: false, retryFailed: true, zip: true, reveal: false, remove: true });
  });

  it("cancelando: nada de pausar/retomar/cancelar de novo até terminar", () => {
    expect(batchActions(batch({ cancel_requested: true, counts: counts({ running: 1, cancelled: 9 }) }))).toMatchObject({
      pause: false,
      resume: false,
      cancel: false,
      retryFailed: false,
      remove: false,
    });
  });
});

describe("lote: jobs", () => {
  const base = { status: "pending" as const, stage: null, held: false, edit_status: null, edit_progress: null, mode: "caption" as const };

  it("etapa: pausado, editando, etapa do CapCut, copiando no modo edição", () => {
    expect(batchJobStep({ ...base, held: true })).toBe("Pausado");
    expect(batchJobStep({ ...base, mode: "edit_caption", edit_status: "processing", edit_progress: 42.4 })).toBe(
      "Editando (42%)",
    );
    expect(batchJobStep({ ...base, mode: "edit_caption", edit_status: "pending" })).toBe("Editando");
    expect(batchJobStep({ ...base, mode: "edit_caption", edit_status: "completed" })).toBe("Na fila");
    expect(batchJobStep({ ...base, status: "running", stage: "exporting" })).toBe("Exportando");
    expect(batchJobStep({ ...base, status: "running", mode: "edit" })).toBe("Copiando o vídeo editado");
    expect(batchJobStep({ ...base, status: "failed" })).toBe("Erro");
  });

  it("ações por job", () => {
    const job = { status: "pending" as const, held: false, cancel_requested: false, output_exists: false };
    expect(batchJobActions(job)).toEqual({ hold: true, release: false, cancel: true, retry: false, download: false });
    expect(batchJobActions({ ...job, held: true })).toMatchObject({ hold: false, release: true });
    expect(batchJobActions({ ...job, status: "running" })).toMatchObject({ hold: false, release: false, cancel: true });
    expect(batchJobActions({ ...job, status: "failed" })).toMatchObject({ retry: true, cancel: false });
    expect(batchJobActions({ ...job, status: "completed", output_exists: true })).toMatchObject({ download: true });
  });
});

describe("lote: correções da verificação", () => {
  it("D1: held só conta com status na fila/aguardando (etapa e Retomar)", () => {
    const job = { stage: null, held: true, edit_status: null, edit_progress: null, mode: "caption" as const };
    expect(batchJobStep({ ...job, status: "cancelled" })).toBe("Cancelado");
    expect(batchJobStep({ ...job, status: "completed" })).toBe("Concluído");
    expect(batchJobStep({ ...job, status: "failed" })).toBe("Erro");
    expect(batchJobStep({ ...job, status: "waiting" })).toBe("Pausado");
    const actions = { held: true, cancel_requested: false, output_exists: false };
    expect(batchJobActions({ ...actions, status: "cancelled" })).toMatchObject({ release: false, hold: false, retry: true });
    expect(batchJobActions({ ...actions, status: "failed" })).toMatchObject({ release: false });
    expect(batchJobActions({ ...actions, status: "waiting" })).toMatchObject({ release: true, hold: false });
  });

  it("D2: em cancelamento sem job rodando, Cancelar lote e Reprocessar falhas continuam disponíveis", () => {
    const stuck = batchActions(batch({ cancel_requested: true, counts: counts({ pending: 3, cancelled: 4, failed: 1, completed: 2 }) }));
    expect(stuck).toMatchObject({ cancel: true, retryFailed: true, pause: false, resume: false, remove: false });
    // Só falhas e nenhum aberto: reprocessar, sem cancelar.
    const done = batchActions(batch({ cancel_requested: true, counts: counts({ cancelled: 6, completed: 4 }) }));
    expect(done).toMatchObject({ cancel: false, retryFailed: true, remove: true });
    // Ainda há job rodando: espera o executor parar.
    const running = batchActions(batch({ cancel_requested: true, counts: counts({ running: 1, pending: 2, failed: 1 }) }));
    expect(running).toMatchObject({ cancel: false, retryFailed: false });
  });

  it("estimativa: edição + legendas sem histórico usa a de Só legendas como aproximada", () => {
    const none = { per_video_seconds: null, total_seconds: null, samples: 0 };
    const caption = { per_video_seconds: 60, total_seconds: 600, samples: 8 };
    expect(needsCaptionFallback("edit_caption", none)).toBe(true);
    expect(needsCaptionFallback("edit_caption", caption)).toBe(false);
    expect(needsCaptionFallback("caption", none)).toBe(false);
    expect(needsCaptionFallback("edit_caption", undefined)).toBe(false);
    const shown = resolveEstimate(none, caption);
    expect(shown).toEqual({ estimate: caption, approximate: true });
    expect(estimateText(shown.estimate, shown.approximate)).toBe("estimativa aproximada ~10 min");
    expect(resolveEstimate(none, none)).toEqual({ estimate: none, approximate: false });
    expect(resolveEstimate(caption, undefined)).toEqual({ estimate: caption, approximate: false });
    expect(resolveEstimate(none, undefined)).toEqual({ estimate: none, approximate: false });
  });

  it("tempo restante do lote: do servidor; sem ele, aproximado; sem nada, traço", () => {
    expect(batchEtaText(120, undefined)).toBe("~2 min");
    expect(batchEtaText(null, { per_video_seconds: 60, total_seconds: 300, samples: 3 })).toBe("~5 min (aproximado)");
    expect(batchEtaText(null, { per_video_seconds: null, total_seconds: null, samples: 0 })).toBe("—");
    expect(batchEtaText(null, undefined)).toBe("—");
  });
});
