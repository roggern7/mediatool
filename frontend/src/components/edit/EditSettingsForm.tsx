import { useEffect, useId, useState } from "react";
import {
  AUTO_SAVE_TEXT,
  DURATION_NOTE,
  DURATION_OPTIONS,
  EXPORT_AFTER_HINT,
  type EditForm,
  SHARPEN_DESCRIPTION,
  SHARPEN_LABEL,
  SPEED_MAX,
  SPEED_MIN,
  SPEED_INVALID_TEXT,
  SPEED_NOTE,
  SPEED_PRESETS,
  SPEED_STEP,
  formatEditSpeed,
  formatSpeedPreset,
  parseSpeedInput,
  snapSpeed,
} from "../../lib/editSettings";
import { Switch } from "../ui";

function RadioGroup<T extends string>({
  name,
  value,
  options,
  onChange,
}: {
  name: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="radio-list" role="radiogroup">
      {options.map((option) => (
        <label key={option.value} className="radio">
          <input
            type="radio"
            name={name}
            value={option.value}
            checked={value === option.value}
            onChange={() => onChange(option.value)}
          />
          <span>{option.label}</span>
        </label>
      ))}
    </div>
  );
}

/**
 * Velocidade: atalhos de um clique, barra de arrasto contínuo (passo 0,01), botões − / + e campo para digitar.
 * O campo aceita "1,07", "1.07" ou "1,07x" e confirma ao sair dele ou com Enter; setas ↑/↓ ajustam 0,01.
 */
function SpeedControl({ id, value, onChange }: { id: string; value: number; onChange: (speed: number) => void }) {
  const shown = formatEditSpeed(value).replace("x", "");
  const [text, setText] = useState(shown);
  const [invalid, setInvalid] = useState(false);
  useEffect(() => {
    setText(shown);
    setInvalid(false);
  }, [shown]);
  const set = (speed: number) => onChange(snapSpeed(speed));
  const commit = () => {
    const parsed = parseSpeedInput(text);
    if (parsed === null) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    if (parsed === value) setText(shown);
    else onChange(parsed);
  };
  const fill = ((value - SPEED_MIN) / (SPEED_MAX - SPEED_MIN)) * 100;
  return (
    <div className="speed-control">
      <div className="seg speed-presets" role="group" aria-label="Atalhos de velocidade">
        {SPEED_PRESETS.map((preset) => (
          <button
            key={preset}
            type="button"
            className={value === preset ? "on" : ""}
            aria-pressed={value === preset}
            onClick={() => set(preset)}
          >
            {formatSpeedPreset(preset)}
          </button>
        ))}
      </div>
      <div className="speed-fine">
        <input
          type="range"
          className="range speed-range grow"
          aria-label="Arrastar para ajustar a velocidade"
          min={SPEED_MIN}
          max={SPEED_MAX}
          step={SPEED_STEP}
          value={value}
          style={{ ["--fill" as string]: `${fill}%` }}
          aria-valuetext={formatEditSpeed(value)}
          onChange={(e) => set(Number(e.target.value))}
        />
        <span className="speed-stepper">
          <button
            type="button"
            className="btn speed-step"
            aria-label="Diminuir 0,01"
            disabled={value <= SPEED_MIN}
            onClick={() => set(value - SPEED_STEP)}
          >
            −
          </button>
          <span className={`speed-input${invalid ? " invalid" : ""}`}>
            <input
              id={id}
              className="input"
              inputMode="decimal"
              value={text}
              aria-invalid={invalid}
              aria-describedby={invalid ? `${id}-err` : undefined}
              onChange={(e) => setText(e.target.value)}
              onFocus={(e) => e.target.select()}
              onBlur={commit}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commit();
                } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
                  e.preventDefault();
                  set(value + (e.key === "ArrowUp" ? SPEED_STEP : -SPEED_STEP));
                }
              }}
            />
            <span className="muted">x</span>
          </span>
          <button
            type="button"
            className="btn speed-step"
            aria-label="Aumentar 0,01"
            disabled={value >= SPEED_MAX}
            onClick={() => set(value + SPEED_STEP)}
          >
            +
          </button>
        </span>
      </div>
      {invalid ? (
        <div id={`${id}-err`} className="field-error">
          {SPEED_INVALID_TEXT}
        </div>
      ) : null}
    </div>
  );
}

/** Configurações da edição (a saída é sempre MP4 H.264 + AAC), nesta ordem: duração, velocidade, nitidez e salvamento automático. */
export function EditSettingsForm({ form, onChange }: { form: EditForm; onChange: (next: EditForm) => void }) {
  const base = useId();
  const customId = `${base}-custom`;
  const speedId = `${base}-speed`;
  return (
    <div className="edit-settings">
      <section className="edit-group">
        <h3>Duração</h3>
        <RadioGroup
          name={`${base}-duration`}
          value={form.durationMode}
          options={DURATION_OPTIONS}
          onChange={(durationMode) => onChange({ ...form, durationMode })}
        />
        {form.durationMode === "custom" ? (
          <div className="row" style={{ marginTop: 8 }}>
            <label htmlFor={customId} className="muted">
              Limite
            </label>
            <input
              id={customId}
              className="input"
              style={{ width: 90 }}
              inputMode="decimal"
              placeholder="45"
              value={form.customText}
              onChange={(e) => onChange({ ...form, customText: e.target.value })}
            />
            <span className="muted">segundos</span>
          </div>
        ) : null}
        <div className="field-hint">{DURATION_NOTE}</div>
      </section>

      <section className="edit-group">
        <h3>
          <label htmlFor={speedId}>Velocidade</label>
        </h3>
        <SpeedControl id={speedId} value={form.speed} onChange={(speed) => onChange({ ...form, speed })} />
        <div className="field-hint">{SPEED_NOTE}</div>
      </section>

      <section className="edit-group">
        <h3>Nitidez</h3>
        <Switch
          label={SHARPEN_LABEL}
          checked={form.sharpen}
          onChange={(sharpen) => onChange({ ...form, sharpen })}
        />
        <div className="field-hint">{SHARPEN_DESCRIPTION}</div>
      </section>

      <section className="edit-group">
        <h3>Salvamento automático</h3>
        <p className="muted small" style={{ margin: "0 0 8px" }}>
          {AUTO_SAVE_TEXT}
        </p>
        <div className="field-hint">{EXPORT_AFTER_HINT}</div>
      </section>
    </div>
  );
}
