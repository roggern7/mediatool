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
