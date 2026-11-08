import { ROUTES } from "./router";

/** Endereço da Fila filtrada por um lote. */
export function queueForBatch(batchId: number): string {
  return `${ROUTES.queue}?lote=${batchId}`;
}

/** Endereço da Edição CapCut com um lote aberto. */
export function capcutForBatch(batchId: number): string {
  return `${ROUTES.captions}?lote=${batchId}`;
}

/** Lote pedido na busca da URL (?lote=ID), ou null se ausente ou inválido. */
export function batchIdFromSearch(search: string): number | null {
  const raw = new URLSearchParams(search).get("lote");
  if (raw === null || !/^\d+$/.test(raw)) return null;
  const id = Number(raw);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}
