// Regras puras da aba Processar: os mesmos controles da página Editar do app (duração, velocidade, nitidez,
// saída MP4), lidos de GET /api/ext/edit/options, e o corpo de POST /api/ext/edits ou /process.

export const FALLBACK_OPTIONS = {
  edit: {
    duration_modes: ["original", "15", "30", "60", "custom"],
    custom_seconds_max: 7200,
    speed: { min: 1.0, max: 1.5, step: 0.01, presets: [1, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.5] },
    sharpen: true,
    output: "mp4",
  },
  presets: [],
};

export const DURATION_MARGIN = 0.03;
export const DURATION_NOTE = "Com corte, a duração final fica em limite + 0,03 s; vídeos menores não são prolongados.";
export const SPEED_NOTE = "Vídeo e áudio ficam sincronizados.";
export const MAX_EDIT_ITEMS = 500;

const DURATION_LABELS = { original: "Original", 15: "15 s", 30: "30 s", 60: "60 s", custom: "Personalizada" };

export function durationOptions(options) {
  const modes = options?.edit?.duration_modes ?? FALLBACK_OPTIONS.edit.duration_modes;
  // O backend manda [{value, label}]; aceita também a forma simples ["original", "15", ...].
  return modes.map((mode) => (mode && typeof mode === "object"
    ? { value: String(mode.value), label: String(mode.label ?? DURATION_LABELS[mode.value] ?? mode.value) }
    : { value: String(mode), label: DURATION_LABELS[mode] ?? `${mode} s` }));
}

export function speedRange(options) {
  const s = { ...FALLBACK_OPTIONS.edit.speed, ...(options?.edit?.speed ?? {}) };
  return { min: Number(s.min), max: Number(s.max), step: Number(s.step), presets: s.presets.map(Number) };
}

export const DEFAULT_PROCESS = {
  mode: "edit", durationMode: "original", customText: "", speed: 1.0, sharpen: false, presetId: null,
};

/** "1,25" / "1.25" / "1,25x" → número, ou null. */
export function parseDecimal(text) {
  const clean = String(text ?? "").trim().replace(/x$/i, "").trim().replace(",", ".");
  if (!/^\d+(\.\d+)?$/.test(clean)) return null;
  const value = Number(clean);
  return Number.isFinite(value) ? value : null;
}

export function snapSpeed(value, range = speedRange(null)) {
  if (!Number.isFinite(value)) return range.min;
  const clamped = Math.min(range.max, Math.max(range.min, value));
  const steps = Math.round((clamped - range.min) / range.step);
  return Math.min(range.max, Number((range.min + steps * range.step).toFixed(2)));
}

/** Texto digitado → velocidade no passo, ou null se inválido/fora da faixa. */
export function parseSpeedInput(text, range = speedRange(null)) {
  const value = parseDecimal(text);
  if (value === null || value < range.min || value > range.max) return null;
  return snapSpeed(value, range);
}

export function stepSpeed(speed, direction, range = speedRange(null)) {
  return snapSpeed(Number(speed) + direction * range.step, range);
}

export function formatSpeed(speed) {
  return `${Number(speed).toFixed(2).replace(".", ",")}x`;
}

export function formatSpeedPreset(speed) {
  return `${String(speed).replace(".", ",")}x`;
}

/** Primeiro problema do formulário, ou null. */
export function validateProcess(form, options = null) {
  if (form.mode === "preset") return form.presetId ? null : "Escolha um preset de processamento.";
  const range = speedRange(options);
  if (!Number.isFinite(form.speed) || form.speed < range.min || form.speed > range.max) {
    return `A velocidade deve ficar entre ${formatSpeed(range.min)} e ${formatSpeed(range.max)}.`;
  }
  if (form.durationMode === "custom") {
    const max = options?.edit?.custom_seconds_max ?? 7200;
    const value = parseDecimal(form.customText);
    if (value === null) return "Informe a duração personalizada em segundos.";
    if (value <= 0) return "A duração personalizada deve ser maior que zero.";
    if (value > max) return `A duração personalizada pode ter no máximo ${max} segundos.`;
  }
  return null;
}

/** Bloco `settings` de POST /api/edits (saída sempre MP4, como a tela Editar). */
export function toEditSettings(form) {
  return {
    duration_mode: form.durationMode,
    custom_seconds: form.durationMode === "custom" ? parseDecimal(form.customText) : null,
    speed: form.speed,
    sharpen: Boolean(form.sharpen),
    output_type: "mp4",
  };
}

/** Configuração guardada para o "processar automaticamente" da importação (ou null se inválida). */
export function toStoredProcess(form, options = null) {
  if (validateProcess(form, options)) return null;
  return form.mode === "preset"
    ? { mode: "preset", presetId: Number(form.presetId) }
    : { mode: "edit", edit: toEditSettings(form) };
}

export function processSummary(form, options = null) {
  if (form.mode === "preset") {
    const preset = (options?.presets ?? []).find((p) => Number(p.id) === Number(form.presetId));
    if (preset) return `Preset "${preset.name}"${preset.summary ? `: ${preset.summary}` : ""}`;
    return form.presetId && form.presetName ? `Preset "${form.presetName}"` : "Nenhum preset escolhido";
  }
  const parts = [];
  if (form.durationMode === "original") parts.push("duração original");
  else {
    const limit = form.durationMode === "custom" ? parseDecimal(form.customText) : Number(form.durationMode);
    parts.push(limit ? `corte em ${String(limit).replace(".", ",")} s (+0,03 s)` : "duração personalizada");
  }
  parts.push(`velocidade ${formatSpeed(form.speed)}`);
  parts.push(form.sharpen ? "nitidez sutil" : "sem nitidez");
  parts.push("saída MP4");
  return parts.join(" · ");
}

/** Divide a seleção: vídeos vão para a edição; o resto é ignorado (com contagem para o aviso). */
export function splitForEdit(entries) {
  const videos = [];
  let ignored = 0;
  for (const [id, meta] of entries) {
    if (meta && meta.media_type === "video" && meta.role !== "cover") videos.push(Number(id));
    else if (meta && meta.media_type) ignored += 1;
    else videos.push(Number(id)); // tipo desconhecido: o backend decide e devolve em `skipped`
  }
  return { videos, ignored };
}

/**
 * Caminho da página Editar do app com os vídeos da seleção (`/editar?ids=1,2,3`, até 500, na ordem); sem vídeos:
 * `/editar`. O app lê o `?ids=`, adiciona só os vídeos e limpa o parâmetro.
 */
export function editPagePath(videoIds) {
  const ids = [];
  for (const raw of videoIds || []) {
    const id = Number(raw);
    if (Number.isSafeInteger(id) && id > 0 && !ids.includes(id)) ids.push(id);
    if (ids.length >= MAX_EDIT_ITEMS) break;
  }
  return ids.length ? `/editar?ids=${ids.join(",")}` : "/editar";
}

/** Corpo do pedido: {route: "edits"|"process", body}. */
export function buildProcessRequest(form, mediaIds) {
  if (!mediaIds.length) throw new Error("Nenhum vídeo selecionado.");
  if (mediaIds.length > MAX_EDIT_ITEMS) throw new Error(`Máximo de ${MAX_EDIT_ITEMS} vídeos por lote.`);
  if (form.mode === "preset") {
    return { route: "process", body: { media_ids: mediaIds, preset_id: Number(form.presetId) } };
  }
  return { route: "edits", body: { media_ids: mediaIds, settings: toEditSettings(form) } };
}
