// Tema da interface: escuro (padrão), claro ou o do sistema. Guardado neste navegador; aplicado no <html> como
// data-theme="dark" | "light" (os tokens de cor ficam em styles/app.css). public/theme-init.js aplica o salvo antes
// de a página desenhar; aqui o seletor troca e, em "system", acompanha o Windows ao vivo.
import { useEffect, useState } from "react";

export type ThemePref = "dark" | "light" | "system";
export type Theme = "dark" | "light";

export const THEME_KEY = "media-tool-theme";
export const DEFAULT_THEME: ThemePref = "dark";
export const THEME_OPTIONS: { value: ThemePref; label: string }[] = [
  { value: "dark", label: "Escuro" },
  { value: "light", label: "Claro" },
  { value: "system", label: "Sistema" },
];

export function parseThemePref(value: string | null | undefined): ThemePref {
  return value === "dark" || value === "light" || value === "system" ? value : DEFAULT_THEME;
}

export function resolveTheme(pref: ThemePref, systemDark: boolean): Theme {
  if (pref === "system") return systemDark ? "dark" : "light";
  return pref;
}

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function readThemePref(): ThemePref {
  try {
    return parseThemePref(storage()?.getItem(THEME_KEY));
  } catch {
    return DEFAULT_THEME;
  }
}

function systemQuery(): MediaQueryList | null {
  return typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
}

export function applyTheme(pref: ThemePref): Theme {
  const theme = resolveTheme(pref, systemQuery()?.matches ?? false);
  if (typeof document !== "undefined") document.documentElement.setAttribute("data-theme", theme);
  return theme;
}

/** Preferência atual + troca (salva e aplica). Em "system", reaplica quando o Windows muda de tema. */
export function useThemePref(): [ThemePref, (pref: ThemePref) => void] {
  const [pref, setPref] = useState<ThemePref>(readThemePref);
  useEffect(() => {
    applyTheme(pref);
    if (pref !== "system") return;
    const query = systemQuery();
    if (!query) return;
    const onChange = () => applyTheme("system");
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, [pref]);
  const change = (next: ThemePref) => {
    try {
      storage()?.setItem(THEME_KEY, next);
    } catch {
      /* sem armazenamento: vale só nesta sessão */
    }
    setPref(next);
  };
  return [pref, change];
}
