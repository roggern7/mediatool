// Capas na Biblioteca (media.role = "cover"): filtro "Capas" e selo. Funções puras.

import type { Media, MediaRole, MediaType } from "../api/types";

/** Filtro de tipo da Biblioteca: "Todos" mostra tudo (capas inclusive); "Capas" só as capas. */
export type LibraryTypeFilter = "all" | MediaType | "cover";

export const LIBRARY_TYPE_FILTERS: readonly (readonly [LibraryTypeFilter, string])[] = [
  ["all", "Todos"],
  ["video", "Vídeos"],
  ["image", "Imagens"],
  ["cover", "Capas"],
];

/** Parte do filtro enviada à API (GET /media, exportação e processamento por filtro). */
export function libraryTypeQuery(filter: LibraryTypeFilter): { media_type?: MediaType; role?: MediaRole } {
  if (filter === "all") return {};
  if (filter === "cover") return { role: "cover" };
  return { media_type: filter };
}

export function isCover(media: Pick<Media, "role">): boolean {
  return media.role === "cover";
}

/** Rótulo do tipo na lista: "Capa" para capas; "Vídeo"/"Imagem" para as demais. */
export function mediaKindLabel(media: Pick<Media, "role" | "media_type">): string {
  if (isCover(media)) return "Capa";
  return media.media_type === "video" ? "Vídeo" : "Imagem";
}
