// Classificação local (sem rede) da URL da aba. O backend faz a classificação oficial em /api/ext/page-info;
// esta serve para a extensão decidir o que mostrar antes/sem o app e para testes.

const HOSTS = new Set(["instagram.com", "www.instagram.com", "m.instagram.com"]);
const POST_KINDS = { p: "post", reel: "reel", reels: "reel", tv: "reel" };
const USERNAME_RE = /^[A-Za-z0-9._]{1,30}$/;
const SHORTCODE_RE = /^[A-Za-z0-9_-]{5,64}$/;
const PROFILE_TABS = new Set(["reels", "tagged"]);
const RESERVED = new Set([
  "about", "accounts", "api", "ar", "blog", "challenge", "developer", "direct", "directory", "emails", "explore",
  "graphql", "help", "igtv", "invites", "jobs", "legal", "lite", "locations", "login", "logout", "nametag",
  "oauth", "p", "press", "privacy", "reel", "reels", "s", "session", "share", "static", "stories", "tags",
  "terms", "topics", "tv", "web",
]);

function validUsername(name) {
  return USERNAME_RE.test(name) && !name.includes("..") && /[A-Za-z0-9]/.test(name) &&
    !RESERVED.has(name.toLowerCase());
}

/** Classifica uma URL: {kind: "post"|"reel"|"profile"|"other", shortcode?, username?, canonicalUrl?}. */
export function parseInstagramUrl(raw) {
  if (typeof raw !== "string" || !raw.trim()) return { kind: "other" };
  let url;
  try {
    url = new URL(raw.trim());
  } catch {
    return { kind: "other" };
  }
  if (!["https:", "http:"].includes(url.protocol) || !HOSTS.has(url.hostname.toLowerCase())) {
    return { kind: "other" };
  }
  const segments = url.pathname.split("/").filter(Boolean);
  let rest = segments;
  let owner;
  if (segments.length === 3 && validUsername(segments[0]) && POST_KINDS[segments[1].toLowerCase()]) {
    owner = segments[0].toLowerCase();
    rest = segments.slice(1);
  }
  if (rest.length === 2 && POST_KINDS[rest[0].toLowerCase()]) {
    const code = rest[1];
    if (!SHORTCODE_RE.test(code)) return { kind: "other" };
    const path = ["p", "tv"].includes(rest[0].toLowerCase()) ? "p" : "reel"; // /tv/ vira /p/ (mesmo shortcode)
    const result = { kind: POST_KINDS[rest[0].toLowerCase()], shortcode: code,
      canonicalUrl: `https://www.instagram.com/${path}/${code}/` };
    if (owner) result.username = owner;
    return result;
  }
  if ((segments.length === 1 || (segments.length === 2 && PROFILE_TABS.has(segments[1].toLowerCase()))) &&
      validUsername(segments[0])) {
    const username = segments[0].toLowerCase();
    return { kind: "profile", username, canonicalUrl: `https://www.instagram.com/${username}/` };
  }
  return { kind: "other" };
}

/** Converte a resposta de /api/ext/page-info (snake_case) para o mesmo formato de parseInstagramUrl. */
export function fromPageInfo(info) {
  if (!info || typeof info !== "object") return { kind: "other" };
  const result = { kind: ["post", "reel", "profile"].includes(info.kind) ? info.kind : "other" };
  if (info.shortcode) result.shortcode = info.shortcode;
  if (info.username) result.username = info.username;
  if (info.canonical_url) result.canonicalUrl = info.canonical_url;
  return result;
}
