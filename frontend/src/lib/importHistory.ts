import type { HistoryHideRequest, HistoryHideResult } from "../api/types";
import { formatBytes, formatCount } from "./format";

// Histórico de "Importações recentes": remover um item ou limpar os terminados (POST /api/imports/history/hide).
// Remover só oculta da lista; os vídeos continuam na Biblioteca e nas pastas da página Editar, salvo a opção marcada.

/** Um item (lote avulso ou grupo) ou "Limpar histórico" (todos os terminados). */
export type HistoryTarget = { kind: "batch" | "group"; id: number; label: string } | { kind: "all" };

export const IN_PROGRESS_HINT = "Cancele ou aguarde terminar";
export const DELETE_WARNING = "Isso apaga os arquivos do computador e não pode ser desfeito.";

/** Dica do botão de remover quando a importação ainda está em andamento; null quando pode remover. */
export function removeBlockedHint(active: boolean): string | null {
  return active ? IN_PROGRESS_HINT : null;
}

/** Corpo do pedido. `deleteMedia` só vai quando marcado (padrão desmarcado: só oculta). */
export function historyHideBody(target: HistoryTarget, deleteMedia: boolean, dryRun = false): HistoryHideRequest {
  const base: HistoryHideRequest =
    target.kind === "all"
      ? { all_finished: true }
      : target.kind === "group"
        ? { group_ids: [target.id] }
        : { batch_ids: [target.id] };
  return { ...base, ...(deleteMedia ? { delete_media: true } : {}), ...(dryRun ? { dry_run: true } : {}) };
}

export function historyConfirmTitle(target: HistoryTarget): string {
  return target.kind === "all" ? "Limpar histórico" : "Remover do histórico";
}

/** Texto principal da confirmação: o que sai da lista e o que fica. */
export function historyConfirmText(
  target: HistoryTarget,
  preview: HistoryHideResult | null,
  deleteMedia = false,
): string {
  const after = deleteMedia
    ? "Os vídeos também serão apagados da Biblioteca e das pastas da página Editar."
    : "Os vídeos continuam na Biblioteca e nas pastas da página Editar.";
  if (target.kind !== "all") {
    return `${target.label} sai da lista de importações recentes. ${after}`;
  }
  const items = preview ? preview.batches + preview.groups : null;
  const what =
    items === null
      ? "Todas as importações terminadas saem"
      : `${formatCount(items)} ${items === 1 ? "importação terminada sai" : "importações terminadas saem"}`;
  return `${what} da lista de importações recentes. As que estão em andamento ficam. ${after}`;
}

/** Rótulo da opção de também apagar os vídeos, com a contagem e o espaço quando já conhecidos. */
export function deleteMediaLabel(target: HistoryTarget, preview: HistoryHideResult | null): string {
  const base = `Também apagar os vídeos ${target.kind === "all" ? "destas importações" : "desta importação"}`;
  if (preview === null) return base;
  const n = preview.video_count;
  return `${base} (${formatCount(n)} ${n === 1 ? "vídeo" : "vídeos"}, ${formatBytes(preview.total_bytes)})`;
}

/** Mensagem depois de remover (com o resumo da exclusão quando os vídeos também foram apagados). */
export function historyResultMessage(result: HistoryHideResult): string {
  const items = result.batches + result.groups;
  const removed =
    items === 0
      ? "Nenhuma importação terminada para remover do histórico."
      : `${formatCount(items)} ${items === 1 ? "importação removida" : "importações removidas"} do histórico.`;
  const d = result.deleted;
  if (!d) return removed;
  const parts = [`${formatCount(d.deleted)} ${d.deleted === 1 ? "vídeo apagado" : "vídeos apagados"}`];
  if (d.skipped.length > 0) {
    const reasons = Array.from(new Set(d.skipped.map((s) => s.message))).join(" ");
    parts.push(`${formatCount(d.skipped.length)} ${d.skipped.length === 1 ? "pulado" : "pulados"} (${reasons})`);
  }
  if (d.failed.length > 0) parts.push(`${formatCount(d.failed.length)} com falha`);
  return `${removed} ${parts.join(", ")}.`;
}

/** Aviso de vídeos que também estão em outras importações (só com a opção de apagar marcada); null sem nenhum. */
export function sharedWarning(preview: HistoryHideResult | null): string | null {
  const n = preview?.shared_count ?? 0;
  if (n <= 0) return null;
  return n === 1
    ? "1 desses vídeos também está em outras importações e sairá delas."
    : `${formatCount(n)} desses vídeos também estão em outras importações e sairão delas.`;
}
