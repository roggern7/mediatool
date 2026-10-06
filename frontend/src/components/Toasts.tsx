import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";
import { ToastContext, type ToastKind } from "../lib/toast";

interface ToastItem {
  id: number;
  kind: ToastKind;
  text: string;
}

const TTL_MS = 4500;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setItems((list) => list.filter((t) => t.id !== id));
  }, []);

  const push = useCallback(
    (kind: ToastKind, text: string) => {
      const id = nextId.current++;
      setItems((list) => [...list.slice(-3), { id, kind, text }]);
      window.setTimeout(() => dismiss(id), kind === "err" ? TTL_MS * 1.6 : TTL_MS);
    },
    [dismiss],
  );

  const value = useMemo(() => ({ push }), [push]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => {
          const Icon = t.kind === "ok" ? CheckCircle2 : t.kind === "err" ? AlertCircle : Info;
          return (
            <div key={t.id} className={`toast ${t.kind}`} role={t.kind === "err" ? "alert" : "status"}>
              <Icon className="toast-icon" size={15} strokeWidth={1.75} aria-hidden="true" />
              <span className="grow">{t.text}</span>
              <button
                type="button"
                className="btn btn-ghost btn-sm btn-icon"
                aria-label="Fechar"
                onClick={() => dismiss(t.id)}
                style={{ height: 20, width: 20 }}
              >
                <X size={13} />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
