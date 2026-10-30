// Aba "Importar": um perfil ou vários links/perfis, tipo de mídia, quantidade, período, visualizações, fluxo após
// o download, exportação automática e predefinições de operação. Envia POST /api/ext/start com client_request_id.
import {
  BUILTIN_PRESETS, DEFAULT_FORM, EXPORT_CONTENT_OPTIONS, FLOW_OPTIONS, FormError, LIMIT_PRESETS, MEDIA_OPTIONS,
  PERIOD_PRESETS, RequestIdKeeper, VIEWS_DISABLED_TEXT, VIEWS_MEDIA, VIEWS_PRESETS, applyPreset, buildStartBody,
  classifyEntries, daysSinceYearStart, describeStartBody, makeUserPreset, needsConfirmation, viewsLabel,
} from "../../lib/importModel.js";
import { processSummary, DEFAULT_PROCESS } from "../../lib/editModel.js";
import { loadUiState, saveUiState } from "../../lib/settings.js";
import { button, checkbox, confirmBox, field, h, notice, select, setChildren } from "../dom.js";

const LIMIT_OPTIONS = [...LIMIT_PRESETS.map((n) => ({ value: String(n), label: String(n) })),
  { value: "custom", label: "Personalizada" }, { value: "all", label: "Todas" }];
const PERIOD_OPTIONS = [{ value: "any", label: "Qualquer data" },
  ...PERIOD_PRESETS.map((n) => ({ value: String(n), label: `Últimos ${n} dias` })),
  { value: "year", label: "Ano atual" }, { value: "custom", label: "Personalizado (dias)" }];
const viewsOptions = (empty) => [{ value: "", label: empty },
  ...VIEWS_PRESETS.map((n) => ({ value: String(n), label: viewsLabel(n) })), { value: "custom", label: "Personalizado" }];

export function importTab(panel, ctx) {
  let form = { ...DEFAULT_FORM };
  let userPresets = [];
  let processForm = { ...DEFAULT_PROCESS };
  let message = null;
  let sending = false;
  let loaded = false;
  const keeper = new RequestIdKeeper();

  const formEl = h("form", { class: "form", id: "import-form", novalidate: true });
  const msgEl = h("div", { class: "msg" });
  const previewEl = h("p", { class: "small muted", id: "classify" });
  const presetSel = h("select", { name: "preset", id: "preset" });
  setChildren(panel, msgEl, formEl);

  /* ---------- campos ---------- */
  const sourceSel = select("source", [{ value: "profile", label: "Um perfil" },
    { value: "many", label: "Vários links e perfis" }]);
  const profileIn = h("input", { name: "profile", type: "text", placeholder: "@perfil", autocomplete: "off",
    spellcheck: "false" });
  const textIn = h("textarea", { name: "text", rows: 5, placeholder: "Um por linha: @perfil, links de perfis, " +
    "publicações ou Reels", spellcheck: "false" });
  const mediaSel = select("media", MEDIA_OPTIONS);
  const limitSel = select("limit", LIMIT_OPTIONS);
  const limitCustom = h("input", { name: "limitCustom", type: "number", min: 1, max: 1000000, inputmode: "numeric" });
  const confirmAll = checkbox("confirmAll", "Entendo que \"Todas\" pode levar muito tempo e fazer muitas consultas " +
    "ao Instagram.");
  const periodSel = select("period", PERIOD_OPTIONS);
  const periodCustom = h("input", { name: "periodCustom", type: "number", min: 1, max: 3650, inputmode: "numeric" });
  const periodHint = h("span", { class: "hint" });
  const minSel = select("minViews", viewsOptions("Sem mínimo"));
  const maxSel = select("maxViews", viewsOptions("Sem máximo"));
  const minCustom = h("input", { name: "minViewsCustom", type: "number", min: 1, inputmode: "numeric",
    placeholder: "mínimo" });
  const maxCustom = h("input", { name: "maxViewsCustom", type: "number", min: 1, inputmode: "numeric",
    placeholder: "máximo" });
  const viewsHint = h("p", { class: "hint", text: VIEWS_DISABLED_TEXT });
  const viewsSet = h("fieldset", { class: "fieldset", id: "views" }, h("legend", { text: "Visualizações" }),
    h("div", { class: "grid2" }, field("Mínimo", minSel), field("Máximo", maxSel)),
    h("div", { class: "grid2" }, minCustom, maxCustom), viewsHint);
  const flowSel = select("flow", FLOW_OPTIONS);
  const flowHint = h("p", { class: "hint" });
  const autoExport = checkbox("autoExport", "Exportar ZIP automaticamente ao concluir");
  const exportContent = select("autoExportContent", EXPORT_CONTENT_OPTIONS);
  const submit = h("button", { type: "submit", class: "btn primary", id: "import-submit", text: "Iniciar importação" });
  const profileFilters = h("div", { class: "stack" },
    h("div", { class: "grid2" }, field("Quantidade por perfil", limitSel), field("Período", periodSel)),
    h("div", { class: "grid2" }, limitCustom, h("div", {}, periodCustom, periodHint)),
    confirmAll.el, viewsSet);
  const linksNote = h("p", { class: "hint", text: "Quantidade, período e visualizações valem só para perfis; links " +
    "de publicações baixam a publicação inteira conforme o tipo de mídia." });

  setChildren(formEl,
    h("div", { class: "row bottom" }, field("Predefinição", presetSel),
      button("Salvar atual como…", () => void savePreset(), "btn small", { id: "save-preset" })),
    field("Origem", sourceSel),
    h("div", { dataset: { when: "profile" } }, field("Perfil", profileIn)),
    h("div", { dataset: { when: "many" } }, field("Links e perfis", textIn), previewEl),
    field("Tipo de mídia", mediaSel),
    linksNote,
    profileFilters,
    h("fieldset", { class: "fieldset" }, h("legend", { text: "Depois do download" }), flowSel, flowHint,
      autoExport.el, h("div", { dataset: { when: "export" } }, field("Conteúdo do ZIP", exportContent))),
    h("p", { class: "small muted", id: "import-summary" }),
    submit);

  /* ---------- estado ⇄ tela ---------- */
  function readForm() {
    form = {
      source: sourceSel.value, profile: profileIn.value, text: textIn.value, media: mediaSel.value,
      limit: limitSel.value, limitCustom: limitCustom.value, confirmAll: confirmAll.input.checked,
      period: periodSel.value, periodCustom: periodCustom.value, minViews: minSel.value, minViewsCustom: minCustom.value,
      maxViews: maxSel.value, maxViewsCustom: maxCustom.value, flow: flowSel.value,
      autoExport: autoExport.input.checked, autoExportContent: exportContent.value,
    };
    return form;
  }

  function writeForm() {
    sourceSel.value = form.source;
    profileIn.value = form.profile;
    textIn.value = form.text;
    mediaSel.value = form.media;
    limitSel.value = LIMIT_OPTIONS.some((o) => o.value === String(form.limit)) ? String(form.limit) : "50";
    limitCustom.value = form.limitCustom;
    confirmAll.input.checked = Boolean(form.confirmAll);
    periodSel.value = PERIOD_OPTIONS.some((o) => o.value === form.period) ? form.period : "any";
    periodCustom.value = form.periodCustom;
    minSel.value = form.minViews;
    minCustom.value = form.minViewsCustom;
    maxSel.value = form.maxViews;
    maxCustom.value = form.maxViewsCustom;
    flowSel.value = form.flow;
    autoExport.input.checked = Boolean(form.autoExport);
    exportContent.value = form.autoExportContent;
    sync();
  }

  function sync() {
    const f = readForm();
    for (const el of formEl.querySelectorAll("[data-when]")) {
      const key = el.dataset.when;
      el.hidden = key === "export" ? !f.autoExport : key !== f.source;
    }
    let hasProfiles = f.source === "profile";
    let hasLinks = false;
    if (f.source === "many") {
      const c = classifyEntries(f.text);
      hasProfiles = c.profiles.length > 0;
      hasLinks = c.links.length > 0;
      previewEl.textContent = f.text.trim()
        ? `Prévia: ${c.profiles.length} perfis, ${c.links.length} publicações, ${c.invalid.length} inválidos` +
          `${c.invalid.length ? ` (${c.invalid.slice(0, 3).join(", ")}${c.invalid.length > 3 ? "…" : ""})` : ""}, ` +
          `${c.duplicates} repetidos. O Media Tool confirma ao iniciar.`
        : "";
    }
    profileFilters.hidden = !hasProfiles && f.source === "many";
    linksNote.hidden = !hasLinks;
    limitCustom.hidden = f.limit !== "custom";
    confirmAll.el.hidden = f.limit !== "all";
    periodCustom.hidden = f.period !== "custom";
    periodHint.textContent = f.period === "year" ? `Desde 1º de janeiro: ${daysSinceYearStart()} dias.` : "";
    const viewsOn = VIEWS_MEDIA.has(f.media);
    viewsSet.disabled = !viewsOn;
    viewsHint.hidden = viewsOn;
    minCustom.hidden = !viewsOn || f.minViews !== "custom";
    maxCustom.hidden = !viewsOn || f.maxViews !== "custom";
    flowHint.textContent = f.flow === "auto"
      ? `Processamento (aba Processar): ${processSummary(processForm, null)}.`
      : f.flow === "manual" ? "Os itens ficam na Biblioteca para você editar depois (aba Processar ou Media Tool)."
        : "";
    if (f.flow === "auto") {
      flowHint.append(" ", button("Configurar", () => ctx.go("process"), "link"));
    }
    submit.disabled = sending;
    submit.textContent = sending ? "Enviando…" : "Iniciar importação";
    try {
      formEl.querySelector("#import-summary").textContent = `Resumo: ${describeStartBody(buildStartBody(f,
        { mode: "edit", edit: {} }))}`;
    } catch {
      formEl.querySelector("#import-summary").textContent = "";
    }
  }

  function renderPresets(selectedId = "") {
    presetSel.replaceChildren(new Option("Escolher…", ""));
    const builtin = h("optgroup", { label: "Prontas" });
    for (const p of BUILTIN_PRESETS) builtin.append(new Option(p.name, p.id));
    presetSel.append(builtin);
    if (userPresets.length) {
      const mine = h("optgroup", { label: "Minhas" });
      for (const p of userPresets) mine.append(new Option(p.name, p.id));
      presetSel.append(mine);
    }
    presetSel.value = selectedId;
  }

  function setMessage(tone, text, ...extra) {
    message = text ? { tone, text } : null;
    setChildren(msgEl, text ? notice(tone, text, ...extra) : null);
  }

  async function savePreset() {
    const name = await promptName();
    if (!name) return;
    try {
      const preset = makeUserPreset(name, readForm());
      userPresets = [...userPresets.filter((p) => p.name !== preset.name), preset].slice(-20);
      await saveUiState("userPresets", userPresets);
      renderPresets(preset.id);
      setMessage("ok", `Predefinição "${preset.name}" salva neste navegador.`);
    } catch (err) {
      setMessage("err", err.message);
    }
  }

  function promptName() {
    return new Promise((resolve) => {
      const input = h("input", { type: "text", maxlength: 40, name: "presetName", placeholder: "Nome" });
      const dialog = h("dialog", { class: "confirm" });
      const done = (value) => {
        dialog.close();
        dialog.remove();
        resolve(value);
      };
      setChildren(dialog, h("p", { class: "confirm-title", text: "Salvar predefinição" }), input,
        h("div", { class: "row end" }, button("Cancelar", () => done(null), "btn"),
          button("Salvar", () => done(input.value.trim() || null), "btn primary", { dataset: { confirm: "yes" } })));
      document.body.append(dialog);
      dialog.showModal();
      input.focus();
    });
  }

  async function onSubmit(event) {
    event.preventDefault();
    if (sending) return;
    setMessage(null, "");
    const f = readForm();
    let body;
    try {
      const stored = f.flow === "auto" ? await loadUiState("processStored", null) : null;
      body = buildStartBody(f, stored);
    } catch (err) {
      setMessage("err", err instanceof FormError ? err.message : String(err.message || err));
      return;
    }
    const big = needsConfirmation(body);
    if (big && !(await confirmBox("Confirmar operação grande", `${big} ${describeStartBody(body)}.`, "Iniciar"))) return;
    body.client_request_id = keeper.idFor(body);
    sending = true;
    sync();
    await saveUiState("importForm", { ...f, confirmAll: false, text: "", profile: "" });
    try {
      const result = await ctx.client.start(body);
      keeper.succeeded();
      const notes = [];
      if (result.invalid?.length) notes.push(`Inválidos: ${result.invalid.map((i) => `${i.entry} (${i.reason})`).join("; ")}`);
      if (result.duplicates) notes.push(`${result.duplicates} repetidos ignorados`);
      if (result.skipped?.length) notes.push(`Pulados (já em andamento): ${result.skipped.map((s) => s.profile ?? s.entry ?? "")
        .join(", ")}`);
      for (const n of result.notes || []) notes.push(n);
      const created = (result.batch_ids?.length || 0) + (result.group_id ? 1 : 0);
      setMessage(created ? "ok" : "warn", created ? "Importação iniciada. Acompanhe em Atividade." :
        "Nada foi criado.", notes.length ? h("p", { class: "small", text: notes.join(" · ") }) : null,
      created ? button("Ver Atividade", () => ctx.go("activity"), "btn small") : null);
    } catch (err) {
      const text = ctx.handleError(err);
      if (text) setMessage("err", text);
    } finally {
      sending = false;
      sync();
    }
  }

  formEl.addEventListener("input", sync);
  formEl.addEventListener("change", (event) => {
    if (event.target === presetSel) {
      const preset = [...BUILTIN_PRESETS, ...userPresets].find((p) => p.id === presetSel.value);
      if (preset) {
        form = applyPreset(readForm(), preset);
        writeForm();
      }
      return;
    }
    sync();
  });
  formEl.addEventListener("submit", (event) => void onSubmit(event));

  async function loadState() {
    const [last, presets, proc] = await Promise.all([loadUiState("importForm", {}), loadUiState("userPresets", []),
      loadUiState("processForm", null)]);
    form = { ...DEFAULT_FORM, ...(last || {}), confirmAll: false };
    if (!last || !last.limit) {
      const def = ctx.status?.defaults?.limit;
      if (def) form.limit = LIMIT_OPTIONS.some((o) => o.value === String(def)) ? String(def) : "custom";
    }
    userPresets = Array.isArray(presets) ? presets : [];
    processForm = { ...DEFAULT_PROCESS, ...(proc || {}) };
    renderPresets();
    loaded = true;
  }

  return {
    async show(params = {}) {
      if (!loaded) await loadState();
      else processForm = { ...DEFAULT_PROCESS, ...((await loadUiState("processForm", null)) || {}) };
      if (params.profile) form = { ...form, source: "profile", profile: `@${params.profile}` };
      writeForm();
    },
    get message() {
      return message;
    },
  };
}
