// Escolha "Depois de baixar" (Só baixar · Editar após baixar · Processar com preset · aguardar edição manual) com o
// formulário de edição ou o preset ali mesmo e o resumo do que será aplicado. Usada pela aba Importar e pelo bloco
// compacto da Página atual; as duas gravam a mesma configuração (`importForm` em chrome.storage.local).
import { FALLBACK_OPTIONS } from "../lib/editModel.js";
import {
  DEFAULT_EDIT, FLOW_OPTIONS, MEDIA_OPTIONS, NO_EDIT_MEDIA, PROCESSING_FLOWS, flowProblem, flowSummary,
} from "../lib/importModel.js";
import { button, h, select } from "./dom.js";
import { editControls, presetPicker } from "./editControls.js";

/** Opções da edição e presets do app (GET /api/ext/edit/options); sem resposta, os controles padrão. */
export async function fetchEditOptions(ctx) {
  try {
    return (await ctx.client.editOptions()) || FALLBACK_OPTIONS;
  } catch {
    return FALLBACK_OPTIONS;
  }
}

/**
 * state: {flow, edit, presetId, presetName, autoExport?, autoExportContent?} (alterado no lugar).
 * opts: {choices, options, media() → tipo de mídia ou null, open, collapsible, withZip, onChange(what)}.
 */
export function flowChooser(state, opts) {
  let options = opts.options ?? null;
  let open = opts.open ?? true;
  const media = () => (opts.media ? opts.media() : null);

  const flowSel = select("flow", [], state.flow, { "aria-label": "Depois de baixar", class: "flow-select" });
  const summaryEl = h("p", { class: "small flow-summary" });
  const toggle = button("", () => {
    open = !open;
    renderBody();
  }, "link small flow-toggle");
  const hintEl = h("p", { class: "hint" });
  const bodyEl = h("div", { class: "flow-body" });
  const el = h("div", { class: "flow-chooser" }, flowSel, h("div", { class: "row between flow-line" }, summaryEl,
    toggle), hintEl, bodyEl);

  function renderChoices() {
    const choices = (opts.choices ?? FLOW_OPTIONS.map((o) => o.value))
      .concat(FLOW_OPTIONS.some((o) => o.value === state.flow) ? [state.flow] : []);
    const unique = [...new Set(choices)];
    flowSel.replaceChildren(...FLOW_OPTIONS.filter((o) => unique.includes(o.value))
      .map((o) => new Option(o.label, o.value)));
    flowSel.value = state.flow;
  }

  function refresh() {
    const m = media();
    const problem = flowProblem(state, m, options);
    const summary = flowSummary(opts.withZip ? state : { ...state, autoExport: false }, options);
    summaryEl.textContent = problem ?? summary;
    summaryEl.classList.toggle("err-text", Boolean(problem));
    summaryEl.classList.toggle("muted", !problem && !PROCESSING_FLOWS.has(state.flow));
    const processing = PROCESSING_FLOWS.has(state.flow);
    let hint = "";
    if (state.flow === "manual") {
      hint = "Os itens ficam na Biblioteca para você editar depois (aba Processar ou Media Tool).";
    } else if (processing && NO_EDIT_MEDIA.has(m)) {
      hint = `Com "${MEDIA_OPTIONS.find((o) => o.value === m)?.label}" as mídias são só baixadas (sem processamento ` +
        "ou edição).";
    } else if (processing && (m === "all" || m === "videos_covers" || m === null)) {
      hint = "Só os vídeos são editados ou processados; fotos e capas são só baixadas.";
    }
    hintEl.textContent = hint;
    hintEl.hidden = !hint || (opts.collapsible && !open && m === null);
    return problem;
  }

  const changed = (what) => () => {
    refresh();
    opts.onChange?.(what);
  };

  function renderBody() {
    const processing = PROCESSING_FLOWS.has(state.flow);
    toggle.hidden = !opts.collapsible || !processing;
    toggle.textContent = open ? "Ocultar ajustes" : "Ajustar";
    toggle.setAttribute("aria-expanded", String(open));
    bodyEl.replaceChildren();
    if (processing && (open || !opts.collapsible)) {
      if (state.flow === "edit") {
        bodyEl.append(editControls(state.edit, options, changed("edit"), { compact: true }));
      } else {
        bodyEl.append(presetPicker(state, options, changed("preset"), { name: "processPreset" }));
      }
    }
    bodyEl.hidden = !bodyEl.childElementCount;
    refresh();
  }

  flowSel.addEventListener("change", () => {
    state.flow = flowSel.value;
    renderChoices();
    renderBody();
    opts.onChange?.("flow");
  });

  renderChoices();
  renderBody();

  return {
    el,
    state,
    refresh,
    get problem() {
      return flowProblem(state, media(), options);
    },
    get options() {
      return options;
    },
    /** Troca os valores (predefinição aplicada, configuração relida) e redesenha. */
    set(values) {
      for (const key of ["flow", "presetId", "presetName", "autoExport", "autoExportContent"]) {
        if (key in values) state[key] = values[key];
      }
      if (values.edit) state.edit = { ...DEFAULT_EDIT, ...values.edit };
      renderChoices();
      renderBody();
    },
    setOptions(next) {
      options = next;
      renderBody();
    },
  };
}
