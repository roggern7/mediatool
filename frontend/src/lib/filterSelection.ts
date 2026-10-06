import type { MediaType, MediaView, ProcessByFilterRequest } from "../api/types";

/** Teto do servidor para processar por filtro (MAX_BULK_PROCESS). */
export const MAX_BULK_PROCESS = 10_000;

export interface LibraryFilter {
  view: MediaView;
  typeFilter: "all" | MediaType;
  q: string;
}

/** Corpo de process-by-filter com o mesmo filtro usado na listagem da Biblioteca. */
export function processByFilterBody(filter: LibraryFilter, presetId: number): ProcessByFilterRequest {
  const body: ProcessByFilterRequest = { view: filter.view, preset_id: presetId };
  if (filter.typeFilter !== "all") body.media_type = filter.typeFilter;
  if (filter.q) body.q = filter.q;
  return body;
}

/** Oferece "Selecionar todos os N resultados": página inteira marcada e há mais resultados fora dela. */
export function canOfferSelectAll(pageCount: number, selectedOnPage: number, total: number): boolean {
  return pageCount > 0 && selectedOnPage === pageCount && total > pageCount;
}

export function withinBulkCap(total: number): boolean {
  return total > 0 && total <= MAX_BULK_PROCESS;
}
