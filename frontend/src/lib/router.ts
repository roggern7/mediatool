import { useSyncExternalStore } from "react";

// Roteador mínimo baseado na History API.

const NAV_EVENT = "mt:navigate";

function subscribe(cb: () => void): () => void {
  window.addEventListener("popstate", cb);
  window.addEventListener(NAV_EVENT, cb);
  return () => {
    window.removeEventListener("popstate", cb);
    window.removeEventListener(NAV_EVENT, cb);
  };
}

function snapshot(): string {
  return window.location.pathname;
}

export function usePath(): string {
  return useSyncExternalStore(subscribe, snapshot, () => "/");
}

function searchSnapshot(): string {
  return window.location.search;
}

/** Parte de busca da URL atual (ex.: "?lote=12"). */
export function useSearch(): string {
  return useSyncExternalStore(subscribe, searchSnapshot, () => "");
}

export function navigate(to: string): void {
  if (to === window.location.pathname + window.location.search) return;
  window.history.pushState(null, "", to);
  window.dispatchEvent(new Event(NAV_EVENT));
  document.querySelector(".main")?.scrollTo(0, 0);
}

export const ROUTES = {
  import: "/",
  library: "/biblioteca",
  edit: "/editar",
  captions: "/legendas",
  queue: "/fila",
  profiles: "/analise-perfis",
  presets: "/presets",
  settings: "/configuracoes",
  postTimes: "/padroes",
} as const;
