// Lotes da página Edição CapCut: seleção, pedido por modo, estimativa, status e ações (funções puras, testadas em
// capcutBatch.test.ts).

import type {
  CapCutBatch,
  CapCutBatchCounts,
  CapCutBatchCreate,
  CapCutBatchCreated,
  CapCutBatchEditSettings,
  CapCutBatchFilter,
  CapCutBatchMode,
  CapCutBatchStatus,
  CapCutBatchSummary,
  CapCutCaptioned,
  CapCutEstimate,
  CapCutImportSettings,
  CapCutJob,
  CapCutLibraryItem,
  CapCutOptionsPatch,
  CapCutSettings,
  EditDurationMode,
  EditFolder,
  JobStatus,
  Preset,
} from "../api/types";
import {
  CAPCUT_BITRATES,
  CAPCUT_RESOLUTIONS,
  CAPCUT_STATUS_LABEL,
  isCapcutActive,
  isHeldJob,
  needsReview,
  REVIEW_STATUS_LABEL,
  stageLabel,
} from "./capcut";
import { CUSTOM_SECONDS_MAX, EDIT_DURATION_MARGIN, NO_CHANGES_TEXT } from "./editSettings";
import { formatCount, formatSpeed, parseDecimal, toDecimalInput } from "./format";
import { TERMINAL_JOB, type Tone } from "./labels";
import { nextPollMs } from "./polling";

/** Mesmo teto do backend (422 batch_too_large). */
export const MAX_BATCH_VIDEOS = 1000;
export const BATCH_SPEED_MIN = 0.5;
export const BATCH_SPEED_MAX = 2;
export const BATCH_SPEED_STEP = 0.01;
export const BATCH_START_MAX = 86_400;

/* ---------------- Modo e status ---------------- */

export const BATCH_MODE_OPTIONS: { value: CapCutBatchMode; label: string; help: string }[] = [
  { value: "caption", label: "Legendar", help: "Transcreve e coloca legendas pelo CapCut" },
  { value: "edit", label: "Editar", help: "Corta, acelera ou dá nitidez, sem CapCut" },
  { value: "edit_caption", label: "Editar e legendar", help: "Edita e depois legenda a versão editada" },
];

export const BATCH_MODE_LABEL: Record<CapCutBatchMode, string> = {
  caption: "Legendar",
  edit: "Editar",
  edit_caption: "Editar e legendar",
};

/** Próximo modo no grupo de cartões (setas do teclado; volta ao início no fim). */
export function nextMode(mode: CapCutBatchMode, step: 1 | -1): CapCutBatchMode {
  const index = BATCH_MODE_OPTIONS.findIndex((o) => o.value === mode);
  const size = BATCH_MODE_OPTIONS.length;
  return BATCH_MODE_OPTIONS[(index + step + size) % size]?.value ?? mode;
}

export const BATCH_STATUS_LABEL: Record<CapCutBatchStatus, string> = {
  importing: "Aguardando downloads",
  running: "Em andamento",
  paused: "Pausado",
  review: "Aguardando revisão",
  completed: "Concluído",
  completed_with_errors: "Concluído com erros",
  cancelled: "Cancelado",
};

export const CAPTIONED_OPTIONS: { value: CapCutCaptioned; label: string }[] = [
  { value: "any", label: "Todos" },
  { value: "no", label: "Só os não legendados" },
  { value: "yes", label: "Só os legendados" },
];

export function modeHasCaption(mode: CapCutBatchMode): boolean {
  return mode !== "edit";
}

export function modeHasEdit(mode: CapCutBatchMode): boolean {
  return mode !== "caption";
}

export function batchTone(status: CapCutBatchStatus): Tone {
  switch (status) {
    case "completed":
      return "ok";
    case "completed_with_errors":
      return "err";
    case "running":
    case "importing":
      return "accent";
    case "paused":
    case "review":
      return "warn";
    default:
      return "muted";
  }
}

/* ---------------- Seleção ---------------- */

/**
 * Vídeos marcados um a um (`ids`) OU "todos os filtrados" (`all`, resolvido no servidor: vai como `filter` no pedido,
 * com o total que a lista mostrou).
 */
export interface BatchSelection {
  ids: ReadonlySet<number>;
  all: { filter: CapCutBatchFilter; total: number } | null;
}

export const EMPTY_SELECTION: BatchSelection = { ids: new Set(), all: null };

/** Filtro sem campos vazios; "Já legendados: todos" não vai no pedido. */
export function cleanFilter(filter: CapCutBatchFilter): CapCutBatchFilter {
  const out: CapCutBatchFilter = {};
  const q = filter.q?.trim();
  const profile = filter.profile?.trim();
  if (q) out.q = q;
  if (profile) out.profile = profile;
  if (filter.folder) out.folder = filter.folder;
  if (filter.captioned && filter.captioned !== "any") out.captioned = filter.captioned;
  return out;
}

export function selectionCount(sel: BatchSelection): number {
  return sel.all ? sel.all.total : sel.ids.size;
}

export function isSelected(sel: BatchSelection, id: number): boolean {
  return sel.all !== null || sel.ids.has(id);
}

/** Marca ou desmarca um vídeo. Com "todos os filtrados" ativo, não muda (a lista mostra tudo marcado). */
export function toggleOne(sel: BatchSelection, id: number): BatchSelection {
  if (sel.all) return sel;
  const ids = new Set(sel.ids);
  if (ids.has(id)) ids.delete(id);
  else ids.add(id);
  return { ids, all: null };
}

/** Situação da página atual: nenhum, alguns ou todos marcados. */
export function pageState(sel: BatchSelection, pageIds: readonly number[]): "none" | "some" | "all" {
  if (pageIds.length === 0) return "none";
  if (sel.all) return "all";
  const marked = pageIds.filter((id) => sel.ids.has(id)).length;
  return marked === 0 ? "none" : marked === pageIds.length ? "all" : "some";
}

/** "Marcar a página": marca todos da página; se já estavam todos, desmarca os da página (os de outras ficam). */
export function togglePage(sel: BatchSelection, pageIds: readonly number[]): BatchSelection {
  if (sel.all || pageIds.length === 0) return sel;
  const ids = new Set(sel.ids);
  const allMarked = pageIds.every((id) => ids.has(id));
  for (const id of pageIds) {
    if (allMarked) ids.delete(id);
    else ids.add(id);
  }
  return { ids, all: null };
}

/** "Selecionar todos os N filtrados": troca a marcação individual pelo filtro. */
export function selectAllFiltered(filter: CapCutBatchFilter, total: number): BatchSelection {
  return { ids: new Set(), all: { filter: cleanFilter(filter), total } };
}

/**
 * Junta ids à seleção (vídeos enviados do computador). Com "todos os filtrados" ativo, a seleção passa a ser só a
 * lista de ids (o pedido aceita ids OU filtro): `leftFilter` avisa a troca.
 */
export function addIds(sel: BatchSelection, ids: readonly number[]): { selection: BatchSelection; leftFilter: boolean } {
  if (ids.length === 0) return { selection: sel, leftFilter: false };
  const next = new Set(sel.all ? [] : sel.ids);
  for (const id of ids) next.add(id);
  return { selection: { ids: next, all: null }, leftFilter: sel.all !== null };
}

/** Problema que impede processar a seleção, ou null. */
export function selectionBlocker(sel: BatchSelection): string | null {
  const count = selectionCount(sel);
  if (count === 0) return "Selecione ao menos um vídeo.";
  if (count > MAX_BATCH_VIDEOS) {
    return `Máximo de ${formatCount(MAX_BATCH_VIDEOS)} vídeos por lote (${formatCount(count)} selecionados).`;
  }
  return null;
}

/** Parte do pedido com os vídeos: `filter` em "todos os filtrados", senão `media_ids` (em ordem crescente). */
export function selectionTarget(sel: BatchSelection): Pick<CapCutBatchCreate, "media_ids" | "filter"> {
  if (sel.all) return { filter: { ...sel.all.filter } };
  return { media_ids: Array.from(sel.ids).sort((a, b) => a - b) };
}

/** "Legendado (2)" nos vídeos que já têm legenda concluída; null nos outros. */
export function captionedText(item: Pick<CapCutLibraryItem, "capcut">): string | null {
  return item.capcut.done > 0 ? `Legendado (${formatCount(item.capcut.done)})` : null;
}

/** Perfis (sem repetir, em ordem alfabética) das pastas do Editar, para o filtro de perfil. */
export function folderProfiles(folders: readonly Pick<EditFolder, "profiles">[]): string[] {
  const seen = new Set<string>();
  for (const f of folders) for (const p of f.profiles) if (p) seen.add(p);
  return Array.from(seen).sort((a, b) => a.localeCompare(b, "pt-BR"));
}

/* ---------------- Edição ---------------- */

/** Estado do formulário de edição do lote; textos como digitados (vírgula ou ponto). */
export interface BatchEditForm {
  presetId: number | null;
  durationMode: EditDurationMode;
  customText: string;
  /** Início do corte em segundos; vazio = 0. */
  startText: string;
  speed: number;
  sharpen: boolean;
  /** Espelhar o vídeo (inverter na horizontal); escolher um preset copia o dele. */
  mirror: boolean;
}

export const DEFAULT_BATCH_EDIT: BatchEditForm = {
  presetId: null,
  durationMode: "original",
  customText: "",
  startText: "",
  speed: 1,
  sharpen: false,
  mirror: false,
};

/** Velocidade presa a 0,5x..2,0x no passo de 0,01. */
export function snapBatchSpeed(value: number): number {
  if (!Number.isFinite(value)) return 1;
  const clamped = Math.min(BATCH_SPEED_MAX, Math.max(BATCH_SPEED_MIN, value));
  return Number((Math.round(clamped / BATCH_SPEED_STEP) * BATCH_SPEED_STEP).toFixed(2));
}

/** Início do corte em segundos (vazio = 0); null se inválido ou fora de 0..86400. */
export function parseStart(text: string): number | null {
  if (text.trim() === "") return 0;
  const value = parseDecimal(text);
  return value !== null && value >= 0 && value <= BATCH_START_MAX ? value : null;
}

function customSeconds(form: BatchEditForm): number | null {
  const value = parseDecimal(form.customText);
  return value !== null && value > 0 && value <= CUSTOM_SECONDS_MAX ? value : null;
}

/** Nada muda: sem preset, duração original, início 0, 1,0x e sem nitidez (o backend recusa com edit_no_changes). */
export function isNoEditChange(form: BatchEditForm): boolean {
  return (
    form.presetId === null &&
    form.durationMode === "original" &&
    parseStart(form.startText) === 0 &&
    form.speed === 1 &&
    !form.sharpen &&
    !form.mirror
  );
}

/** Mensagem do primeiro problema da edição, ou null. */
export function validateBatchEdit(form: BatchEditForm): string | null {
  if (!Number.isFinite(form.speed) || form.speed < BATCH_SPEED_MIN || form.speed > BATCH_SPEED_MAX) {
    return "A velocidade deve ficar entre 0,5x e 2,0x.";
  }
  if (form.durationMode === "custom") {
    const value = parseDecimal(form.customText);
    if (value === null) return "Informe a duração personalizada em segundos.";
    if (value <= 0) return "A duração personalizada deve ser maior que zero.";
    if (value > CUSTOM_SECONDS_MAX) return "A duração personalizada pode ter no máximo 7200 segundos.";
  }
  if (parseStart(form.startText) === null) return "Informe o início do corte em segundos (0 a 86.400).";
  if (isNoEditChange(form)) return NO_CHANGES_TEXT;
  return null;
}

/** Duração do preset (`max_duration`, com ou sem a margem de 0,03 s) como opção do formulário. */
function durationFromPreset(maxDuration: number | undefined): Pick<BatchEditForm, "durationMode" | "customText"> {
  if (!maxDuration || maxDuration <= 0) return { durationMode: "original", customText: "" };
  for (const limit of [15, 30, 60] as const) {
    if (Math.abs(maxDuration - limit) < 0.001 || Math.abs(maxDuration - EDIT_DURATION_MARGIN - limit) < 0.001) {
      return { durationMode: String(limit) as EditDurationMode, customText: "" };
    }
  }
  return { durationMode: "custom", customText: toDecimalInput(Math.round(maxDuration * 100) / 100) };
}

/** Escolher um preset pré-preenche velocidade, nitidez, duração e início a partir dos params dele; null limpa só o preset. */
export function editFormFromPreset(form: BatchEditForm, preset: Pick<Preset, "id" | "params"> | null): BatchEditForm {
  if (!preset) return { ...form, presetId: null };
  const p = preset.params;
  return {
    presetId: preset.id,
    ...durationFromPreset(p.max_duration),
    startText: p.trim_start > 0 ? toDecimalInput(p.trim_start) : "",
    speed: snapBatchSpeed(p.speed),
    sharpen: Boolean(p.sharpen),
    mirror: Boolean(p.mirror),
  };
}

/** Parte `edit` do pedido. Só chame com o formulário válido. */
export function toBatchEdit(form: BatchEditForm): CapCutBatchEditSettings {
  const out: CapCutBatchEditSettings = {
    preset_id: form.presetId,
    duration_mode: form.durationMode,
    speed: form.speed,
    sharpen: form.sharpen,
    mirror: form.mirror,
    start_seconds: parseStart(form.startText) ?? 0,
  };
  if (form.durationMode === "custom") {
    const value = customSeconds(form);
    if (value !== null) out.custom_seconds = value;
  }
  return out;
}

/** "Preset X · 30 s · início 5 s · 1,20x · nitidez" (mesmo formato do resumo do lote). */
export function editSummaryText(form: BatchEditForm, presetName: string | null): string {
  const parts: string[] = [];
  if (presetName) parts.push(`Preset ${presetName}`);
  if (form.durationMode === "custom") {
    const value = customSeconds(form);
    parts.push(value === null ? "duração personalizada" : `${toDecimalInput(value)} s`);
  } else if (form.durationMode !== "original") {
    parts.push(`${form.durationMode} s`);
  }
  const start = parseStart(form.startText);
  if (start) parts.push(`início ${toDecimalInput(start)} s`);
  if (form.speed !== 1) parts.push(formatSpeed(form.speed));
  if (form.sharpen) parts.push("nitidez");
  if (form.mirror) parts.push("espelhado");
  return parts.length > 0 ? parts.join(" · ") : "Sem alterações";
}

/* ---------------- Exportação ---------------- */

export type BatchExportForm = Required<CapCutOptionsPatch>;

/** Pré-preenchida com as configurações salvas. */
export function exportFormFrom(settings: CapCutSettings): BatchExportForm {
  return {
    resolution: settings.resolution,
    fps: settings.fps,
    bitrate: settings.bitrate,
    keep_original_audio: settings.keep_original_audio,
    review_mode: settings.review_mode,
  };
}

/** "1080p · 60 fps · qualidade Recomendada · com áudio original". */
export function exportSummaryText(form: Partial<BatchExportForm>): string {
  const parts: string[] = [];
  const res = CAPCUT_RESOLUTIONS.find((o) => o.value === form.resolution)?.label;
  if (res) parts.push(form.resolution === "original" ? "resolução original" : res);
  if (form.fps) parts.push(form.fps === "original" ? "fps original" : `${form.fps} fps`);
  const bitrate = CAPCUT_BITRATES.find((o) => o.value === form.bitrate)?.label;
  if (bitrate) parts.push(`qualidade ${bitrate}`);
  if (form.keep_original_audio !== undefined && form.keep_original_audio !== null) {
    parts.push(form.keep_original_audio ? "com áudio original" : "sem áudio original");
  }
  return parts.join(" · ");
}

/* ---------------- Pedido ---------------- */

export interface BatchPlan {
  mode: CapCutBatchMode;
  selection: BatchSelection;
  name: string;
  includeDone: boolean;
  /** null = modelo padrão. */
  styleId: number | null;
  /** null enquanto as configurações salvas não chegaram (sem `overrides`: valem as salvas). */
  exportForm: BatchExportForm | null;
  edit: BatchEditForm;
}

/** Corpo de POST /api/capcut/batches: legenda só nos modos com legenda, edição só nos modos com edição. */
export function buildBatchRequest(plan: BatchPlan): CapCutBatchCreate {
  const body: CapCutBatchCreate = {
    mode: plan.mode,
    ...selectionTarget(plan.selection),
    include_done: plan.includeDone,
  };
  const name = plan.name.trim();
  if (name) body.name = name;
  if (modeHasCaption(plan.mode)) {
    body.caption = { style_id: plan.styleId, ...(plan.exportForm ? { overrides: { ...plan.exportForm } } : {}) };
  }
  if (modeHasEdit(plan.mode)) body.edit = toBatchEdit(plan.edit);
  return body;
}

/** Configuração do lote sem a seleção: o campo `capcut` dos pedidos de importação ("Edição CapCut" após baixar). */
export type BatchConfigPlan = Omit<BatchPlan, "selection" | "includeDone">;

/** Campo `capcut` da importação: o mesmo corpo de `buildBatchRequest`, sem a seleção (e sem `include_done`). */
export function buildImportCapcut(plan: BatchConfigPlan): CapCutImportSettings {
  const body = buildBatchRequest({ ...plan, selection: EMPTY_SELECTION, includeDone: false });
  const out: CapCutImportSettings = { mode: body.mode };
  if (body.name) out.name = body.name;
  if (body.caption) out.caption = body.caption;
  if (body.edit) out.edit = body.edit;
  return out;
}

/** Problema da configuração da importação (a validação da edição de `planBlocker`, sem a seleção), ou null. */
export function configBlocker(plan: BatchConfigPlan): string | null {
  if (modeHasEdit(plan.mode)) {
    const problem = validateBatchEdit(plan.edit);
    if (problem) return problem;
  }
  return modeHasCaption(plan.mode) && !plan.exportForm ? "Carregando as configurações de exportação…" : null;
}

/** Origem do lote criado por uma importação: "Da importação (lote 12)", "Da importação (grupo 3 e lote 14)". */
export function batchSourceText(batch: Pick<CapCutBatch, "source">): string | null {
  const source = batch.source;
  if (!source || source.kind !== "import") return null;
  const parts: string[] = [];
  if (source.group_id !== null) parts.push(`grupo ${source.group_id}`);
  if (source.batch_ids.length) parts.push(`${source.batch_ids.length === 1 ? "lote" : "lotes"} ${source.batch_ids.join(", ")}`);
  return parts.length ? `Da importação (${parts.join(" e ")})` : "Da importação";
}

/** Primeiro problema do pedido (seleção e edição), ou null. */
export function planBlocker(plan: BatchPlan): string | null {
  return selectionBlocker(plan.selection) ?? (modeHasEdit(plan.mode) ? validateBatchEdit(plan.edit) : null);
}

/** Linhas do resumo mostrado antes de processar. */
export function planLines(plan: BatchPlan, styleName: string, presetName: string | null): string[] {
  const count = selectionCount(plan.selection);
  const lines = [`${videoCount(count)} · ${BATCH_MODE_LABEL[plan.mode]}`];
  if (modeHasEdit(plan.mode)) lines.push(`Edição: ${editSummaryText(plan.edit, presetName)}`);
  if (modeHasCaption(plan.mode)) {
    lines.push(`Legendas: modelo ${styleName}`);
    if (plan.exportForm) lines.push(`Exportação: ${exportSummaryText(plan.exportForm)}`);
  }
  lines.push(
    plan.includeDone ? "Vídeos já processados neste modo são feitos de novo." : "Vídeos já processados neste modo são pulados.",
  );
  return lines;
}

/* ---------------- Estimativa e textos ---------------- */

export function videoCount(n: number): string {
  return `${formatCount(n)} ${n === 1 ? "vídeo" : "vídeos"}`;
}

/** Barra de ação: "1 vídeo selecionado", "3 vídeos selecionados". */
export function selectedText(n: number): string {
  return `${videoCount(n)} ${n === 1 ? "selecionado" : "selecionados"}`;
}

/** Botão principal: "Processar 3 vídeos"; sem seleção, "Processar vídeos". */
export function processButtonText(n: number): string {
  return n > 0 ? `Processar ${videoCount(n)}` : "Processar vídeos";
}

/** Tempo aproximado: "menos de 1 min", "~12 min", "~1 h 05 min"; "—" sem estimativa. */
export function formatEta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return "menos de 1 min";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `~${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `~${h} h` : `~${h} h ${String(m).padStart(2, "0")} min`;
}

/** Sem amostras do modo pedido (edição + legendas ainda não tem histórico). */
export function estimateMissing(estimate: CapCutEstimate | undefined): boolean {
  return estimate !== undefined && (estimate.samples === 0 || estimate.total_seconds === null);
}

/**
 * Edição + legendas sem histórico próprio usa a estimativa de "Só legendas" como aproximação (a edição roda em
 * paralelo no worker; a legenda costuma dominar o tempo). Os outros modos não têm aproximação.
 */
export function needsCaptionFallback(mode: CapCutBatchMode, estimate: CapCutEstimate | undefined): boolean {
  return mode === "edit_caption" && estimateMissing(estimate);
}

/** Estimativa a mostrar: a do modo; sem ela, a aproximada (de "Só legendas"), marcada como aproximada. */
export function resolveEstimate(
  primary: CapCutEstimate | undefined,
  fallback: CapCutEstimate | undefined,
): { estimate: CapCutEstimate | undefined; approximate: boolean } {
  if (!estimateMissing(primary) || !fallback) return { estimate: primary, approximate: false };
  if (estimateMissing(fallback)) return { estimate: primary, approximate: false };
  return { estimate: fallback, approximate: true };
}

/** Tempo restante do lote; sem o do servidor, a aproximação (de "Só legendas") marcada como aproximada. */
export function batchEtaText(etaSeconds: number | null, approx: CapCutEstimate | undefined): string {
  if (etaSeconds !== null) return formatEta(etaSeconds);
  if (approx && !estimateMissing(approx)) return `${formatEta(approx.total_seconds)} (aproximado)`;
  return formatEta(null);
}

/** Texto da barra de seleção: "estimativa ~12 min", "estimativa aproximada ~12 min" ou "sem histórico para estimar". */
export function estimateText(estimate: CapCutEstimate | undefined, approximate = false): string {
  if (!estimate) return "calculando estimativa…";
  if (estimate.total_seconds === null) return "sem histórico para estimar";
  return `estimativa ${approximate ? "aproximada " : ""}${formatEta(estimate.total_seconds)}`;
}

const SKIP_REASON: Record<CapCutBatchCreated["skipped"][number]["reason"], string> = {
  media_not_found: "não encontrados",
  not_video: "não são vídeo",
  cover: "são capa",
  already_queued: "já estavam na fila",
  already_done: "já processados neste modo",
};

/** "3 vídeos pulados: 2 já processados neste modo, 1 já estavam na fila."; null sem pulados. */
export function skippedSummary(skipped: CapCutBatchCreated["skipped"]): string | null {
  if (skipped.length === 0) return null;
  const counts = new Map<string, number>();
  for (const s of skipped) counts.set(s.reason, (counts.get(s.reason) ?? 0) + 1);
  const parts = Array.from(counts, ([reason, n]) => `${formatCount(n)} ${SKIP_REASON[reason as keyof typeof SKIP_REASON] ?? reason}`);
  return `${formatCount(skipped.length)} ${skipped.length === 1 ? "vídeo pulado" : "vídeos pulados"}: ${parts.join(", ")}.`;
}

/** Resumo das configurações usadas no lote (histórico). */
export function batchSummaryText(batch: Pick<CapCutBatch, "mode"> & { summary: CapCutBatchSummary | null }): string {
  const s = batch.summary;
  if (!s) return BATCH_MODE_LABEL[batch.mode];
  const parts: string[] = [];
  if (modeHasEdit(batch.mode)) parts.push(s.edit || "edição");
  if (modeHasCaption(batch.mode)) {
    parts.push(`modelo ${s.style_name ?? "padrão"}`);
    const exp = exportSummaryText({
      resolution: s.resolution ?? undefined,
      fps: s.fps ?? undefined,
      bitrate: s.bitrate ?? undefined,
      keep_original_audio: s.keep_audio ?? undefined,
    });
    if (exp) parts.push(exp);
  }
  return parts.join(" · ");
}

/* ---------------- Status derivado do lote ---------------- */

/** Jobs ainda abertos (na fila, em andamento, aguardando ou pausados). */
export function openCount(counts: CapCutBatchCounts): number {
  return Math.max(0, counts.total - counts.completed - counts.failed - counts.cancelled);
}

/** "3 concluídos · 5 na fila · 1 em andamento · 2 com erro · 1 pausado" (só os que existem). */
export function batchCountsText(counts: CapCutBatchCounts): string {
  const parts: string[] = [`${formatCount(counts.completed)} de ${formatCount(counts.total)} concluídos`];
  const queued = counts.pending + counts.waiting;
  if (queued > 0) parts.push(`${formatCount(queued)} na fila`);
  if (counts.running > 0) parts.push(`${formatCount(counts.running)} em andamento`);
  if (counts.failed > 0) parts.push(`${formatCount(counts.failed)} com erro`);
  const review = counts.review ?? 0;
  if (review > 0) parts.push(`${formatCount(review)} aguardando revisão`);
  if (counts.held > 0) parts.push(`${formatCount(counts.held)} ${counts.held === 1 ? "pausado" : "pausados"}`);
  if (counts.cancelled > 0) parts.push(`${formatCount(counts.cancelled)} ${counts.cancelled === 1 ? "cancelado" : "cancelados"}`);
  return parts.join(" · ");
}

/** Fração (0..1) da barra geral; concluído = 1. */
export function batchProgress(batch: Pick<CapCutBatch, "status" | "progress">): number {
  if (batch.status === "completed") return 1;
  return Math.min(100, Math.max(0, batch.progress || 0)) / 100;
}

/** Lote que ainda anda (polling rápido). Pausado não conta. */
export function isBatchRunning(batch: Pick<CapCutBatch, "status">): boolean {
  return batch.status === "running";
}

/** Texto do aviso depois de remover: "2 lotes removidos do histórico (1 em andamento ficou)". */
export function hiddenText(hidden: number, active: number): string {
  const main =
    hidden === 0 ? "Nenhum lote removido" : `${formatCount(hidden)} ${hidden === 1 ? "lote removido" : "lotes removidos"} do histórico`;
  if (active === 0) return `${main}.`;
  return `${main} (${formatCount(active)} em andamento ${active === 1 ? "ficou" : "ficaram"}).`;
}

/** Botão do ZIP: "Baixar todos (ZIP)" com o lote terminado; enquanto processa, só os concluídos até agora. */
export function zipLabel(batch: Pick<CapCutBatch, "counts">): string {
  const n = formatCount(batch.counts.completed);
  return isBatchOpen(batch) ? `Baixar ${n} concluídos (ZIP)` : `Baixar todos (ZIP)`;
}

/** Selo da importação com "Edição CapCut": "Edição CapCut: 3 de 10 prontos · editando" (abre o lote). */
export function capcutLinkText(link: { status: string; total: number; completed: number; running: number; failed: number; review: number }): string {
  if (link.status === "importing" && link.total === 0) return "Edição CapCut: aguardando downloads";
  const parts = [`Edição CapCut: ${formatCount(link.completed)} de ${formatCount(link.total)} prontos`];
  if (link.running > 0) parts.push("editando agora");
  if (link.review > 0) parts.push(`${formatCount(link.review)} para revisar`);
  if (link.failed > 0) parts.push(`${formatCount(link.failed)} com erro`);
  return parts.join(" · ");
}

/** Lote com job aberto (em andamento ou pausado): não sai do histórico. */
export function isBatchOpen(
  batch: Pick<CapCutBatch, "counts"> & { status?: CapCutBatch["status"]; cancel_requested?: boolean },
): boolean {
  // Lote da importação aguardando downloads também conta (removê-lo faria os próximos vídeos se perderem).
  return openCount(batch.counts) > 0 || (batch.status === "importing" && !batch.cancel_requested);
}

/** 2 s com algum lote em andamento; ocioso, os degraus de lib/polling. */
export function batchPollMs(batches: Pick<CapCutBatch, "status">[] | undefined, current: number | null): number {
  return nextPollMs(current, (batches ?? []).some(isBatchRunning));
}

export interface BatchActions {
  pause: boolean;
  resume: boolean;
  cancel: boolean;
  retryFailed: boolean;
  zip: boolean;
  reveal: boolean;
  remove: boolean;
}

/** Ações disponíveis para o lote no estado atual. */
export function batchActions(
  batch: Pick<CapCutBatch, "status" | "paused" | "cancel_requested" | "counts" | "output_exists">,
): BatchActions {
  const open = isBatchOpen(batch);
  // Em cancelamento com um job do lote rodando: espera o executor parar. Sem job rodando, o cancelamento pode ter
  // ficado pela metade: "Cancelar lote" de novo e "Reprocessar falhas" continuam disponíveis.
  const cancelling = batch.cancel_requested && open && batch.counts.running > 0;
  return {
    pause: open && !batch.paused && !batch.cancel_requested,
    resume: open && batch.paused && !batch.cancel_requested,
    // Lote de importação aguardando downloads (sem vídeo ainda): cancelar fecha a entrada de vídeos.
    cancel: (open && !cancelling) || (batch.status === "importing" && !batch.cancel_requested),
    retryFailed: batch.counts.failed + batch.counts.cancelled > 0 && !cancelling,
    zip: batch.counts.completed > 0,
    reveal: batch.output_exists,
    remove: !open,
  };
}

/* ---------------- Jobs do lote ---------------- */

/** Etapa do job no lote: "Pausado", "Editando (40%)", a etapa do CapCut ou o status. */
export function batchJobStep(
  job: Pick<CapCutJob, "status" | "stage" | "held" | "edit_status" | "edit_progress" | "mode" | "needs_review">,
): string {
  if (needsReview(job)) return REVIEW_STATUS_LABEL;
  if (isHeldJob(job)) return "Pausado";
  if (job.status === "pending" && job.edit_status && !TERMINAL_JOB.has(job.edit_status as JobStatus)) {
    const pct = job.edit_progress;
    return pct === null || pct === undefined ? "Editando" : `Editando (${Math.round(pct)}%)`;
  }
  if (job.status === "running") {
    if (job.mode === "edit" && !job.stage) return "Copiando o vídeo editado";
    return stageLabel(job.stage);
  }
  return CAPCUT_STATUS_LABEL[job.status];
}

export interface BatchJobActions {
  hold: boolean;
  release: boolean;
  cancel: boolean;
  retry: boolean;
  download: boolean;
  /** Abrir a revisão das legendas. */
  review: boolean;
}

export function batchJobActions(
  job: Pick<CapCutJob, "status" | "held" | "cancel_requested" | "output_exists" | "needs_review">,
): BatchJobActions {
  const review = needsReview(job) && !job.cancel_requested;
  const waiting = (job.status === "pending" || job.status === "waiting") && !needsReview(job);
  return {
    review,
    hold: waiting && !job.held,
    release: isHeldJob(job),
    cancel: isCapcutActive(job) && !job.cancel_requested,
    retry: job.status === "failed" || job.status === "cancelled",
    download: job.status === "completed" && job.output_exists,
  };
}
