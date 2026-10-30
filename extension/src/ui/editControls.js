// Controles de edição da página Editar do app (duração, velocidade, nitidez) e o seletor de preset de
// processamento. Compartilhados pela aba Processar, por "Editar após baixar" na aba Importar e pelo bloco
// "Depois de baixar" da Página atual: a mesma lógica, um layout compacto para o popup.
import {
  DURATION_NOTE, SPEED_NOTE, durationOptions, formatSpeed, formatSpeedPreset, parseSpeedInput, speedRange, stepSpeed,
} from "../lib/editModel.js";
import { button, checkbox, field, h, select } from "./dom.js";

/** Enter num campo de texto confirma o valor sem enviar o formulário em volta (Importar é um <form>). */
function commitOnEnter(input) {
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

/**
 * Duração, velocidade e nitidez. `settings` é alterado no lugar ({durationMode, customText, speed, sharpen}) e
 * `onChange()` é chamado depois de cada mudança. `compact`: layout reduzido (sem as notas de velocidade e saída).
 */
export function editControls(settings, options, onChange, { compact = false } = {}) {
  const range = speedRange(options);

  const durSel = select("durationMode", durationOptions(options), settings.durationMode);
  const max = options?.edit?.custom_seconds_max ?? 7200;
  const custom = h("input", { type: "text", name: "customText", inputmode: "decimal", value: settings.customText ?? "",
    placeholder: compact ? `até ${max}` : "segundos", "aria-label": "Duração personalizada (segundos)" });
  commitOnEnter(custom);
  const customBox = compact ? field("Segundos", custom) : custom;
  customBox.hidden = settings.durationMode !== "custom";
  durSel.addEventListener("change", () => {
    settings.durationMode = durSel.value;
    customBox.hidden = settings.durationMode !== "custom";
    onChange();
  });
  custom.addEventListener("input", () => {
    settings.customText = custom.value;
    onChange();
  });

  const input = h("input", { type: "text", name: "speed", inputmode: "decimal",
    value: formatSpeed(settings.speed).replace("x", ""), class: "speed-input", "aria-label": "Velocidade" });
  commitOnEnter(input);
  const slider = h("input", { type: "range", min: range.min, max: range.max, step: range.step, value: settings.speed,
    name: "speedRange", "aria-label": "Velocidade (barra)" });
  const error = h("span", { class: "hint err-text" });
  const chips = range.presets.map((p) => button(formatSpeedPreset(p), () => setSpeed(p),
    `chip${Number(settings.speed) === p ? " on" : ""}`, { dataset: { speed: String(p) } }));
  function setSpeed(value) {
    settings.speed = value;
    input.value = formatSpeed(value).replace("x", "");
    slider.value = String(value);
    error.textContent = "";
    for (const chip of chips) chip.classList.toggle("on", Number(chip.dataset.speed) === value);
    onChange();
  }
  input.addEventListener("change", () => {
    const value = parseSpeedInput(input.value, range);
    if (value === null) error.textContent = `Use um valor de ${formatSpeed(range.min)} a ${formatSpeed(range.max)}.`;
    else setSpeed(value);
  });
  slider.addEventListener("input", () => setSpeed(stepSpeed(Number(slider.value), 0, range)));
  const minus = button("−", () => setSpeed(stepSpeed(settings.speed, -1, range)), "btn small",
    { "aria-label": "Diminuir 0,01" });
  const plus = button("+", () => setSpeed(stepSpeed(settings.speed, 1, range)), "btn small",
    { "aria-label": "Aumentar 0,01" });

  const sharpen = checkbox("sharpen", compact ? "Nitidez sutil" : "Nitidez sutil (realce leve)", settings.sharpen);
  sharpen.input.addEventListener("change", () => {
    settings.sharpen = sharpen.input.checked;
    onChange();
  });
  const sharpenEl = options?.edit?.sharpen === false ? null : sharpen.el;

  if (compact) {
    return h("div", { class: "edit-controls compact" },
      h("div", { class: "grid2" }, field("Duração", durSel), customBox),
      h("span", { class: "hint", text: DURATION_NOTE }),
      h("div", { class: "field" }, h("span", { class: "field-label", text: "Velocidade" }),
        h("div", { class: "row speed-row" }, minus, input, h("span", { text: "x" }), plus, slider)),
      h("div", { class: "row wrap chips" }, ...chips), error, sharpenEl);
  }
  return h("div", { class: "edit-controls" },
    field("Duração", durSel, DURATION_NOTE), custom,
    h("fieldset", { class: "fieldset" }, h("legend", { text: "Velocidade" }),
      h("div", { class: "row" }, minus, input, h("span", { text: "x" }), plus),
      slider,
      h("div", { class: "row wrap" }, ...chips),
      h("span", { class: "hint", text: SPEED_NOTE }), error),
    sharpenEl,
    h("p", { class: "muted small", text: "Saída: MP4 (H.264 + AAC). Os originais nunca são alterados." }));
}

/** Seletor de preset de processamento (presets de /edit/options). Altera target.presetId/presetName. */
export function presetPicker(target, options, onChange, { name = "preset" } = {}) {
  const presets = options?.presets ?? [];
  const choices = [{ value: "", label: presets.length ? "Escolher…" : "Nenhum preset no app" },
    ...presets.map((p) => ({ value: String(p.id), label: p.name }))];
  // Preset salvo que não veio na lista (opções ainda carregando ou indisponíveis): continua visível.
  if (target.presetId && !presets.some((p) => Number(p.id) === Number(target.presetId))) {
    choices.push({ value: String(target.presetId), label: target.presetName ?? `Preset nº ${target.presetId}` });
  }
  const sel = select(name, choices, target.presetId ?? "");
  sel.addEventListener("change", () => {
    target.presetId = sel.value ? Number(sel.value) : null;
    target.presetName = presets.find((p) => String(p.id) === sel.value)?.name ?? null;
    onChange();
  });
  return field("Preset", sel, "Os presets são criados na página Presets do Media Tool.");
}
