// Erro da API → estado da tela e política de nova tentativa (401 / sem conexão / 429).
import { FormError } from "./importModel.js";

/** {state: "offline"|"unpaired"|"revoked"|"rate_limited"|"error", message, retryMs}. */
export function describeError(err) {
  if (err instanceof FormError) return { state: "error", message: err.message, retryMs: null };
  const kind = err && err.kind;
  if (kind === "offline" || kind === "timeout") return { state: "offline", message: err.message, retryMs: 5_000 };
  if (kind === "unauthorized") {
    return { state: err.code ? "revoked" : "unpaired", message: err.message, retryMs: null };
  }
  if (kind === "rate_limited") {
    const seconds = Number(err.retryAfter) > 0 ? Math.min(120, Number(err.retryAfter)) : 10;
    return { state: "rate_limited", message: err.message, retryMs: seconds * 1000 };
  }
  return { state: "error", message: (err && err.message) || "Erro inesperado.", retryMs: null };
}

/** Estados que bloqueiam a interface inteira (o resto vira mensagem na própria aba). */
export function isBlocking(state) {
  return state === "offline" || state === "unpaired" || state === "revoked";
}
