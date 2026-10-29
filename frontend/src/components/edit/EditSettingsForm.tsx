import { useId } from "react";
import {
  AUTO_SAVE_TEXT,
  DURATION_NOTE,
  DURATION_OPTIONS,
  EXPORT_AFTER_HINT,
  type EditForm,
  OUTPUT_AUTO_NOTE,
  OUTPUT_OPTIONS,
  SHARPEN_DESCRIPTION,
  SHARPEN_LABEL,
  SPEED_MAX,
  SPEED_MIN,
  SPEED_NOTE,
  SPEED_STEP,
  formatEditSpeed,
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

/** Configurações da edição, nesta ordem: tipo de saída, duração, velocidade, nitidez e salvamento automático. */
export function EditSettingsForm({ form, onChange }: { form: EditForm; onChange: (next: EditForm) => void }) {
  const base = useId();
  const customId = `${base}-custom`;
  const speedId = `${base}-speed`;
  return (
    <div className="edit-settings">
      <section className="edit-group">
        <h3>Tipo de saída</h3>
        <RadioGroup
          name={`${base}-output`}
          value={form.outputType}
          options={OUTPUT_OPTIONS}
          onChange={(outputType) => onChange({ ...form, outputType })}
        />
        {form.outputType === "auto" ? <div className="field-hint">{OUTPUT_AUTO_NOTE}</div> : null}
      </section>

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
        <div className="row">
          <input
            id={speedId}
            type="range"
            className="range grow"
            min={SPEED_MIN}
            max={SPEED_MAX}
            step={SPEED_STEP}
            value={form.speed}
            aria-valuetext={formatEditSpeed(form.speed)}
            onChange={(e) => onChange({ ...form, speed: snapSpeed(Number(e.target.value)) })}
          />
          <strong className="speed-value">{formatEditSpeed(form.speed)}</strong>
        </div>
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
