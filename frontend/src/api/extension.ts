/** Gerenciamento da extensão do Chrome em Configurações (código de pareamento, extensões conectadas, revogação). */
import { ApiError } from "./client";
import { errorMessageFor } from "./errors";

const CLIENT_HEADER = "X-Media-Tool-Client";
const CLIENT_VALUE = "panel";
const TIMEOUT_MS = 20_000;

export interface ExtensionPairingCode {
  /** Identifica o código na consulta de status (não é segredo). */
  id: string;
  code: string;
  expires_at: string;
  ttl_seconds: number;
}

/** GET /api/settings/extension/pairing-code/{id}: "used" quando a extensão pareou com ele. */
export interface ExtensionPairingStatus {
  status: "pending" | "used" | "expired";
  device_id: string | null;
}

export interface ExtensionDevice {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
}

interface DeviceList {
  items: ExtensionDevice[];
}

async function call<T>(method: "GET" | "POST" | "DELETE", path: string): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), TIMEOUT_MS);
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers[CLIENT_HEADER] = CLIENT_VALUE;
  let response: Response;
  try {
    response = await fetch(`/api/settings/extension${path}`, {
      method,
      headers,
      signal: controller.signal,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch {
    if (controller.signal.aborted) throw new ApiError(0, "timeout", "O servidor demorou demais para responder.");
    throw new ApiError(0, "backend_offline", "Não foi possível falar com o backend local.");
  } finally {
    window.clearTimeout(timer);
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const obj = (payload && typeof payload === "object" ? payload : {}) as Record<string, unknown>;
    const code = typeof obj.code === "string" ? obj.code : `http_${response.status}`;
    const detail = typeof obj.detail === "string" ? obj.detail : null;
    throw new ApiError(response.status, code, errorMessageFor(code, response.status, detail), detail);
  }
  return payload as T;
}

export const extensionApi = {
  createPairingCode: () => call<ExtensionPairingCode>("POST", "/pairing-code"),
  pairingCodeStatus: (id: string) =>
    call<ExtensionPairingStatus>("GET", `/pairing-code/${encodeURIComponent(id)}`),
  listDevices: () => call<DeviceList>("GET", "/devices").then((r) => r.items),
  revokeDevice: (id: string) =>
    call<DeviceList>("DELETE", `/devices/${encodeURIComponent(id)}`).then((r) => r.items),
};

/** "ABCDEFGH" -> "ABCD-EFGH" (mais fácil de ler e digitar; o hífen é ignorado no pareamento). */
export function formatPairingCode(code: string): string {
  return code.length === 8 ? `${code.slice(0, 4)}-${code.slice(4)}` : code;
}
