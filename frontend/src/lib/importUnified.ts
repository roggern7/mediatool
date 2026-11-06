// Importação unificada (POST /api/imports/start): publicações e/ou perfis num campo só. Funções puras; a tela só
// liga os estados.

import type {
  AutoExport,
  AutoExportContent,
  BatchProgress,
  ClassifyResult,
  EditSettings,
  MediaChoice,
  UnifiedImportRequest,
  UnifiedImportResult,
} from "../api/types";
import { formatCount } from "./format";
import type { PostProcessing } from "./importOptions";

export const MEDIA_CHOICES: { value: MediaChoice; label: string; hint: string }[] = [
  { value: "videos", label: "Somente vídeos", hint: "Reels e qualquer vídeo, inclusive vídeos de carrossel." },
  { value: "photos", label: "Somente fotos", hint: "Fotos avulsas e as fotos dos carrosséis; vídeos são ignorados." },
  { value: "covers", label: "Somente capas", hint: "Só a capa de cada vídeo/Reel, sem baixar o vídeo; fotos são ignoradas." },
  { value: "videos_covers", label: "Vídeos e capas", hint: "Cada vídeo/Reel e a capa dele; fotos são ignoradas." },
  {
    value: "all",
    label: "Todas: Fotos, Vídeos e Capas",
    hint: "Todos os itens de cada publicação (carrossel inteiro, na ordem) e a capa de cada vídeo.",
  },
  {
    value: "none",
    label: "Nada: Só Consultar Padrões",
    hint: "Não baixa nenhuma mídia: só lê o horário das publicações de cada perfil para o relatório de padrões (.zip).",
  },
];

/** Rótulo curto do tipo de mídia de um lote/grupo ("Somente fotos", "Vídeos e capas"...). */
export function mediaLabel(media: MediaChoice): string {
  if (media === "none") return "Só padrões";
  return media === "all" ? "Todas as mídias" : (MEDIA_CHOICES.find((c) => c.value === media)?.label ?? media);
}

/** Tipos com fotos ou capas: o pós-processamento (preset/edição) vale só para os vídeos originais. */
export function postProcessingApplies(media: MediaChoice): boolean {
  return media !== "covers" && media !== "photos" && media !== "none";
}

/** Lote/grupo só de padrões (aba "Consultar padrões"): nada foi baixado. */
export function isPatternsOnly(x: { media?: MediaChoice | null; media_set?: MediaChoice | null }): boolean {
  return x.media === "none" || x.media_set === "none";
}

/** Há horários gravados para o relatório de padrões deste lote/grupo. */
export function hasPostTimesReport(x: { post_times?: boolean; post_times_count?: number }): boolean {
  return !!x.post_times && (x.post_times_count ?? 0) > 0;
}

/** "37 publicações analisadas". */
export function analyzedPostsText(n: number): string {
  return `${formatCount(n)} ${n === 1 ? "publicação analisada" : "publicações analisadas"}`;
}

/** GET /api/post-times/report.zip do lote ou do grupo. */
export function postTimesReportUrl(target: { kind: "batch" | "group"; id: number }): string {
  return `/api/post-times/report.zip?${target.kind === "group" ? "group_id" : "batch_id"}=${target.id}`;
}

export const PATTERNS_NEED_PROFILES_TEXT =
  "“Consultar padrões” vale só para perfis: links de publicações não têm padrão de postagem. " +
  "Inclua ao menos um perfil na lista.";

/** O que o pedido vai fazer com o horário dos posts; null sem horário pedido. */
export function postTimesPlanText(media: MediaChoice, postTimes: boolean, hasLinks: boolean): string | null {
  const links = hasLinks ? " Links de publicações não têm padrão de postagem" : "";
  if (media === "none") {
    return (
      "Nenhuma mídia será baixada: só o horário das publicações de cada perfil é lido, para o relatório de padrões " +
      `(.zip).${links ? `${links} e ficam de fora.` : ""}`
    );
  }
  if (!postTimes) return null;
  return (
    "As mídias são baixadas e o horário das publicações de cada perfil é guardado para o relatório de padrões " +
    `(.zip), baixado à parte.${links ? `${links}: eles só baixam as mídias.` : ""}`
  );
}

/** Exportação ZIP automática ao concluir: desligada ou o conteúdo escolhido. */
export type AutoExportChoice = "off" | AutoExportContent;
export const AUTO_EXPORT_CHOICES: { value: AutoExportChoice; label: string }[] = [
  { value: "off", label: "Não exportar automaticamente" },
  { value: "originals", label: "Originais" },
  { value: "edited", label: "Editados" },
  { value: "both", label: "Originais e editados" },
];

/** "Editados" e "Originais e editados" só existem com pós-processamento (o backend recusa sem ele: 422). */
export function autoExportNeedsProcessing(choice: AutoExportChoice): boolean {
  return choice === "edited" || choice === "both";
}

export const AUTO_EXPORT_NEEDS_PROCESSING_TEXT =
  "“Editados” e “Originais e editados” exigem processar ou editar depois de baixar. Escolha um pós-processamento " +
  "ou exporte só os originais.";

/** Linha do painel do lote/grupo sobre a exportação automática; null sem ela. */
export function autoExportText(auto: AutoExport | null | undefined): string | null {
  if (!auto) return null;
  const what = AUTO_EXPORT_CHOICES.find((c) => c.value === auto.content)?.label.toLowerCase() ?? auto.content;
  switch (auto.status) {
    case "waiting":
      return auto.request_id
        ? `Exportação automática (${what}): um ZIP único quando os perfis e as publicações deste pedido terminarem.`
        : `Exportação automática (${what}): aguardando a importação terminar.`;
    case "pending":
    case "running":
      return `Exportação automática (${what}): preparando o ZIP…`;
    case "completed":
      return auto.zip_ready
        ? `Exportação automática (${what}): ZIP pronto.`
        : `Exportação automática (${what}): concluída.`;
    case "expired":
      return `Exportação automática (${what}): o ZIP já foi removido.`;
    case "cancelled":
      return `Exportação automática (${what}): cancelada.`;
    default:
      return `Exportação automática (${what}) falhou${auto.error_message ? `: ${auto.error_message}` : "."}`;
  }
}

/** Quantidade por perfil: atalhos, personalizada e "Todas" (pede confirmação). */
export const QUANTITY_PRESETS = [10, 50, 100, 500] as const;
export type QuantityMode = "custom" | "all" | `${number}`;
export const MAX_QUANTITY = 1_000_000;
export type QuantityChoice = { ok: true; limit: number | null } | { ok: false; error: string };

export const CONFIRM_ALL_TEXT =
  "“Todas” pede ao Instagram todas as publicações de cada perfil. Pode demorar bastante e fazer muitas " +
  "requisições (com pausas automáticas se o Instagram limitar). Deseja continuar?";

export function resolveQuantity(mode: QuantityMode, text: string): QuantityChoice {
  if (mode === "all") return { ok: true, limit: null };
  if (mode !== "custom") return { ok: true, limit: Number(mode) };
  const t = text.trim();
  const n = /^\d+$/.test(t) || /^\d{1,3}(\.\d{3})+$/.test(t) ? Number(t.replace(/\./g, "")) : NaN;
  if (!Number.isSafeInteger(n) || n < 1 || n > MAX_QUANTITY) {
    return { ok: false, error: `Informe uma quantidade inteira de 1 a ${formatCount(MAX_QUANTITY)}.` };
  }
  return { ok: true, limit: n };
}

/** "Todas" com ao menos um perfil exige a confirmação explícita (a API responde 422 confirm_all_required sem ela). */
export function needsConfirmAll(classified: ClassifyResult | null, quantity: QuantityChoice): boolean {
  return !!classified && classified.profiles.length > 0 && quantity.ok && quantity.limit === null;
}

/** Visualizações só valem para vídeos (e capas, que usam as visualizações do vídeo); nunca com fotos. */
export function viewsAllowed(media: MediaChoice): boolean {
  return media === "videos" || media === "covers" || media === "videos_covers";
}

function plural(n: number, one: string, many: string): string {
  return `${formatCount(n)} ${n === 1 ? one : many}`;
}

/** "2 perfis • 3 publicações • 1 inválida • 1 repetida" (só o que existe). */
export function classificationText(c: ClassifyResult): string {
  const parts = [plural(c.profiles.length, "perfil", "perfis"), plural(c.links.length, "publicação", "publicações")];
  if (c.invalid.length) parts.push(plural(c.invalid.length, "inválida", "inválidas"));
  if (c.duplicates) parts.push(plural(c.duplicates, "repetida", "repetidas"));
  return parts.join(" • ");
}

/**
 * Entradas enviadas: uma por linha não vazia (o backend separa também por vírgula e ponto e vírgula; espaço só separa
 * quando todos os pedaços da linha são URLs do Instagram ou @usuario, senão a linha inteira é inválida).
 */
export function entriesOf(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

export interface UnifiedForm {
  text: string;
  media: MediaChoice;
  quantity: QuantityChoice;
  confirmAll: boolean;
  periodDays: number | null;
  minViews: number | null;
  maxViews: number | null;
  post: PostProcessing;
  presetId: number | null;
  edit: EditSettings | null;
  /** Exportação ZIP automática ao concluir ("off" ou ausente = não exportar). */
  autoExport?: AutoExportChoice;
  /** Puxar horário dos posts (sempre ligado com "none"). */
  postTimes?: boolean;
}

/**
 * Corpo de POST /api/imports/start. Filtros só entram quando ligados (e visualizações só com vídeos/capas). Com
 * "none" (só padrões): `post_times` ligado e sem pós-processamento nem exportação automática (o backend recusa).
 */
export function unifiedBody(form: UnifiedForm): UnifiedImportRequest | null {
  if (!form.quantity.ok) return null;
  const patternsOnly = form.media === "none";
  const post = patternsOnly ? "none" : form.post;
  const body: UnifiedImportRequest = {
    inputs: entriesOf(form.text),
    media: form.media,
    limit: form.quantity.limit,
    confirm_all: form.quantity.limit === null && form.confirmAll,
    post_processing: post,
  };
  if (patternsOnly || form.postTimes) body.post_times = true;
  if (form.periodDays !== null) body.posted_within_days = form.periodDays;
  if (viewsAllowed(form.media)) {
    if (form.minViews !== null) body.min_views = form.minViews;
    if (form.maxViews !== null) body.max_views = form.maxViews;
  }
  if (post === "preset" && form.presetId !== null) body.preset_id = form.presetId;
  if (post === "edit" && form.edit) body.edit = form.edit;
  if (!patternsOnly && form.autoExport && form.autoExport !== "off") body.auto_export = { content: form.autoExport };
  return body;
}

/** Aviso do que foi criado junto: "Criados: grupo com 2 perfis e lote de 3 publicações." */
export function createdText(result: UnifiedImportResult): string {
  const parts: string[] = [];
  if (result.group_id !== null) parts.push(`grupo com ${plural(result.profiles, "perfil", "perfis")}`);
  else if (result.profiles === 1) parts.push("importação de 1 perfil");
  if (result.links_batch_id !== null) parts.push(`lote de ${plural(result.links, "publicação", "publicações")}`);
  const skipped = result.skipped?.length ?? 0;
  const tail = [
    result.invalid.length ? `${plural(result.invalid.length, "entrada inválida ignorada", "entradas inválidas ignoradas")}` : null,
    skipped ? `${plural(skipped, "perfil já em andamento pulado", "perfis já em andamento pulados")}` : null,
  ].filter(Boolean);
  const notes = (result.notes ?? []).map((note) => ` ${note}`).join("");
  return `Criados: ${parts.join(" e ")}.${tail.length ? ` ${tail.join("; ")}.` : ""}${notes}`;
}

/**
 * Contadores da importação unificada, com os mesmos rótulos do resumo da Importação:
 * "Já baixados antes: 2 · Filtrados: 5".
 * Só os que o backend manda (ausentes em backends antigos); null sem nenhum dos dois.
 */
export function progressExtrasText(p: Pick<BatchProgress, "duplicates" | "filtered">): string | null {
  const parts: string[] = [];
  if (typeof p.duplicates === "number") parts.push(`Já baixados antes: ${formatCount(p.duplicates)}`);
  if (typeof p.filtered === "number") parts.push(`Filtrados: ${formatCount(p.filtered)}`);
  return parts.length ? parts.join(" · ") : null;
}

/**
 * Aviso do resumo do lote quando parte dos itens já estava na Biblioteca: eles não são baixados de novo (o arquivo
 * seria o mesmo), mas continuam fazendo parte da importação e vão no ZIP/pasta dela. null sem repetidos.
 */
export function alreadyDownloadedNote(p: Pick<BatchProgress, "duplicates">): string | null {
  const n = p.duplicates;
  if (typeof n !== "number" || n <= 0) return null;
  const head =
    n === 1
      ? "1 item já tinha sido baixado antes e não foi baixado de novo: o app usou o arquivo que já estava na Biblioteca."
      : `${formatCount(n)} itens já tinham sido baixados antes e não foram baixados de novo: o app usou os arquivos que já estavam na Biblioteca.`;
  const tail =
    n === 1
      ? " Ele faz parte desta importação e vai junto no ZIP e na exportação para pasta."
      : " Eles fazem parte desta importação e vão juntos no ZIP e na exportação para pasta.";
  return head + tail;
}
