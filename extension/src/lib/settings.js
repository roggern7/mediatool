// Configuração da extensão em chrome.storage.local (nunca sync: o token não deve sair deste computador).
import { DEFAULT_PORT, normalizePort } from "./api.js";

const KEYS = ["port", "deviceName", "token", "deviceId"];

export async function loadSettings() {
  const stored = await chrome.storage.local.get(KEYS);
  return {
    port: normalizePort(stored.port) ?? DEFAULT_PORT,
    deviceName: typeof stored.deviceName === "string" && stored.deviceName ? stored.deviceName : "Chrome",
    token: typeof stored.token === "string" && stored.token ? stored.token : null,
    deviceId: typeof stored.deviceId === "string" ? stored.deviceId : null,
  };
}

export async function saveSettings(values) {
  await chrome.storage.local.set(values);
}

export async function forgetPairing() {
  await chrome.storage.local.remove(["token", "deviceId"]);
}
