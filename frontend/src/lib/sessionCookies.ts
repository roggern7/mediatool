// Cookies adicionais da sessão do Instagram (nunca guardados nem reexibidos pelo painel).

export const COOKIE_KEYS = ["csrftoken", "ds_user_id", "mid", "ig_did"] as const;
export type CookieKey = (typeof COOKIE_KEYS)[number];
export type CookiePatch = Partial<Record<CookieKey, string>>;

const COOKIE_RE = /^[A-Za-z0-9%:_\-.]+$/;
const MAX_LEN = 512;

export function savedKeysLabel(keys: readonly string[] | null | undefined): string {
  const known = (keys ?? []).filter((k): k is CookieKey => (COOKIE_KEYS as readonly string[]).includes(k));
  return known.length > 0 ? `Salvos: ${known.join(", ")}` : "Nenhum";
}

export function invalidCookieKeys(values: Record<CookieKey, string>): CookieKey[] {
  return COOKIE_KEYS.filter((k) => {
    const v = values[k].trim();
    return v !== "" && (v.length > MAX_LEN || !COOKIE_RE.test(v));
  });
}

/**
 * Corpo do PATCH: valor novo para chaves preenchidas, "" para chaves marcadas para remoção.
 * Chaves vazias e não marcadas ficam de fora (não mudam).
 */
export function buildCookiePatch(values: Record<CookieKey, string>, remove: ReadonlySet<CookieKey>): CookiePatch {
  const body: CookiePatch = {};
  for (const k of COOKIE_KEYS) {
    const v = values[k].trim();
    if (v) body[k] = v;
    else if (remove.has(k)) body[k] = "";
  }
  return body;
}
