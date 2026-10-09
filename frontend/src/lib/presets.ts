import type { PresetParams } from "../api/types";
import { parseDecimal, toDecimalInput } from "./format";

/** Preset novo: equivalente ao "Padrão" (ajustes opcionais desligados, por isso ausentes). */
export const DEFAULT_PARAMS: PresetParams = {
  metadata_cleanup: true,
  trim_start: 0.5,
  mirror: true,
  speed: 1.05,
  auto_adjust: true,
  reencode: true,
};

export const TRIM_STEPS = [0, 0.2, 0.5, 1.0, 2.0] as const;
export const SPEED_STEPS = [1.0, 1.02, 1.05, 1.1] as const;
export const TRIM_MIN = 0;
export const TRIM_MAX = 30;
export const SPEED_MIN = 0.5;
export const SPEED_MAX = 2.0;
export const NAME_MAX = 60;

export type OptionalKey = "zoom" | "brightness" | "contrast" | "saturation" | "trim_end" | "pitch_shift" | "grain";

export interface OptionalAdjustment {
  key: OptionalKey;
  label: string;
  /** Valor que equivale a desligado; não é gravado no preset. */
  neutral: number;
  min: number;
  max: number;
  /** Valor sugerido ao ligar o ajuste. */
  suggested: number;
  unit: string;
  hint?: string;
  error: string;
}

/** Ajustes opcionais do preset, na ordem da tela. Desligado = valor neutro = chave ausente nos params. */
export const OPTIONAL_ADJUSTMENTS: readonly OptionalAdjustment[] = [
  { key: "zoom", label: "Zoom central", neutral: 0, min: 0, max: 10, suggested: 3, unit: "%",
    error: "Use um valor entre 0 e 10%." },
  { key: "brightness", label: "Brilho", neutral: 0, min: -0.1, max: 0.1, suggested: 0.01, unit: "",
    error: "Use um valor entre -0,1 e 0,1." },
  { key: "contrast", label: "Contraste", neutral: 1, min: 0.9, max: 1.1, suggested: 1.02, unit: "x",
    error: "Use um valor entre 0,9 e 1,1." },
  { key: "saturation", label: "Saturação", neutral: 1, min: 0.8, max: 1.2, suggested: 1.03, unit: "x",
    error: "Use um valor entre 0,8 e 1,2." },
  { key: "trim_end", label: "Cortar final", neutral: 0, min: 0, max: 30, suggested: 0.3, unit: "s",
    hint: "Ignorado em vídeos curtos (quando sobraria menos de 1 s).",
    error: "Use um valor entre 0 e 30 segundos." },
  { key: "pitch_shift", label: "Tom do áudio", neutral: 0, min: -3, max: 3, suggested: 1, unit: "%",
    error: "Use um valor entre -3 e 3%." },
  { key: "grain", label: "Granulação", neutral: 0, min: 0, max: 4, suggested: 2, unit: "",
    error: "Use um valor entre 0 e 4." },
];

/** Valor do ajuste opcional; ausente (preset antigo ou ajuste desligado) = neutro. */
export function optionalValue(params: PresetParams, adjustment: OptionalAdjustment): number {
  return params[adjustment.key] ?? adjustment.neutral;
}

export function isOptionalActive(params: PresetParams, adjustment: OptionalAdjustment): boolean {
  return optionalValue(params, adjustment) !== adjustment.neutral;
}

/** Params como são gravados: ajustes opcionais neutros ficam de fora (o "Padrão" continua com as 6 chaves). */
export function compactParams(params: PresetParams): PresetParams {
  const out: PresetParams = { ...params };
  for (const adjustment of OPTIONAL_ADJUSTMENTS) {
    if (!isOptionalActive(params, adjustment)) delete out[adjustment.key];
  }
  return out;
}

export type PresetErrors = Partial<Record<"name" | "trim_start" | "speed" | OptionalKey, string>>;

export function validatePreset(name: string, params: PresetParams): PresetErrors {
  const errors: PresetErrors = {};
  if (!name.trim()) errors.name = "Informe um nome.";
  else if (name.trim().length > NAME_MAX) errors.name = `Use no máximo ${NAME_MAX} caracteres.`;
  if (!Number.isFinite(params.trim_start) || params.trim_start < TRIM_MIN || params.trim_start > TRIM_MAX) {
    errors.trim_start = "Use um valor entre 0 e 30 segundos.";
  }
  if (!Number.isFinite(params.speed) || params.speed < SPEED_MIN || params.speed > SPEED_MAX) {
    errors.speed = "Use um valor entre 0,5 e 2,0.";
  }
  for (const adjustment of OPTIONAL_ADJUSTMENTS) {
    const value = optionalValue(params, adjustment);
    if (!Number.isFinite(value) || value < adjustment.min || value > adjustment.max) {
      errors[adjustment.key] = adjustment.error;
    }
  }
  return errors;
}

/** Há transformação que exige reencode (o processor força reencode nesses casos). */
export function hasActiveTransforms(params: PresetParams): boolean {
  return (
    params.trim_start > 0 ||
    params.mirror ||
    params.speed !== 1 ||
    params.auto_adjust ||
    OPTIONAL_ADJUSTMENTS.some((adjustment) => isOptionalActive(params, adjustment))
  );
}

export function forcesReencode(params: PresetParams): boolean {
  return !params.reencode && hasActiveTransforms(params);
}

const summaryFmt = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2 });

/** Trechos do resumo do preset para os ajustes opcionais ligados (nenhum em presets antigos). */
export function optionalSummary(params: PresetParams): string[] {
  const parts: string[] = [];
  const on = (key: OptionalKey) => {
    const adjustment = OPTIONAL_ADJUSTMENTS.find((a) => a.key === key);
    return adjustment !== undefined && isOptionalActive(params, adjustment);
  };
  if (on("zoom")) parts.push(`Zoom ${summaryFmt.format(params.zoom ?? 0)}%`);
  if (on("brightness") || on("contrast") || on("saturation")) parts.push("Cor");
  if (on("trim_end")) parts.push(`Corta ${summaryFmt.format(params.trim_end ?? 0)} s do final`);
  if (on("pitch_shift")) {
    const pitch = params.pitch_shift ?? 0;
    parts.push(`Tom ${pitch > 0 ? "+" : ""}${summaryFmt.format(pitch)}%`);
  }
  if (on("grain")) parts.push("Granulação");
  return parts;
}

/** Estado de um ajuste opcional no editor: ligado/desligado e o texto do campo. */
export interface OptionalField {
  on: boolean;
  text: string;
}
export type OptionalState = Record<OptionalKey, OptionalField>;

/**
 * Estado inicial do editor: ligado só o que o preset traz com valor não neutro (preset antigo, sem as chaves, começa
 * com todos desligados). Desligado, o campo já fica com o valor sugerido para quando for ligado.
 */
export function initialOptionalState(params: PresetParams): OptionalState {
  const state = {} as OptionalState;
  for (const adjustment of OPTIONAL_ADJUSTMENTS) {
    const on = isOptionalActive(params, adjustment);
    state[adjustment.key] = {
      on,
      text: toDecimalInput(on ? optionalValue(params, adjustment) : adjustment.suggested),
    };
  }
  return state;
}

export type PresetFlags = Pick<PresetParams, "metadata_cleanup" | "mirror" | "auto_adjust" | "reencode">;

/**
 * Params do editor a partir dos campos: texto inválido vira NaN (a validação acusa) e ajuste desligado fica ausente.
 * O corpo enviado à API é `compactParams` disto.
 */
export function editorParams(
  flags: PresetFlags,
  trimText: string,
  speedText: string,
  optional: OptionalState,
): PresetParams {
  const params: PresetParams = {
    ...flags,
    trim_start: parseDecimal(trimText) ?? Number.NaN,
    speed: parseDecimal(speedText) ?? Number.NaN,
  };
  for (const adjustment of OPTIONAL_ADJUSTMENTS) {
    const state = optional[adjustment.key];
    if (state.on) params[adjustment.key] = parseDecimal(state.text) ?? Number.NaN;
  }
  return params;
}
