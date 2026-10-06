import type { PresetParams } from "../api/types";

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

export type PresetErrors = Partial<Record<"name" | "trim_start" | "speed", string>>;

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
  return errors;
}

/** Há transformação que exige reencode (o processor força reencode nesses casos). */
export function hasActiveTransforms(params: PresetParams): boolean {
  return params.trim_start > 0 || params.mirror || params.speed !== 1 || params.auto_adjust;
}

export function forcesReencode(params: PresetParams): boolean {
  return !params.reencode && hasActiveTransforms(params);
}
