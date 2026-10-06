import type { DiskStatus } from "../api/types";
import { formatBytes } from "./format";

/** true quando o backend indica (ou os números mostram) espaço livre abaixo do mínimo. */
export function diskShortage(d: DiskStatus | null | undefined): boolean {
  if (!d) return false;
  if (d.disk_full === true || d.ok === false) return true;
  return d.free_bytes >= 0 && d.required_bytes > 0 && d.free_bytes < d.required_bytes;
}

/** "Espaço livre: 12,30 GB (mínimo para continuar baixando: 9,00 GB)". */
export function diskText(d: DiskStatus): string {
  const min = d.required_bytes > 0 ? ` (mínimo para continuar baixando: ${formatBytes(d.required_bytes)})` : "";
  if (d.free_bytes < 0) return `Não foi possível medir o espaço livre${min}`;
  return `Espaço livre: ${formatBytes(d.free_bytes)}${min}`;
}
