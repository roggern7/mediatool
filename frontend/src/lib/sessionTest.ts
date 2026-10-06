import type { ListingApiState, SessionTest, SessionTestResult } from "../api/types";
import type { Tone } from "./labels";

const LABEL: Record<SessionTestResult, string> = {
  valid: "Válida",
  invalid: "Inválida",
  expired: "Expirada",
  checkpoint_required: "Verificação de segurança necessária",
  rate_limited: "Limite de requisições",
  restricted: "Aviso de restrição do Instagram",
  api_refused: "Sessão reconhecida; listagem recusada",
  timeout: "Tempo esgotado",
  network_error: "Erro de rede",
  instagram_error: "Erro do Instagram",
  session_missing: "Sessão não configurada",
};

const TONE: Record<SessionTestResult, Tone> = {
  valid: "ok",
  invalid: "err",
  expired: "err",
  checkpoint_required: "err",
  rate_limited: "warn",
  restricted: "err",
  api_refused: "warn",
  timeout: "warn",
  network_error: "warn",
  instagram_error: "err",
  session_missing: "muted",
};

const HINT: Partial<Record<SessionTestResult, string>> = {
  invalid: "Atualize a sessão com um sessionid novo.",
  expired: "Atualize a sessão com um sessionid novo.",
  checkpoint_required:
    "Abra o Instagram no navegador, conclua a verificação de segurança e depois teste de novo ou atualize a sessão.",
  rate_limited: "Aguarde alguns minutos antes de testar de novo.",
  restricted:
    "Como proteção, o Media Tool pausa o acesso automático ao Instagram por 1 hora; a pausa pode ser liberada em Configurações.",
  timeout: "O Instagram não respondeu a tempo. Tente de novo em instantes.",
  network_error: "Verifique a conexão com a internet e tente de novo.",
};

const FROM_CODE: Record<string, SessionTestResult> = {
  ok: "valid",
  valid: "valid",
  session_invalid: "invalid",
  invalid: "invalid",
  session_expired: "expired",
  expired: "expired",
  checkpoint_required: "checkpoint_required",
  rate_limited: "rate_limited",
  restricted: "restricted",
  action_blocked: "restricted",
  api_refused: "api_refused",
  access_refused: "api_refused",
  timeout: "timeout",
  network_error: "network_error",
  session_missing: "session_missing",
  instagram_error: "instagram_error",
};

/** Resultado normalizado; usa `code` quando `result` não vier. */
export function sessionResult(test: SessionTest): SessionTestResult {
  if (test.result && test.result in LABEL) return test.result;
  if (test.ok) return "valid";
  return FROM_CODE[test.code] ?? "instagram_error";
}

export interface SessionTestView {
  result: SessionTestResult;
  label: string;
  tone: Tone;
  hint: string | null;
  /** "Sessão: reconhecida/não reconhecida/desconhecida". */
  session: string;
  /** "Listagem: funcionando/recusada/limitada/desconhecida". */
  listing: string;
}

const LISTING: Record<ListingApiState, string> = {
  ok: "funcionando",
  refused: "recusada",
  rate_limited: "limitada",
  unknown: "desconhecida",
};

export function sessionRecognizedLabel(value: boolean | null | undefined): string {
  if (value === true) return "Sessão: reconhecida";
  if (value === false) return "Sessão: não reconhecida";
  return "Sessão: desconhecida";
}

export function listingApiLabel(value: ListingApiState | null | undefined): string {
  return `Listagem: ${(value && LISTING[value]) || LISTING.unknown}`;
}

export function describeSessionTest(test: SessionTest): SessionTestView {
  const result = sessionResult(test);
  const base = LABEL[result];
  let label = result === "valid" && test.username ? `${base} (@${test.username})` : base;
  let tone = TONE[result];
  if (result === "valid" && test.session_recognized === false) {
    label = "Funcionando com outra conta";
    tone = "warn";
  } else if (result === "valid" && test.session_recognized == null && test.graphql === "ok") {
    label = "Funcionando, conta não confirmada";
    tone = "warn";
  }
  return {
    result,
    label,
    tone,
    hint: HINT[result] ?? null,
    session: sessionRecognizedLabel(test.session_recognized),
    listing: listingApiLabel(test.listing_api),
  };
}

/** Duração em pt-BR: "em 2,3 s" ou "em 850 ms". */
export function formatTestDuration(ms: number | null | undefined): string | null {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `em ${Math.round(ms)} ms`;
  const fmt = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  return `em ${fmt.format(ms / 1000)} s`;
}
