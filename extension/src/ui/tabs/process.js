// Aba "Processar": os controles REAIS da página Editar (GET /api/ext/edit/options) ou um preset de processamento;
// aplica à seleção da Biblioteca (só vídeos) via POST /api/ext/edits ou /process. Os controles são o componente
// compartilhado com "Editar após baixar" (Importar e Página atual), mas a configuração desta aba é só dela.
import {
  DEFAULT_PROCESS, FALLBACK_OPTIONS, buildProcessRequest, editPagePath, processSummary, splitForEdit, validateProcess,
} from "../../lib/editModel.js";
import { selectionText } from "../../lib/selection.js";
import { loadUiState, saveUiState } from "../../lib/settings.js";
import { button, confirmBox, field, h, notice, select, setChildren } from "../dom.js";
import { editControls, presetPicker } from "../editControls.js";

export function processTab(panel, ctx) {
  let options = null;
  let form = { ...DEFAULT_PROCESS };
  let sending = false;
  let loaded = false;
  const msgEl = h("div", { class: "msg" });
  const body = h("div", { class: "stack" });
  setChildren(panel, msgEl, body);

  async function persist() {
    await saveUiState("processForm", form);
  }

  async function load() {
    form = { ...DEFAULT_PROCESS, ...((await loadUiState("processForm", null)) || {}) };
    try {
      options = await ctx.client.editOptions();
    } catch (err) {
      const text = ctx.handleError(err);
      options = FALLBACK_OPTIONS;
      if (text) setChildren(msgEl, notice("warn", `Não consegui ler as opções do Media Tool (${text}). ` +
        "Mostrando os controles padrão."));
    }
    const defaults = options?.defaults;
    if (defaults && !(await loadUiState("processForm", null))) {
      if (defaults.speed) form.speed = Number(defaults.speed);
      if (defaults.duration_mode) form.durationMode = String(defaults.duration_mode);
      if (typeof defaults.sharpen === "boolean") form.sharpen = defaults.sharpen;
    }
    loaded = true;
    render();
  }

  const summaryEl = h("p", { class: "small", id: "process-summary" });
  function renderSummary() {
    const problem = validateProcess(form, options);
    summaryEl.textContent = problem ? problem : `Será aplicado: ${processSummary(form, options)}.`;
    summaryEl.className = `small${problem ? " err-text" : ""}`;
    renderApply();
  }

  const applyEl = h("div", { class: "stack" });
  function renderApply() {
    const { videos, ignored } = splitForEdit(ctx.selection.entries());
    const problem = validateProcess(form, options);
    setChildren(applyEl,
      h("p", { class: "small", text: selectionText(ctx.selection) }),
      ignored ? h("p", { class: "small warn-text", text: `${ignored} ite${ignored > 1 ? "ns" : "m"} não ` +
        `${ignored > 1 ? "são vídeos e serão ignorados" : "é vídeo e será ignorado"}.` }) : null,
      h("div", { class: "row wrap" },
        button(sending ? "Enviando…" : `Aplicar a ${videos.length} vídeo${videos.length === 1 ? "" : "s"}`,
          () => void apply(), "btn primary", { disabled: sending || !videos.length || Boolean(problem), id: "apply" }),
        button("Escolher na Biblioteca", () => ctx.go("library"), "btn"),
        button("Recortar no Media Tool", () => ctx.openApp(editPagePath(splitForEdit(ctx.selection.entries()).videos)),
          "btn", { title: "Abre a página Editar do Media Tool com os vídeos selecionados (até 500); o recorte " +
          "manual é feito lá." })));
  }

  async function apply() {
    const { videos, ignored } = splitForEdit(ctx.selection.entries());
    let req;
    try {
      req = buildProcessRequest(form, videos);
    } catch (err) {
      setChildren(msgEl, notice("err", err.message));
      return;
    }
    if (videos.length > 20 && !(await confirmBox(`Processar ${videos.length} vídeos?`,
      `${processSummary(form, options)}${ignored ? `. ${ignored} itens não são vídeo e serão ignorados.` : "."}`,
      "Processar"))) return;
    sending = true;
    renderApply();
    try {
      const result = req.route === "process" ? await ctx.client.process(req.body.media_ids, req.body.preset_id)
        : await ctx.client.edits(req.body);
      const skipped = result?.skipped?.length ?? 0;
      setChildren(msgEl, notice("ok", `Processamento criado${result?.batch_id ? ` (lote ${result.batch_id})` : ""}.` +
        `${skipped ? ` ${skipped} itens ignorados (não são vídeo).` : ""}`,
      button("Acompanhar na Atividade", () => ctx.go("activity"), "btn small")));
    } catch (err) {
      const text = ctx.handleError(err);
      if (text) setChildren(msgEl, notice("err", text));
    } finally {
      sending = false;
      renderApply();
    }
  }

  function render() {
    const modeSel = select("mode", [{ value: "edit", label: "Edição (duração, velocidade, nitidez)" },
      { value: "preset", label: "Usar preset de processamento" }], form.mode);
    modeSel.addEventListener("change", () => {
      form.mode = modeSel.value;
      void persist();
      render();
    });
    const parts = [h("p", { class: "section-title", text: "Processar vídeos" }), field("Modo", modeSel)];
    const changed = () => {
      void persist();
      renderSummary();
    };
    if (form.mode === "preset") parts.push(presetPicker(form, options, changed));
    else parts.push(editControls(form, options, changed));
    parts.push(summaryEl, h("hr"), applyEl);
    setChildren(body, ...parts);
    renderSummary();
  }

  return {
    show() {
      if (!loaded) void load();
      else renderApply();
    },
    onSelection() {
      if (loaded) renderApply();
    },
  };
}
