import { describeErrorCode } from "../api/errors";
import { formatCount } from "./format";
import { formatPauseTime } from "./pause";

/** Campos do lote usados aqui (todos opcionais: backends antigos não mandam). */
export interface BatchStateLike {
  listing_state?: string | null;
  paused_reason?: string | null;
  paused_until?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  rate_limit_resume_at?: string | null;
  rate_limit_pages_used?: number | null;
  rate_limit_pages_max?: number | null;
  listing_dropped?: { unexpected_total?: number | null; reasons?: Record<string, number> | null } | null;
}

export type BatchNoticeKind = "needs_attention" | "batch_paused" | "listing_paused" | "rate_limit_wait";

export interface BatchNotice {
  kind: BatchNoticeKind;
  text: string;
  /** Mostra "Retomar importação" (POST /api/batches/{id}/resume-listing). */
  canResume: boolean;
}

function sentence(text: string): string {
  const t = text.trim();
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

export function needsAttention(b: BatchStateLike): boolean {
  return b.listing_state === "needs_attention";
}

/** Lote pausado pelo disjuntor (falhas seguidas): vale enquanto paused_until estiver no futuro ou sem horário. */
export function isBatchPaused(b: BatchStateLike, now: number): boolean {
  if (!b.paused_reason) return false;
  if (!b.paused_until) return true;
  const t = Date.parse(b.paused_until);
  return !Number.isFinite(t) || t > now;
}

/** "em cerca de N min" até a retomada (recalculado com o relógio local entre uma atualização e outra). */
export function rateLimitRemainingText(resumeAt: string, now: number): string {
  const ms = Date.parse(resumeAt) - now;
  if (!Number.isFinite(ms) || ms <= 0) return "em instantes";
  if (ms < 60_000) return "em menos de 1 min";
  return `em cerca de ${Math.round(ms / 60_000)} min`;
}

/** Texto de apoio da espera pelo teto de páginas por hora (o lote continua ativo; não é erro nem pausa). */
export function rateLimitWaitText(b: BatchStateLike, now: number): string {
  const at = b.rate_limit_resume_at;
  const when = at && Number.isFinite(Date.parse(at))
    ? ` às ${formatPauseTime(at, now)} (${rateLimitRemainingText(at, now)})`
    : "";
  const used = b.rate_limit_pages_used;
  const max = b.rate_limit_pages_max;
  const pages = typeof used === "number" && typeof max === "number"
    ? ` Páginas usadas na última hora: ${used} de ${max}.`
    : "";
  return `O lote continua ativo e retoma automaticamente${when}.${pages}`;
}

/** Aviso do estado do lote para o painel de importação e a Fila; null quando não há nada a dizer. */
export function batchNotice(b: BatchStateLike, now: number): BatchNotice | null {
  if (needsAttention(b)) {
    const reason = b.error_code || b.error_message ? describeErrorCode(b.error_code ?? null, b.error_message ?? null) : null;
    return {
      kind: "needs_attention",
      text:
        "A importação parou e precisa de você." +
        (reason ? ` Motivo: ${sentence(reason)}` : "") +
        " O que já foi baixado continua salvo; ao retomar, ela continua de onde parou.",
      canResume: true,
    };
  }
  if (isBatchPaused(b, now)) {
    const reason = describeErrorCode(b.paused_reason ?? null, null).trim().replace(/\.$/, "");
    const when = b.paused_until && Number.isFinite(Date.parse(b.paused_until))
      ? ` Retomada automática prevista às ${formatPauseTime(b.paused_until, now)}.`
      : " Ela será retomada automaticamente.";
    return {
      kind: "batch_paused",
      text: `Importação pausada depois de várias falhas seguidas (${reason}).${when}`,
      canResume: false,
    };
  }
  if (b.listing_state === "waiting_rate_limit") {
    return {
      kind: "rate_limit_wait",
      text: `Aguardando limite de requisições. ${rateLimitWaitText(b, now)}`,
      canResume: false,
    };
  }
  if (b.listing_state === "paused") {
    return {
      kind: "listing_paused",
      text: "A coleta da lista está em pausa. O Media Tool tenta de novo sozinho e continua de onde parou.",
      canResume: false,
    };
  }
  return null;
}

/** Motivo de descarte da listagem (nome fixo do backend) em linguagem humana; motivos irmãos somam no mesmo texto. */
const DROPPED_REASON_LABEL: Record<string, string> = {
  image_no_url: "imagem sem URL utilizável",
  video_no_url: "vídeo sem URL utilizável",
  carousel_child_invalid: "item de carrossel inválido",
  carousel_children_missing: "item faltando em carrossel",
  carousel_no_children: "carrossel sem itens",
  node_no_code: "post sem identificador",
  media_type_missing: "post sem tipo de mídia",
  edge_null: "post sem dados na resposta",
  edge_not_object: "post sem dados na resposta",
  node_null: "post sem dados na resposta",
  node_not_object: "post sem dados na resposta",
};
const DROPPED_OTHER_LABEL = "outro motivo";
/** Motivos em que o post inteiro foi descartado: não se sabe quantas mídias ele tinha (a contagem é um mínimo). */
const WHOLE_POST_REASONS = new Set([
  "edge_null", "edge_not_object", "node_null", "node_not_object", "node_no_code", "carousel_no_children",
  "media_type_missing",
]);

export interface ListingDroppedInfo {
  total: number;
  /** Há post inteiro descartado: o total é "pelo menos". */
  atLeast: boolean;
  /** Aviso do lote: "1 mídia não pôde ser adicionada durante a listagem". */
  text: string;
  /** Resumo final e lista: "1 mídia descartada durante a listagem". */
  summary: string;
  /** Detalhe curto por motivo: "imagem sem URL utilizável: 1; item de carrossel inválido: 2" (vazio se desconhecido). */
  detail: string;
}

/**
 * Mídias que a listagem não pôde adicionar ao lote (aviso de atenção; o lote não falha por isso).
 * null quando não houve descarte ou o backend é antigo (sem ruído).
 */
export function listingDropped(b: BatchStateLike): ListingDroppedInfo | null {
  const dropped = b.listing_dropped;
  if (!dropped) return null;
  const byLabel = new Map<string, number>();
  let sum = 0;
  let atLeast = false;
  for (const [reason, value] of Object.entries(dropped.reasons ?? {})) {
    const n = typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
    if (n === 0) continue;
    sum += n;
    if (WHOLE_POST_REASONS.has(reason)) atLeast = true;
    const label = DROPPED_REASON_LABEL[reason] ?? DROPPED_OTHER_LABEL;
    byLabel.set(label, (byLabel.get(label) ?? 0) + n);
  }
  const declared = dropped.unexpected_total;
  const total = typeof declared === "number" && Number.isFinite(declared) && declared > 0 ? Math.floor(declared) : sum;
  if (total <= 0) return null;
  const one = total === 1;
  const count = `${atLeast ? "pelo menos " : ""}${formatCount(total)} ${one ? "mídia" : "mídias"}`;
  const text = `${count} ${one ? "não pôde ser adicionada" : "não puderam ser adicionadas"} durante a listagem`;
  return {
    total,
    atLeast,
    text: text.charAt(0).toUpperCase() + text.slice(1),
    summary: `${count} ${one ? "descartada" : "descartadas"} durante a listagem`,
    detail: [...byLabel].map(([label, n]) => `${label}: ${formatCount(n)}`).join("; "),
  };
}

/** Sufixo do resumo do lote: " • 1 mídia descartada durante a listagem"; vazio quando não houve descarte. */
export function listingDroppedSuffix(b: BatchStateLike): string {
  const dropped = listingDropped(b);
  return dropped ? ` • ${dropped.summary}` : "";
}

/**
 * Frase completa do aviso de descarte. O fecho depende do lote: ativo -> os demais itens seguem; concluído -> não
 * foram afetados; cancelado/falho -> sem fecho (não há o que prometer sobre os demais).
 */
export function listingDroppedNotice(b: BatchStateLike & { status?: string | null }): string | null {
  const dropped = listingDropped(b);
  if (!dropped) return null;
  const closing =
    b.status === "listing" || b.status === "running"
      ? " Os demais itens do lote seguem normalmente."
      : b.status === "completed"
        ? " Os demais itens do lote não foram afetados."
        : "";
  return `${dropped.text}${dropped.detail ? ` (${dropped.detail})` : ""}.${closing}`;
}
