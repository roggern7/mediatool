import type { CapCutImportSettings, ContentType, EditSettings, ImportGroupRequest, ImportRequest } from "../api/types";
import { type EditForm, NO_CHANGES_TEXT, SPEED_MIN, toEditRequest, validateEditForm } from "./editSettings";
import { formatCount } from "./format";

/** Padrão da importação: somente baixar os originais. Não é salvo entre sessões. */
export const PROCESS_AFTER_DOWNLOAD_DEFAULT = false;

/** Pós-processamento da importação: uma escolha só por lote (preset, edição OU Edição CapCut). */
export type PostProcessing = "none" | "preset" | "edit" | "capcut";
export const POST_PROCESSING_DEFAULT: PostProcessing = "none";
export const POST_PROCESSING_OPTIONS: { value: PostProcessing; label: string }[] = [
  { value: "none", label: "Só baixar" },
  { value: "preset", label: "Processar com preset" },
  { value: "edit", label: "Editar após baixar" },
  { value: "capcut", label: "Edição CapCut" },
];

/** Opções do pedido para a escolha da tela: preset só em "preset"; `edit` só em "edit" (configurações da Editar). */
export function postProcessingOptions(
  choice: PostProcessing,
  presetId: number | null,
  form: EditForm,
): { processAfterDownload: boolean; presetId: number | null; edit: EditSettings | null } {
  return {
    processAfterDownload: choice === "preset",
    presetId: choice === "preset" ? presetId : null,
    edit: choice === "edit" ? toEditRequest([], form).settings : null,
  };
}

/** Problema do formulário de edição (mesmas validações da página Editar); null fora de "edit" ou se válido. */
export function editProblemOf(choice: PostProcessing, form: EditForm): string | null {
  if (choice !== "edit") return null;
  // Na importação, sem corte, velocidade ou nitidez só sairia uma cópia recodificada do vídeo baixado (já MP4).
  if (form.durationMode === "original" && form.speed === SPEED_MIN && !form.sharpen) return NO_CHANGES_TEXT;
  return validateEditForm(form);
}

/** Tipos de mídia oferecidos na tela (os antigos "posts", "videos" e "images" só aparecem em lotes antigos). */
export const CONTENT_CHOICES: { value: ContentType; label: string }[] = [
  { value: "all", label: "Todos" },
  { value: "reels", label: "Reels" },
  { value: "photos_carousels", label: "Publicações do feed" },
];

export type LimitMode = "last" | "all";

/** Teto de sanidade da API para "Últimos N". */
export const MAX_IMPORT_LIMIT = 1_000_000;
export const DEFAULT_LIMIT_TEXT = "10";
/** Atalhos do campo "Últimos N". */
export const LIMIT_SHORTCUTS = [100, 500, 1000] as const;

/** Texto digitado em "Últimos N" (aceita "1000" e "1.000"); null se não for inteiro de 1 a 1.000.000. */
export function parseLimitText(text: string): number | null {
  const t = text.trim();
  if (!/^\d+$/.test(t) && !/^\d{1,3}(\.\d{3})+$/.test(t)) return null;
  const n = Number(t.replace(/\./g, ""));
  return Number.isSafeInteger(n) && n >= 1 && n <= MAX_IMPORT_LIMIT ? n : null;
}

export type LimitChoice = { ok: true; limit: number | null } | { ok: false; error: string };

/** Quantidade enviada à API: "Todos" é `null` (até o fim real do perfil); "Últimos N" é o inteiro digitado. */
export function resolveLimit(mode: LimitMode, text: string): LimitChoice {
  if (mode === "all") return { ok: true, limit: null };
  const n = parseLimitText(text);
  if (n === null) return { ok: false, error: "Informe um número inteiro de 1 a 1.000.000." };
  return { ok: true, limit: n };
}

interface ImportOptionsInput {
  content: ContentType;
  /** `null` = "Todos". */
  limit: number | null;
  processAfterDownload?: boolean;
  presetId?: number | null;
  /** "Editar após baixar": manda `edit` com `process: false` e `preset_id: null` (exclusivo com o preset). */
  edit?: EditSettings | null;
  /** "Edição CapCut": manda `capcut` com `process: false` e `preset_id: null` (exclusivo com o preset e `edit`). */
  capcut?: CapCutImportSettings | null;
  /** Visualizações mínimas (só vídeos); null/ausente = sem filtro. */
  minViews?: number | null;
  /** Período de postagem: últimos N dias; null/ausente = qualquer data. */
  postedWithinDays?: number | null;
}

/** Filtros da importação por perfil: sem filtro a chave nem aparece (o corpo fica idêntico ao de antes). */
function withFilters<T extends object>(body: T, input: ImportOptionsInput) {
  return {
    ...body,
    ...(typeof input.minViews === "number" ? { min_views: input.minViews } : {}),
    ...(typeof input.postedWithinDays === "number" ? { posted_within_days: input.postedWithinDays } : {}),
  };
}

function commonBody(input: ImportOptionsInput) {
  if (input.capcut) {
    return withFilters(
      { content_type: input.content, limit: input.limit, preset_id: null, process: false, capcut: input.capcut },
      input,
    );
  }
  if (input.edit) {
    return withFilters(
      { content_type: input.content, limit: input.limit, preset_id: null, process: false, edit: input.edit },
      input,
    );
  }
  const process = input.processAfterDownload ?? PROCESS_AFTER_DOWNLOAD_DEFAULT;
  const body = {
    content_type: input.content,
    limit: input.limit,
    preset_id: process ? (input.presetId ?? null) : null,
    process,
  };
  return withFilters(body, input);
}

/**
 * Corpo de POST /api/imports. Sempre manda `process` e `preset_id` explícitos:
 * download puro é `process: false, preset_id: null`, mesmo com um preset escolhido na tela.
 */
export function buildImportRequest(input: ImportOptionsInput & { profile: string }): ImportRequest {
  return { profile: input.profile, ...commonBody(input) };
}

/** Corpo de POST /api/import-groups (`limit` vale para cada perfil). */
export function buildImportGroupRequest(input: ImportOptionsInput & { profiles: string[] }): ImportGroupRequest {
  return { profiles: [...input.profiles], ...commonBody(input) };
}

export type ImportPlan =
  | { kind: "single"; body: ImportRequest }
  | { kind: "group"; body: ImportGroupRequest };

/** 1 perfil = lote avulso (POST /api/imports, como sempre foi); 2 ou mais = grupo (POST /api/import-groups). */
export function planImport(input: ImportOptionsInput & { profiles: string[] }): ImportPlan | null {
  const [first] = input.profiles;
  if (first === undefined) return null;
  if (input.profiles.length === 1) return { kind: "single", body: buildImportRequest({ ...input, profile: first }) };
  return { kind: "group", body: buildImportGroupRequest(input) };
}

/** Só pode enviar com processamento ligado se houver preset; download puro sempre pode. */
export function canSubmitImport(processAfterDownload: boolean, presetId: number | null): boolean {
  return !processAfterDownload || presetId !== null;
}

export function profilesCountText(n: number): string {
  return `${formatCount(n)} ${n === 1 ? "perfil" : "perfis"}`;
}

function contentSummary(content: ContentType, all: boolean): string {
  switch (content) {
    case "reels":
      return all ? "Todos os Reels" : "Reels";
    case "photos_carousels":
      return all ? "Todas as publicações do feed" : "Publicações do feed";
    case "videos":
      return all ? "Todos os vídeos" : "Vídeos";
    case "images":
      return all ? "Todas as imagens" : "Imagens";
    default:
      return "Todos os tipos";
  }
}

/**
 * Resumo em uma linha do que será importado:
 * "37 perfis • Todos os Reels • Todas as mídias disponíveis" ou
 * "37 perfis • Publicações do feed • Até 1.000 mídias por perfil" (com 1 perfil, sem "por perfil").
 */
export function importSummary(input: { profiles: number; content: ContentType; limit: number | null }): string {
  const { profiles, content, limit } = input;
  const amount =
    limit === null
      ? "Todas as mídias disponíveis"
      : `Até ${formatCount(limit)} ${limit === 1 ? "mídia" : "mídias"}${profiles === 1 ? "" : " por perfil"}`;
  return [profilesCountText(profiles), contentSummary(content, limit === null), amount].join(" • ");
}

function filteredNoun(content: ContentType, one: boolean): string | null {
  if (content === "reels") return one ? "Reel" : "Reels";
  if (content === "photos_carousels") return one ? "mídia de publicação do feed" : "mídias de publicações do feed";
  return null;
}

/** Avisos curtos de apoio (texto, não modal), conforme as opções escolhidas. */
export function importHints(input: { profiles: number; content: ContentType; limit: number | null }): string[] {
  const { profiles, content, limit } = input;
  const hints: string[] = [];
  if (limit === null) {
    hints.push(
      "Todos: percorre o perfil até o fim. Em perfis grandes pode levar horas por causa do limite de requisições " +
        "por hora; o lote continua sozinho.",
    );
  } else {
    const noun = filteredNoun(content, limit === 1);
    if (noun) hints.push(`O app continua procurando até achar ${formatCount(limit)} ${noun} ou chegar ao fim do perfil.`);
  }
  if (profiles > 1) {
    hints.push(
      limit === null
        ? "Os perfis são listados um de cada vez."
        : "A quantidade vale para cada perfil; os perfis são listados um de cada vez.",
    );
  }
  return hints;
}

/** "37 perfis válidos • 2 duplicatas removidas • 1 entrada inválida" (só as partes que existem além dos válidos). */
export function entriesSummary(input: { valid: number; duplicates: number; invalid: number }): string {
  const { valid, duplicates, invalid } = input;
  const parts = [`${formatCount(valid)} ${valid === 1 ? "perfil válido" : "perfis válidos"}`];
  if (duplicates > 0) {
    parts.push(`${formatCount(duplicates)} ${duplicates === 1 ? "duplicata removida" : "duplicatas removidas"}`);
  }
  if (invalid > 0) parts.push(invalidCountText(invalid));
  return parts.join(" • ");
}

export function invalidCountText(n: number): string {
  return `${formatCount(n)} ${n === 1 ? "entrada inválida" : "entradas inválidas"}`;
}

/** Rótulo do botão de início; com entradas inválidas, o próprio botão diz que elas ficam de fora. */
export function startLabel(input: { process: boolean; invalid: number; edit?: boolean }): string {
  const base = input.edit ? "Baixar e editar" : input.process ? "Baixar e processar" : "Baixar originais";
  if (input.invalid <= 0) return base;
  return `${base} (ignorar ${formatCount(input.invalid)} ${input.invalid === 1 ? "inválida" : "inválidas"})`;
}
