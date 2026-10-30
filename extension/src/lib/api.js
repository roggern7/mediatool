// Cliente da API local do Media Tool (/api/ext/*). Sem dependências; recebe `fetchImpl` para os testes.
// O token vai só no cabeçalho Authorization; nunca em URL, log ou DOM.

export const DEFAULT_PORT = 8765;
export const DEFAULT_TIMEOUT_MS = 15_000;
export const SLOW_TIMEOUT_MS = 70_000; // post-owner, post-preview e start consultam o Instagram (prazo do backend ~50 s)
const CLIENT_HEADER = "X-Media-Tool-Client";

export const MESSAGES = {
  offline: "Não foi possível falar com o Media Tool. Abra o Media Tool neste computador e tente de novo.",
  timeout: "O Media Tool demorou demais para responder. Tente de novo.",
  unauthorized: "Esta extensão não está mais conectada ao Media Tool (acesso revogado ou pareamento perdido). " +
    "Pareie de novo em Configurações.",
  unpaired: "A extensão ainda não está conectada ao Media Tool. Faça o pareamento em Configurações.",
  rateLimited: "Muitos pedidos seguidos. Aguarde alguns segundos e tente de novo.",
  generic: "O Media Tool recusou o pedido.",
};

export class ExtApiError extends Error {
  /** kind: "offline" | "timeout" | "unauthorized" | "rate_limited" | "http" */
  constructor(kind, message, { status = 0, code = null, payload = null, retryAfter = null } = {}) {
    super(message);
    this.name = "ExtApiError";
    this.kind = kind;
    this.status = status;
    this.code = code;
    this.payload = payload;
    this.retryAfter = retryAfter;
  }
}

export function normalizePort(value) {
  const port = Number(value);
  return Number.isInteger(port) && port >= 1024 && port <= 65535 ? port : null;
}

export function baseUrl(port) {
  return `http://127.0.0.1:${normalizePort(port) ?? DEFAULT_PORT}`;
}

function detailOf(payload) {
  if (!payload || typeof payload !== "object") return null;
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail) && payload.detail[0] && typeof payload.detail[0].msg === "string") {
    const first = payload.detail[0];
    const field = Array.isArray(first.loc) ? first.loc.filter((p) => p !== "body").join(".") : "";
    return field ? `${field}: ${first.msg}` : first.msg;
  }
  return null;
}

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export function createClient({ port = DEFAULT_PORT, token = null, fetchImpl = globalThis.fetch,
  timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const root = baseUrl(port);

  async function send(method, path, { body, auth = true, timeout = timeoutMs, accept = "application/json" } = {}) {
    const headers = { Accept: accept };
    if (method !== "GET") headers[CLIENT_HEADER] = "extension";
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (auth) {
      if (!token) throw new ExtApiError("unauthorized", MESSAGES.unpaired, { status: 401 });
      headers.Authorization = `Bearer ${token}`;
    }
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeout);
    let response;
    try {
      response = await fetchImpl(`${root}${path}`, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal, cache: "no-store", credentials: "omit",
      });
    } catch {
      throw timedOut ? new ExtApiError("timeout", MESSAGES.timeout) : new ExtApiError("offline", MESSAGES.offline);
    } finally {
      clearTimeout(timer);
    }
    if (!response.ok) {
      let payload = null;
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }
      const code = payload && typeof payload.code === "string" ? payload.code : `http_${response.status}`;
      if (response.status === 401 && auth) {
        throw new ExtApiError("unauthorized", MESSAGES.unauthorized, { status: 401, code, payload });
      }
      if (response.status === 429) {
        const header = response.headers && typeof response.headers.get === "function"
          ? Number(response.headers.get("Retry-After")) : NaN;
        throw new ExtApiError("rate_limited", detailOf(payload) || MESSAGES.rateLimited,
          { status: 429, code, payload, retryAfter: Number.isFinite(header) && header > 0 ? header : null });
      }
      throw new ExtApiError("http", detailOf(payload) || MESSAGES.generic, { status: response.status, code, payload });
    }
    return response;
  }

  async function request(method, path, options) {
    const response = await send(method, path, options);
    try {
      return await response.json();
    } catch {
      return null;
    }
  }

  return {
    pair: (code, deviceName) => request("POST", "/api/ext/pair", { body: { code, device_name: deviceName },
      auth: false }),
    status: () => request("GET", "/api/ext/status"),
    pageInfo: (url) => request("POST", "/api/ext/page-info", { body: { url } }),
    postPreview: (url) => request("POST", "/api/ext/post-preview", { body: { url }, timeout: SLOW_TIMEOUT_MS }),
    postOwner: (url) => request("POST", "/api/ext/post-owner", { body: { url }, timeout: SLOW_TIMEOUT_MS }),
    start: (body) => request("POST", "/api/ext/start", { body, timeout: SLOW_TIMEOUT_MS }),
    activity: (limit = 20) => request("GET", `/api/ext/activity${query({ limit })}`),
    batchItems: (id, { beforeId = null, limit = 50 } = {}) =>
      request("GET", `/api/ext/batches/${Number(id)}/items${query({ before_id: beforeId, limit })}`),
    /** Perfis (lotes filhos) de um grupo, paginados por `after_position`. */
    groupChildren: (id, { afterPosition = null, limit = 20 } = {}) =>
      request("GET", `/api/ext/groups/${Number(id)}/children${query({ after_position: afterPosition, limit })}`),
    cancel: (kind, id) => request("POST", `/api/ext/${kind === "group" ? "groups" : "batches"}/${Number(id)}/cancel`,
      { body: {} }),
    library: (filters = {}) => request("GET", `/api/ext/library${query(filters)}`),
    libraryIds: (filters = {}) => request("GET", `/api/ext/library/ids${query(filters)}`),
    /** Miniatura como Blob (a tela cria o blob URL e o libera depois). */
    thumbnail: async (mediaId) => {
      const response = await send("GET", `/api/ext/media/${Number(mediaId)}/thumbnail`, { accept: "image/*" });
      return response.blob();
    },
    editOptions: () => request("GET", "/api/ext/edit/options"),
    edits: (body) => request("POST", "/api/ext/edits", { body }),
    process: (mediaIds, presetId) => request("POST", "/api/ext/process", { body: { media_ids: mediaIds,
      preset_id: presetId } }),
    createExport: (body) => request("POST", "/api/ext/exports", { body }),
    exportStatus: (id) => request("GET", `/api/ext/exports/${Number(id)}`),
  };
}
