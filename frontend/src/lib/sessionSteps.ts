import type { SessionTest } from "../api/types";
import { sessionResult } from "./sessionTest";

export type StepState = "ok" | "failed" | "untested" | "other_account" | "unconfirmed";

export interface SessionStep {
  label: string;
  state: StepState;
}

/** Aceita bool ou "ok"/"failed"/"unknown"; ausente ou desconhecido vira "não testado". */
export function stepState(value: unknown): StepState {
  if (value === true || value === "ok") return "ok";
  if (value === false || value === "failed" || value === "refused" || value === "rate_limited") return "failed";
  return "untested";
}

export const STEP_STATE_LABEL: Record<StepState, string> = {
  ok: "ok",
  failed: "falhou",
  untested: "não testado",
  other_account: "outra conta",
  unconfirmed: "não confirmado",
};

/** true: ok; false com sessão válida: outra conta; false: falhou; null com consulta ok: não confirmado. */
function recognizedState(test: SessionTest): StepState {
  const v = test.session_recognized;
  if (v === true) return "ok";
  if (v === false) return sessionResult(test) === "valid" ? "other_account" : "failed";
  if (test.graphql === "ok") return "unconfirmed";
  return "untested";
}

/** Etapas do teste em linguagem simples; vazio quando o registro não traz as etapas web (antigo ou gallery-dl). */
export function sessionSteps(test: SessionTest): SessionStep[] {
  if (test.session_present == null && test.bootstrap == null && test.graphql == null) return [];
  const raw: [string, unknown][] = [
    ["Sessão salva", test.session_present],
    ["Reconhecida pelo Instagram", test.session_recognized],
    ["Acesso web preparado", test.bootstrap],
    ["Consulta de publicações", test.graphql],
    ["Listagem de perfil", test.listing_api],
  ];
  return raw.map(([label, v]) => ({
    label,
    state: label === "Reconhecida pelo Instagram" ? recognizedState(test) : stepState(v),
  }));
}
