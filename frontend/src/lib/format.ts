// Formatadores pt-BR.

const numberFmt1 = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const numberFmt2 = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const intFmt = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 0 });
const dateTimeFmt = new Intl.DateTimeFormat("pt-BR", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});
const dateFmt = new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric" });

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return "-";
  if (bytes < 1024) return `${intFmt.format(bytes)} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${intFmt.format(kb)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${numberFmt1.format(mb)} MB`;
  return `${numberFmt2.format(mb / 1024)} GB`;
}

/** Duração em mm:ss (ou h:mm:ss acima de uma hora). */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "-";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, "0");
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${ss}`;
  return `${String(m).padStart(2, "0")}:${ss}`;
}

function parseDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function formatDateTime(iso: string | null | undefined): string {
  const d = parseDate(iso);
  return d ? dateTimeFmt.format(d) : "-";
}

export function formatDate(iso: string | null | undefined): string {
  const d = parseDate(iso);
  return d ? dateFmt.format(d) : "-";
}

/** Velocidade como "1,05x". */
export function formatSpeed(speed: number): string {
  return `${numberFmt2.format(speed)}x`;
}

/** Segundos curtos como "0,5 s". */
export function formatSeconds(value: number): string {
  const fmt = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2 });
  return `${fmt.format(value)} s`;
}

export function formatResolution(width: number | null | undefined, height: number | null | undefined): string {
  if (!width || !height) return "-";
  return `${width}×${height}`;
}

export function formatFps(fps: number | null | undefined): string {
  if (fps === null || fps === undefined || !Number.isFinite(fps)) return "-";
  const fmt = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2 });
  return `${fmt.format(fps)} fps`;
}

export function formatPercent(fraction: number): string {
  const clamped = Math.min(1, Math.max(0, fraction));
  return `${Math.round(clamped * 100)}%`;
}

export function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined) return "-";
  return intFmt.format(n);
}

/** Converte texto digitado com vírgula ou ponto em número; null se inválido. */
export function parseDecimal(text: string): number | null {
  const cleaned = text.trim().replace(",", ".");
  if (cleaned === "" || !/^-?\d*\.?\d+$|^-?\d+\.?$/.test(cleaned)) return null;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : null;
}

/** Formata número para campo editável em pt-BR (vírgula decimal). */
export function toDecimalInput(value: number): string {
  return String(value).replace(".", ",");
}
