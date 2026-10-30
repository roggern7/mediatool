// Configuração e estado local da extensão em chrome.storage.local (nunca sync: o token não deve sair deste computador).
// O pareamento (token) persiste entre aberturas do Chrome; nada aqui é mostrado na tela.
import { DEFAULT_PORT, normalizePort } from "./api.js";
import { FLOW_FIELDS, migrateFlowForm } from "./importModel.js";

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
 * Estado da interface guardado entre aberturas do popup: últimas escolhas da importação (`importForm`, inclusive o
 * fluxo após o download e as configurações de "Editar após baixar"), predefinições do usuário (`userPresets`),
 * configuração da aba Processar (`processForm`), seleção da Biblioteca
 * (`selection`) e a última exportação pedida (`lastExport`).
 */
export async function loadUiState(key, fallback) {
  const stored = await chrome.storage.local.get([key]);
  return stored[key] === undefined ? fallback : stored[key];
}

export async function saveUiState(key, value) {
  await chrome.storage.local.set({ [key]: value });
}

/**
 * Últimas escolhas da importação (`importForm`), já no formato atual: é a fonte ÚNICA do fluxo após o download,
 * usada pela aba Importar e pelo bloco "Depois de baixar" da Página atual. Migra o antigo `flow: "auto"` com a
 * configuração da aba Processar (`processForm`/`processStored`) e grava o resultado.
 */
export async function loadFlowForm() {
  const stored = await chrome.storage.local.get(["importForm", "processForm", "processStored"]);
  const { form, changed } = migrateFlowForm(stored.importForm, stored.processForm, stored.processStored);
  if (changed) await chrome.storage.local.set({ importForm: form });
  return form;
}

/** Grava só os campos do fluxo após o download (fluxo, edição, preset, ZIP) em `importForm`. */
export async function saveFlowFields(values) {
  const current = await loadFlowForm();
  const next = { ...current };
  for (const key of FLOW_FIELDS) if (key in values) next[key] = values[key];
  await chrome.storage.local.set({ importForm: next });
  return next;
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
