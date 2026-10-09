// Início da importação: o que pode ser enviado, o pedido montado a partir do preview do backend e a leitura
// da resposta/erro da criação. Funções puras (a tela só liga os estados).

import { errorMessageFor } from "../api/errors";
import type { ContentType, InstagramProvider, ProfilesPreview } from "../api/types";
import { skippedFrom, skippedNotice } from "./importGroup";
import {
  type ImportPlan,
  type LimitChoice,
  canSubmitImport,
  importHints,
  importSummary,
  planImport,
  profilesCountText,
} from "./importOptions";

/** Resposta da pré-validação, com a lista (chave) para a qual ela foi pedida. */
export interface PreviewResult<T = ProfilesPreview> {
  key: string;
  data: T;
}

/** Chave da lista de entradas enviada ao preview (as entradas nunca contêm quebra de linha). */
export function entriesKey(entries: string[]): string {
  return entries.join("\n");
}

/**
 * Preview que pode ser exibido e usado para iniciar: só a resposta do backend para EXATAMENTE a lista atual,
 * com a digitação parada e a conferência terminada. Qualquer outra situação é "ainda conferindo" (null).
 */
export function settledPreview<T = ProfilesPreview>(input: {
  /** Chave da lista atual do campo. */
  currentKey: string;
  /** Chave pedida ao backend (atrasada pelo debounce). */
  requestedKey: string;
  /** O campo mudou e a lista atual ainda não foi recalculada. */
  typing: boolean;
  loading: boolean;
  result: PreviewResult<T> | null | undefined;
}): T | null {
  const { currentKey, requestedKey, typing, loading, result } = input;
  if (typing || loading || currentKey === "" || requestedKey !== currentKey) return null;
  return result && result.key === currentKey ? result.data : null;
}

export const WEB_ONLY_MESSAGE =
  "“Todos” e “Publicações do feed” exigem o método de importação web. Troque o método em Configurações > Instagram.";

/**
 * Com o método legado gallery-dl configurado, "Todos" e "Publicações do feed" são recusados pela API
 * (422 not_supported_by_provider): avisa antes de enviar. Com `auto`/`web` (ou sem a informação), null.
 */
export function providerBlocker(
  provider: InstagramProvider | null | undefined,
  content: ContentType,
  allSelected: boolean,
): string | null {
  if (provider !== "gallery_dl") return null;
  return allSelected || content === "photos_carousels" ? WEB_ONLY_MESSAGE : null;
}

export type StartBlocker =
  | "submitting"
  | "no_session"
  | "paused"
  | "no_preview"
  | "no_valid"
  | "limit"
  | "preset"
  | "provider";

export interface StartState {
  sessionOk: boolean;
  /** Pausa global do Instagram vigente. */
  paused: boolean;
  submitting: boolean;
  /** Resultado de `settledPreview` (null enquanto confere ou em erro). */
  preview: ProfilesPreview | null;
  limit: LimitChoice;
  process: boolean;
  presetId: number | null;
  /** Resultado de `providerBlocker`. */
  providerProblem: string | null;
}

/** Por que a importação não pode começar agora; null quando pode. */
export function startBlocker(s: StartState): StartBlocker | null {
  if (s.submitting) return "submitting";
  if (!s.sessionOk) return "no_session";
  if (s.paused) return "paused";
  if (s.preview === null) return "no_preview";
  if (s.preview.valid.length === 0) return "no_valid";
  if (!s.limit.ok) return "limit";
  if (!canSubmitImport(s.process, s.presetId)) return "preset";
  if (s.providerProblem !== null) return "provider";
  return null;
}

export function canStartImport(s: StartState): boolean {
  return startBlocker(s) === null;
}

export type StartSummary =
  | { kind: "summary"; text: string; hints: string[] }
  | { kind: "problem"; text: string };

/**
 * Linha de resumo antes de iniciar. Só existe com o preview assentado e ao menos um perfil válido; com a
 * quantidade inválida, a mensagem de validação entra no lugar do resumo (nunca "Todas as mídias" por engano).
 */
export function startSummary(input: {
  preview: ProfilesPreview | null;
  limit: LimitChoice;
  content: ContentType;
}): StartSummary | null {
  const { preview, limit, content } = input;
  if (preview === null || preview.valid.length === 0) return null;
  if (!limit.ok) return { kind: "problem", text: `Quantidade por perfil: ${limit.error}` };
  const options = { profiles: preview.valid.length, content, limit: limit.limit };
  return { kind: "summary", text: importSummary(options), hints: importHints(options) };
}

/**
 * Pedido de criação montado SÓ com `preview.valid` (a lista que o backend validou), nunca com o texto do campo:
 * 1 perfil = POST /api/imports; 2 ou mais = POST /api/import-groups. null sem perfil válido ou quantidade inválida.
 */
export function planFromPreview(
  preview: ProfilesPreview | null,
  options: { content: ContentType; limit: LimitChoice; processAfterDownload: boolean; presetId: number | null },
): ImportPlan | null {
  if (preview === null || !options.limit.ok) return null;
  return planImport({
    profiles: preview.valid,
    content: options.content,
    limit: options.limit.limit,
    processAfterDownload: options.processAfterDownload,
    presetId: options.presetId,
  });
}

/** Resposta 202 da criação do grupo: mensagem de sucesso e, se houve perfis pulados, o aviso (não é erro). */
export function interpretGroupCreated(created: {
  group: { id: number; profiles: { total: number } };
  skipped?: unknown;
}): { groupId: number; message: string; notice: string | null } {
  return {
    groupId: created.group.id,
    message: `Importação de ${profilesCountText(created.group.profiles.total)} iniciada.`,
    notice: skippedNotice(skippedFrom(created)),
  };
}

export interface StartFailure {
  /** Aviso na tela (não é erro geral): todos os perfis já tinham importação em andamento. */
  notice: string | null;
  /** Mensagem de erro (toast). */
  error: string | null;
  /** A lista mudou de situação no backend: refazer a pré-validação. */
  refreshPreview: boolean;
  /** Atualizar a lista de recentes para mostrar a importação que já existe. */
  reloadRecent: boolean;
}

/** Erro da criação (`ApiError` ou null para erro desconhecido) -> o que a tela mostra e refaz. */
export function interpretStartError(
  err: { status: number; code: string; message: string; payload?: unknown } | null,
): StartFailure {
  if (err === null) return { notice: null, error: "Erro inesperado.", refreshPreview: false, reloadRecent: false };
  const skipped = skippedNotice(skippedFrom(err.payload));
  if (skipped !== null) {
    return {
      notice: `Nenhuma importação nova foi criada. ${skipped}`,
      error: null,
      refreshPreview: false,
      reloadRecent: true,
    };
  }
  if (err.status === 413) {
    return {
      notice: null,
      error: errorMessageFor("payload_too_large", 413, null),
      refreshPreview: false,
      reloadRecent: false,
    };
  }
  return {
    notice: null,
    error: err.message,
    refreshPreview: err.code === "invalid_profiles",
    reloadRecent: err.code === "profile_already_importing",
  };
}
