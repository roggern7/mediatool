// Exportar para pasta: textos, estado dos botões e montagem do pedido (funções puras).
// Contrato: docs/CONTRACTS.md, seção 8.2. O destino nunca sai do painel como caminho: só o token do seletor nativo.

import type {
  Batch,
  ExportDestination,
  ExportJob,
  ExportRequest,
  ExportSource,
  ImportGroup,
  LastExport,
  MediaType,
  MediaView,
} from "../api/types";
import { formatCount } from "./format";
import { ROUTES } from "./router";

/** Ritmo de atualização do progresso enquanto a cópia roda. */
export const EXPORT_POLL_MS = 1_000;
/** O diálogo nativo espera o usuário por até 10 min no backend; o painel espera um pouco mais. */
export const PICK_FOLDER_TIMEOUT_MS = 11 * 60_000;

export const PICKING_TEXT = "Escolha a pasta na janela do Windows…";
export const ORIGINALS_NOTE = "A exportação copia os arquivos originais (não as versões processadas).";
export const NOT_EXPORTED_HINT = "Ainda não exportada: use “Exportar para pasta”.";
export const UNFINISHED_BATCH_NOTE =
  "A importação não terminou: a exportação leva só o que já foi baixado até agora.";
export const UNFINISHED_GROUP_NOTE =
  "Há perfis que não terminaram: a exportação leva só o que já foi baixado até agora.";
/** Botão oferecido quando o seletor nativo não está disponível neste computador (501 picker_unavailable). */
export const DEFAULT_FOLDER_BUTTON = "Exportar para a pasta padrão em Documentos";

type Counts = Pick<LastExport, "total" | "copied" | "skipped_existing" | "renamed" | "missing" | "failed">;

export function isExportActive(job: Pick<ExportJob, "status"> | null | undefined): boolean {
  return job != null && (job.status === "pending" || job.status === "running");
}

/** "3.482 / 10.000". */
export function exportProgressText(job: Pick<ExportJob, "processed" | "total">): string {
  return `${formatCount(Math.min(job.processed, job.total))} / ${formatCount(job.total)}`;
}

export function exportFraction(job: Pick<ExportJob, "processed" | "total">): number {
  if (job.total <= 0) return 0;
  return Math.min(1, Math.max(0, job.processed / job.total));
}

/** Linha de estado durante a cópia (na fila ou copiando). */
export function exportRunningText(job: Pick<ExportJob, "status" | "dest_path">): string {
  return job.status === "pending"
    ? `Na fila para copiar para ${job.dest_path}`
    : `Copiando para ${job.dest_path}`;
}

/** "1.248 mídias exportadas para C:\...\Minha pasta". */
export function exportResultText(job: Counts & { dest_path: string }): string {
  if (job.copied === 0 && job.skipped_existing > 0) {
    return `Nenhuma mídia nova para copiar: a pasta ${job.dest_path} já está completa`;
  }
  if (job.copied === 1) return `1 mídia exportada para ${job.dest_path}`;
  return `${formatCount(job.copied)} mídias exportadas para ${job.dest_path}`;
}

/** Complemento curto do resultado: "12 já estavam na pasta · 3 renomeadas · 1 original ausente". Vazio se nada. */
export function exportExtrasText(job: Counts): string {
  const parts: string[] = [];
  if (job.skipped_existing > 0) {
    parts.push(
      job.skipped_existing === 1 ? "1 já estava na pasta" : `${formatCount(job.skipped_existing)} já estavam na pasta`,
    );
  }
  if (job.renamed > 0) parts.push(job.renamed === 1 ? "1 renomeada" : `${formatCount(job.renamed)} renomeadas`);
  if (job.missing > 0) {
    parts.push(job.missing === 1 ? "1 original ausente" : `${formatCount(job.missing)} originais ausentes`);
  }
  if (job.failed > 0) parts.push(job.failed === 1 ? "1 não copiada" : `${formatCount(job.failed)} não copiadas`);
  return parts.join(" · ");
}

const LIBRARY_SAFE = "A Biblioteca não foi alterada.";

/** Guarda de "um pedido por vez": recusa a segunda entrada enquanto a primeira não saiu (duplo clique). */
export interface SingleFlight {
  tryEnter: () => boolean;
  leave: () => void;
}

export function createSingleFlight(): SingleFlight {
  let busy = false;
  return {
    tryEnter: () => {
      if (busy) return false;
      busy = true;
      return true;
    },
    leave: () => {
      busy = false;
    },
  };
}

/** Roda `task` se a guarda estiver livre (devolve null sem rodar, se ocupada) e libera ao fim, com sucesso ou erro. */
export async function runExclusive<T>(flight: SingleFlight, task: () => Promise<T>): Promise<T | null> {
  if (!flight.tryEnter()) return null;
  try {
    return await task();
  } finally {
    flight.leave();
  }
}

/**
 * Exportação concluída com parte das mídias não copiadas: o motivo informado pelo backend (ex.: "3 arquivos não
 * puderam ser gravados na pasta de destino: acesso negado. Exporte de novo..."). null se não há o que avisar.
 */
export function exportPartialText(
  job: Pick<ExportJob, "status" | "failed" | "error_message">,
): string | null {
  if (job.status !== "completed" || job.failed <= 0) return null;
  return (job.error_message ?? "").trim() || null;
}

// ---- Baixar ZIP ----

export const ZIP_SPACE_NOTE =
  "O ZIP leva os arquivos originais. O navegador vai precisar do mesmo espaço de novo na pasta de Downloads.";
export const ZIP_EXPIRED_TEXT = "Este ZIP já foi removido do computador. Use “Baixar ZIP” para preparar de novo.";

const sizeFmt1 = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const timeFmt = new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" });

/** Tamanho em pt-BR: "512 B", "340 KB", "12,5 MB", "18,4 GB". */
export function formatZipSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return "-";
  if (bytes < 1024) return `${formatCount(bytes)} B`;
  if (bytes < 1024 ** 2) return `${formatCount(Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 ** 3) return `${sizeFmt1.format(bytes / 1024 ** 2)} MB`;
  return `${sizeFmt1.format(bytes / 1024 ** 3)} GB`;
}

function mediaCount(n: number): string {
  return n === 1 ? "1 mídia" : `${formatCount(n)} mídias`;
}

/** "Preparando ZIP • 3.482 / 10.000 mídias". */
export function zipPreparingText(job: Pick<ExportJob, "processed" | "total">): string {
  return `Preparando ZIP • ${exportProgressText(job)} mídias`;
}

/** "ZIP pronto • 10.000 mídias • 18,4 GB". */
export function zipReadyText(job: Pick<ExportJob, "copied" | "zip_bytes">): string {
  return `ZIP pronto • ${mediaCount(job.copied)} • ${formatZipSize(job.zip_bytes)}`;
}

const dayFmt = new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit" });

/**
 * "disponível até 14:05" quando o prazo é hoje; "disponível até 07/10 14:05" quando é outro dia (hora local).
 * null sem prazo conhecido.
 */
export function zipAvailableText(job: Pick<ExportJob, "zip_expires_at">, now: number = Date.now()): string | null {
  if (!job.zip_expires_at) return null;
  const date = new Date(job.zip_expires_at);
  if (Number.isNaN(date.getTime())) return null;
  const today = new Date(now);
  const sameDay =
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate();
  return sameDay
    ? `disponível até ${timeFmt.format(date)}`
    : `disponível até ${dayFmt.format(date)} ${timeFmt.format(date)}`;
}

/** Ritmo lento de atualização enquanto um ZIP pronto está na tela (validade muda após o download; expira). */
export const ZIP_READY_POLL_MS = 30_000;
export const ZIP_AUTO_DOWNLOAD_TEXT = "O download começou pelo navegador.";

export interface ResumeDecision {
  job: ExportJob;
  /** `follow`: exportação em andamento (acompanhar o progresso); `offer`: ZIP pronto ainda disponível (só oferecer
   *  "Baixar novamente", sem download automático). */
  action: "follow" | "offer";
}

/**
 * Ao abrir a tela: o que fazer com as últimas exportações da origem (mais recente primeiro). Em andamento ->
 * acompanha; senão, ZIP pronto e ainda válido -> oferece o link; expirada, cancelada, falha ou exportação para
 * pasta já concluída -> nada.
 */
export function pickResumeExport(items: ExportJob[], now: number = Date.now()): ResumeDecision | null {
  const active = items.find((item) => isExportActive(item));
  if (active) return { job: active, action: "follow" };
  const ready = items.find((item) => {
    if (!zipDownloadUrl(item)) return false;
    const expires = item.zip_expires_at ? new Date(item.zip_expires_at).getTime() : Number.NaN;
    return Number.isNaN(expires) || expires > now;
  });
  return ready ? { job: ready, action: "offer" } : null;
}

/** Id da exportação já em andamento informado no corpo de um 409 `export_already_running`; null se ausente. */
export function runningExportId(code: string | null, payload: Record<string, unknown> | null): number | null {
  if (code !== "export_already_running" || !payload) return null;
  const id = payload.export_id;
  return typeof id === "number" && Number.isInteger(id) && id > 0 ? id : null;
}

/** O download pode ser iniciado: ZIP pronto, com link. */
export function zipDownloadUrl(job: Pick<ExportJob, "mode" | "status" | "zip_ready" | "download_url"> | null): string | null {
  if (!job || job.mode !== "zip" || job.status !== "completed" || !job.zip_ready) return null;
  return job.download_url || null;
}

/** Corpo de POST /api/exports. O destino é o token do seletor, a pasta padrão ou o ZIP; nunca um caminho. */
export function buildExportRequest(
  source: ExportSource,
  destination: { kind: "token"; token: string } | { kind: "default" } | { kind: "zip" },
): ExportRequest {
  const dest: ExportDestination =
    destination.kind === "token"
      ? { token: destination.token }
      : destination.kind === "zip"
        ? { zip: true }
        : { default: true };
  return { source, destination: dest };
}

/** Mensagem de exportação cancelada ou com falha; null nos outros estados. Deixa claro que a Biblioteca está intacta. */
export function exportEndedText(
  job: Pick<ExportJob, "status" | "copied" | "total" | "error_message" | "dest_path" | "mode">,
): string | null {
  if (job.mode === "zip" && job.status === "expired") return ZIP_EXPIRED_TEXT;
  if (job.mode === "zip" && job.status === "cancelled") {
    return `Preparação do ZIP cancelada. Nenhum arquivo foi baixado. ${LIBRARY_SAFE}`;
  }
  if (job.status === "cancelled") {
    const kept =
      job.copied === 0
        ? "Nenhum arquivo foi copiado."
        : job.copied === 1
          ? "1 arquivo já copiado continua na pasta."
          : `${formatCount(job.copied)} arquivos já copiados continuam na pasta.`;
    return `Exportação cancelada. ${kept} ${LIBRARY_SAFE}`;
  }
  if (job.status === "failed") {
    const fallback = job.mode === "zip" ? "O ZIP não pôde ser preparado." : "A exportação não terminou.";
    const reason = (job.error_message ?? "").trim() || fallback;
    return reason.includes(LIBRARY_SAFE) ? reason : `${reason} ${LIBRARY_SAFE}`;
  }
  return null;
}

export interface ExportOffer {
  /** "Exportar para pasta" é oferecido. */
  offer: boolean;
  /** Aviso curto quando a importação não terminou por inteiro (exporta o que há). */
  note: string | null;
}

/**
 * Lote: oferece exportar quando encerrou (concluído, com falhas, falho ou cancelado) e também quando está parado
 * em "precisa de atenção" com itens já baixados (com aviso de que a importação não terminou).
 */
export function batchExportOffer(batch: Pick<Batch, "status" | "listing_state" | "progress">): ExportOffer {
  if (batch.status !== "listing" && batch.status !== "running") return { offer: true, note: null };
  const saved = batch.progress.downloaded + batch.progress.skipped;
  if (batch.status === "listing" && batch.listing_state === "needs_attention" && saved > 0) {
    return { offer: true, note: UNFINISHED_BATCH_NOTE };
  }
  return { offer: false, note: null };
}

/**
 * Grupo: oferece exportar quando nenhum perfil está na fila nem trabalhando. Vale para concluído (inclusive
 * "com pendências"), cancelado e para o grupo parado em atenção (perfis aguardando ação), este com aviso.
 */
export function groupExportOffer(group: Pick<ImportGroup, "profiles">): ExportOffer {
  const { waiting, active, attention, total } = group.profiles;
  if (total <= 0 || waiting + active > 0) return { offer: false, note: null };
  return { offer: true, note: attention > 0 ? UNFINISHED_GROUP_NOTE : null };
}

export interface ExportActionsInput {
  /** "Exportar para pasta" é oferecido (ver batchExportOffer / groupExportOffer). */
  finished: boolean;
  /** Última exportação concluída informada pelo lote/grupo. */
  lastExport: LastExport | null | undefined;
  /** Exportação acompanhada nesta tela (a que acabou de ser pedida ou a que já estava em andamento). */
  current: ExportJob | null;
  /** Diálogo nativo aberto ou pedido sendo criado. */
  busy: boolean;
}

export interface ExportActions {
  /** "Exportar para pasta" e "Usar pasta padrão" aparecem. */
  showExport: boolean;
  canExport: boolean;
  /** "Abrir pasta" habilitado: há exportação concluída. */
  canOpen: boolean;
  /** Exportação cuja pasta o "Abrir pasta" abre (a concluída mais recente). */
  openId: number | null;
  /** Texto curto de orientação quando ainda não há pasta para abrir. */
  openHint: string | null;
}

export function exportActions(input: ExportActionsInput): ExportActions {
  const active = isExportActive(input.current);
  // Só exportação PARA PASTA concluída tem pasta para abrir (ZIP não).
  const done =
    input.current?.status === "completed" && input.current.mode !== "zip" ? input.current : null;
  const last = input.lastExport ?? null;
  // A concluída mais recente vence (ids crescem com o tempo).
  const openId = done && (!last || done.id >= last.id) ? done.id : (last?.id ?? null);
  return {
    showExport: input.finished,
    canExport: input.finished && !input.busy && !active,
    canOpen: openId !== null,
    openId,
    openHint: input.finished && openId === null ? NOT_EXPORTED_HINT : null,
  };
}

export function importExportSource(kind: "batch" | "group", id: number): ExportSource {
  return { kind, id };
}

export interface LibrarySelection {
  /** "Todos os N resultados" do filtro atual (resolvido no servidor, sem mandar ids). */
  allByFilter: boolean;
  view: MediaView;
  typeFilter: "all" | MediaType;
  q: string;
  /** Ids marcados na página (usados quando não é "todos os resultados"). */
  selectedIds: number[];
  /** Ids desmarcados dentro de "todos os resultados". */
  excludedIds?: number[];
}

/** Pedido de exportação a partir da seleção da Biblioteca; null se nada está selecionado. */
export function librarySelectionSource(sel: LibrarySelection): ExportSource | null {
  if (sel.allByFilter) {
    const filter: { view: MediaView; media_type?: MediaType; q?: string } = { view: sel.view };
    if (sel.typeFilter !== "all") filter.media_type = sel.typeFilter;
    if (sel.q) filter.q = sel.q;
    const excluded = sel.excludedIds ?? [];
    return {
      kind: "library",
      selection: excluded.length > 0 ? { filter, exclude_ids: excluded } : { filter },
    };
  }
  if (sel.selectedIds.length === 0) return null;
  return { kind: "library", selection: { media_ids: sel.selectedIds } };
}

/** Rótulo acessível do "Baixar ZIP" da Biblioteca. */
export function zipSelectedLabel(count: number): string {
  return count === 1 ? "Baixar 1 selecionado em um ZIP" : `Baixar ${formatCount(count)} selecionados em um ZIP`;
}

/** "Exportar 5.000 selecionados" (rótulo acessível do botão da Biblioteca). */
export function exportSelectedLabel(count: number): string {
  return count === 1 ? "Exportar 1 selecionado para uma pasta" : `Exportar ${formatCount(count)} selecionados para uma pasta`;
}

const PROFILE_PARAM = "perfil";

/** "Ver na Biblioteca": a Biblioteca já aberta com a busca pelo perfil do lote; sem perfil, só navega. */
export function libraryPathForProfile(profile: string | null | undefined): string {
  const name = (profile ?? "").trim().replace(/^@/, "");
  return name ? `${ROUTES.library}?${PROFILE_PARAM}=${encodeURIComponent(name)}` : ROUTES.library;
}

/** Busca inicial da Biblioteca a partir da URL (`?perfil=usuario`). */
export function initialLibrarySearch(search: string): string {
  const value = new URLSearchParams(search).get(PROFILE_PARAM) ?? "";
  return value.trim().replace(/^@/, "").slice(0, 64);
}
