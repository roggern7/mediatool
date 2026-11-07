// Edição CapCut: "Limpar lista" do seletor (a visão fica no servidor; nada é apagado) e, opcionalmente, a exclusão dos
// arquivos dos vídeos que saíram da lista (POST /api/media/delete em blocos). Testado em capcutLibraryView.test.ts.

import type {
  CapCutBatchFilter,
  CapCutLibraryItem,
  CapCutLibraryQuery,
  CapCutLibraryView,
  MediaDeleteRequest,
  MediaDeleteResult,
  Paged,
} from "../api/types";
import type { BatchSelection } from "./capcutBatch";
import { formatCount } from "./format";

/** Teto de ids de POST /api/media/delete (schema: max_length=500). */
export const DELETE_CHUNK = 500;
/** Teto de `limit` de GET /api/capcut/library. */
export const COLLECT_PAGE = 200;
/** Teto de ids de POST /api/capcut/library/clear e /show. */
export const MAX_VIEW_IDS = 20_000;

export const CLEAR_CONFIRM_TEXT =
  "Os vídeos atuais saem desta lista. Nada é apagado; novos downloads e envios continuam aparecendo.";
export const DELETE_FILES_LABEL = "Também apagar os arquivos da biblioteca (não dá para desfazer)";

/** Chamadas da API usadas aqui (injetáveis nos testes). */
export interface LibraryViewApi {
  capcutLibrary: (q: CapCutLibraryQuery) => Promise<Paged<CapCutLibraryItem>>;
  clearCapcutLibrary: (mediaIds?: number[]) => Promise<CapCutLibraryView>;
  deleteMediaBulk: (body: MediaDeleteRequest) => Promise<MediaDeleteResult>;
}

/** O que sai da lista: tudo ("Limpar lista") ou a seleção ("Tirar selecionados da lista"). */
export type ClearTarget = { kind: "all" } | { kind: "selection"; selection: BatchSelection };

export interface ClearOutcome {
  /** Vídeos que saíram da lista; null em "Limpar lista" sem exclusão (o servidor não conta). */
  removed: number | null;
  deleteRequested: boolean;
  deleted: number;
  /** Com job do CapCut aberto: não foram apagados (nem enviados para exclusão). */
  skippedOpen: number;
  /** Pulados pelo servidor (job em andamento, exportação aberta, já não existiam). */
  skipped: number;
  failed: number;
  /** Erro no meio da exclusão (a lista já foi limpa). */
  deleteError: string | null;
}

/** A visão tem algo escondido (mostra "Mostrar todos de novo"). */
export function viewHasHidden(view: CapCutLibraryView | undefined): boolean {
  return Boolean(view && (view.cleared || view.hidden_count > 0));
}

/** Divide em blocos de até `size` (ordem mantida). */
export function chunk<T>(items: readonly T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

/** Todos os vídeos visíveis no filtro (pagina por keyset até o fim): id e se há job do CapCut aberto. */
export async function collectVisible(
  deps: Pick<LibraryViewApi, "capcutLibrary">,
  filter: CapCutBatchFilter,
): Promise<{ id: number; open: boolean }[]> {
  const out: { id: number; open: boolean }[] = [];
  let before: number | undefined;
  for (;;) {
    const page = await deps.capcutLibrary({ ...filter, limit: COLLECT_PAGE, before_id: before });
    for (const item of page.items) out.push({ id: item.id, open: Boolean(item.capcut?.open) });
    const next = page.next_before_id ?? null;
    if (next === null || page.items.length === 0) return out;
    before = next;
  }
}

/** Ids que saem da lista na seleção, ou um erro de limite. "Todos os filtrados" é resolvido antes (lista visível). */
export function selectionIds(
  selection: BatchSelection,
  visible: readonly { id: number }[] | null,
): number[] {
  if (selection.all) return (visible ?? []).map((v) => v.id);
  return Array.from(selection.ids).sort((a, b) => a - b);
}

/**
 * Tira os vídeos da lista e, com `deleteFiles`, apaga os arquivos deles em blocos de 500. Vídeos com job do CapCut
 * aberto ficam de fora da exclusão. Em "Limpar lista", só os vídeos que estavam visíveis são apagados (a lista é
 * lida antes de limpar). Falha antes de limpar: a exceção sobe e nada mudou.
 */
export async function clearLibrary(
  deps: LibraryViewApi,
  target: ClearTarget,
  deleteFiles: boolean,
): Promise<ClearOutcome> {
  const outcome: ClearOutcome = {
    removed: null,
    deleteRequested: deleteFiles,
    deleted: 0,
    skippedOpen: 0,
    skipped: 0,
    failed: 0,
    deleteError: null,
  };
  let affected: { id: number; open: boolean }[] = [];
  if (target.kind === "all") {
    if (deleteFiles) {
      affected = await collectVisible(deps, {});
      outcome.removed = affected.length;
    }
    await deps.clearCapcutLibrary();
  } else {
    const sel = target.selection;
    // "Todos os filtrados" vira a lista de ids do filtro; com exclusão, a lista visível diz quem tem job aberto.
    const visible = sel.all ? await collectVisible(deps, sel.all.filter) : deleteFiles ? await collectVisible(deps, {}) : null;
    const ids = selectionIds(sel, visible);
    if (ids.length === 0) throw new Error("Nenhum vídeo para tirar da lista.");
    if (ids.length > MAX_VIEW_IDS) {
      throw new Error(`Tire no máximo ${formatCount(MAX_VIEW_IDS)} vídeos por vez, ou use "Limpar lista".`);
    }
    const open = new Set((visible ?? []).filter((v) => v.open).map((v) => v.id));
    affected = ids.map((id) => ({ id, open: open.has(id) }));
    outcome.removed = ids.length;
    await deps.clearCapcutLibrary(ids);
  }
  if (!deleteFiles) return outcome;

  const deletable = affected.filter((v) => !v.open).map((v) => v.id);
  outcome.skippedOpen = affected.length - deletable.length;
  try {
    for (const part of chunk(deletable, DELETE_CHUNK)) {
      const result = await deps.deleteMediaBulk({ media_ids: part, delete_original: true });
      outcome.deleted += result.deleted;
      outcome.skipped += result.skipped.length;
      outcome.failed += result.failed.length;
    }
  } catch (err) {
    outcome.deleteError = err instanceof Error ? err.message : String(err);
  }
  return outcome;
}

function videos(n: number): string {
  return `${formatCount(n)} ${n === 1 ? "vídeo" : "vídeos"}`;
}

/** Resumo do resultado para o aviso da página. */
export function clearSummary(outcome: ClearOutcome): string {
  const parts: string[] = [];
  if (outcome.removed === null) parts.push("A lista foi limpa.");
  else if (outcome.removed === 0) parts.push("A lista já estava vazia.");
  else parts.push(`${videos(outcome.removed)} ${outcome.removed === 1 ? "saiu" : "saíram"} da lista.`);
  if (!outcome.deleteRequested) return parts.join(" ");
  parts.push(
    outcome.deleted === 1 ? "1 arquivo apagado da biblioteca." : `${formatCount(outcome.deleted)} arquivos apagados da biblioteca.`,
  );
  if (outcome.skippedOpen > 0) {
    parts.push(
      outcome.skippedOpen === 1
        ? "1 vídeo com job do CapCut aberto não foi apagado."
        : `${formatCount(outcome.skippedOpen)} vídeos com job do CapCut aberto não foram apagados.`,
    );
  }
  if (outcome.skipped > 0) {
    parts.push(
      outcome.skipped === 1
        ? "1 vídeo foi pulado (em uso, numa exportação ou já apagado)."
        : `${formatCount(outcome.skipped)} vídeos foram pulados (em uso, numa exportação ou já apagados).`,
    );
  }
  if (outcome.failed > 0) {
    parts.push(outcome.failed === 1 ? "1 vídeo não pôde ser apagado." : `${formatCount(outcome.failed)} vídeos não puderam ser apagados.`);
  }
  if (outcome.deleteError) parts.push(`A exclusão parou no meio: ${outcome.deleteError}`);
  return parts.join(" ");
}

/** Tom do aviso: erro se algo falhou, alerta se algo ficou sem apagar. */
export function clearTone(outcome: ClearOutcome): "ok" | "warn" | "err" {
  if (outcome.deleteError || outcome.failed > 0) return "err";
  if (outcome.skippedOpen > 0 || outcome.skipped > 0) return "warn";
  return "ok";
}
