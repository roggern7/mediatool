// Legendas CapCut: rótulos, progresso, polling e patch das configurações (funções puras, testadas em capcut.test.ts).

import type {
  CapCutBitrate,
  CapCutExportMode,
  CapCutFps,
  CapCutJob,
  CapCutJobStatus,
  CapCutJobsCreated,
  CapCutQueueCounts,
  CapCutResolution,
  CapCutSettings,
  CapCutSettingsUpdate,
  CapCutStage,
  CapCutStyleSummary,
  CapCutWhisperModel,
} from "../api/types";
import { formatCount } from "./format";
import type { Tone } from "./labels";
import { nextPollMs } from "./polling";

export const CAPCUT_STATUS_LABEL: Record<CapCutJobStatus, string> = {
  pending: "Na fila",
  running: "Em andamento",
  waiting: "Aguardando",
  completed: "Concluído",
  failed: "Erro",
  cancelled: "Cancelado",
};

export const CAPCUT_STAGE_LABEL: Record<CapCutStage, string> = {
  transcribing: "Transcrevendo o áudio",
  drafting: "Montando o projeto",
  waiting_capcut: "Esperando o CapCut",
  launching: "Abrindo o CapCut",
  opening: "Abrindo o projeto",
  exporting: "Exportando",
  finalizing: "Conferindo o vídeo",
  cleaning: "Limpando",
};

/** Faixa de cada etapa no progresso agregado (0..100); igual à do backend (app/jobs/capcut_runner.py). */
export const CAPCUT_STAGE_RANGES: Record<CapCutStage, [number, number]> = {
  transcribing: [0, 35],
  drafting: [35, 40],
  waiting_capcut: [40, 42],
  launching: [42, 46],
  opening: [46, 50],
  exporting: [50, 90],
  finalizing: [90, 98],
  cleaning: [98, 100],
};

export const CAPCUT_ACTIVE: ReadonlySet<CapCutJobStatus> = new Set<CapCutJobStatus>(["pending", "running", "waiting"]);

export const WHISPER_MODEL_SIZE: Record<CapCutWhisperModel, string> = {
  small: "~500 MB",
  medium: "~1,5 GB",
  "large-v3-turbo": "~1,6 GB",
};

export function capcutTone(status: CapCutJobStatus): Tone {
  switch (status) {
    case "completed":
      return "ok";
    case "failed":
      return "err";
    case "running":
      return "accent";
    case "waiting":
      return "warn";
    default:
      return "muted";
  }
}

export function stageLabel(stage: string | null): string {
  if (!stage) return "—";
  return CAPCUT_STAGE_LABEL[stage as CapCutStage] ?? stage;
}

/** Progresso agregado (0..1) de uma etapa com `fraction` concluída; etapa desconhecida vale 0. */
export function stageProgress(stage: string | null, fraction: number | null): number {
  const range = stage ? CAPCUT_STAGE_RANGES[stage as CapCutStage] : undefined;
  if (!range) return 0;
  const f = fraction === null || !Number.isFinite(fraction) ? 0 : Math.min(1, Math.max(0, fraction));
  return (range[0] + (range[1] - range[0]) * f) / 100;
}

/** Fração (0..1) mostrada na barra: concluído = 1; senão o maior entre o agregado gravado e o da etapa atual. */
export function jobProgress(job: Pick<CapCutJob, "status" | "progress" | "stage" | "stage_progress">): number {
  if (job.status === "completed") return 1;
  const stored = Math.min(100, Math.max(0, job.progress || 0)) / 100;
  return job.status === "running" ? Math.max(stored, stageProgress(job.stage, job.stage_progress)) : stored;
}

export function isCapcutActive(job: Pick<CapCutJob, "status">): boolean {
  return CAPCUT_ACTIVE.has(job.status);
}

/** Pausa individual vale só para job na fila ou aguardando; nos outros status `held` é ignorado. */
export function isHeldJob(job: Pick<CapCutJob, "status" | "held">): boolean {
  return Boolean(job.held) && (job.status === "pending" || job.status === "waiting");
}

/** Status do job na lista: "Na fila · pausado" com pausa individual; senão o rótulo do status. */
export function jobStatusText(job: Pick<CapCutJob, "status" | "held">): string {
  const label = CAPCUT_STATUS_LABEL[job.status];
  return isHeldJob(job) ? `${label} · pausado` : label;
}

/**
 * Linha da fila global. `pending` já inclui os pausados individualmente: eles aparecem entre parênteses, sem somar
 * de novo. "Fila ativa: 1 em andamento, 5 na fila (2 pausados), 0 aguardando o CapCut, 1 com erro."
 */
export function queueSummaryText(q: CapCutQueueCounts): string {
  const held = q.held ?? 0;
  const pausedPart = held > 0 ? ` (${formatCount(held)} ${held === 1 ? "pausado" : "pausados"})` : "";
  return (
    `Fila ${q.paused ? "pausada" : "ativa"}: ${formatCount(q.running)} em andamento, ${formatCount(q.pending)} na fila` +
    `${pausedPart}, ${formatCount(q.waiting)} aguardando o CapCut, ${formatCount(q.failed)} com erro.`
  );
}

/** 2 s com algum job ativo; ocioso, os degraus de lib/polling. */
export function capcutPollMs(jobs: Pick<CapCutJob, "status">[] | undefined, current: number | null): number {
  return nextPollMs(current, (jobs ?? []).some(isCapcutActive));
}

const SETTINGS_FIELDS = [
  "output_dir",
  "resolution",
  "bitrate",
  "fps",
  "highlight",
  "highlight_color",
  "max_words",
  "max_chars",
  "whisper_model",
  "keep_original_audio",
  "keep_draft",
  "auto_after_download",
  "max_attempts",
  "export_mode",
] as const;

/**
 * Só os campos que mudaram em relação ao que veio do servidor. Pasta de saída vazia ou igual à padrão (quando ainda é
 * a padrão) não muda nada; vazia depois de personalizada volta à padrão (`null`). Cor vazia = automática (`null`).
 */
export function capcutSettingsPatch(initial: CapCutSettings, draft: CapCutSettings): CapCutSettingsUpdate {
  const patch: Record<string, unknown> = {};
  for (const key of SETTINGS_FIELDS) {
    let value: unknown = draft[key];
    if (key === "output_dir") {
      const text = draft.output_dir.trim();
      if (text === initial.output_dir.trim()) continue;
      value = text === "" ? (initial.output_dir_is_default ? undefined : null) : text;
      if (value === undefined) continue;
    } else if (key === "highlight_color") {
      const text = (draft.highlight_color ?? "").trim();
      value = text === "" ? null : text;
      if (value === initial.highlight_color) continue;
    } else if (value === initial[key]) {
      continue;
    }
    patch[key] = value;
  }
  return patch as CapCutSettingsUpdate;
}

const SKIP_REASON: Record<CapCutJobsCreated["skipped"][number]["reason"], string> = {
  media_not_found: "não encontrados",
  not_video: "não são vídeo",
  cover: "são capa",
  already_queued: "já estavam na fila",
};

/** Resumo do envio da Biblioteca: "2 vídeos enviados para legendar; 1 pulado (já estavam na fila)." */
export function enqueueSummary(result: CapCutJobsCreated): string {
  const created = result.created.length;
  const head =
    created === 0
      ? "Nenhum vídeo enviado para legendar"
      : `${formatCount(created)} ${created === 1 ? "vídeo enviado" : "vídeos enviados"} para legendar`;
  if (result.skipped.length === 0) return `${head}.`;
  const counts = new Map<string, number>();
  for (const s of result.skipped) counts.set(s.reason, (counts.get(s.reason) ?? 0) + 1);
  const parts = Array.from(counts, ([reason, n]) => `${formatCount(n)} ${SKIP_REASON[reason as keyof typeof SKIP_REASON]}`);
  const skipped = result.skipped.length;
  return `${head}; ${formatCount(skipped)} ${skipped === 1 ? "pulado" : "pulados"} (${parts.join(", ")}).`;
}

/** "Montserrat · #ffffff · destaque #ffd400 · com animação · posição -0,70". */
export function styleSummaryText(summary: CapCutStyleSummary | null): string {
  if (!summary) return "Resumo indisponível";
  const parts: string[] = [];
  parts.push(summary.font || "Fonte padrão");
  if (summary.color) parts.push(summary.color);
  parts.push(summary.highlight_color ? `destaque ${summary.highlight_color}` : "destaque automático");
  if (summary.caption_template) parts.push(`modelo ${summary.caption_template}`);
  parts.push(summary.has_animation ? "com animação" : "sem animação");
  // Verificado no CapCut 9.7.1: um efeito de texto define as cores e encobre a cor do destaque por palavra.
  if (summary.has_effect) parts.push("com efeito de texto (o destaque por cor não aparece)");
  if (typeof summary.position_y === "number") {
    parts.push(`posição ${summary.position_y.toLocaleString("pt-BR", { maximumFractionDigits: 2 })}`);
  }
  return parts.join(" · ");
}

/** Situação do modelo de transcrição escolhido. */
export function whisperModelText(models: Record<string, boolean> | undefined, model: CapCutWhisperModel): string {
  if (models?.[model]) return `${model}: baixado`;
  return `${model}: baixa no 1º uso (${WHISPER_MODEL_SIZE[model]})`;
}

/** Texto da coluna de erro/andamento de um job. */
export function jobNote(job: Pick<CapCutJob, "status" | "error_message" | "detail" | "cancel_requested">): string {
  if (job.cancel_requested && job.status === "running") return "Cancelando…";
  if (job.status === "failed") return job.error_message ?? "Falhou.";
  if (job.status === "pending" && job.error_message) return `Última tentativa: ${job.error_message}`;
  return job.detail ?? "";
}

/** Modos de exportação (configurações): rótulo e ajuda curta. */
export const CAPCUT_EXPORT_MODES: { value: CapCutExportMode; label: string; help: string }[] = [
  {
    value: "isolated",
    label: "Isolado (recomendado)",
    help: "Não usa mouse, teclado nem a tela; você pode usar o PC normalmente.",
  },
  { value: "interactive", label: "Interativo", help: "Usa a tela e precisa do PC livre." },
];

export function exportModeLabel(mode: CapCutExportMode | null | undefined): string {
  return CAPCUT_EXPORT_MODES.find((m) => m.value === mode)?.label ?? "Desconhecido";
}

/** Modo usado pelo job na lista: "Isolado" | "Interativo" | "—" (ainda não rodou). */
export function jobExportModeText(mode: CapCutExportMode | null | undefined): string {
  if (mode === "isolated") return "Isolado";
  if (mode === "interactive") return "Interativo";
  return "—";
}

export function exportModeHelp(mode: CapCutExportMode | null | undefined): string {
  return CAPCUT_EXPORT_MODES.find((m) => m.value === mode)?.help ?? "";
}

/** Texto do status do modo configurado; o isolado sem os pré-requisitos mostra o motivo. */
export function exportModeStatusText(
  mode: CapCutExportMode,
  isolated: { available: boolean; reason: string | null } | undefined,
): { ok: boolean; text: string } {
  if (mode === "isolated" && isolated && !isolated.available) {
    return {
      ok: false,
      text: `${exportModeLabel(mode)}: indisponível${isolated.reason ? ` (${isolated.reason})` : ""}. Troque para Interativo nas configurações.`,
    };
  }
  return { ok: true, text: `${exportModeLabel(mode)}: ${exportModeHelp(mode)}` };
}

/** Opções das configurações e da exportação do lote (mesmos rótulos nos dois lugares). */
export const CAPCUT_RESOLUTIONS: { value: CapCutResolution; label: string }[] = [
  { value: "original", label: "Igual ao original" },
  { value: "480P", label: "480p" },
  { value: "720P", label: "720p" },
  { value: "1080P", label: "1080p" },
  { value: "2K", label: "2K" },
  { value: "4K", label: "4K" },
];
export const CAPCUT_BITRATES: { value: CapCutBitrate; label: string }[] = [
  { value: "low", label: "Menor" },
  { value: "recommended", label: "Recomendada" },
  { value: "high", label: "Maior" },
];
export const CAPCUT_FPS_OPTIONS: { value: CapCutFps; label: string }[] = [
  { value: "60", label: "60 fps (padrão)" },
  { value: "50", label: "50 fps" },
  { value: "30", label: "30 fps" },
  { value: "25", label: "25 fps" },
  { value: "24", label: "24 fps" },
  { value: "original", label: "Igual ao original" },
];
export const CAPCUT_WHISPER_MODELS: { value: CapCutWhisperModel; label: string }[] = [
  { value: "small", label: "Rápido (small)" },
  { value: "medium", label: "Equilibrado (medium)" },
  { value: "large-v3-turbo", label: "Mais preciso (large-v3-turbo)" },
];
