import type { Media } from "../api/types";
import { MAX_EDIT_ITEMS } from "./editSettings";

// Vídeos enviados para a página Editar pela URL (?ids=1,2,3), como faz o "Recortar no Media Tool" da extensão.

export const IDS_PARAM = "ids";

/** Ids pedidos em ?ids= (só inteiros positivos, sem repetir, até 500) e quantas entradas foram ignoradas. */
export function editIdsFromSearch(search: string): { ids: number[]; ignored: number } {
  const raw = new URLSearchParams(search).get(IDS_PARAM);
  if (raw === null) return { ids: [], ignored: 0 };
  const seen = new Set<number>();
  const ids: number[] = [];
  let ignored = 0;
  for (const part of raw.split(",")) {
    const text = part.trim();
    if (!text) continue;
    const id = /^\d+$/.test(text) ? Number(text) : NaN;
    if (seen.has(id)) continue; // repetido: já está pedido
    if (!Number.isSafeInteger(id) || id < 1 || ids.length >= MAX_EDIT_ITEMS) {
      ignored += 1;
      continue;
    }
    seen.add(id);
    ids.push(id);
  }
  return { ids, ignored };
}

/** A busca da URL sem o ?ids= (as outras partes ficam); "" quando não sobra nada. */
export function searchWithoutIds(search: string): string {
  const params = new URLSearchParams(search);
  params.delete(IDS_PARAM);
  const rest = params.toString();
  return rest ? `?${rest}` : "";
}

/** Só os vídeos (não as capas) entre as mídias lidas. */
export function incomingVideos(items: Media[]): Media[] {
  return items.filter((m) => m.media_type === "video" && (m.role ?? "original") !== "cover");
}

/** Aviso dos itens ignorados (não são vídeo, não existem mais ou id inválido); null se nenhum. */
export function ignoredText(ignored: number): string | null {
  if (ignored <= 0) return null;
  return ignored === 1
    ? "1 item ignorado: não é vídeo ou não está mais na biblioteca."
    : `${ignored} itens ignorados: não são vídeos ou não estão mais na biblioteca.`;
}
