// Regras puras da importação (aba Importar e ações da Página atual): corpo de POST /api/ext/start, período,
// visualizações, predefinições de operação e idempotência local (client_request_id).
import { parseInstagramUrl } from "./instagramUrl.js";
import { formatSpeed, parseDecimal, speedRange, toEditSettings, validateProcess } from "./editModel.js";

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
  { value: "edit", label: "Editar após baixar" },
  { value: "preset", label: "Processar com preset" },
  { value: "manual", label: "Baixar e aguardar edição manual" },
];
/** Fluxos que processam os vídeos depois do download (edição ou preset). */
export const PROCESSING_FLOWS = new Set(["edit", "preset"]);
/** Tipos de mídia sem vídeo para editar: fotos e capas só são baixadas. */
export const NO_EDIT_MEDIA = new Set(["covers", "photos"]);
export const NO_CHANGES_TEXT = "Nenhuma edição selecionada.";
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

/** Configurações de "Editar após baixar" (as mesmas da página Editar, saída sempre MP4). */
export const DEFAULT_EDIT = { durationMode: "original", customText: "", speed: 1.0, sharpen: false };

/** Problema da edição após baixar (mesmas regras do app), ou null. Sem corte, velocidade nem nitidez: bloqueia. */
export function editProblem(edit, options = null) {
  const e = { ...DEFAULT_EDIT, ...(edit || {}) };
  if (e.durationMode === "original" && Number(e.speed) === speedRange(options).min && !e.sharpen) return NO_CHANGES_TEXT;
  return validateProcess({ ...e, mode: "edit" }, options);
}

/** Problema do fluxo escolhido para este tipo de mídia (null = pode enviar). */
export function flowProblem(form, media = null, options = null) {
  if (!PROCESSING_FLOWS.has(form.flow) || NO_EDIT_MEDIA.has(media)) return null;
  if (form.flow === "preset") return form.presetId ? null : "Escolha um preset de processamento.";
  return editProblem(form.edit, options);
}

/**
 * Fluxo após o download → campos do corpo do start. "Editar após baixar" vai como `post_processing: "edit"` +
 * `edit`; "Processar com preset", como `"preset"` + `preset_id`. Fotos e capas nunca são processadas.
 */
export function processingFields(form, media = null, options = null) {
  if (!PROCESSING_FLOWS.has(form.flow) || NO_EDIT_MEDIA.has(media)) return { post_processing: "none" };
  const problem = flowProblem(form, media, options);
  if (problem) throw new FormError(problem);
  if (form.flow === "preset") return { post_processing: "preset", preset_id: Number(form.presetId) };
  return { post_processing: "edit", edit: toEditSettings({ ...DEFAULT_EDIT, ...form.edit }) };
}

/** Resumo curto da edição: "Corte em 15 s · 1,05x · nitidez" (ou "Nenhuma edição selecionada."). */
export function editSummary(edit) {
  const e = { ...DEFAULT_EDIT, ...(edit || {}) };
  const parts = [];
  if (e.durationMode !== "original") {
    const limit = e.durationMode === "custom" ? parseDecimal(e.customText) : Number(e.durationMode);
    parts.push(limit ? `corte em ${String(limit).replace(".", ",")} s` : "duração personalizada");
  }
  if (Number(e.speed) !== 1) parts.push(formatSpeed(e.speed));
  if (e.sharpen) parts.push("nitidez");
  if (!parts.length) return NO_CHANGES_TEXT;
  const text = parts.join(" · ");
  return text[0].toUpperCase() + text.slice(1);
}

/** Configurações da API (`edit` do start) → formulário de edição. */
export function editFromSettings(settings) {
  if (!settings) return { ...DEFAULT_EDIT };
  return {
    durationMode: String(settings.duration_mode ?? "original"),
    customText: settings.custom_seconds === null || settings.custom_seconds === undefined ? ""
      : String(settings.custom_seconds).replace(".", ","),
    speed: Number(settings.speed ?? 1),
    sharpen: Boolean(settings.sharpen),
  };
}

function presetLabel(presetId, presetName, presets = []) {
  const found = (presets || []).find((p) => Number(p.id) === Number(presetId));
  const name = found?.name ?? presetName;
  return name ? `"${name}"` : `nº ${presetId}`;
}

/** O que acontece depois de baixar, numa linha (Importar e Página atual). */
export function flowSummary(form, options = null) {
  let text;
  if (form.flow === "edit") text = editSummary(form.edit);
  else if (form.flow === "preset") {
    text = form.presetId ? `Preset ${presetLabel(form.presetId, form.presetName, options?.presets)}`
      : "Nenhum preset escolhido";
  } else text = FLOW_OPTIONS.find((o) => o.value === form.flow)?.label ?? "Só baixar";
  if (form.autoExport) {
    const content = EXPORT_CONTENT_OPTIONS.find((o) => o.value === (form.autoExportContent || "originals"));
    text += ` · ZIP automático (${content?.label.toLowerCase() ?? "originais"})`;
  }
  return text;
}

/* ---------------- configuração salva (compatibilidade) ---------------- */

/** Campos do fluxo após o download, guardados em `importForm` (fonte única de Importar e Página atual). */
export const FLOW_FIELDS = ["flow", "edit", "presetId", "presetName", "autoExport", "autoExportContent"];

/** Configuração antiga da aba Processar (`processForm` ou `processStored`) → {mode, edit, presetId, presetName}. */
function legacyProcess(processForm, processStored) {
  if (processForm && typeof processForm === "object") {
    return {
      mode: processForm.mode === "preset" ? "preset" : "edit",
      edit: { durationMode: String(processForm.durationMode ?? "original"),
        customText: String(processForm.customText ?? ""), speed: Number(processForm.speed ?? 1),
        sharpen: Boolean(processForm.sharpen) },
      presetId: processForm.presetId ?? null,
      presetName: processForm.presetName ?? null,
    };
  }
  if (processStored && typeof processStored === "object") {
    return { mode: processStored.mode === "preset" ? "preset" : "edit", edit: editFromSettings(processStored.edit),
      presetId: processStored.presetId ?? null, presetName: null };
  }
  return null;
}

/**
 * Escolhas salvas por versões anteriores → formato atual. O antigo `flow: "auto"` ("Baixar e processar
 * automaticamente", que usava a configuração da aba Processar) vira "Editar após baixar" ou "Processar com preset"
 * conforme aquela configuração; sem `edit`/`presetId` salvos, eles vêm da aba Processar. Devolve {form, changed}.
 */
export function migrateFlowForm(stored, processForm = null, processStored = null) {
  const form = { ...(stored || {}) };
  const legacy = legacyProcess(processForm, processStored);
  let changed = false;
  if (form.flow === "auto") {
    form.flow = legacy?.mode === "preset" ? "preset" : "edit";
    changed = true;
  } else if (form.flow !== undefined && !FLOW_OPTIONS.some((o) => o.value === form.flow)) {
    form.flow = "download";
    changed = true;
  }
  if (!form.edit || typeof form.edit !== "object") {
    form.edit = legacy ? { ...legacy.edit } : { ...DEFAULT_EDIT };
    changed = true;
  } else {
    form.edit = { ...DEFAULT_EDIT, ...form.edit };
  }
  if (!("presetId" in form)) {
    form.presetId = legacy?.presetId ?? null;
    form.presetName = legacy?.presetName ?? null;
    changed = true;
  }
  return { form, changed };
}

/** Predefinição do usuário salva com o antigo `flow: "auto"` → "Editar após baixar" ou "Processar com preset". */
export function migrateUserPreset(preset, processForm = null, processStored = null) {
  if (preset?.values?.flow !== "auto") return preset;
  const { form } = migrateFlowForm({ flow: "auto" }, processForm, processStored);
  return { ...preset, values: { ...preset.values, flow: form.flow } };
}

export function autoExportField(form) {
  if (!form.autoExport) return null;
  const content = form.autoExportContent || "originals";
  if (!EXPORT_CONTENT_OPTIONS.some((o) => o.value === content)) throw new FormError("Conteúdo da exportação inválido.");
  if (content !== "originals" && !PROCESSING_FLOWS.has(form.flow)) {
    throw new FormError("Para exportar editados automaticamente, escolha \"Editar após baixar\" ou \"Processar com " +
      "preset\".");
  }
  if (content !== "originals" && (form.media === "covers" || form.media === "photos")) {
    throw new FormError("Capas e fotos não são editadas: exporte só os originais.");
  }
  return { content };
}

/**
 * Formulário da aba Importar → corpo de POST /api/ext/start.
 * form: {source: "profile"|"many", profile, text, media, limit, limitCustom, confirmAll, period, periodCustom,
 *        minViews, minViewsCustom, maxViews, maxViewsCustom, flow, edit, presetId, autoExport, autoExportContent}
 * flow: "download" | "edit" | "preset" | "manual". options: GET /api/ext/edit/options (limites da edição).
 */
export function buildStartBody(form, { now = new Date(), requestId = null, options = null } = {}) {
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
  Object.assign(body, processingFields(form, media, options));
  body.auto_export = autoExportField({ ...form, media });
  if (requestId) body.client_request_id = requestId;
  return body;
}

/* ---------------- Página atual: ações por tipo de publicação ---------------- */

/**
 * Ações de uma publicação conforme o tipo (kind do post-preview, ou o da URL antes da análise). Com "Editar após
 * baixar" ou "Processar com preset", o rótulo diz a ação completa (capas e fotos só são baixadas).
 */
export function postActions(kind, flow = "download") {
  const verb = flow === "edit" ? "editar" : flow === "preset" ? "processar" : null;
  const pick = (plain, processed) => (verb ? processed : plain);
  if (kind === "reel" || kind === "video") {
    return [
      // Num Reel "Vídeos e capas" e "Todas" dão o mesmo resultado: fica só "Baixar tudo".
      { media: "videos", label: pick("Baixar vídeo", `Baixar e ${verb} vídeo`), primary: true },
      { media: "covers", label: "Baixar capa" },
      { media: "all", label: pick("Baixar tudo (vídeo e capa)", `Baixar tudo e ${verb} (vídeo e capa)`) },
    ];
  }
  if (kind === "photo") return [{ media: "photos", label: "Baixar foto(s)", primary: true }];
  // carrossel, ou publicação /p/ ainda não analisada (tipo desconhecido): todas as opções.
  return [
    { media: "all", label: pick("Baixar todos os itens", `Baixar todos os itens e ${verb} os vídeos`), primary: true },
    { media: "photos", label: "Só fotos" },
    { media: "videos", label: pick("Só vídeos", `Só vídeos e ${verb}`) },
    { media: "covers", label: "Capas dos vídeos" },
    { media: "videos_covers", label: pick("Vídeos e capas", `Vídeos e capas (${verb} os vídeos)`) },
  ];
}

/** Corpo do start para uma publicação (um link), com o fluxo/exportação guardados (a mesma configuração de Importar). */
export function buildPostBody(canonicalUrl, media, flowForm = {}, requestId = null, options = null) {
  if (!canonicalUrl) throw new FormError("Link da publicação ausente.");
  if (!MEDIA_OPTIONS.some((o) => o.value === media)) throw new FormError("Ação inválida.");
  const form = { ...flowForm, flow: flowForm.flow || "download" };
  const body = { inputs: [canonicalUrl], media };
  Object.assign(body, processingFields(form, media, options));
  body.auto_export = autoExportField({ ...form, media });
  if (requestId) body.client_request_id = requestId;
  return body;
}

/* ---------------- predefinições de operação ---------------- */

export const DEFAULT_FORM = {
  source: "profile", profile: "", text: "", media: "videos", limit: "50", limitCustom: "", confirmAll: false,
  period: "any", periodCustom: "", minViews: "", minViewsCustom: "", maxViews: "", maxViewsCustom: "",
  flow: "download", edit: { ...DEFAULT_EDIT }, presetId: null, presetName: null, autoExport: false,
  autoExportContent: "originals",
};

/** Campos que uma predefinição guarda (nunca o perfil/links). */
export const PRESET_FIELDS = ["media", "limit", "limitCustom", "period", "periodCustom", "minViews", "minViewsCustom",
  "maxViews", "maxViewsCustom", "flow", "edit", "presetId", "presetName", "autoExport", "autoExportContent"];

export const BUILTIN_PRESETS = [
  { id: "builtin:download", name: "Somente baixar", builtin: true,
    values: { flow: "download", autoExport: false } },
  // "Baixar e editar" usa as últimas configurações de edição (a predefinição pronta não guarda `edit`).
  { id: "builtin:edit", name: "Baixar e editar", builtin: true,
    values: { flow: "edit", autoExport: false } },
  { id: "builtin:edit-export", name: "Baixar, editar e exportar ZIP", builtin: true,
    values: { flow: "edit", autoExport: true, autoExportContent: "both" } },
  { id: "builtin:process", name: "Baixar e processar", builtin: true,
    values: { flow: "preset", autoExport: false } },
  { id: "builtin:process-export", name: "Baixar, processar e exportar", builtin: true,
    values: { flow: "preset", autoExport: true, autoExportContent: "edited" } },
  { id: "builtin:all-raw", name: "Baixar tudo sem processamento", builtin: true,
    values: { media: "all", flow: "download", autoExport: false, minViews: "", maxViews: "" } },
];

export function pickPresetValues(form) {
  const values = {};
  for (const key of PRESET_FIELDS) if (key in form) values[key] = form[key];
  if (values.edit) values.edit = { ...values.edit };
  return values;
}

/** Aplica uma predefinição ao formulário (mantém origem/perfil/links e a confirmação de "Todas" desmarcada). */
export function applyPreset(form, preset) {
  const values = {};
  for (const key of PRESET_FIELDS) if (preset && preset.values && key in preset.values) values[key] = preset.values[key];
  if (values.edit) values.edit = { ...DEFAULT_EDIT, ...values.edit };
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

/** Resumo legível do que o start vai fazer (mostrado antes de enviar). presets: os de /edit/options (nomes). */
export function describeStartBody(body, presets = []) {
  const media = MEDIA_OPTIONS.find((o) => o.value === body.media)?.label ?? body.media;
  const parts = [media];
  if ("limit" in body) parts.push(body.limit === null ? "todas as publicações" : `até ${body.limit} por perfil`);
  if (body.posted_within_days) parts.push(`últimos ${body.posted_within_days} dias`);
  if (body.min_views) parts.push(`mín. ${viewsLabel(body.min_views)} visualizações`);
  if (body.max_views) parts.push(`máx. ${viewsLabel(body.max_views)} visualizações`);
  if (body.post_processing === "edit") {
    const text = editSummary(editFromSettings(body.edit));
    parts.push(`editar após baixar (${text[0].toLowerCase()}${text.slice(1)})`);
  }
  if (body.post_processing === "preset") parts.push(`processar com preset ${presetLabel(body.preset_id, null, presets)}`);
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
