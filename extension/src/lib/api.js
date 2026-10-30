// Cliente da API local do Media Tool (/api/ext/*). Sem dependências; recebe `fetchImpl` para os testes.

export const DEFAULT_PORT = 8765;
export const DEFAULT_TIMEOUT_MS = 15_000;
export const SLOW_TIMEOUT_MS = 70_000; // post-owner consulta o Instagram (prazo do backend ~50 s)
const CLIENT_HEADER = "X-Media-Tool-Client";

export const MESSAGES = {
  offline: "Não foi possível falar com o Media Tool. Abra o Media Tool neste computador e tente de novo.",
  timeout: "O Media Tool demorou demais para responder. Tente de novo.",
  unauthorized: "Esta extensão não está mais conectada ao Media Tool (acesso revogado ou pareamento perdido). " +
    "Pareie de novo nas Opções da extensão.",
  unpaired: "A extensão ainda não está conectada ao Media Tool. Abra as Opções e faça o pareamento.",
  generic: "O Media Tool recusou o pedido.",
};

export class ExtApiError extends Error {
  /** kind: "offline" | "timeout" | "unauthorized" | "http" */
  constructor(kind, message, { status = 0, code = null, payload = null } = {}) {
    super(message);
    this.name = "ExtApiError";
    this.kind = kind;
    this.status = status;
    this.code = code;
    this.payload = payload;
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

export function createClient({ port = DEFAULT_PORT, token = null, fetchImpl = globalThis.fetch,
  timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const root = baseUrl(port);

  async function request(method, path, { body, auth = true, timeout = timeoutMs } = {}) {
    const headers = { Accept: "application/json" };
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
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const code = payload && typeof payload.code === "string" ? payload.code : `http_${response.status}`;
      if (response.status === 401 && auth) {
        throw new ExtApiError("unauthorized", MESSAGES.unauthorized, { status: 401, code, payload });
      }
      throw new ExtApiError("http", detailOf(payload) || MESSAGES.generic, { status: response.status, code, payload });
    }
    return payload;
  }

  return {
    pair: (code, deviceName) => request("POST", "/api/ext/pair", { body: { code, device_name: deviceName },
      auth: false }),
    status: () => request("GET", "/api/ext/status"),
    pageInfo: (url) => request("POST", "/api/ext/page-info", { body: { url } }),
    postOwner: (url) => request("POST", "/api/ext/post-owner", { body: { url }, timeout: SLOW_TIMEOUT_MS }),
    start: (body) => request("POST", "/api/ext/start", { body, timeout: SLOW_TIMEOUT_MS }),
    progress: ({ batchIds = [], groupId = null } = {}) => {
      const params = new URLSearchParams();
      if (batchIds.length) params.set("batch_ids", batchIds.join(","));
      if (groupId) params.set("group_id", String(groupId));
      return request("GET", `/api/ext/progress?${params.toString()}`);
    },
  };
}
