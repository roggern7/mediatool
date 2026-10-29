import type { EditDurationMode, EditOutputType, EditRequest, Media } from "../api/types";
import { formatSeconds, parseDecimal } from "./format";

// Configurações da página Editar (POST /api/edits): padrões, validação, pedido e duração prevista.

/** Margem somada ao limite pelo backend: com corte, a saída fica em limite + 0,03 s. */
export const EDIT_DURATION_MARGIN = 0.03;
/** Mesmo teto do backend (media_ids 1..500). */
export const MAX_EDIT_ITEMS = 500;
export const SPEED_MIN = 1.0;
export const SPEED_MAX = 1.5;
export const SPEED_STEP = 0.05;
export const CUSTOM_SECONDS_MAX = 7200;

export const MAX_ITEMS_TEXT = "Máximo de 500 vídeos por lote.";
export const DURATION_NOTE =
  "Com corte, a duração final fica em limite + 0,03 s; vídeos menores não são prolongados.";
export const SPEED_NOTE = "Vídeo e áudio ficam sincronizados.";
export const SHARPEN_LABEL = "Nitidez sutil";
export const SHARPEN_DESCRIPTION = "Realce leve para melhorar a aparência do vídeo.";
export const AUTO_SAVE_TEXT =
  "As versões editadas são salvas automaticamente na biblioteca como versões do vídeo original; os originais nunca são alterados.";
/** As versões editadas saem pelo painel do lote (ZIP ou pasta, `content: "edited"`), não por exportação automática. */
export const EXPORT_AFTER_HINT = "Ao terminar, baixe o ZIP das versões editadas no painel do lote.";
export const OUTPUT_AUTO_NOTE = "Corte, velocidade ou nitidez sempre recodificam o vídeo.";
export const NO_CHANGES_TEXT = "Nenhuma edição selecionada.";

export const OUTPUT_OPTIONS: { value: EditOutputType; label: string }[] = [
  { value: "mp4", label: "MP4 (H.264 + AAC)" },
  { value: "auto", label: "Automático: copiar sem recodificar quando possível" },
];

export const DURATION_OPTIONS: { value: EditDurationMode; label: string }[] = [
  { value: "original", label: "Original" },
  { value: "15", label: "15 s" },
  { value: "30", label: "30 s" },
  { value: "60", label: "60 s" },
  { value: "custom", label: "Personalizada" },
];

/** Estado do formulário; `customText` é o texto digitado (vírgula ou ponto). */
export interface EditForm {
  outputType: EditOutputType;
  durationMode: EditDurationMode;
  customText: string;
  speed: number;
  sharpen: boolean;
}

export const DEFAULT_EDIT_FORM: EditForm = {
  outputType: "mp4",
  durationMode: "original",
  customText: "",
  speed: 1.0,
  sharpen: false,
};

/** Velocidade presa à faixa 1,0x a 1,5x e ao passo de 0,05. */
export function snapSpeed(value: number): number {
  if (!Number.isFinite(value)) return SPEED_MIN;
  const clamped = Math.min(SPEED_MAX, Math.max(SPEED_MIN, value));
  const steps = Math.round((clamped - SPEED_MIN) / SPEED_STEP);
  return Math.min(SPEED_MAX, Number((SPEED_MIN + steps * SPEED_STEP).toFixed(2)));
}

/** Velocidade como "1,25x" (duas casas, vírgula decimal). */
export function formatEditSpeed(speed: number): string {
  return `${speed.toFixed(2).replace(".", ",")}x`;
}

/** Limite escolhido em segundos (sem a margem), ou null em "Original" ou personalizado inválido. */
export function durationLimit(form: EditForm): number | null {
  switch (form.durationMode) {
    case "original":
      return null;
    case "custom": {
      const value = parseDecimal(form.customText);
      return value !== null && value > 0 && value <= CUSTOM_SECONDS_MAX ? value : null;
    }
    default:
      return Number(form.durationMode);
  }
}

/** Nenhuma edição: tipo Automático, duração original, 1,0x e sem nitidez (o backend recusa com edit_no_changes). */
export function isNoChange(form: EditForm): boolean {
  return form.outputType === "auto" && form.durationMode === "original" && form.speed === SPEED_MIN && !form.sharpen;
}

/** Mensagem do primeiro problema do formulário, ou null se pode enviar. */
export function validateEditForm(form: EditForm): string | null {
  if (!Number.isFinite(form.speed) || form.speed < SPEED_MIN || form.speed > SPEED_MAX) {
    return "A velocidade deve ficar entre 1,0x e 1,5x.";
  }
  if (form.durationMode === "custom") {
    const value = parseDecimal(form.customText);
    if (value === null) return "Informe a duração personalizada em segundos.";
    if (value <= 0) return "A duração personalizada deve ser maior que zero.";
    if (value > CUSTOM_SECONDS_MAX) return "A duração personalizada pode ter no máximo 7200 segundos.";
  }
  if (isNoChange(form)) return NO_CHANGES_TEXT;
  return null;
}

/** Corpo de POST /api/edits. Só chame com o formulário válido. */
export function toEditRequest(mediaIds: number[], form: EditForm): EditRequest {
  return {
    media_ids: mediaIds,
    settings: {
      duration_mode: form.durationMode,
      custom_seconds: form.durationMode === "custom" ? durationLimit(form) : null,
      speed: form.speed,
      sharpen: form.sharpen,
      output_type: form.outputType,
    },
  };
}

/** Duração prevista da saída: duração / velocidade, cortada em limite + 0,03 s quando passar disso. */
export function predictedDuration(duration: number | null | undefined, form: EditForm): number | null {
  if (duration === null || duration === undefined || !Number.isFinite(duration) || duration <= 0) return null;
  const speed = Number.isFinite(form.speed) && form.speed > 0 ? form.speed : SPEED_MIN;
  const natural = duration / speed;
  const limit = durationLimit(form);
  if (limit === null) return natural;
  const target = limit + EDIT_DURATION_MARGIN;
  return natural > target ? target : natural;
}

/** Duração curta em segundos com vírgula ("15,03 s"); "-" quando desconhecida. */
export function formatEditDuration(seconds: number | null): string {
  return seconds === null ? "-" : formatSeconds(Math.round(seconds * 100) / 100);
}

/**
 * Junta itens à seleção (só vídeos, sem repetir, até MAX_EDIT_ITEMS). Devolve a nova seleção, quantos entraram e
 * quantos ficaram de fora pelo teto.
 */
export function mergeSelection(
  current: Media[],
  incoming: Media[],
  max = MAX_EDIT_ITEMS,
): { selection: Media[]; added: number; overflow: number } {
  const seen = new Set(current.map((m) => m.id));
  const selection = [...current];
  let added = 0;
  let overflow = 0;
  for (const media of incoming) {
    if (media.media_type !== "video" || seen.has(media.id)) continue;
    if (selection.length >= max) {
      overflow += 1;
      continue;
    }
    seen.add(media.id);
    selection.push(media);
    added += 1;
  }
  return { selection, added, overflow };
}

/** Nome para exibir: arquivo enviado, perfil de origem ou "Vídeo <id>". */
export function mediaName(m: Pick<Media, "id" | "original_filename" | "source_profile">): string {
  return m.original_filename ?? (m.source_profile ? `@${m.source_profile}` : `Vídeo ${m.id}`);
}

/** Rótulo do botão de iniciar. */
export function startLabel(count: number): string {
  return `Iniciar edição (${count} ${count === 1 ? "vídeo" : "vídeos"})`;
}

/**
 * Chave da consulta da lista "Vídeos da biblioteca". `version` sobe a cada envio local pronto (novo ou duplicado):
 * a lista recarrega na mesma busca e página, mantendo os dados atuais na tela até a resposta chegar.
 */
export function pickerQueryKey(q: string, cursor: number | null, version: number): string {
  return `edit-videos:${q}:${cursor ?? ""}:${version}`;
}
