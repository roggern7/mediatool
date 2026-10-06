import { useEffect, useState } from "react";

export type PauseReason = "rate_limited" | "action_blocked" | "checkpoint_required" | "session_invalid" | "auth_check";
export type PauseScope = "session" | "instagram";

const REASON_LABEL: Record<PauseReason, string> = {
  rate_limited: "limite de requisições",
  action_blocked: "proteção do Media Tool após aviso de restrição",
  checkpoint_required: "o Instagram pediu uma verificação; conclua no navegador e depois libere a pausa",
  session_invalid: "Sessão precisa ser verificada",
  auth_check: "Verificando acesso ao Instagram",
};

const SCOPE_TEXT: Record<PauseScope, string> = {
  session: "Downloads e processamento continuam; novas consultas ao Instagram aguardam",
  instagram: "Processamento continua; downloads e consultas ao Instagram aguardam",
};

const ACTION_HINT: Partial<Record<PauseReason, string>> = {
  checkpoint_required: "conclua a verificação no Instagram pelo navegador e depois libere a pausa",
  session_invalid: "teste a sessão ou salve a sessão de novo em Configurações",
};

/** O que continua e o que aguarda durante a pausa. */
export function pauseScopeText(scope: string | null | undefined): string | null {
  if (!scope) return null;
  return SCOPE_TEXT[scope as PauseScope] ?? null;
}

/** "Nova verificação automática às HH:MM" (ou null sem horário). */
export function nextCheckText(nextAttemptAt: string | null | undefined, now: number): string | null {
  if (!nextAttemptAt) return null;
  const t = Date.parse(nextAttemptAt);
  if (!Number.isFinite(t)) return null;
  if (t <= now) return "Verificação automática em andamento";
  return `Nova verificação automática às ${formatPauseTime(nextAttemptAt, now)}`;
}

/** "Ação necessária: ..." para checkpoint/sessão inválida; null quando a pausa se resolve sozinha. */
export function actionRequiredText(
  actionRequired: boolean | null | undefined,
  reason: string | null | undefined,
): string | null {
  if (!actionRequired) return null;
  const hint = ACTION_HINT[(reason ?? "") as PauseReason];
  return hint ? `Ação necessária: ${hint}` : "Ação necessária";
}

export function pauseReasonLabel(reason: string | null | undefined): string | null {
  if (!reason) return null;
  return REASON_LABEL[reason as PauseReason] ?? reason;
}

export function isPaused(until: string | null | undefined, now: number): boolean {
  if (!until) return false;
  const t = Date.parse(until);
  return Number.isFinite(t) && t > now;
}

/** Horário local: "14:30" no mesmo dia, "04/10 14:30" em outro dia. */
export function formatPauseTime(until: string, now: number): string {
  const d = new Date(until);
  const n = new Date(now);
  const time = d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  const sameDay = d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  if (sameDay) return time;
  return `${d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" })} ${time}`;
}

export function pauseText(until: string, reason: string | null | undefined, now: number): string {
  const label = pauseReasonLabel(reason);
  return `Instagram em pausa até ${formatPauseTime(until, now)}${label ? ` (${label})` : ""}`;
}

/** Relógio que avança a cada `ms` (para expirar a pausa sem recarregar). */
export function useNow(ms = 15_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(timer);
  }, [ms]);
  return now;
}
