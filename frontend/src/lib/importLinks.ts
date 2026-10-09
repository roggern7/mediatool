// Importação por link (publicações avulsas): estimativa local da lista colada, o que pode ser enviado, o pedido
// montado a partir do preview do backend e os textos do lote por link. Funções puras (a tela só liga os estados).

import { describeErrorCode } from "../api/errors";
import type { BatchLink, BatchLinksSummary, ImportLinksRequest, LinksPreview } from "../api/types";
import { formatCount } from "./format";
import { canSubmitImport, invalidCountText } from "./importOptions";
import { type StartBlocker, interpretStartError } from "./importStart";
import { parseProfileInput } from "./profile";

/** Teto da API por importação (422 too_many_links acima disso). */
export const MAX_LINKS = 500;

const HOSTS = new Set(["instagram.com", "www.instagram.com"]);
const SHORTCODE_RE = /^[A-Za-z0-9_-]{5,64}$/;
const PRINTABLE_ASCII_RE = /^[\x21-\x7e]+$/;
const MAX_INPUT_LENGTH = 512;
/** `/reels/` vira `/reel/` na URL normalizada. */
const KINDS: Record<string, "p" | "reel"> = { p: "p", reel: "reel", reels: "reel" };
/** Um link por linha; vírgula, ponto e vírgula e espaço em branco também separam (link não tem espaço). */
const ENTRY_SPLIT_RE = /[\s,;]+/;

export type LinkParse = { ok: true; shortcode: string; url: string } | { ok: false; error: string };

const INVALID_URL: LinkParse = { ok: false, error: "URL inválida." };
const NOT_A_POST: LinkParse = { ok: false, error: "A URL não aponta para uma publicação." };

/**
 * Um link de publicação, espelhando `parse_post_link` do backend
 * (backend/app/integrations/instagram/validation.py): `/p/<código>/`, `/reel/<código>/` e `/reels/<código>/`,
 * também com o usuário na frente (`/<usuario>/p/<código>/`); http/https, ou sem esquema começando por
 * `instagram.com/` ou `www.instagram.com/`; parâmetros e fragmento são descartados. Serve só para a estimativa na
 * tela: quem decide o que é válido é POST /api/import-links/preview.
 */
export function parseLinkEntry(entry: string): LinkParse {
  let value = entry.trim();
  if (!value) return { ok: false, error: "Informe um link." };
  if (value.length > MAX_INPUT_LENGTH) return { ok: false, error: "Texto longo demais." };
  if (!PRINTABLE_ASCII_RE.test(value)) return { ok: false, error: "O link contém caracteres não permitidos." };
  const lowered = value.toLowerCase();
  if (lowered.startsWith("http://")) value = `https://${value.slice(7)}`;
  else if (lowered.startsWith("instagram.com/") || lowered.startsWith("www.instagram.com/")) value = `https://${value}`;
  const schemeEnd = value.indexOf("://");
  if (schemeEnd < 0) return { ok: false, error: "Informe o link completo da publicação." };
  if (value.includes("\\")) return INVALID_URL;
  if (value.slice(0, schemeEnd).toLowerCase() !== "https") return { ok: false, error: "A URL precisa usar https." };
  // Caminho como foi escrito (o URL do navegador resolveria "." e "..", que o backend recusa).
  const rest = value.slice(schemeEnd + 3);
  const cut = rest.search(/[/?#]/);
  const netloc = cut < 0 ? rest : rest.slice(0, cut);
  const path = cut >= 0 && rest[cut] === "/" ? (rest.slice(cut).split(/[?#]/)[0] ?? "") : "";
  if (netloc.includes("@")) return INVALID_URL;
  const [host = "", port, ...extra] = netloc.split(":");
  if (extra.length > 0 || (port !== undefined && port !== "" && port !== "443")) return INVALID_URL;
  if (!HOSTS.has(host.toLowerCase())) return { ok: false, error: "A URL não é do Instagram." };
  if (path.includes("%") || path.split("/").some((seg) => seg === "." || seg === "..")) return INVALID_URL;
  let segments = path.split("/").filter(Boolean);
  if (segments.length === 3) {
    // Link com o usuário na frente: precisa ser um usuário válido, não um caminho reservado (share, explore...).
    const user = segments[0] ?? "";
    if (user.startsWith("@") || !parseProfileInput(user).ok) return NOT_A_POST;
    segments = segments.slice(1);
  }
  const kind = KINDS[(segments[0] ?? "").toLowerCase()];
  const code = segments[1];
  if (segments.length !== 2 || kind === undefined || code === undefined) return NOT_A_POST;
  if (!SHORTCODE_RE.test(code)) return { ok: false, error: "Código da publicação inválido." };
  return { ok: true, shortcode: code, url: `https://www.instagram.com/${kind}/${code}/` };
}

/**
 * Entradas enviadas à pré-validação (`entries`), separadas como o backend separa (`parse_link_entries`): quebra de
 * linha, vírgula, ponto e vírgula e espaço em branco. Sem entradas vazias; os parâmetros da URL vão intactos.
 */
export function compactLinkEntries(text: string): string[] {
  return text.split(ENTRY_SPLIT_RE).filter((entry) => entry !== "");
}

/**
 * Estimativa local da lista colada (número provisório enquanto a conferência do backend não termina). Repetido é
 * o mesmo código em qualquer forma de URL; entrada inválida repetida também conta como repetida.
 */
export function parseLinkList(text: string): { valid: number; invalid: number; duplicates: number } {
  const seen = new Set<string>();
  const seenInvalid = new Set<string>();
  let duplicates = 0;
  for (const entry of compactLinkEntries(text)) {
    const parsed = parseLinkEntry(entry);
    if (!parsed.ok) {
      if (seenInvalid.has(entry)) duplicates += 1;
      else seenInvalid.add(entry);
    } else if (seen.has(parsed.shortcode)) duplicates += 1;
    else seen.add(parsed.shortcode);
  }
  return { valid: seen.size, invalid: seenInvalid.size, duplicates };
}

export function linksCountText(n: number): string {
  return `${formatCount(n)} ${n === 1 ? "link" : "links"}`;
}

/** "3 links válidos • 2 repetidos removidos • 1 entrada inválida" (só as partes que existem além dos válidos). */
export function linkEntriesSummary(input: { valid: number; duplicates: number; invalid: number }): string {
  const { valid, duplicates, invalid } = input;
  const parts = [`${formatCount(valid)} ${valid === 1 ? "link válido" : "links válidos"}`];
  if (duplicates > 0) {
    parts.push(`${formatCount(duplicates)} ${duplicates === 1 ? "repetido removido" : "repetidos removidos"}`);
  }
  if (invalid > 0) parts.push(invalidCountText(invalid));
  return parts.join(" • ");
}

export interface LinksStartState {
  sessionOk: boolean;
  /** Pausa global do Instagram vigente. */
  paused: boolean;
  submitting: boolean;
  /** Resultado de `settledPreview` (null enquanto confere ou em erro). */
  preview: LinksPreview | null;
  process: boolean;
  presetId: number | null;
}

/** Por que a importação por link não pode começar agora; null quando pode. */
export function linksStartBlocker(s: LinksStartState): StartBlocker | null {
  if (s.submitting) return "submitting";
  if (!s.sessionOk) return "no_session";
  if (s.paused) return "paused";
  if (s.preview === null) return "no_preview";
  if (s.preview.valid.length === 0) return "no_valid";
  if (!canSubmitImport(s.process, s.presetId)) return "preset";
  return null;
}

/**
 * Corpo de POST /api/import-links montado SÓ com as URLs normalizadas de `preview.valid`, nunca com o texto do
 * campo. Download puro é `process: false, preset_id: null`. null sem link válido.
 */
export function buildLinksRequest(
  preview: LinksPreview | null,
  options: { processAfterDownload: boolean; presetId: number | null },
): ImportLinksRequest | null {
  if (preview === null || preview.valid.length === 0) return null;
  return {
    links: preview.valid.map((link) => link.url),
    preset_id: options.processAfterDownload ? options.presetId : null,
    process: options.processAfterDownload,
  };
}

/** Linha de resumo antes de iniciar; null sem link válido. */
export function linksStartSummary(preview: LinksPreview | null): string | null {
  if (preview === null || preview.valid.length === 0) return null;
  const n = preview.valid.length;
  return `${linksCountText(n)} • ${n === 1 ? "A publicação inteira" : "Cada publicação inteira"} (foto, Reel, vídeo ou carrossel)`;
}

/** Mensagem de sucesso da criação. `sent` é o que foi enviado, usado se a resposta vier sem o resumo dos links. */
export function linksCreatedMessage(
  batch: { links?: { total: number } | null },
  sent: number,
  process: boolean,
): string {
  const count = linksCountText(batch.links?.total ?? sent);
  return process ? `Download e processamento de ${count} iniciados.` : `Download de ${count} iniciado.`;
}

/** Erro da criação por link -> o que a tela mostra e refaz (a lista inválida volta pela pré-validação). */
export function interpretLinksStartError(
  err: { status: number; code: string; message: string; payload?: unknown } | null,
): { error: string; refreshPreview: boolean } {
  const failure = interpretStartError(err);
  return { error: failure.error ?? "Erro inesperado.", refreshPreview: err?.code === "invalid_links" };
}

/**
 * Resumo dos links de um lote: "3 links • 2 resolvidos • 1 com falha" (pendentes só enquanto existem);
 * null quando o lote não é por link.
 */
export function linksSummaryText(links: BatchLinksSummary | null | undefined): string | null {
  if (!links) return null;
  const parts = [linksCountText(links.total)];
  if (links.resolved > 0) parts.push(`${formatCount(links.resolved)} ${links.resolved === 1 ? "resolvido" : "resolvidos"}`);
  if (links.failed > 0) parts.push(`${formatCount(links.failed)} com falha`);
  if (links.pending > 0) parts.push(`${formatCount(links.pending)} ${links.pending === 1 ? "pendente" : "pendentes"}`);
  return parts.join(" • ");
}

/** "1 link não pôde ser importado" / "3 links não puderam ser importados". */
export function failedLinksTitle(n: number): string {
  return `${linksCountText(n)} ${n === 1 ? "não pôde ser importado" : "não puderam ser importados"}`;
}

export interface FailedLink {
  position: number;
  url: string;
  reason: string;
}

/** Links que falharam, na ordem do lote, com o motivo em texto. */
export function failedLinks(items: BatchLink[]): FailedLink[] {
  return items
    .filter((item) => item.status === "failed")
    .map((item) => ({
      position: item.position,
      url: item.url,
      reason: describeErrorCode(item.error_code, item.error_message),
    }));
}
