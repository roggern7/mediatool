import { OPTIONAL_ADJUSTMENTS } from "../lib/presets";
import type { OptionalState, PresetErrors } from "../lib/presets";
import { Switch } from "./ui";

/** Ajustes opcionais do editor de preset: um interruptor por ajuste e, ligado, o campo do valor. */
export function OptionalAdjustmentsForm({
  optional,
  errors,
  onChange,
}: {
  optional: OptionalState;
  errors: PresetErrors;
  onChange: (next: OptionalState) => void;
}) {
  return (
    <>
      <div className="field-label">Ajustes opcionais (desligado não altera a mídia)</div>
      {OPTIONAL_ADJUSTMENTS.map((adjustment) => {
        const state = optional[adjustment.key];
        const error = errors[adjustment.key];
        return (
          <div key={adjustment.key}>
            <div className="control-row">
              <Switch
                label={adjustment.label}
                checked={state.on}
                onChange={(on) => onChange({ ...optional, [adjustment.key]: { ...state, on } })}
              />
              {state.on ? (
                <div className="row">
                  <input
                    className={`input${error ? " invalid" : ""}`}
                    style={{ width: 72 }}
                    inputMode="decimal"
                    aria-label={adjustment.label}
                    value={state.text}
                    onChange={(e) => onChange({ ...optional, [adjustment.key]: { ...state, text: e.target.value } })}
                  />
                  {adjustment.unit ? <span className="muted">{adjustment.unit}</span> : null}
                  {error ? <span className="field-error">{error}</span> : null}
                </div>
              ) : (
                <span className="muted small">Desligado</span>
              )}
            </div>
            {adjustment.hint ? (
              <div className="field-hint" style={{ marginTop: 5 }}>
                {adjustment.hint}
              </div>
            ) : null}
          </div>
        );
      })}
    </>
  );
}
