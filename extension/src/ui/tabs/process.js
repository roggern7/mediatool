// Aba "Processar": os controles REAIS da página Editar (GET /api/ext/edit/options) ou um preset de processamento;
// aplica à seleção da Biblioteca (só vídeos) via POST /api/ext/edits ou /process. A configuração também é a usada
// pelo "Baixar e processar automaticamente" da importação.
import {
  DEFAULT_PROCESS, DURATION_NOTE, FALLBACK_OPTIONS, SPEED_NOTE, buildProcessRequest, durationOptions, editPagePath,
  formatSpeed,
  formatSpeedPreset, parseSpeedInput, processSummary, speedRange, splitForEdit, stepSpeed, toStoredProcess,
  validateProcess,
} from "../../lib/editModel.js";
import { selectionText } from "../../lib/selection.js";
import { loadUiState, saveUiState } from "../../lib/settings.js";
import { button, checkbox, confirmBox, field, h, notice, select, setChildren } from "../dom.js";

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
    await saveUiState("processStored", toStoredProcess(form, options));
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

  function speedControls() {
    const range = speedRange(options);
    const input = h("input", { type: "text", name: "speed", inputmode: "decimal", value: formatSpeed(form.speed)
      .replace("x", ""), class: "speed-input", "aria-label": "Velocidade" });
    const slider = h("input", { type: "range", min: range.min, max: range.max, step: range.step, value: form.speed,
      name: "speedRange", "aria-label": "Velocidade (barra)" });
    const error = h("span", { class: "hint err-text" });
    const chips = range.presets.map((p) => button(formatSpeedPreset(p), () => setSpeed(p),
      `chip${Number(form.speed) === p ? " on" : ""}`, { dataset: { speed: String(p) } }));
    const setSpeed = (value) => {
      form.speed = value;
      input.value = formatSpeed(value).replace("x", "");
      slider.value = String(value);
      error.textContent = "";
      for (const chip of chips) chip.classList.toggle("on", Number(chip.dataset.speed) === value);
      void persist();
      renderSummary();
    };
    input.addEventListener("change", () => {
      const value = parseSpeedInput(input.value, range);
      if (value === null) error.textContent = `Use um valor de ${formatSpeed(range.min)} a ${formatSpeed(range.max)}.`;
      else setSpeed(value);
    });
    slider.addEventListener("input", () => setSpeed(stepSpeed(Number(slider.value), 0, range)));
    return h("fieldset", { class: "fieldset" }, h("legend", { text: "Velocidade" }),
      h("div", { class: "row" },
        button("−", () => setSpeed(stepSpeed(form.speed, -1, range)), "btn small", { "aria-label": "Diminuir 0,01" }),
        input, h("span", { text: "x" }),
        button("+", () => setSpeed(stepSpeed(form.speed, 1, range)), "btn small", { "aria-label": "Aumentar 0,01" })),
      slider,
      h("div", { class: "row wrap" }, ...chips),
      h("span", { class: "hint", text: SPEED_NOTE }), error);
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
    if (form.mode === "preset") {
      const presets = options?.presets ?? [];
      const presetSel = select("preset", [{ value: "", label: presets.length ? "Escolher…" : "Nenhum preset no app" },
        ...presets.map((p) => ({ value: String(p.id), label: p.name }))], form.presetId ?? "");
      presetSel.addEventListener("change", () => {
        form.presetId = presetSel.value ? Number(presetSel.value) : null;
        form.presetName = presets.find((p) => String(p.id) === presetSel.value)?.name ?? null;
        void persist();
        renderSummary();
      });
      parts.push(field("Preset", presetSel, "Os presets são criados na página Presets do Media Tool."));
    } else {
      const durSel = select("durationMode", durationOptions(options), form.durationMode);
      const custom = h("input", { type: "text", name: "customText", inputmode: "decimal", value: form.customText,
        placeholder: "segundos", hidden: form.durationMode !== "custom" });
      durSel.addEventListener("change", () => {
        form.durationMode = durSel.value;
        custom.hidden = form.durationMode !== "custom";
        void persist();
        renderSummary();
      });
      custom.addEventListener("input", () => {
        form.customText = custom.value;
        void persist();
        renderSummary();
      });
      const sharpen = checkbox("sharpen", "Nitidez sutil (realce leve)", form.sharpen);
      sharpen.input.addEventListener("change", () => {
        form.sharpen = sharpen.input.checked;
        void persist();
        renderSummary();
      });
      parts.push(field("Duração", durSel, DURATION_NOTE), custom, speedControls(),
        options?.edit?.sharpen === false ? null : sharpen.el,
        h("p", { class: "muted small", text: "Saída: MP4 (H.264 + AAC). Os originais nunca são alterados." }));
    }
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
