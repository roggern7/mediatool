import { baseUrl, createClient } from "./src/lib/api.js";
import { fromPageInfo, parseInstagramUrl } from "./src/lib/instagramUrl.js";
import {
  FormError, VIEWS_PRESETS, buildPostBody, buildProfileBody, describeError, summarizeProgress, viewsLabel,
} from "./src/lib/popupModel.js";
import { loadSettings } from "./src/lib/settings.js";

const POLL_MS = 2_000;
const $ = (id) => document.getElementById(id);

let client = null;
let settings = null;
let page = { kind: "other" };
let pollTimer = null;
let lastOpenUrl = null;

function show(id, visible = true) {
  $(id).hidden = !visible;
}

function setError(message) {
  $("error").textContent = message || "";
  show("error", Boolean(message));
}

function setBusy(busy) {
  for (const el of document.querySelectorAll("main button, main select, main input")) {
    if (el.name === "postProcessing") continue;
    el.disabled = busy;
  }
  if (!busy) syncProfileForm();
}

function showState(state, message) {
  show("loading", false);
  for (const id of ["post", "profile", "other", "progress"]) show(id, false);
  $("state-message").textContent = message;
  $("state").className = state === "offline" ? "alert warn" : "alert err";
  show("state-options", state === "unpaired" || state === "revoked");
  show("state-retry", state === "offline");
  show("state");
}

function handleError(err) {
  const view = describeError(err);
  if (view.state === "error") {
    setError(view.message);
  } else {
    stopPolling();
    showState(view.state, view.message);
  }
}

/** Instalação sem compactação (dev/teste): o manifesto não tem `update_url` (a loja sempre o acrescenta). */
function isUnpacked() {
  try {
    return !chrome.runtime.getManifest().update_url;
  } catch {
    return false;
  }
}

/**
 * URL da aba ativa. `?testUrl=` existe só para o teste automatizado do popup aberto como página e só vale na
 * extensão sem compactação (sem `update_url`); numa instalação da loja é ignorado (docs/EXTENSION.md).
 */
async function currentTabUrl() {
  const testUrl = isUnpacked() ? new URLSearchParams(location.search).get("testUrl") : null;
  if (testUrl) return testUrl;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return (tab && tab.url) || "";
  } catch {
    return "";
  }
}

function openInApp(url) {
  if (url) chrome.tabs.create({ url });
}

/* ---------------- formulário do perfil ---------------- */

function fillViewsSelect(select, emptyLabel) {
  select.replaceChildren();
  const add = (value, label) => select.add(new Option(label, value));
  add("", emptyLabel);
  for (const n of VIEWS_PRESETS) add(String(n), viewsLabel(n));
  add("custom", "Personalizado");
}

function formValues() {
  const form = $("profile-form");
  const data = new FormData(form);
  const value = (name) => (data.get(name) ?? "").toString();
  return {
    media: value("media"), limit: value("limit"), limitCustom: value("limitCustom"), period: value("period"),
    periodCustom: value("periodCustom"), minViews: value("minViews"), minViewsCustom: value("minViewsCustom"),
    maxViews: value("maxViews"), maxViewsCustom: value("maxViewsCustom"),
    confirmAll: form.elements.confirmAll.checked,
  };
}

function syncProfileForm() {
  const form = $("profile-form");
  if (!form) return;
  const v = formValues();
  const when = (key, visible) => {
    for (const el of form.querySelectorAll(`[data-when="${key}"]`)) el.hidden = !visible;
  };
  when("limit-custom", v.limit === "custom");
  when("limit-all", v.limit === "all");
  when("period-custom", v.period === "custom");
  const viewsOff = v.media === "all";
  $("views").disabled = viewsOff;
  show("views-hint", viewsOff);
  when("min-custom", !viewsOff && v.minViews === "custom");
  when("max-custom", !viewsOff && v.maxViews === "custom");
}

function applyDefaults(defaults) {
  const form = $("profile-form");
  if (!defaults) return;
  if (defaults.media) form.elements.media.value = defaults.media;
  if (defaults.limit) {
    const preset = [...form.elements.limit.options].some((o) => o.value === String(defaults.limit));
    form.elements.limit.value = preset ? String(defaults.limit) : "custom";
    if (!preset) form.elements.limitCustom.value = String(defaults.limit);
  }
  syncProfileForm();
}

/* ---------------- iniciar e acompanhar ---------------- */

function stopPolling() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
}

async function poll(ids) {
  try {
    const progress = await client.progress(ids);
    renderProgress(progress);
    if (!progress.finished) pollTimer = setTimeout(() => void poll(ids), POLL_MS);
  } catch (err) {
    handleError(err);
  }
}

function renderProgress(progress) {
  lastOpenUrl = progress.open_url || lastOpenUrl;
  const list = $("progress-list");
  list.replaceChildren();
  for (const line of summarizeProgress(progress)) {
    const li = document.createElement("li");
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = line.title;
    const detail = document.createElement("div");
    detail.className = "detail";
    detail.textContent = line.detail;
    li.append(title, detail);
    list.append(li);
  }
  $("progress-note").textContent = progress.finished
    ? "Terminado."
    : "Atualiza a cada 2 s enquanto este popup estiver aberto. Fechar o popup não interrompe a importação.";
}

async function start(body) {
  setError("");
  setBusy(true);
  try {
    const result = await client.start(body);
    lastOpenUrl = result.open_url;
    const ids = { batchIds: result.batch_ids || [], groupId: result.group_id || null };
    for (const id of ["post", "profile"]) show(id, false);
    show("progress");
    const invalid = (result.invalid || []).map((i) => `${i.entry}: ${i.reason}`).join("; ");
    $("progress-note").textContent = invalid ? `Ignorado: ${invalid}` : "Enviado ao Media Tool.";
    if (ids.batchIds.length || ids.groupId) await poll(ids);
  } catch (err) {
    handleError(err);
  } finally {
    setBusy(false);
  }
}

async function collectOwner() {
  setError("");
  setBusy(true);
  $("post-owner").textContent = "Descobrindo o perfil…";
  try {
    const owner = await client.postOwner(page.canonicalUrl);
    page = { kind: "profile", username: owner.username, canonicalUrl: `https://www.instagram.com/${owner.username}/` };
    renderPage();
  } catch (err) {
    handleError(err);
  } finally {
    $("post-owner").textContent = "Coletar perfil desta publicação";
    setBusy(false);
  }
}

/* ---------------- tela ---------------- */

function renderPage() {
  for (const id of ["post", "profile", "other"]) show(id, false);
  if (page.kind === "post" || page.kind === "reel") {
    $("post-title").textContent = page.kind === "reel" ? "Reel" : "Publicação";
    $("post-url").textContent = page.canonicalUrl;
    show("post");
  } else if (page.kind === "profile") {
    $("profile-name").textContent = `@${page.username}`;
    show("profile");
    syncProfileForm();
  } else {
    show("other");
  }
}

function renderInstagram(ig) {
  const message = ig && (ig.paused || !ig.session_configured) ? ig.message : null;
  $("ig-warning").textContent = message || "";
  show("ig-warning", Boolean(message));
}

async function init() {
  stopPolling();
  setError("");
  show("state", false);
  show("loading");
  settings = await loadSettings();
  if (!settings.token) {
    showState("unpaired", "A extensão ainda não está conectada ao Media Tool. Abra as Opções e faça o pareamento.");
    return;
  }
  client = createClient({ port: settings.port, token: settings.token });
  const url = await currentTabUrl();
  try {
    const status = await client.status();
    renderInstagram(status.instagram);
    applyDefaults(status.defaults);
    if (!url) {
      page = { kind: "other" };
      show("loading", false);
      renderPage();
      setError("Não consegui ler o endereço desta aba. Clique no ícone da extensão com a aba do Instagram ativa.");
      return;
    }
    page = fromPageInfo(await client.pageInfo(url));
  } catch (err) {
    if (describeError(err).state === "error") page = parseInstagramUrl(url);
    show("loading", false);
    handleError(err);
    if (describeError(err).state !== "error") return;
  }
  show("loading", false);
  renderPage();
}

/* ---------------- eventos ---------------- */

$("open-options").addEventListener("click", () => chrome.runtime.openOptionsPage());
$("state-options").addEventListener("click", () => chrome.runtime.openOptionsPage());
$("state-retry").addEventListener("click", () => void init());
$("more-options").addEventListener("click", () => openInApp(`${baseUrl(settings?.port)}/`));
$("open-app").addEventListener("click", () => openInApp(lastOpenUrl || `${baseUrl(settings?.port)}/fila`));
$("open-library").addEventListener("click", () => openInApp(`${baseUrl(settings?.port)}/biblioteca`));
$("post-owner").addEventListener("click", () => void collectOwner());
for (const button of document.querySelectorAll("[data-post-action]")) {
  button.addEventListener("click", () => {
    try {
      void start(buildPostBody(page.canonicalUrl, button.dataset.postAction));
    } catch (err) {
      setError(err instanceof FormError ? err.message : String(err));
    }
  });
}
for (const select of document.querySelectorAll("select[data-views]")) {
  fillViewsSelect(select, select.name === "minViews" ? "Sem mínimo" : "Sem máximo");
}
$("profile-form").addEventListener("change", syncProfileForm);
$("profile-form").addEventListener("submit", (event) => {
  event.preventDefault();
  setError("");
  try {
    void start(buildProfileBody(page.username, formValues()));
  } catch (err) {
    setError(err instanceof FormError ? err.message : String(err));
  }
});
window.addEventListener("unload", stopPolling);

void init();
