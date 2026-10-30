// Configuração e estado local da extensão em chrome.storage.local (nunca sync: o token não deve sair deste computador).
// O pareamento (token) persiste entre aberturas do Chrome; nada aqui é mostrado na tela.
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

/**
 * Estado da interface guardado entre aberturas do popup: últimas escolhas da importação (`importForm`),
 * predefinições do usuário (`userPresets`), configuração do processamento (`processForm`), seleção da Biblioteca
 * (`selection`) e a última exportação pedida (`lastExport`).
 */
export async function loadUiState(key, fallback) {
  const stored = await chrome.storage.local.get([key]);
  return stored[key] === undefined ? fallback : stored[key];
}

export async function saveUiState(key, value) {
  await chrome.storage.local.set({ [key]: value });
}

/** Página do Instagram entregue ao painel completo (só nesta sessão do navegador). */
export async function handOffPageUrl(url) {
  try {
    await chrome.storage.session.set({ handoffUrl: url || "" });
  } catch {
    // storage.session indisponível: o painel mostra "abra pelo ícone".
  }
}

export async function takeHandOffPageUrl() {
  try {
    const { handoffUrl } = await chrome.storage.session.get(["handoffUrl"]);
    return typeof handoffUrl === "string" ? handoffUrl : "";
  } catch {
    return "";
  }
}
