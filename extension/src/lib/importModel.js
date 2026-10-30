// Regras puras da importação (aba Importar e ações da Página atual): corpo de POST /api/ext/start, período,
// visualizações, predefinições de operação e idempotência local (client_request_id).
import { parseInstagramUrl } from "./instagramUrl.js";

export const LIMIT_PRESETS = [10, 50, 100, 500];
export const PERIOD_PRESETS = [7, 30, 90];
export const VIEWS_PRESETS = [1_000, 10_000, 50_000, 100_000, 500_000, 1_000_000];
export const MAX_LIMIT = 1_000_000;
export const MAX_PERIOD_DAYS = 3650;
export const MAX_VIEWS = 10_000_000_000;

export const MEDIA_OPTIONS = [
  { value: "videos", label: "Somente vídeos" },
  { value: "photos", label: "Somente fotos" },
  { value: "covers", label: "Somente capas" },
  { value: "videos_covers", label: "Vídeos e capas" },
  { value: "all", label: "Todas (fotos, vídeos e capas)" },
];
/** Tipos de mídia com os quais o backend aceita filtro de visualizações. */
export const VIEWS_MEDIA = new Set(["videos", "covers", "videos_covers"]);
export const VIEWS_DISABLED_TEXT =
  "O filtro de visualizações só vale para vídeos e capas; com fotos ou todas as mídias ele não se aplica.";

export const FLOW_OPTIONS = [
  { value: "download", label: "Só baixar" },
  { value: "auto", label: "Baixar e processar automaticamente" },
  { value: "manual", label: "Baixar e aguardar edição manual" },
];
export const EXPORT_CONTENT_OPTIONS = [
  { value: "originals", label: "Originais" },
  { value: "edited", label: "Editados" },
  { value: "both", label: "Originais e editados" },
];

export class FormError extends Error {}

function positiveInt(text, max, label) {
  const value = Number(String(text ?? "").trim().replace(/\./g, ""));
  if (!Number.isInteger(value) || value < 1 || (max && value > max)) {
    throw new FormError(`${label}: informe um número inteiro de 1 a ${max.toLocaleString("pt-BR")}.`);
  }
  return value;
}

/** "1 mil", "1 mi" etc. */
export function viewsLabel(n) {
  if (n >= 1_000_000) return `${String(n / 1_000_000).replace(".", ",")} mi`;
  if (n >= 1_000) return `${String(n / 1_000).replace(".", ",")} mil`;
  return String(n);
}

/** Dias de 1º de janeiro até hoje, contando hoje ("ano atual" → posted_within_days). */
export function daysSinceYearStart(now = new Date()) {
  const start = new Date(now.getFullYear(), 0, 1);
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((today - start) / 86_400_000) + 1;
}

/** Período escolhido → dias (ou null = qualquer data). period: "any"|"7"|"30"|"90"|"year"|"custom". */
export function periodDays(period, custom, now = new Date()) {
  if (!period || period === "any") return null;
  if (period === "year") return daysSinceYearStart(now);
  if (period === "custom") return positiveInt(custom, MAX_PERIOD_DAYS, "Período (dias)");
  return positiveInt(period, MAX_PERIOD_DAYS, "Período (dias)");
}

/** Mínimo/máximo de visualizações (null = sem filtro). Só vale com vídeos/capas. */
export function viewsRange(media, form) {
  if (!VIEWS_MEDIA.has(media)) return { min: null, max: null };
  const pick = (choice, custom, label) => {
    if (!choice) return null;
    return positiveInt(choice === "custom" ? custom : choice, MAX_VIEWS, label);
  };
  const min = pick(form.minViews, form.minViewsCustom, "Visualizações mínimas");
  const max = pick(form.maxViews, form.maxViewsCustom, "Visualizações máximas");
  if (min !== null && max !== null && max < min) {
    throw new FormError("O máximo de visualizações precisa ser maior ou igual ao mínimo.");
  }
  return { min, max };
}

/** Linhas coladas → entradas (sem vazias). */
export function splitEntries(text) {
  return String(text ?? "").split(/[\r\n,;\s]+/).map((s) => s.trim()).filter(Boolean);
}

const BARE_USERNAME_RE = /^@?[A-Za-z0-9._]{1,30}$/;

/**
 * Prévia local (sem rede) do que foi colado: perfis, publicações, inválidos e repetidos. É só uma prévia; o app
 * classifica de novo no `start` e a resposta (`invalid`, `duplicates`) é a oficial.
 */
export function classifyEntries(text) {
  const seen = new Set();
  const out = { profiles: [], links: [], invalid: [], duplicates: 0 };
  for (const entry of splitEntries(text)) {
    let parsed;
    if (BARE_USERNAME_RE.test(entry)) {
      parsed = parseInstagramUrl(`https://www.instagram.com/${entry.replace(/^@/, "")}/`);
    } else {
      parsed = parseInstagramUrl(/^https?:\/\//i.test(entry) ? entry : `https://${entry}`);
    }
    if (parsed.kind === "other") {
      out.invalid.push(entry);
      continue;
    }
    const key = parsed.kind === "profile" ? `@${parsed.username}` : `#${parsed.shortcode}`;
    if (seen.has(key)) {
      out.duplicates += 1;
      continue;
    }
    seen.add(key);
    if (parsed.kind === "profile") out.profiles.push(parsed.username);
    else out.links.push(parsed.canonicalUrl);
  }
  return out;
}

/**
 * Configuração do "processar" (aba Processar) → campos do corpo do start.
 * process: {mode: "edit"|"preset", edit: <corpo settings de /api/edits>, presetId}
 */
export function processingFields(flow, process) {
  if (flow !== "auto") return { post_processing: "none" };
  if (!process) throw new FormError("Configure o processamento na aba Processar antes de escolher processar automaticamente.");
  if (process.mode === "preset") {
    if (!process.presetId) throw new FormError("Escolha um preset de processamento na aba Processar.");
    return { post_processing: "preset", preset_id: Number(process.presetId) };
  }
  if (!process.edit) throw new FormError("Configure a edição na aba Processar.");
  return { post_processing: "edit", edit: process.edit };
}

export function autoExportField(form) {
  if (!form.autoExport) return null;
  const content = form.autoExportContent || "originals";
  if (!EXPORT_CONTENT_OPTIONS.some((o) => o.value === content)) throw new FormError("Conteúdo da exportação inválido.");
  if (content !== "originals" && form.flow !== "auto") {
    throw new FormError("Para exportar editados automaticamente, escolha \"Baixar e processar automaticamente\".");
  }
  if (content !== "originals" && (form.media === "covers" || form.media === "photos")) {
    throw new FormError("Capas e fotos não são editadas: exporte só os originais.");
  }
  return { content };
}

/**
 * Formulário da aba Importar → corpo de POST /api/ext/start.
 * form: {source: "profile"|"many", profile, text, media, limit, limitCustom, confirmAll, period, periodCustom,
 *        minViews, minViewsCustom, maxViews, maxViewsCustom, flow, autoExport, autoExportContent}
 * process: configuração salva da aba Processar (só usada com flow "auto").
 */
export function buildStartBody(form, process = null, { now = new Date(), requestId = null } = {}) {
  let inputs;
  let hasProfiles;
  if (form.source === "many") {
    const preview = classifyEntries(form.text);
    inputs = [...preview.profiles, ...preview.links];
    if (!inputs.length) throw new FormError("Cole pelo menos um perfil ou link válido do Instagram.");
    hasProfiles = preview.profiles.length > 0;
  } else {
    const name = String(form.profile ?? "").trim().replace(/^@/, "");
    const parsed = parseInstagramUrl(`https://www.instagram.com/${name}/`);
    if (!name || parsed.kind !== "profile") throw new FormError("Informe um @ de perfil válido.");
    inputs = [parsed.username];
    hasProfiles = true;
  }
  const media = form.media || "videos";
  if (!MEDIA_OPTIONS.some((o) => o.value === media)) throw new FormError("Tipo de mídia inválido.");
  const body = { inputs, media };

  if (hasProfiles) {
    if (form.limit === "all") {
      if (!form.confirmAll) throw new FormError("Confirme que quer coletar todas as publicações do perfil.");
      body.limit = null;
      body.confirm_all = true;
    } else {
      body.limit = positiveInt(form.limit === "custom" ? form.limitCustom : (form.limit ?? "50"), MAX_LIMIT,
        "Quantidade");
    }
    const days = periodDays(form.period, form.periodCustom, now);
    if (days !== null) body.posted_within_days = days;
    const { min, max } = viewsRange(media, form);
    if (min !== null) body.min_views = min;
    if (max !== null) body.max_views = max;
  }
  Object.assign(body, processingFields(form.flow, process));
  body.auto_export = autoExportField(form);
  if (requestId) body.client_request_id = requestId;
  return body;
}

/* ---------------- Página atual: ações por tipo de publicação ---------------- */

/** Ações de uma publicação conforme o tipo (kind do post-preview, ou o da URL antes da análise). */
export function postActions(kind) {
  if (kind === "reel" || kind === "video") {
    return [
      // Num Reel "Vídeos e capas" e "Todas" dão o mesmo resultado: fica só "Baixar tudo".
      { media: "videos", label: "Baixar vídeo", primary: true },
      { media: "covers", label: "Baixar capa" },
      { media: "all", label: "Baixar tudo (vídeo e capa)" },
    ];
  }
  if (kind === "photo") return [{ media: "photos", label: "Baixar foto(s)", primary: true }];
  // carrossel, ou publicação /p/ ainda não analisada (tipo desconhecido): todas as opções.
  return [
    { media: "all", label: "Baixar todos os itens", primary: true },
    { media: "photos", label: "Só fotos" },
    { media: "videos", label: "Só vídeos" },
    { media: "covers", label: "Capas dos vídeos" },
    { media: "videos_covers", label: "Vídeos e capas" },
  ];
}

/** Corpo do start para uma publicação (um link), com o fluxo/exportação escolhidos na aba Importar. */
export function buildPostBody(canonicalUrl, media, flowForm = {}, process = null, requestId = null) {
  if (!canonicalUrl) throw new FormError("Link da publicação ausente.");
  if (!MEDIA_OPTIONS.some((o) => o.value === media)) throw new FormError("Ação inválida.");
  const body = { inputs: [canonicalUrl], media };
  Object.assign(body, processingFields(flowForm.flow || "download", process));
  body.auto_export = autoExportField({ ...flowForm, flow: flowForm.flow || "download" });
  if (requestId) body.client_request_id = requestId;
  return body;
}

/* ---------------- predefinições de operação ---------------- */

export const DEFAULT_FORM = {
  source: "profile", profile: "", text: "", media: "videos", limit: "50", limitCustom: "", confirmAll: false,
  period: "any", periodCustom: "", minViews: "", minViewsCustom: "", maxViews: "", maxViewsCustom: "",
  flow: "download", autoExport: false, autoExportContent: "originals",
};

/** Campos que uma predefinição guarda (nunca o perfil/links). */
export const PRESET_FIELDS = ["media", "limit", "limitCustom", "period", "periodCustom", "minViews", "minViewsCustom",
  "maxViews", "maxViewsCustom", "flow", "autoExport", "autoExportContent"];

export const BUILTIN_PRESETS = [
  { id: "builtin:download", name: "Somente baixar", builtin: true,
    values: { flow: "download", autoExport: false } },
  { id: "builtin:process", name: "Baixar e processar", builtin: true,
    values: { flow: "auto", autoExport: false } },
  { id: "builtin:process-export", name: "Baixar, processar e exportar", builtin: true,
    values: { flow: "auto", autoExport: true, autoExportContent: "edited" } },
  { id: "builtin:all-raw", name: "Baixar tudo sem processamento", builtin: true,
    values: { media: "all", flow: "download", autoExport: false, minViews: "", maxViews: "" } },
];

export function pickPresetValues(form) {
  const values = {};
  for (const key of PRESET_FIELDS) if (key in form) values[key] = form[key];
  return values;
}

/** Aplica uma predefinição ao formulário (mantém origem/perfil/links e a confirmação de "Todas" desmarcada). */
export function applyPreset(form, preset) {
  const values = {};
  for (const key of PRESET_FIELDS) if (preset && preset.values && key in preset.values) values[key] = preset.values[key];
  return { ...form, ...values, confirmAll: false };
}

export function makeUserPreset(name, form, idFactory = () => `user:${Date.now()}`) {
  const clean = String(name ?? "").trim().slice(0, 40);
  if (!clean) throw new FormError("Dê um nome para a predefinição.");
  return { id: idFactory(), name: clean, builtin: false, values: pickPresetValues(form) };
}

/* ---------------- idempotência local ---------------- */

/**
 * Guarda o client_request_id do pedido em curso: o mesmo corpo reenviado depois de erro de rede reaproveita a mesma
 * id (o backend devolve a mesma resposta sem criar de novo); depois de sucesso, a próxima vez gera outra.
 */
export class RequestIdKeeper {
  constructor(uuid = () => globalThis.crypto.randomUUID()) {
    this.uuid = uuid;
    this.fingerprint = null;
    this.id = null;
  }

  idFor(body) {
    const { client_request_id: _ignored, ...rest } = body || {};
    const fingerprint = JSON.stringify(rest);
    if (fingerprint !== this.fingerprint || !this.id) {
      this.fingerprint = fingerprint;
      this.id = this.uuid();
    }
    return this.id;
  }

  succeeded() {
    this.fingerprint = null;
    this.id = null;
  }
}

/** Resumo legível do que o start vai fazer (mostrado antes de enviar). */
export function describeStartBody(body) {
  const media = MEDIA_OPTIONS.find((o) => o.value === body.media)?.label ?? body.media;
  const parts = [media];
  if ("limit" in body) parts.push(body.limit === null ? "todas as publicações" : `até ${body.limit} por perfil`);
  if (body.posted_within_days) parts.push(`últimos ${body.posted_within_days} dias`);
  if (body.min_views) parts.push(`mín. ${viewsLabel(body.min_views)} visualizações`);
  if (body.max_views) parts.push(`máx. ${viewsLabel(body.max_views)} visualizações`);
  if (body.post_processing === "edit") parts.push("edição automática");
  if (body.post_processing === "preset") parts.push("preset automático");
  if (body.auto_export) {
    parts.push(`ZIP automático (${EXPORT_CONTENT_OPTIONS.find((o) => o.value === body.auto_export.content)?.label
      .toLowerCase()})`);
  }
  return parts.join(" · ");
}

/** Operação grande que pede confirmação explícita antes de enviar. */
export function needsConfirmation(body) {
  const many = (body.inputs || []).length;
  // "Todas" já exige a caixa de confirmação explícita no formulário (confirm_all).
  if (typeof body.limit === "number" && body.limit * Math.max(1, many) > 1000) {
    return `Até ${(body.limit * many).toLocaleString("pt-BR")} publicações no total.`;
  }
  if (many > 50) return `${many} entradas de uma vez.`;
  return null;
}
