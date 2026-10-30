// Casca da interface: a MESMA para o popup (popup.html) e o painel completo (app.html). Abas, conexão com o app,
// recuperação de erros (401 / app desligado / 429) e o estado compartilhado (seleção da Biblioteca).
import { baseUrl, createClient } from "../lib/api.js";
import { describeError, isBlocking } from "../lib/connection.js";
import { parseInstagramUrl } from "../lib/instagramUrl.js";
import { Selection } from "../lib/selection.js";
import { handOffPageUrl, loadSettings, loadUiState, saveUiState, takeHandOffPageUrl } from "../lib/settings.js";
import { button, h, setChildren } from "./dom.js";
import { ThumbLoader } from "./thumbs.js";
import { pageTab } from "./tabs/page.js";
import { importTab } from "./tabs/import.js";
import { activityTab } from "./tabs/activity.js";
import { libraryTab } from "./tabs/library.js";
import { processTab } from "./tabs/process.js";
import { exportTab } from "./tabs/export.js";
import { settingsTab } from "./tabs/settings.js";

const TABS = [
  { id: "page", label: "Página atual", short: "Página", factory: pageTab },
  { id: "import", label: "Importar", short: "Importar", factory: importTab },
  { id: "activity", label: "Atividade", short: "Atividade", factory: activityTab },
  { id: "library", label: "Biblioteca", short: "Biblioteca", factory: libraryTab },
  { id: "process", label: "Processar", short: "Processar", factory: processTab },
  { id: "export", label: "Exportar", short: "Exportar", factory: exportTab },
  { id: "settings", label: "Configurações", short: "Config.", factory: settingsTab },
];
const OFFLINE_RETRY_MS = 5_000;

const mode = document.body.classList.contains("mode-full") ? "full" : "popup";
const root = document.getElementById("app");

/** Instalação sem compactação (dev/teste): o manifesto não tem `update_url` (a loja sempre o acrescenta). */
function isUnpacked() {
  try {
    return !chrome.runtime.getManifest().update_url;
  } catch {
    return false;
  }
}

async function readPageUrl() {
  const params = new URLSearchParams(location.search);
  const testUrl = isUnpacked() ? params.get("testUrl") : null;
  if (testUrl) return testUrl;
  if (mode === "full") return takeHandOffPageUrl();
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return (tab && tab.url) || "";
  } catch {
    return "";
  }
}

const ctx = {
  mode,
  settings: null,
  client: null,
  status: null,
  pageUrl: "",
  selection: new Selection(),
  thumbs: null,
  connection: { state: "loading", message: "" },
  appUrl: (path = "/") => `${baseUrl(ctx.settings?.port)}${path}`,
  openUrl(url) {
    if (url) chrome.tabs.create({ url });
  },
  openApp(path) {
    ctx.openUrl(/^https?:/.test(path || "") ? path : ctx.appUrl(path || "/"));
  },
  go(tabId, params = {}) {
    showTab(tabId, params);
  },
  async saveSelection() {
    await saveUiState("selection", ctx.selection.toJSON());
    for (const tab of mounted.values()) tab.onSelection?.();
    renderSelectionCount();
  },
  /** Trata o erro: estados de conexão viram a faixa global; o resto devolve a mensagem para a aba mostrar. */
  handleError(err) {
    const view = describeError(err);
    if (isBlocking(view.state)) {
      setConnection(view.state, view.message);
      return null;
    }
    return view.message;
  },
  setConnected() {
    if (ctx.connection.state !== "ok") setConnection("ok", "");
  },
  async reconnect() {
    await connect();
  },
};
ctx.thumbs = new ThumbLoader(() => ctx.client);

/* ---------------- estrutura ---------------- */

const tabButtons = new Map();
const panels = new Map();
const mounted = new Map();
let currentTab = null;
let retryTimer = null;

const statusDot = h("span", { class: "conn-dot", title: "" });
const statusText = h("span", { class: "conn-text muted" });
const selectionCount = h("button", { type: "button", class: "link sel-count", hidden: true,
  onclick: () => showTab("library") });
const banner = h("div", { class: "banner", hidden: true });

const nav = h("nav", { class: "tabs", role: "tablist" });
for (const tab of TABS) {
  const btn = h("button", { type: "button", role: "tab", class: "tab", dataset: { tab: tab.id },
    onclick: () => showTab(tab.id) }, h("span", { class: "tab-long", text: tab.label }),
  h("span", { class: "tab-short", text: tab.short }));
  tabButtons.set(tab.id, btn);
  nav.append(btn);
  panels.set(tab.id, h("section", { class: "panel", role: "tabpanel", hidden: true, dataset: { panel: tab.id } }));
}

const header = h("header", { class: "head" },
  h("img", { src: "icons/icon32.png", alt: "", width: 18, height: 18 }),
  h("strong", { text: "Media Tool" }),
  statusDot, statusText,
  h("span", { class: "grow" }),
  selectionCount,
  mode === "popup" ? button("Abrir painel completo", openFullPanel, "btn small", { id: "open-full" }) : null);

setChildren(root, header, h("div", { class: "body" }, nav, h("main", { class: "content" }, banner, ...panels.values())));

async function openFullPanel() {
  await handOffPageUrl(ctx.pageUrl);
  chrome.tabs.create({ url: chrome.runtime.getURL("app.html") });
  window.close();
}

function renderSelectionCount() {
  const n = ctx.selection.size;
  selectionCount.hidden = n === 0;
  selectionCount.textContent = `${n} selecionado${n > 1 ? "s" : ""}`;
}

/* ---------------- conexão ---------------- */

function setConnection(state, message) {
  ctx.connection = { state, message };
  clearTimeout(retryTimer);
  retryTimer = null;
  statusDot.dataset.state = state;
  statusText.textContent = { ok: "conectado", offline: "sem conexão", unpaired: "não pareado",
    revoked: "pareamento perdido", loading: "conectando…" }[state] ?? "";
  if (state === "ok" || state === "loading") {
    banner.hidden = true;
    banner.replaceChildren();
  } else {
    const actions = [];
    if (state === "offline") {
      actions.push(button("Tentar de novo", () => void connect(), "btn", { id: "retry" }));
      // Recuperação automática enquanto a janela estiver visível.
      retryTimer = setTimeout(function tick() {
        if (document.visibilityState === "visible") void connect();
        else retryTimer = setTimeout(tick, OFFLINE_RETRY_MS);
      }, OFFLINE_RETRY_MS);
    } else if (currentTab !== "settings") {
      actions.push(button("Ir para Configurações", () => showTab("settings"), "btn"));
    }
    const hint = state === "offline" ? "Tentando de novo a cada 5 s." : null;
    setChildren(banner, h("p", { text: message }), hint ? h("p", { class: "muted small", text: hint }) : null,
      actions.length ? h("div", { class: "row" }, ...actions) : null);
    banner.className = `banner ${state === "offline" ? "warn" : "err"}`;
    banner.dataset.state = state;
    banner.hidden = false;
  }
  for (const [id, panel] of panels) {
    // Sem conexão/pareamento, só Configurações continua utilizável.
    panel.classList.toggle("blocked", isBlocking(state) && id !== "settings");
  }
  if ((state === "unpaired" || state === "revoked") && currentTab !== "settings") showTab("settings");
  for (const tab of mounted.values()) tab.onConnection?.(state);
}

async function connect() {
  ctx.settings = await loadSettings();
  if (!ctx.settings.token) {
    ctx.client = null;
    setConnection("unpaired", "A extensão ainda não está conectada ao Media Tool. Faça o pareamento abaixo.");
    return false;
  }
  ctx.client = createClient({ port: ctx.settings.port, token: ctx.settings.token });
  try {
    ctx.status = await ctx.client.status();
    setConnection("ok", "");
    if (currentTab) mounted.get(currentTab)?.show?.({ refresh: true });
    return true;
  } catch (err) {
    const view = describeError(err);
    setConnection(isBlocking(view.state) ? view.state : "offline", view.message);
    return false;
  }
}

/* ---------------- abas ---------------- */

function showTab(id, params = {}) {
  if (!panels.has(id)) id = "activity";
  if (currentTab && currentTab !== id) mounted.get(currentTab)?.hide?.();
  currentTab = id;
  for (const [tabId, btn] of tabButtons) {
    btn.setAttribute("aria-selected", String(tabId === id));
    btn.classList.toggle("active", tabId === id);
  }
  for (const [tabId, panel] of panels) panel.hidden = tabId !== id;
  let tab = mounted.get(id);
  if (!tab) {
    tab = TABS.find((t) => t.id === id).factory(panels.get(id), ctx);
    mounted.set(id, tab);
  }
  void saveUiState("lastTab", id);
  if (ctx.connection.state === "ok" || id === "settings") tab.show?.(params);
  if (ctx.connection.state !== "ok" && ctx.connection.state !== "loading") {
    for (const [tabId, panel] of panels) panel.classList.toggle("blocked", isBlocking(ctx.connection.state) && tabId !== "settings");
  }
}

document.addEventListener("visibilitychange", () => {
  const tab = currentTab && mounted.get(currentTab);
  if (!tab) return;
  if (document.visibilityState === "visible") tab.show?.({ refresh: true });
  else tab.hide?.();
});

async function init() {
  setConnection("loading", "");
  const [stored, pageUrl, lastTab] = await Promise.all([loadUiState("selection", []), readPageUrl(),
    loadUiState("lastTab", null)]);
  ctx.selection = new Selection(Array.isArray(stored) ? stored : []);
  ctx.pageUrl = pageUrl;
  renderSelectionCount();
  const ok = await connect();
  if (!ok && ctx.connection.state !== "offline") return; // já foi para Configurações
  const page = parseInstagramUrl(pageUrl);
  const params = new URLSearchParams(location.search);
  const wanted = params.get("tab");
  if (wanted && panels.has(wanted)) showTab(wanted);
  else if (page.kind !== "other") showTab("page");
  else showTab(mode === "full" ? (lastTab && lastTab !== "page" ? lastTab : "activity") : "activity");
}

void init();
