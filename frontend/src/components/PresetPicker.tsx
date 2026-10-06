import { useState } from "react";
import type { Preset } from "../api/types";
import { Field, Modal, Spinner } from "./ui";

export function PresetPickerDialog({
  title,
  presets,
  defaultPresetId,
  busy,
  onConfirm,
  onCancel,
}: {
  title: string;
  presets: Preset[];
  defaultPresetId: number | null;
  busy: boolean;
  onConfirm: (presetId: number) => void;
  onCancel: () => void;
}) {
  const initial =
    (defaultPresetId !== null && presets.some((p) => p.id === defaultPresetId) ? defaultPresetId : null) ??
    presets.find((p) => p.name === "Anti-duplicidade")?.id ??
    presets[0]?.id ??
    null;
  const [presetId, setPresetId] = useState<number | null>(initial);

  return (
    <Modal
      title={title}
      onClose={onCancel}
      actions={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            Cancelar
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || presetId === null}
            onClick={() => presetId !== null && onConfirm(presetId)}
          >
            {busy ? <Spinner /> : null}
            Processar
          </button>
        </>
      }
    >
      {presets.length === 0 ? (
        <p className="muted">Nenhum preset disponível.</p>
      ) : (
        <Field label="Preset" htmlFor="picker-preset">
          <select
            id="picker-preset"
            className="select"
            value={presetId ?? ""}
            onChange={(e) => setPresetId(Number(e.target.value))}
            autoFocus
          >
            {presets.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </Field>
      )}
      <p className="muted small" style={{ marginTop: 10 }}>
        O processamento é opcional: cria uma versão processada e mantém o original.
      </p>
    </Modal>
  );
}
