import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, capcutJobFileUrl } from "../api/client";
import type { CapCutSettings } from "../api/types";
import {
  CAPCUT_STAGE_LABEL,
  CAPCUT_STAGE_RANGES,
  capcutPollMs,
  capcutSettingsPatch,
  enqueueSummary,
  jobNote,
  jobProgress,
  stageLabel,
  stageProgress,
  styleSummaryText,
  whisperModelText,
} from "./capcut";

const SETTINGS: CapCutSettings = {
  output_dir: "C:\\media-tool\\storage\\capcut",
  output_dir_is_default: true,
  resolution: "original",
  bitrate: "recommended",
  fps: "original",
  highlight: true,
  highlight_color: null,
  max_words: 3,
  max_chars: 24,
  whisper_model: "large-v3-turbo",
  keep_original_audio: true,
  keep_draft: false,
  auto_after_download: false,
  max_attempts: 3,
  default_style_id: null,
};

describe("legendas: etapas e progresso", () => {
  it("toda etapa tem rótulo em pt-BR e faixa contínua de 0 a 100", () => {
    const stages = Object.keys(CAPCUT_STAGE_RANGES);
    expect(Object.keys(CAPCUT_STAGE_LABEL).sort()).toEqual([...stages].sort());
    const ranges = Object.values(CAPCUT_STAGE_RANGES);
    expect(ranges[0]?.[0]).toBe(0);
    expect(ranges[ranges.length - 1]?.[1]).toBe(100);
    for (let i = 1; i < ranges.length; i++) expect(ranges[i]?.[0]).toBe(ranges[i - 1]?.[1]);
    expect(stageLabel("exporting")).toBe("Exportando");
    expect(stageLabel(null)).toBe("—");
    expect(stageLabel("nova_etapa")).toBe("nova_etapa");
  });

  it("calcula o progresso da etapa e do job", () => {
    expect(stageProgress("transcribing", 1)).toBeCloseTo(0.35);
    expect(stageProgress("exporting", 0.5)).toBeCloseTo(0.7);
    expect(stageProgress("exporting", 9)).toBeCloseTo(0.9);
    expect(stageProgress("exporting", null)).toBeCloseTo(0.5);
    expect(stageProgress("x", 0.5)).toBe(0);
    const base = { status: "running" as const, progress: 60, stage: "exporting" as const, stage_progress: 0.75 };
    expect(jobProgress(base)).toBeCloseTo(0.8);
    expect(jobProgress({ ...base, stage_progress: 0 })).toBeCloseTo(0.6);
    expect(jobProgress({ ...base, status: "completed" })).toBe(1);
    expect(jobProgress({ ...base, status: "failed" })).toBeCloseTo(0.6);
  });

  it("polling rápido só com job ativo", () => {
    expect(capcutPollMs([{ status: "waiting" }], 30_000)).toBe(2_000);
    expect(capcutPollMs([{ status: "completed" }], 2_000)).toBe(5_000);
    expect(capcutPollMs(undefined, null)).toBe(5_000);
  });
});

describe("legendas: patch das configurações", () => {
  it("manda só o que mudou", () => {
    expect(capcutSettingsPatch(SETTINGS, SETTINGS)).toEqual({});
    expect(capcutSettingsPatch(SETTINGS, { ...SETTINGS, max_words: 2, resolution: "1080P" })).toEqual({
      max_words: 2,
      resolution: "1080P",
    });
  });

  it("pasta vazia volta à padrão só quando estava personalizada", () => {
    expect(capcutSettingsPatch(SETTINGS, { ...SETTINGS, output_dir: "" })).toEqual({});
    const custom = { ...SETTINGS, output_dir: "D:\\Legendas", output_dir_is_default: false };
    expect(capcutSettingsPatch(custom, { ...custom, output_dir: "  " })).toEqual({ output_dir: null });
    expect(capcutSettingsPatch(SETTINGS, { ...SETTINGS, output_dir: " D:\\Saida " })).toEqual({
      output_dir: "D:\\Saida",
    });
  });

  it("cor vazia é automática (null)", () => {
    const colored = { ...SETTINGS, highlight_color: "#FFD400" };
    expect(capcutSettingsPatch(colored, { ...colored, highlight_color: "" })).toEqual({ highlight_color: null });
    expect(capcutSettingsPatch(SETTINGS, { ...SETTINGS, highlight_color: "" })).toEqual({});
    expect(capcutSettingsPatch(SETTINGS, { ...SETTINGS, highlight_color: "#00FF00" })).toEqual({
      highlight_color: "#00FF00",
    });
  });
});

describe("legendas: textos", () => {
  it("resume o envio da Biblioteca", () => {
    expect(enqueueSummary({ created: [], skipped: [] })).toBe("Nenhum vídeo enviado para legendar.");
    const one = { created: [{} as never], skipped: [] };
    expect(enqueueSummary(one)).toBe("1 vídeo enviado para legendar.");
    expect(
      enqueueSummary({
        created: [{} as never, {} as never],
        skipped: [
          { media_id: 3, reason: "already_queued" },
          { media_id: 4, reason: "not_video" },
          { media_id: 5, reason: "not_video" },
        ],
      }),
    ).toBe("2 vídeos enviados para legendar; 3 pulados (1 já estavam na fila, 2 não são vídeo).");
  });

  it("resume o modelo de legenda", () => {
    expect(styleSummaryText(null)).toBe("Resumo indisponível");
    expect(
      styleSummaryText({ font: "Montserrat", color: "#ffffff", highlight_color: "#ffd400", has_animation: true }),
    ).toBe("Montserrat · #ffffff · destaque #ffd400 · com animação");
    expect(styleSummaryText({ font: null, color: null, highlight_color: null, has_animation: false, has_effect: true })).toBe(
      "Fonte padrão · destaque automático · sem animação · com efeito de texto (o destaque por cor não aparece)",
    );
  });

  it("modelo de transcrição e nota do job", () => {
    expect(whisperModelText({ small: true }, "small")).toBe("small: baixado");
    expect(whisperModelText({}, "medium")).toBe("medium: baixa no 1º uso (~1,5 GB)");
    const job = { status: "failed" as const, error_message: "Falhou feio", detail: null, cancel_requested: false };
    expect(jobNote(job)).toBe("Falhou feio");
    expect(jobNote({ ...job, status: "pending" })).toBe("Última tentativa: Falhou feio");
    expect(jobNote({ ...job, status: "running", cancel_requested: true })).toBe("Cancelando…");
  });
});

describe("legendas: cliente", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubGlobal("window", globalThis);
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("enfileira com o header do painel e filtra a lista por status", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ created: [], skipped: [] }), {
        status: 202,
        headers: { "content-type": "application/json" },
      }),
    );
    await api.createCapcutJobs({ media_ids: [1, 2] });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/capcut/jobs");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["X-Media-Tool-Client"]).toBe("panel");
    expect(JSON.parse(String(init.body))).toEqual({ media_ids: [1, 2] });

    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0, counts: {} }), {
        headers: { "content-type": "application/json" },
      }),
    );
    await api.listCapcutJobs({ status: "failed", limit: 50 });
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/capcut/jobs?status=failed&limit=50");
    expect(capcutJobFileUrl(7)).toBe("/api/capcut/jobs/7/file");
  });
});
