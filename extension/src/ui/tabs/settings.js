// Aba "Configurações": porta, nome do dispositivo, pareamento por código, status da conexão e desconectar.
// O token fica só em chrome.storage.local e nunca aparece na tela.
import { createClient, normalizePort } from "../../lib/api.js";
import { describeError } from "../../lib/connection.js";
import { forgetPairing, loadSettings, saveSettings } from "../../lib/settings.js";
import { button, confirmBox, field, h, notice, setChildren } from "../dom.js";

export function settingsTab(panel, ctx) {
  const statusEl = h("div", { class: "box", id: "connection" });
  const msgEl = h("div", { class: "msg", id: "settings-message" });
  const portIn = h("input", { name: "port", type: "number", min: 1024, max: 65535, required: true });
  const nameIn = h("input", { name: "deviceName", type: "text", maxlength: 60, required: true });
  const codeIn = h("input", { name: "code", type: "text", maxlength: 16, autocomplete: "off", spellcheck: "false",
    placeholder: "ABCD-EFGH" });
  const pairBtn = h("button", { type: "submit", class: "btn primary", text: "Parear" });
  const settingsForm = h("form", { class: "form", id: "settings-form" },
    h("p", { class: "section-title", text: "Conexão com o Media Tool" }),
    field("Porta do Media Tool", portIn, "Padrão 8765. A extensão só tem permissão para 127.0.0.1:8765 e " +
      "localhost:8765; outra porta exige ajustar host_permissions no manifest.json e recarregar a extensão."),
    field("Nome deste dispositivo", nameIn),
    h("button", { type: "submit", class: "btn", text: "Salvar" }));
  const pairForm = h("form", { class: "form", id: "pair-form" },
    h("p", { class: "section-title", text: "Parear" }),
    h("p", { class: "hint", text: "No Media Tool, abra Configurações → Extensão do Chrome e clique em \"Gerar código\". " +
      "O código vale 5 minutos e só uma vez. O pareamento fica salvo neste navegador, mesmo depois de fechar o Chrome." }),
    field("Código de pareamento", codeIn), pairBtn);
  setChildren(panel, statusEl, msgEl, pairForm, settingsForm);

  function message(tone, text) {
    setChildren(msgEl, text ? notice(tone, text) : null);
  }

  async function refresh() {
    const settings = await loadSettings();
    portIn.value = String(settings.port);
    nameIn.value = settings.deviceName;
    const parts = [];
    if (!settings.token) {
      parts.push(h("p", { text: "Não pareada. Gere um código no Media Tool e digite abaixo." }));
      pairForm.hidden = false;
    } else {
      let line;
      let ok = false;
      try {
        const status = await createClient({ port: settings.port, token: settings.token }).status();
        line = `Conectada ao Media Tool ${status.app?.version ?? ""} na porta ${settings.port} como "${settings.deviceName}".`;
        ok = true;
        ctx.setConnected();
      } catch (err) {
        const view = describeError(err);
        line = view.state === "revoked" || view.state === "unpaired"
          ? "Pareamento perdido ou revogado no Media Tool. Gere um código novo e pareie abaixo." : view.message;
        if (view.state === "revoked" || view.state === "unpaired") ctx.handleError(err);
      }
      parts.push(h("p", { id: "connection-text", text: line }),
        h("div", { class: "row" }, button("Testar conexão", () => void refresh(), "btn small"),
          button("Desconectar", () => void disconnect(), "btn small", { id: "disconnect" })));
      pairForm.hidden = ok;
    }
    setChildren(statusEl, h("p", { class: "box-title", text: "Status" }), ...parts);
  }

  async function disconnect() {
    if (!(await confirmBox("Desconectar esta extensão?", "Apaga o pareamento só deste navegador. Para cortar o acesso " +
      "de vez, use Revogar em Configurações do Media Tool.", "Desconectar"))) return;
    await forgetPairing();
    message("ok", "Desconectada neste navegador.");
    await ctx.reconnect();
    await refresh();
  }

  settingsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const port = normalizePort(portIn.value);
    const deviceName = nameIn.value.trim();
    if (!port) return message("err", "Porta inválida (use um número de 1024 a 65535).");
    if (!deviceName) return message("err", "Informe um nome para este dispositivo.");
    await saveSettings({ port, deviceName });
    message("ok", "Configurações salvas.");
    await ctx.reconnect();
    await refresh();
  });

  pairForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const settings = await loadSettings();
    const code = codeIn.value.trim();
    if (!code) return message("err", "Digite o código mostrado no Media Tool.");
    const port = normalizePort(portIn.value) ?? settings.port;
    const deviceName = nameIn.value.trim() || settings.deviceName;
    pairBtn.disabled = true;
    try {
      const result = await createClient({ port }).pair(code, deviceName);
      await saveSettings({ port, deviceName, token: result.token, deviceId: result.device_id });
      codeIn.value = "";
      message("ok", "Pareamento concluído.");
      await ctx.reconnect();
    } catch (err) {
      message("err", describeError(err).message);
    } finally {
      pairBtn.disabled = false;
      await refresh();
    }
  });

  return {
    show() {
      void refresh();
    },
  };
}
