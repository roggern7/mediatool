// Normalização de perfil do Instagram, espelhando parse_profile_input do backend
// (backend/app/integrations/instagram/validation.py).

const USERNAME_RE = /^[A-Za-z0-9._]{1,30}$/;
const PRINTABLE_ASCII_RE = /^[\x21-\x7e]+$/;
const MAX_INPUT_LENGTH = 512;
const HOSTS = new Set(["instagram.com", "www.instagram.com"]);

// Primeiros segmentos de caminho que não são perfis.
const RESERVED = new Set([
  "about", "accounts", "api", "ar", "blog", "challenge", "developer", "direct", "directory",
  "emails", "explore", "graphql", "help", "igtv", "invites", "jobs", "legal", "lite",
  "locations", "login", "logout", "nametag", "oauth", "p", "press", "privacy", "reel",
  "reels", "s", "session", "share", "static", "stories", "tags", "terms", "topics", "tv",
  "web",
]);

export type ProfileParse = { ok: true; username: string } | { ok: false; error: string };

const INVALID_USER: ProfileParse = { ok: false, error: "Nome de usuário inválido." };
const NOT_PROFILE: ProfileParse = { ok: false, error: "A URL não aponta para um perfil." };

function checkUsername(name: string): ProfileParse {
  if (!USERNAME_RE.test(name) || name.includes("..") || !/[A-Za-z0-9]/.test(name)) return INVALID_USER;
  const lowered = name.toLowerCase();
  if (RESERVED.has(lowered)) return { ok: false, error: "Nome de usuário reservado." };
  return { ok: true, username: lowered };
}

export function parseProfileInput(raw: string): ProfileParse {
  let value = raw.trim();
  if (!value) return { ok: false, error: "Informe um perfil." };
  if (value.length > MAX_INPUT_LENGTH) return { ok: false, error: "Texto longo demais." };
  if (!PRINTABLE_ASCII_RE.test(value)) return { ok: false, error: "Use apenas o @usuario ou a URL do perfil." };

  const lowered = value.toLowerCase();
  if (lowered.startsWith("instagram.com/") || lowered.startsWith("www.instagram.com/")) {
    value = `https://${value}`;
  }
  if (value.includes("://") || value.includes("/")) {
    if (value.includes("\\")) return { ok: false, error: "URL inválida." };
    let url: URL;
    try {
      url = new URL(value);
    } catch {
      return { ok: false, error: "URL inválida." };
    }
    if (url.protocol !== "https:") return { ok: false, error: "A URL precisa usar https." };
    if (url.username || url.password) return { ok: false, error: "URL inválida." };
    if (!HOSTS.has(url.hostname.toLowerCase())) return { ok: false, error: "A URL não é do Instagram." };
    if (url.port !== "" && url.port !== "443") return { ok: false, error: "URL inválida." };
    if (url.pathname.includes("%")) return { ok: false, error: "URL inválida." };
    const first = url.pathname.split("/").filter(Boolean)[0];
    if (!first || RESERVED.has(first.toLowerCase())) return NOT_PROFILE;
    return checkUsername(first);
  }
  if (value.startsWith("@")) value = value.slice(1);
  return checkUsername(value);
}

// Abas do próprio perfil aceitas depois do usuário numa URL (`/usuario/reels/`, `/usuario/tagged/`).
const PROFILE_TABS = new Set(["reels", "tagged"]);
const ENTRY_SPLIT_RE = /[\r\n,;]+/;

/**
 * Uma entrada da lista de perfis, com as regras de `normalize_profile_entry` do backend
 * (backend/app/services/import_service.py): além de `parseProfileInput`, aceita `http://` e só URLs claramente de
 * perfil (`/usuario`, `/usuario/`, `/usuario/reels/`, `/usuario/tagged/`); outros caminhos são inválidos.
 */
export function parseProfileEntry(entry: string): ProfileParse {
  let value = entry.trim();
  if (value.slice(0, 7).toLowerCase() === "http://") value = `https://${value.slice(7)}`;
  const lowered = value.toLowerCase();
  if (lowered.startsWith("instagram.com/") || lowered.startsWith("www.instagram.com/")) value = `https://${value}`;
  if (value.includes("://")) {
    // Caminho como foi escrito (o URL do navegador resolveria "." e "..", que o backend recusa).
    const rest = value.slice(value.indexOf("://") + 3);
    const cut = rest.search(/[/?#]/);
    const rawPath = cut >= 0 && rest[cut] === "/" ? (rest.slice(cut).split(/[?#]/)[0] ?? "") : "";
    const segments = rawPath.split("/").filter(Boolean);
    if (segments.length > 2 || (segments.length === 2 && !PROFILE_TABS.has((segments[1] ?? "").toLowerCase()))) {
      return NOT_PROFILE;
    }
    if (rawPath.split("/").some((seg) => seg === "." || seg === "..")) return { ok: false, error: "URL inválida." };
  }
  return parseProfileInput(value);
}

/** Tira query string e fragmento de uma entrada em forma de URL, sem mudar o que o backend decide sobre ela. */
function trimUrlExtras(entry: string): string {
  const cut = entry.search(/[?#]/);
  if (cut <= 0 || !entry.slice(0, cut).includes("/")) return entry;
  // Entrada longa demais ou com caractere não permitido é inválida inteira: vai como está para o backend dizer.
  if (entry.length > MAX_INPUT_LENGTH || !PRINTABLE_ASCII_RE.test(entry)) return entry;
  return entry.slice(0, cut);
}

/**
 * Entradas enviadas à pré-validação (`entries`): uma por linha (vírgula e ponto e vírgula também separam; espaço
 * NÃO separa), aparadas, sem linhas vazias e sem query string/fragmento nas URLs. Deixa o corpo do pedido menor;
 * quem decide o que é válido continua sendo o backend.
 */
export function compactProfileEntries(text: string): string[] {
  const entries: string[] = [];
  for (const piece of text.split(ENTRY_SPLIT_RE)) {
    const entry = piece.trim();
    if (entry) entries.push(trimUrlExtras(entry));
  }
  return entries;
}

export interface ProfileListParse {
  /** Usernames normalizados, na ordem, sem repetição. */
  valid: string[];
  invalid: { entry: string; reason: string }[];
  /** Repetições removidas (depois de normalizar; inválida repetida também conta). */
  duplicates: number;
}

/**
 * Estimativa local de uma lista colada, com os mesmos separadores e regras do backend. Serve só para o número
 * provisório na tela enquanto a conferência não termina: a lista exibida e enviada vem de
 * POST /api/import-groups/preview.
 */
export function parseProfileList(text: string): ProfileListParse {
  const seen = new Set<string>();
  const seenInvalid = new Set<string>();
  const invalid: ProfileListParse["invalid"] = [];
  let duplicates = 0;
  for (const piece of text.split(ENTRY_SPLIT_RE)) {
    const entry = piece.trim();
    if (!entry) continue;
    const parsed = parseProfileEntry(entry);
    if (!parsed.ok) {
      if (seenInvalid.has(entry)) duplicates += 1;
      else {
        seenInvalid.add(entry);
        invalid.push({ entry, reason: parsed.error });
      }
    } else if (seen.has(parsed.username)) duplicates += 1;
    else seen.add(parsed.username);
  }
  return { valid: [...seen], invalid, duplicates };
}
