import { useEffect, useState } from "react";
import { capcutFontFileUrl } from "../api/client";
import { fontFamilyName } from "./captionDesign";

/** Fontes já carregadas (ou que falharam) nesta aba: chave -> família CSS ou null. */
const loaded = new Map<string, Promise<string | null>>();

function loadFont(key: string): Promise<string | null> {
  const cached = loaded.get(key);
  if (cached) return cached;
  const family = fontFamilyName(key);
  const promise =
    typeof FontFace === "undefined"
      ? Promise.resolve(null)
      : new FontFace(family, `url("${capcutFontFileUrl(key)}")`).load().then(
          (face) => {
            document.fonts.add(face);
            return family;
          },
          () => null,
        );
  loaded.set(key, promise);
  return promise;
}

export interface CaptionFontState {
  /** Família CSS da fonte real; null enquanto carrega, sem arquivo local ou sem chave. */
  family: string | null;
  loading: boolean;
  /** A prévia usa uma fonte aproximada (sem arquivo local para esta fonte). */
  approximate: boolean;
}

/**
 * Carrega por @font-face (`/api/capcut/fonts/file?key=`) a fonte do modelo para a prévia. `key` null ou
 * `previewable` false = sem arquivo local: a prévia usa uma fonte genérica.
 */
export function useCaptionFont(key: string | null, previewable = true): CaptionFontState {
  const [result, setResult] = useState<{ key: string; family: string | null } | null>(null);
  const wanted = key && previewable ? key : null;

  useEffect(() => {
    if (!wanted) return;
    let alive = true;
    void loadFont(wanted).then((family) => {
      if (alive) setResult({ key: wanted, family });
    });
    return () => {
      alive = false;
    };
  }, [wanted]);

  if (!wanted) return { family: null, loading: false, approximate: true };
  if (result?.key !== wanted) return { family: null, loading: true, approximate: false };
  return { family: result.family, loading: false, approximate: result.family === null };
}
