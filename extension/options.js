import { createClient, normalizePort } from "./src/lib/api.js";
import { describeError } from "./src/lib/popupModel.js";
import { forgetPairing, loadSettings, saveSettings } from "./src/lib/settings.js";

const $ = (id) => document.getElementById(id);
const settingsForm = $("settings-form");
const pairForm = $("pair-form");

function message(text, tone = "ok") {
  $("message").textContent = text;
  $("message").className = `alert ${tone}`;
  $("message").hidden = !text;
}

async function refreshConnection() {
  const settings = await loadSettings();
  settingsForm.elements.port.value = String(settings.port);
  settingsForm.elements.deviceName.value = settings.deviceName;
  $("disconnect").hidden = !settings.token;
  if (!settings.token) {
    $("connection-text").textContent = "Não conectada. Faça o pareamento abaixo.";
    $("connection").className = "alert warn";
    return;
  }
  try {
    const status = await createClient({ port: settings.port, token: settings.token }).status();
    $("connection-text").textContent = `Conectada ao Media Tool ${status.app.version} na porta ${settings.port}.`;
    $("connection").className = "alert ok";
  } catch (err) {
    $("connection-text").textContent = describeError(err).message;
    $("connection").className = "alert err";
  }
}

settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const port = normalizePort(settingsForm.elements.port.value);
  const deviceName = settingsForm.elements.deviceName.value.trim();
  if (!port) return message("Porta inválida (use um número de 1024 a 65535).", "err");
  if (!deviceName) return message("Informe um nome para este dispositivo.", "err");
  await saveSettings({ port, deviceName });
  message("Configurações salvas.");
  await refreshConnection();
});

pairForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const settings = await loadSettings();
  const code = pairForm.elements.code.value.trim();
  const port = normalizePort(settingsForm.elements.port.value) ?? settings.port;
  const deviceName = settingsForm.elements.deviceName.value.trim() || settings.deviceName;
  const button = pairForm.querySelector("button");
  button.disabled = true;
  try {
    const result = await createClient({ port }).pair(code, deviceName);
    // O token fica só em chrome.storage.local e nunca é mostrado.
    await saveSettings({ port, deviceName, token: result.token, deviceId: result.device_id });
    pairForm.reset();
    message("Pareamento concluído. Já pode usar o ícone da extensão numa aba do Instagram.");
  } catch (err) {
    message(describeError(err).message, "err");
  } finally {
    button.disabled = false;
    await refreshConnection();
  }
});

$("test").addEventListener("click", () => void refreshConnection());
$("disconnect").addEventListener("click", async () => {
  await forgetPairing();
  message("Desconectada neste navegador. Para revogar o acesso de vez, use Revogar em Configurações do Media Tool.");
  await refreshConnection();
});

void refreshConnection();
