import { useId, useState } from "react";
import type { CapCutBatchMode, CapCutBitrate, CapCutFps, CapCutResolution, CapCutStyle, Preset } from "../../api/types";
import { CAPCUT_BITRATES, CAPCUT_FPS_OPTIONS, CAPCUT_RESOLUTIONS } from "../../lib/capcut";
import {
  BATCH_MODE_OPTIONS,
  BATCH_SPEED_MAX,
  BATCH_SPEED_MIN,
  BATCH_SPEED_STEP,
  type BatchEditForm,
  type BatchExportForm,
  editFormFromPreset,
  modeHasCaption,
  modeHasEdit,
  snapBatchSpeed,
} from "../../lib/capcutBatch";
import { DURATION_NOTE, DURATION_OPTIONS, SHARPEN_DESCRIPTION, SHARPEN_LABEL, formatEditSpeed } from "../../lib/editSettings";
import { parseDecimal } from "../../lib/format";
import { Field, SkeletonRows, Switch } from "../ui";

/** Etapa 2: o que fazer (legendas, edição ou os dois), modelo, edição, exportação e reprocessar. */
export function BatchConfigForm({
  mode,
  onMode,
  styles,
  styleId,
  onStyle,
  onEditStyles,
  presets,
  edit,
  onEdit,
  exportForm,
  onExport,
  includeDone,
  onIncludeDone,
}: {
  mode: CapCutBatchMode;
  onMode: (mode: CapCutBatchMode) => void;
  styles: CapCutStyle[] | undefined;
  styleId: number | null;
  onStyle: (id: number | null) => void;
  onEditStyles: () => void;
  presets: Preset[] | undefined;
  edit: BatchEditForm;
  onEdit: (next: BatchEditForm) => void;
  /** null enquanto as configurações salvas carregam. */
  exportForm: BatchExportForm | null;
  onExport: (next: BatchExportForm) => void;
  includeDone: boolean;
  onIncludeDone: (value: boolean) => void;
}) {
  const base = useId();
  const defaultStyle = styles?.find((s) => s.is_default);
  return (
    <div className="cc-config">
      <fieldset className="cc-fieldset">
        <legend className="field-label">O que fazer</legend>
        <div className="radio-list" role="radiogroup" aria-label="O que fazer">
          {BATCH_MODE_OPTIONS.map((o) => (
            <label key={o.value} className="radio">
              <input
                type="radio"
                name={`${base}-mode`}
                value={o.value}
                checked={mode === o.value}
                onChange={() => onMode(o.value)}
              />
              <span>
                {o.label}
                <span className="muted small"> — {o.help}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <div className="cc-config-grid">
        {modeHasCaption(mode) ? (
          <section className="edit-group">
            <h3>Legendas</h3>
            <Field
              label="Modelo de legenda"
              htmlFor={`${base}-style`}
              hint={
                <button type="button" className="link-btn" onClick={onEditStyles}>
                  Editar modelos
                </button>
              }
            >
              <select
                id={`${base}-style`}
                className="select"
                value={styleId === null ? "" : String(styleId)}
                onChange={(e) => onStyle(e.target.value === "" ? null : Number(e.target.value))}
              >
                <option value="">{defaultStyle ? `Modelo padrão: ${defaultStyle.name}` : "Modelo padrão"}</option>
                {(styles ?? []).map((s) => (
                  <option key={s.id} value={String(s.id)}>
                    {s.name}
                  </option>
                ))}
              </select>
            </Field>
          </section>
        ) : null}

        {modeHasEdit(mode) ? (
          <section className="edit-group">
            <h3>Edição</h3>
            <EditFields base={base} presets={presets} edit={edit} onEdit={onEdit} />
          </section>
        ) : null}

        {modeHasCaption(mode) ? (
          <section className="edit-group">
            <h3>Exportação</h3>
            {exportForm ? (
              <ExportFields base={base} form={exportForm} onChange={onExport} />
            ) : (
              <SkeletonRows rows={3} />
            )}
          </section>
        ) : null}
      </div>

      <label className="row" style={{ gap: 8, marginTop: 12 }}>
        <input type="checkbox" checked={includeDone} onChange={(e) => onIncludeDone(e.target.checked)} />
        <span>{modeHasCaption(mode) ? "Reprocessar vídeos já legendados" : "Reprocessar vídeos já editados"}</span>
      </label>
      <div className="field-hint">
        Desmarcado, os vídeos já concluídos neste modo e os que já estão na fila são pulados.
        {mode === "edit" ? " No modo Só edição, resolução, FPS e qualidade não se aplicam (o CapCut não é aberto)." : ""}
      </div>
    </div>
  );
}

function EditFields({
  base,
  presets,
  edit,
  onEdit,
}: {
  base: string;
  presets: Preset[] | undefined;
  edit: BatchEditForm;
  onEdit: (next: BatchEditForm) => void;
}) {
  return (
    <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))" }}>
      <Field label="Preset (opcional)" htmlFor={`${base}-preset`} hint="Pré-preenche os campos abaixo.">
        <select
          id={`${base}-preset`}
          className="select"
          value={edit.presetId === null ? "" : String(edit.presetId)}
          onChange={(e) => {
            const id = e.target.value === "" ? null : Number(e.target.value);
            onEdit(editFormFromPreset(edit, presets?.find((p) => p.id === id) ?? null));
          }}
        >
          <option value="">{presets === undefined ? "Carregando…" : "Nenhum"}</option>
          {(presets ?? []).map((p) => (
            <option key={p.id} value={String(p.id)}>
              {p.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Duração" htmlFor={`${base}-duration`} hint={DURATION_NOTE}>
        <select
          id={`${base}-duration`}
          className="select"
          value={edit.durationMode}
          onChange={(e) => onEdit({ ...edit, durationMode: e.target.value as BatchEditForm["durationMode"] })}
        >
          {DURATION_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </Field>
      {edit.durationMode === "custom" ? (
        <Field label="Limite (segundos)" htmlFor={`${base}-custom`}>
          <input
            id={`${base}-custom`}
            className="input"
            inputMode="decimal"
            placeholder="45"
            value={edit.customText}
            onChange={(e) => onEdit({ ...edit, customText: e.target.value })}
          />
        </Field>
      ) : null}
      <Field label="Início do corte (segundos)" htmlFor={`${base}-start`} hint="Vazio ou 0 começa do início.">
        <input
          id={`${base}-start`}
          className="input"
          inputMode="decimal"
          placeholder="0"
          value={edit.startText}
          onChange={(e) => onEdit({ ...edit, startText: e.target.value })}
        />
      </Field>
      <Field label="Velocidade" htmlFor={`${base}-speed`} hint="De 0,5x a 2,0x; vídeo e áudio ficam sincronizados.">
        <SpeedField id={`${base}-speed`} value={edit.speed} onChange={(speed) => onEdit({ ...edit, speed })} />
      </Field>
      <div className="field">
        <span className="field-label">Nitidez</span>
        <Switch label={SHARPEN_LABEL} checked={edit.sharpen} onChange={(sharpen) => onEdit({ ...edit, sharpen })} />
        <span className="field-hint">{SHARPEN_DESCRIPTION}</span>
      </div>
    </div>
  );
}

/** Barra de 0,5x a 2,0x + campo que aceita "1,2", "1.2" ou "1,2x" (confirma ao sair ou com Enter). */
function SpeedField({ id, value, onChange }: { id: string; value: number; onChange: (speed: number) => void }) {
  const shown = formatEditSpeed(value).replace("x", "");
  const [text, setText] = useState(shown);
  const [synced, setSynced] = useState(shown);
  if (synced !== shown) {
    setSynced(shown);
    setText(shown);
  }
  const commit = () => {
    const parsed = parseDecimal(text.trim().replace(/x$/i, "").trim());
    if (parsed === null || parsed < BATCH_SPEED_MIN || parsed > BATCH_SPEED_MAX) setText(shown);
    else onChange(snapBatchSpeed(parsed));
  };
  return (
    <div className="row" style={{ flexWrap: "nowrap" }}>
      <input
        type="range"
        className="range grow"
        aria-label="Arrastar para ajustar a velocidade"
        min={BATCH_SPEED_MIN}
        max={BATCH_SPEED_MAX}
        step={BATCH_SPEED_STEP}
        value={value}
        aria-valuetext={formatEditSpeed(value)}
        onChange={(e) => onChange(snapBatchSpeed(Number(e.target.value)))}
      />
      <input
        id={id}
        className="input"
        style={{ width: 70 }}
        inputMode="decimal"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          }
        }}
      />
      <span className="muted">x</span>
    </div>
  );
}

function ExportFields({
  base,
  form,
  onChange,
}: {
  base: string;
  form: BatchExportForm;
  onChange: (next: BatchExportForm) => void;
}) {
  return (
    <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))" }}>
      <Field label="Resolução" htmlFor={`${base}-res`}>
        <select
          id={`${base}-res`}
          className="select"
          value={form.resolution}
          onChange={(e) => onChange({ ...form, resolution: e.target.value as CapCutResolution })}
        >
          {CAPCUT_RESOLUTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Quadros por segundo" htmlFor={`${base}-fps`}>
        <select
          id={`${base}-fps`}
          className="select"
          value={form.fps}
          onChange={(e) => onChange({ ...form, fps: e.target.value as CapCutFps })}
        >
          {CAPCUT_FPS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Qualidade" htmlFor={`${base}-bitrate`}>
        <select
          id={`${base}-bitrate`}
          className="select"
          value={form.bitrate}
          onChange={(e) => onChange({ ...form, bitrate: e.target.value as CapCutBitrate })}
        >
          {CAPCUT_BITRATES.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </Field>
      <div className="field">
        <span className="field-label">Áudio</span>
        <Switch
          label="Manter o áudio original"
          checked={form.keep_original_audio}
          onChange={(keep_original_audio) => onChange({ ...form, keep_original_audio })}
        />
      </div>
    </div>
  );
}
