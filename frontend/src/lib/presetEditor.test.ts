import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { PresetParams } from "../api/types";
import { OptionalAdjustmentsForm } from "../components/OptionalAdjustmentsForm";
import {
  DEFAULT_PARAMS,
  OPTIONAL_ADJUSTMENTS,
  compactParams,
  editorParams,
  initialOptionalState,
  validatePreset,
} from "./presets";
import type { OptionalKey, OptionalState, PresetFlags } from "./presets";

const ANTI_DUPLICATE: PresetParams = {
  ...DEFAULT_PARAMS,
  zoom: 3,
  brightness: 0.01,
  contrast: 1.02,
  saturation: 1.03,
  trim_end: 0.3,
};
const FLAGS: PresetFlags = { metadata_cleanup: true, mirror: true, auto_adjust: true, reencode: true };
const KEYS = OPTIONAL_ADJUSTMENTS.map((a) => a.key);

/** Params e corpo enviado à API, como o editor monta (corte 0,5 e velocidade 1,05 nos campos de texto). */
function fromEditor(optional: OptionalState) {
  const params = editorParams(FLAGS, "0,5", "1,05", optional);
  return { params, body: compactParams(params), errors: validatePreset("a", params) };
}

function set(optional: OptionalState, key: OptionalKey, change: Partial<OptionalState[OptionalKey]>): OptionalState {
  return { ...optional, [key]: { ...optional[key], ...change } };
}

function markup(optional: OptionalState): string {
  const { errors } = fromEditor(optional);
  return renderToStaticMarkup(createElement(OptionalAdjustmentsForm, { optional, errors, onChange: () => {} }));
}

/** Interruptores marcados e campos de valor (aria-label -> value) presentes na marcação. */
function rendered(html: string) {
  const switches = [...html.matchAll(/<input type="checkbox" role="switch"( checked="")?[^>]*\/><span[^>]*><\/span><span>([^<]*)<\/span>/g)];
  const fields = [...html.matchAll(/<input[^>]*aria-label="([^"]*)"[^>]*value="([^"]*)"\/>/g)];
  return {
    labels: switches.map((m) => m[2]),
    on: switches.filter((m) => m[1] !== undefined).map((m) => m[2]),
    values: Object.fromEntries(fields.map((m) => [m[1], m[2]])),
  };
}

describe("editor de preset: ajustes opcionais", () => {
  it("preset antigo (6 chaves) aparece com todos os ajustes desligados", () => {
    const optional = initialOptionalState(DEFAULT_PARAMS);
    for (const adjustment of OPTIONAL_ADJUSTMENTS) {
      // desligado, com o valor sugerido já no campo para quando for ligado
      expect(optional[adjustment.key]).toEqual({ on: false, text: String(adjustment.suggested).replace(".", ",") });
    }
    const view = rendered(markup(optional));
    expect(view.labels).toEqual(OPTIONAL_ADJUSTMENTS.map((a) => a.label));
    expect(view.on).toEqual([]);
    expect(view.values).toEqual({});
    expect(markup(optional).match(/Desligado/g)).toHaveLength(7);
    // salvar sem mexer mantém as 6 chaves
    expect(fromEditor(optional).body).toEqual(DEFAULT_PARAMS);
    expect(fromEditor(optional).errors).toEqual({});
  });

  it("Anti-duplicidade mostra os 5 ajustes ligados com os valores certos", () => {
    const optional = initialOptionalState(ANTI_DUPLICATE);
    expect(KEYS.filter((key) => optional[key].on)).toEqual(["zoom", "brightness", "contrast", "saturation", "trim_end"]);
    const html = markup(optional);
    const view = rendered(html);
    expect(view.on).toEqual(["Zoom central", "Brilho", "Contraste", "Saturação", "Cortar final"]);
    expect(view.values).toEqual({
      "Zoom central": "3",
      Brilho: "0,01",
      Contraste: "1,02",
      Saturação: "1,03",
      "Cortar final": "0,3",
    });
    expect(html.match(/Desligado/g)).toHaveLength(2); // tom e granulação
    expect(html).not.toContain("field-error");
    // salvar sem mexer grava os mesmos params
    expect(fromEditor(optional).body).toEqual(ANTI_DUPLICATE);
  });

  it("desligar um ajuste remove a chave do corpo", () => {
    let optional = initialOptionalState(ANTI_DUPLICATE);
    for (const key of ["zoom", "trim_end"] as const) {
      optional = set(optional, key, { on: false });
      expect(key in fromEditor(optional).body).toBe(false);
    }
    expect(fromEditor(optional).body).toEqual({ ...DEFAULT_PARAMS, brightness: 0.01, contrast: 1.02, saturation: 1.03 });
    for (const key of KEYS) optional = set(optional, key, { on: false });
    expect(fromEditor(optional).body).toEqual(DEFAULT_PARAMS);
  });

  it("ligar grava o valor sugerido; ligar com o valor neutro não grava", () => {
    const off = initialOptionalState(DEFAULT_PARAMS);
    for (const adjustment of OPTIONAL_ADJUSTMENTS) {
      const on = set(off, adjustment.key, { on: true });
      expect(fromEditor(on).body).toEqual({ ...DEFAULT_PARAMS, [adjustment.key]: adjustment.suggested });
      expect(fromEditor(on).errors).toEqual({});
      const neutral = set(on, adjustment.key, { text: String(adjustment.neutral) });
      expect(fromEditor(neutral).errors).toEqual({});
      expect(fromEditor(neutral).body).toEqual(DEFAULT_PARAMS);
    }
  });

  it("validação de faixa bloqueia valores fora (e texto que não é número)", () => {
    const off = initialOptionalState(DEFAULT_PARAMS);
    for (const adjustment of OPTIONAL_ADJUSTMENTS) {
      const { key, min, max, error } = adjustment;
      for (const text of [String(max + 0.01).replace(".", ","), String(min - 0.01), "abc", ""]) {
        const optional = set(off, key, { on: true, text });
        expect(fromEditor(optional).errors).toEqual({ [key]: error });
        const html = markup(optional);
        expect(html).toContain(`<span class="field-error">${error}</span>`);
        expect(html).toContain('class="input invalid"');
      }
      for (const text of [String(min), String(max).replace(".", ",")]) {
        expect(fromEditor(set(off, key, { on: true, text })).errors).toEqual({});
      }
      // desligado, o texto inválido do campo não bloqueia nem vai para o corpo
      const hidden = set(off, key, { on: false, text: "999" });
      expect(fromEditor(hidden).errors).toEqual({});
      expect(fromEditor(hidden).body).toEqual(DEFAULT_PARAMS);
    }
  });

  it("mostra o aviso do corte final em vídeos curtos, ligado ou desligado", () => {
    const hint = "Ignorado em vídeos curtos (quando sobraria menos de 1 s).";
    expect(markup(initialOptionalState(DEFAULT_PARAMS))).toContain(hint);
    expect(markup(initialOptionalState(ANTI_DUPLICATE))).toContain(hint);
    expect(markup(initialOptionalState(DEFAULT_PARAMS))).toContain("Ajustes opcionais (desligado não altera a mídia)");
  });
});
