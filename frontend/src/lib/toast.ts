import { createContext, useContext } from "react";

export type ToastKind = "ok" | "err" | "info";

export interface ToastApi {
  push: (kind: ToastKind, text: string) => void;
}

export const ToastContext = createContext<ToastApi>({ push: () => undefined });

export function useToast(): ToastApi {
  return useContext(ToastContext);
}
