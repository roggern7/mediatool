import { describe, expect, it } from "vitest";
import type { CapCutBatchCounts, CapCutJob, CapCutQueueCounts, CapCutReviewSegment } from "../api/types";
import { CAPCUT_REVIEW_MODES, CAPCUT_WHISPER_MODELS, WHISPER_MODEL_SIZE, jobStatusText, queueSummaryText } from "./capcut";
import { BATCH_STATUS_LABEL, batchCountsText, batchJobActions, batchJobStep, batchTone } from "./capcutBatch";
import {
  approveBody,
  defaultOnlyDoubtful,
  doubtCount,
  editedCount,
  formatReviewTime,
  isRowEdited,
  nextReviewJob,
  nextRowIndex,
  reviewCountText,
  reviewRows,
  reviewWaitingText,
  setRowText,
  visibleRows,
} from "./capcutReview";

const SEGMENTS: CapCutReviewSegment[] = [
  {
    index: 0,
    start: 0,
    end: 0.8,
    text: "Olá pessoal",
    doubt: false,
    words: [
      { text: "Olá", start: 0, end: 0.4, doubt: false, probability: 0.9 },
      { text: "pessoal", start: 0.4, end: 0.8, doubt: false, probability: 0.9 },
    ],
  },
  {
    index: 1,
    start: 1,
    end: 1.6,
    text: "Esse laranjinha",
    doubt: true,
    words: [
      { text: "Esse", start: 1, end: 1.3, doubt: true, probability: 0.3 },
      { text: "laranjinha", start: 1.3, end: 1.6, doubt: true, probability: 0.3 },
    ],
  },
  {
    index: 4,
    start: 2,
    end: 2.5,
    text: "tchau",
    doubt: false,
    words: [{ text: "tchau", start: 2, end: 2.5, doubt: false, probability: 0.95 }],
  },
];

describe("revisão das legendas: frases e edições", () => {
  it("monta as linhas com o texto original e marca só as alteradas", () => {
    const rows = reviewRows(SEGMENTS);
    expect(rows.map((r) => [r.index, r.original, r.text, r.doubt])).toEqual([
      [0, "Olá pessoal", "Olá pessoal", false],
      [1, "Esse laranjinha", "Esse laranjinha", true],
      [4, "tchau", "tchau", false],
    ]);
    expect(editedCount(rows)).toBe(0);
    const edited = setRowText(rows, 1, "Esse é o Gerson");
    expect(isRowEdited(edited[1]!)).toBe(true);
    expect(isRowEdited(edited[0]!)).toBe(false);
    expect(editedCount(edited)).toBe(1);
    expect(rows[1]?.text).toBe("Esse laranjinha");
  });

  it("espaços a mais não contam como edição", () => {
    const rows = setRowText(reviewRows(SEGMENTS), 0, "  Olá   pessoal ");
    expect(isRowEdited(rows[0]!)).toBe(false);
    expect(approveBody(rows)).toEqual({ segments: [] });
  });

  it("o corpo do POST leva só as frases alteradas (vazio remove; nenhuma = aprovar como está)", () => {
    const rows = reviewRows(SEGMENTS);
    expect(approveBody(rows)).toEqual({ segments: [] });
    const changed = setRowText(setRowText(rows, 1, " Esse é  o Gerson "), 4, "   ");
    expect(approveBody(changed)).toEqual({
      segments: [
        { index: 1, text: "Esse é o Gerson" },
        { index: 4, text: "" },
      ],
    });
  });
});

describe("revisão das legendas: filtro e navegação", () => {
  it("só trechos duvidosos: ligado por padrão quando há dúvidas e mantém as editadas visíveis", () => {
    const rows = reviewRows(SEGMENTS);
    expect(doubtCount(rows)).toBe(1);
    expect(defaultOnlyDoubtful(rows)).toBe(true);
    expect(defaultOnlyDoubtful(rows.map((r) => ({ ...r, doubt: false })))).toBe(false);
    expect(visibleRows(rows, true).map((r) => r.index)).toEqual([1]);
    expect(visibleRows(rows, false).map((r) => r.index)).toEqual([0, 1, 4]);
    const edited = setRowText(rows, 4, "até mais");
    expect(visibleRows(edited, true).map((r) => r.index)).toEqual([1, 4]);
  });

  it("Enter vai para a próxima frase visível; na última não há próxima", () => {
    const rows = reviewRows(SEGMENTS);
    expect(nextRowIndex(rows, 0)).toBe(1);
    expect(nextRowIndex(rows, 1)).toBe(4);
    expect(nextRowIndex(rows, 4)).toBeNull();
    expect(nextRowIndex(rows, 99)).toBeNull();
  });

  it("textos de contagem e tempo", () => {
    expect(reviewCountText(0)).toBe("Nenhum trecho duvidoso");
    expect(reviewCountText(1)).toBe("1 trecho para conferir");
    expect(reviewCountText(3)).toBe("3 trechos para conferir");
    expect(reviewWaitingText(1)).toBe("1 vídeo aguardando revisão");
    expect(reviewWaitingText(2)).toBe("2 vídeos aguardando revisão");
    expect(formatReviewTime(4.25)).toBe("0:04,2");
    expect(formatReviewTime(65.3)).toBe("1:05,3");
    expect(formatReviewTime(2.3)).toBe("0:02,3");
    expect(formatReviewTime(-1)).toBe("0:00,0");
  });
});

type ReviewJob = Pick<CapCutJob, "id" | "status" | "held" | "cancel_requested" | "output_exists" | "needs_review">;

describe("revisão das legendas: ações e contagens", () => {
  const inReview: ReviewJob = {
    id: 7,
    status: "waiting",
    held: false,
    cancel_requested: false,
    output_exists: false,
    needs_review: true,
  };

  it("job em revisão: Revisar e Cancelar; sem pausar", () => {
    const can = batchJobActions(inReview);
    expect(can.review).toBe(true);
    expect(can.cancel).toBe(true);
    expect(can.hold).toBe(false);
    expect(can.retry).toBe(false);
    expect(batchJobActions({ ...inReview, cancel_requested: true }).review).toBe(false);
    expect(batchJobActions({ ...inReview, needs_review: false }).review).toBe(false);
    expect(batchJobActions({ ...inReview, needs_review: false }).hold).toBe(true);
  });

  it("status e etapa do job em revisão", () => {
    expect(jobStatusText(inReview)).toBe("Aguardando revisão");
    expect(jobStatusText({ ...inReview, needs_review: false })).toBe("Aguardando");
    expect(batchJobStep({ ...inReview, stage: null, edit_status: null, edit_progress: null, mode: "caption" })).toBe(
      "Aguardando revisão",
    );
  });

  it("próximo vídeo a revisar", () => {
    const jobs = [
      { id: 1, status: "waiting" as const, needs_review: false },
      { id: 2, status: "waiting" as const, needs_review: true },
      { id: 3, status: "waiting" as const, needs_review: true },
    ];
    expect(nextReviewJob(jobs)?.id).toBe(2);
    expect(nextReviewJob(jobs, 2)?.id).toBe(3);
    expect(nextReviewJob([])).toBeNull();
  });

  it("contagens de revisão no lote e na fila", () => {
    const counts: CapCutBatchCounts = {
      total: 3,
      pending: 0,
      running: 0,
      waiting: 0,
      held: 0,
      completed: 1,
      failed: 0,
      cancelled: 0,
      review: 2,
    };
    expect(batchCountsText(counts)).toBe("1 de 3 concluídos · 2 aguardando revisão");
    expect(BATCH_STATUS_LABEL.review).toBe("Aguardando revisão");
    expect(batchTone("review")).toBe("warn");
    const q: CapCutQueueCounts = {
      pending: 0,
      running: 0,
      waiting: 0,
      failed: 0,
      completed: 0,
      cancelled: 0,
      paused: false,
      review: 2,
    };
    expect(queueSummaryText(q)).toBe(
      "Fila ativa: 0 em andamento, 0 na fila, 0 aguardando o CapCut, 2 aguardando revisão, 0 com erro.",
    );
  });
});

describe("revisão das legendas: configurações", () => {
  it("modelos com máxima precisão primeiro e modos de revisão", () => {
    expect(CAPCUT_WHISPER_MODELS.map((m) => m.value)).toEqual(["large-v3", "large-v3-turbo", "medium", "small"]);
    expect(CAPCUT_WHISPER_MODELS[0]?.label).toBe("Máxima precisão (large-v3)");
    expect(WHISPER_MODEL_SIZE["large-v3"]).toBe("~3 GB");
    expect(CAPCUT_REVIEW_MODES.map((m) => [m.value, m.label])).toEqual([
      ["doubtful", "Só quando houver trechos duvidosos"],
      ["always", "Sempre"],
      ["never", "Nunca, exportar direto"],
    ]);
  });
});
