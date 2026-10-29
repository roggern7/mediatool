import type { EditFolder, Media, MediaDeleteResult } from "../api/types";
import { formatBytes, formatCount, formatDateTime } from "./format";

// Pastas da página Editar (GET /api/edit/folders): rótulos, totais e textos de exclusão.

/** Quantos perfis aparecem pelo nome num grupo; o resto vira "e mais N". */
export const FOLDER_PROFILES_SHOWN = 3;
export const DELETE_IRREVERSIBLE_TEXT = "Isso apaga os arquivos do computador e não pode ser desfeito.";

export function videoCountText(n: number): string {
  return `${formatCount(n)} ${n === 1 ? "vídeo" : "vídeos"}`;
}

export function versionCountText(n: number): string {
  return `${formatCount(n)} ${n === 1 ? "versão editada" : "versões editadas"}`;
}

/** "aaaa-mm-dd" como "dd/mm/aaaa" (sem fuso: o dia já vem local do servidor). */
export function formatDay(day: string | null): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day ?? "");
  return match ? `${match[3]}/${match[2]}/${match[1]}` : "-";
}

/** Nome da pasta: perfil(s), "Importação por link", "Enviados do computador — dia" ou "Outros vídeos". */
export function folderTitle(folder: EditFolder): string {
  if (folder.kind === "local") return `Enviados do computador — ${formatDay(folder.day)}`;
  if (folder.kind === "other") return "Outros vídeos";
  if (folder.import_mode === "links") return "Importação por link";
  const names = folder.profiles.map((p) => `@${p}`);
  if (names.length === 0) return "Importação";
  if (names.length === 1) return names[0] ?? "";
  const shown = names.slice(0, FOLDER_PROFILES_SHOWN).join(", ");
  const rest = names.length - FOLDER_PROFILES_SHOWN;
  return `${names.length} perfis: ${shown}${rest > 0 ? ` e mais ${rest}` : ""}`;
}

/** Linha de apoio: data/hora local da importação (não nos envios por dia e em "Outros") e a contagem. */
export function folderSubtitle(folder: EditFolder): string {
  const count = videoCountText(folder.video_count);
  if (folder.kind === "local" || folder.kind === "other") return count;
  return `${formatDateTime(folder.created_at)} · ${count}`;
}

/** Nome completo (título e data) usado na confirmação de apagar a pasta. */
export function folderFullName(folder: EditFolder): string {
  if (folder.kind === "local" || folder.kind === "other") return folderTitle(folder);
  return `${folderTitle(folder)} (${formatDateTime(folder.created_at)})`;
}

export interface DeleteTotals {
  videos: number;
  versions: number;
  bytes: number;
}

/** Totais de vídeos (original + versões) a apagar. */
export function mediaDeleteTotals(items: Media[]): DeleteTotals {
  let versions = 0;
  let bytes = 0;
  for (const m of items) {
    bytes += m.file_size;
    for (const r of m.renditions) {
      versions += 1;
      bytes += r.file_size;
    }
  }
  return { videos: items.length, versions, bytes };
}

export function folderDeleteTotals(folder: EditFolder): DeleteTotals {
  return { videos: folder.video_count, versions: folder.rendition_count, bytes: folder.total_bytes };
}

export function versionsDeleteTotals(media: Media): DeleteTotals {
  return { videos: 0, versions: media.renditions.length, bytes: media.renditions.reduce((s, r) => s + r.file_size, 0) };
}

/**
 * Texto da confirmação: o que sai, quanto espaço volta e o aviso de que não há volta. Na pasta inteira, o nome dela e
 * quantos desses vídeos também estão em outras pastas (`shared`).
 */
export function deleteConfirmLines(totals: DeleteTotals, folderName?: string, shared = 0): string[] {
  const lines: string[] = [];
  if (folderName) lines.push(`Pasta: ${folderName}.`);
  if (totals.videos > 0) {
    const verb = totals.videos === 1 ? "Será apagado" : "Serão apagados";
    const originals = totals.videos === 1 ? "original" : "originais";
    lines.push(
      `${verb} ${videoCountText(totals.videos)} (${originals})` +
        (totals.versions > 0 ? ` e ${versionCountText(totals.versions)}.` : "."),
    );
  } else {
    const verb = totals.versions === 1 ? "Será apagada" : "Serão apagadas";
    lines.push(`${verb} ${versionCountText(totals.versions)}; o vídeo original continua na biblioteca.`);
  }
  if (shared > 0) {
    lines.push(
      shared === 1
        ? "1 desses vídeos também está em outra pasta e sairá dela."
        : `${formatCount(shared)} desses vídeos também estão em outras pastas e sairão delas.`,
    );
  }
  lines.push(`Espaço liberado: ${formatBytes(totals.bytes)}.`);
  lines.push(DELETE_IRREVERSIBLE_TEXT);
  return lines;
}

const SKIP_REASONS: Record<string, string> = {
  media_busy: "está sendo baixado ou editado agora",
  export_in_progress: "está numa exportação em andamento",
  media_not_found: "já não estava na biblioteca",
  file_in_use: "arquivo aberto em outro programa",
};

export function deleteIssueReason(code: string, message: string): string {
  return SKIP_REASONS[code] ?? message;
}

/** Resumo depois de apagar: apagados, pulados e falhas, com o motivo de cada um (por nome quando conhecido). */
export function deleteResultText(result: MediaDeleteResult, names: ReadonlyMap<number, string> = new Map()): string {
  const parts = [`${videoCountText(result.deleted)} ${result.deleted === 1 ? "apagado" : "apagados"}.`];
  const describe = (issue: { id: number; code: string; message: string }) =>
    `${names.get(issue.id) ?? `vídeo ${issue.id}`} (${deleteIssueReason(issue.code, issue.message)})`;
  if (result.skipped.length > 0) {
    const label = result.skipped.length === 1 ? "não apagado" : "não apagados";
    parts.push(`${formatCount(result.skipped.length)} ${label}: ${result.skipped.map(describe).join("; ")}.`);
  }
  if (result.failed.length > 0) {
    parts.push(`${formatCount(result.failed.length)} com falha: ${result.failed.map(describe).join("; ")}.`);
  }
  return parts.join(" ");
}

/** Resumo de apagar só as versões editadas de um vídeo. */
export function versionsDeletedText(deleted: number, failures: string[]): string {
  const done = deleted === 1 ? "1 versão editada apagada." : `${formatCount(deleted)} versões editadas apagadas.`;
  if (failures.length === 0) return done;
  const label = failures.length === 1 ? "não apagada" : "não apagadas";
  const reasons = failures.map((f) => f.trim().replace(/\.+$/, ""));
  return `${done} ${formatCount(failures.length)} ${label}: ${reasons.join("; ")}.`;
}

/** Máximo de ids por pedido de POST /api/media/delete (mesmo teto do backend). */
export const DELETE_CHUNK = 500;

/** Ids em blocos de até `size` (a seleção pode passar de 500 com "Selecionar todos da pasta"). */
export function chunkIds(ids: number[], size = DELETE_CHUNK): number[][] {
  const chunks: number[][] = [];
  for (let i = 0; i < ids.length; i += size) chunks.push(ids.slice(i, i + size));
  return chunks;
}

/** Soma os resumos de vários pedidos de exclusão. */
export function mergeDeleteResults(results: MediaDeleteResult[]): MediaDeleteResult {
  return {
    deleted: results.reduce((n, r) => n + r.deleted, 0),
    deleted_ids: results.flatMap((r) => r.deleted_ids),
    skipped: results.flatMap((r) => r.skipped),
    failed: results.flatMap((r) => r.failed),
  };
}
