import { useCallback, useEffect, useEffectEvent, useState } from "react";
import { messageOf } from "../api/client";

interface QueryState<T> {
  key: string;
  tick: number;
  data: T | undefined;
  error: string | null;
}

export interface QueryResult<T> {
  data: T | undefined;
  error: string | null;
  /** Há requisição em andamento para a chave/tick atuais. */
  loading: boolean;
  reload: () => void;
}

/**
 * Carrega dados para uma chave. Mantém o último dado enquanto recarrega.
 * Com `pollMs`, agenda a próxima carga só depois que a anterior termina. Mudar `pollMs`
 * só reagenda a próxima carga (não carrega de novo na hora). Com a aba oculta, o polling pausa.
 */
export function useQuery<T>(key: string, load: () => Promise<T>, pollMs: number | null = null): QueryResult<T> {
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<QueryState<T>>({ key: "", tick: -1, data: undefined, error: null });
  const runLoad = useEffectEvent(() => load());

  useEffect(() => {
    let alive = true;
    runLoad().then(
      (data) => {
        if (alive) setState({ key, tick, data, error: null });
      },
      (err: unknown) => {
        if (!alive) return;
        setState((prev) => ({ key, tick, data: prev.key === key ? prev.data : undefined, error: messageOf(err) }));
      },
    );
    return () => {
      alive = false;
    };
  }, [key, tick]);

  const loading = state.key !== key || state.tick !== tick;

  useEffect(() => {
    if (!pollMs || loading) return;
    let onVisible: (() => void) | undefined;
    const timer = window.setTimeout(() => {
      if (document.visibilityState !== "hidden") {
        setTick((t) => t + 1);
        return;
      }
      // Aba oculta: pausa o polling até a aba voltar a ficar visível.
      onVisible = () => {
        if (document.visibilityState === "hidden") return;
        if (onVisible) document.removeEventListener("visibilitychange", onVisible);
        onVisible = undefined;
        setTick((t) => t + 1);
      };
      document.addEventListener("visibilitychange", onVisible);
    }, pollMs);
    return () => {
      window.clearTimeout(timer);
      if (onVisible) document.removeEventListener("visibilitychange", onVisible);
    };
  }, [pollMs, loading, state]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data: state.data, error: state.error, loading, reload };
}
