import type { ContentType, ImportRequest } from "../api/types";

/** Padrão da importação: somente baixar os originais. Não é salvo entre sessões. */
export const PROCESS_AFTER_DOWNLOAD_DEFAULT = false;

/**
 * Corpo de POST /api/imports. Sempre manda `process` e `preset_id` explícitos:
 * download puro é `process: false, preset_id: null`, mesmo com um preset escolhido na tela.
 */
export function buildImportRequest(input: {
  profile: string;
  content: ContentType;
  limitIndex: number;
  processAfterDownload?: boolean;
  presetId?: number | null;
}): ImportRequest {
  const process = input.processAfterDownload ?? PROCESS_AFTER_DOWNLOAD_DEFAULT;
  return {
    profile: input.profile,
    content_type: input.content,
    limit: limitAt(input.limitIndex),
    preset_id: process ? (input.presetId ?? null) : null,
    process,
  };
}

/** Só pode enviar com processamento ligado se houver preset; download puro sempre pode. */
export function canSubmitImport(processAfterDownload: boolean, presetId: number | null): boolean {
  return !processAfterDownload || presetId !== null;
}

export const LIMIT_OPTIONS: { value: ImportRequest["limit"]; label: string; disabled?: boolean }[] = [
  { value: 10, label: "Últimos 10" },
  { value: 20, label: "Últimos 20" },
  { value: 50, label: "Últimos 50" },
  { value: 100, label: "Últimos 100" },
  // "Todos" fica visível, mas indisponível até ter política própria (a API responde 422 limit_all_disabled).
  { value: null, label: "Todos (indisponível por enquanto)", disabled: true },
];

/** Limite enviado à API para a opção escolhida; opção inexistente ou indisponível vira 10. */
export function limitAt(index: number): ImportRequest["limit"] {
  const option = LIMIT_OPTIONS[index];
  return option === undefined || option.disabled ? 10 : option.value;
}
