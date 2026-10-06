// Intervalo de atualização adaptativo: rápido com atividade, cada vez mais espaçado quando ocioso.

export const POLL_STEPS_MS = [2_000, 5_000, 15_000, 30_000] as const;

const FAST_MS = POLL_STEPS_MS[0];
const FIRST_IDLE_MS = POLL_STEPS_MS[1];
const MAX_MS = POLL_STEPS_MS[3];

/** Próximo intervalo: 2 s com itens ativos; ocioso sobe 5 s, 15 s, 30 s e fica em 30 s. */
export function nextPollMs(current: number | null, active: boolean): number {
  if (active) return FAST_MS;
  const base = current ?? 0;
  return POLL_STEPS_MS.find((step) => step > base && step >= FIRST_IDLE_MS) ?? MAX_MS;
}
