import { useState } from "react";
import { Copy, Lock, Pencil, Plus, Trash2 } from "lucide-react";
import { ApiError, api, messageOf } from "../api/client";
import type { Preset, PresetParams } from "../api/types";
import { ConfirmDialog, Empty, ErrorLine, Field, Modal, SkeletonRows, Spinner, Switch } from "../components/ui";
import { OptionalAdjustmentsForm } from "../components/OptionalAdjustmentsForm";
import { formatSeconds, formatSpeed, toDecimalInput } from "../lib/format";
import {
  DEFAULT_PARAMS,
  NAME_MAX,
  SPEED_STEPS,
  TRIM_STEPS,
  compactParams,
  editorParams,
  forcesReencode,
  initialOptionalState,
  optionalSummary,
  passthroughParams,
  validatePreset,
} from "../lib/presets";
import { useQuery } from "../lib/useQuery";
import { useToast } from "../lib/toast";

function summary(p: PresetParams): string {
  const parts: string[] = [];
  if (p.metadata_cleanup) parts.push("Limpa metadados");
  if (p.trim_start > 0) parts.push(`Corta ${formatSeconds(p.trim_start)}`);
  if (p.mirror) parts.push("Espelha");
  if (p.speed !== 1) parts.push(formatSpeed(p.speed));
  if (p.auto_adjust) parts.push("Ajustes automáticos");
  parts.push(...optionalSummary(p));
  parts.push(p.reencode || forcesReencode(p) ? "Reencode" : "Sem reencode");
  return parts.join(" · ");
}

type Editing = { mode: "create" } | { mode: "edit"; preset: Preset };

export function PresetsPage() {
  const toast = useToast();
  const presets = useQuery("presets", () => api.listPresets());
  const [editing, setEditing] = useState<Editing | null>(null);
  const [deleting, setDeleting] = useState<Preset | null>(null);
  const [busy, setBusy] = useState(false);
  const [dupId, setDupId] = useState<number | null>(null);

  async function duplicate(p: Preset) {
    setDupId(p.id);
    try {
      const created = await api.duplicatePreset(p.id);
      toast.push("ok", `Preset "${created.name}" criado.`);
      presets.reload();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setDupId(null);
    }
  }

  async function remove() {
    if (!deleting) return;
    setBusy(true);
    try {
      await api.deletePreset(deleting.id);
      toast.push("ok", "Preset excluído.");
      setDeleting(null);
      presets.reload();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Presets</h1>
        <button type="button" className="btn btn-primary" onClick={() => setEditing({ mode: "create" })}>
          <Plus size={14} strokeWidth={1.75} aria-hidden="true" />
          Novo preset
        </button>
      </div>

      {presets.data === undefined ? (
        presets.error ? (
          <ErrorLine message={presets.error} onRetry={presets.reload} />
        ) : (
          <SkeletonRows rows={3} />
        )
      ) : presets.data.length === 0 ? (
        <Empty title="Nenhum preset" />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Nome</th>
                <th>Configuração</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {presets.data.map((p) => (
                <tr key={p.id}>
                  <td className="nowrap">
                    <strong style={{ fontWeight: 500 }}>{p.name}</strong>
                  </td>
                  <td className="muted">{summary(p.params)}</td>
                  <td className="actions">
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => setEditing({ mode: "edit", preset: p })}
                    >
                      <Pencil size={13} strokeWidth={1.75} aria-hidden="true" />
                      Editar
                    </button>
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      disabled={dupId === p.id}
                      onClick={() => void duplicate(p)}
                    >
                      {dupId === p.id ? <Spinner /> : <Copy size={13} strokeWidth={1.75} aria-hidden="true" />}
                      Duplicar
                    </button>
                    {p.is_builtin ? (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        disabled
                        title="Presets do sistema não podem ser excluídos"
                      >
                        <Lock size={13} strokeWidth={1.75} aria-hidden="true" />
                        Não excluível
                      </button>
                    ) : (
                      <button type="button" className="btn btn-ghost btn-sm btn-danger" onClick={() => setDeleting(p)}>
                        <Trash2 size={13} strokeWidth={1.75} aria-hidden="true" />
                        Excluir
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing ? (
        <PresetEditor
          key={editing.mode === "edit" ? editing.preset.id : "new"}
          preset={editing.mode === "edit" ? editing.preset : null}
          onClose={() => setEditing(null)}
          onSaved={(saved) => {
            toast.push("ok", `Preset "${saved.name}" salvo.`);
            setEditing(null);
            presets.reload();
          }}
        />
      ) : null}

      {deleting ? (
        <ConfirmDialog
          title="Excluir preset"
          confirmLabel="Excluir"
          danger
          busy={busy}
          onConfirm={() => void remove()}
          onCancel={() => setDeleting(null)}
        >
          O preset "{deleting.name}" será excluído. Arquivos já processados com ele continuam na biblioteca.
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

function PresetEditor({
  preset,
  onClose,
  onSaved,
}: {
  preset: Preset | null;
  onClose: () => void;
  onSaved: (p: Preset) => void;
}) {
  const toast = useToast();
  const start = preset?.params ?? DEFAULT_PARAMS;
  const [name, setName] = useState(preset?.name ?? "");
  const [flags, setFlags] = useState({
    metadata_cleanup: start.metadata_cleanup,
    mirror: start.mirror,
    auto_adjust: start.auto_adjust,
    reencode: start.reencode,
  });
  const [trimText, setTrimText] = useState(toDecimalInput(start.trim_start));
  const [speedText, setSpeedText] = useState(toDecimalInput(start.speed));
  // Ajustes opcionais: desligado = neutro (a chave nem vai para o preset). Preset antigo começa com todos desligados.
  const [optional, setOptional] = useState(() => initialOptionalState(start));
  const [saving, setSaving] = useState(false);
  const [touched, setTouched] = useState(false);
  const [nameTaken, setNameTaken] = useState<string | null>(null);

  // Chaves da Edição (duração máx., nitidez, sincronia do áudio) não têm campo aqui, mas não podem sumir ao salvar.
  const [passthrough] = useState(() => passthroughParams(start));
  const params = editorParams(flags, trimText, speedText, optional, passthrough);
  const errors = validatePreset(name, params);
  const valid = Object.keys(errors).length === 0;
  const builtin = preset?.is_builtin ?? false;
  const nameError =
    (touched ? errors.name : null) ??
    (nameTaken !== null && nameTaken === name.trim() ? "Já existe um preset com este nome." : null);

  async function save() {
    setTouched(true);
    if (!valid) return;
    setSaving(true);
    try {
      const body = compactParams(params);
      const saved = preset
        ? await api.updatePreset(preset.id, builtin ? { params: body } : { name: name.trim(), params: body })
        : await api.createPreset(name.trim(), body);
      onSaved(saved);
    } catch (err) {
      if (err instanceof ApiError && err.code === "preset_name_taken") setNameTaken(name.trim());
      else toast.push("err", messageOf(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      title={preset ? "Editar preset" : "Novo preset"}
      onClose={onClose}
      wide
      actions={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={saving}>
            Cancelar
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void save()}
            disabled={saving || (touched && !valid)}
          >
            {saving ? <Spinner /> : null}
            Salvar
          </button>
        </>
      }
    >
      <form
        className="preset-editor"
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <Field label="Nome" htmlFor="preset-name" error={nameError}>
          <input
            id="preset-name"
            className={`input${nameError ? " invalid" : ""}`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={builtin}
            autoFocus={!builtin}
            maxLength={NAME_MAX}
            autoComplete="off"
          />
        </Field>

        <Switch
          label="Limpar metadados"
          checked={flags.metadata_cleanup}
          onChange={(v) => setFlags({ ...flags, metadata_cleanup: v })}
        />

        <div className="control-row">
          <span>Cortar início</span>
          <div className="row">
            <div className="seg" role="group" aria-label="Cortar início">
              {TRIM_STEPS.map((s) => (
                <button
                  key={s}
                  type="button"
                  className={params.trim_start === s ? "on" : ""}
                  onClick={() => setTrimText(toDecimalInput(s))}
                >
                  {toDecimalInput(s)}
                </button>
              ))}
            </div>
            <input
              className={`input${errors.trim_start ? " invalid" : ""}`}
              style={{ width: 72 }}
              inputMode="decimal"
              aria-label="Segundos a cortar"
              value={trimText}
              onChange={(e) => setTrimText(e.target.value)}
            />
            <span className="muted">s</span>
            {errors.trim_start ? <span className="field-error">{errors.trim_start}</span> : null}
          </div>
        </div>

        <Switch label="Espelhar" checked={flags.mirror} onChange={(v) => setFlags({ ...flags, mirror: v })} />

        <div className="control-row">
          <span>Velocidade</span>
          <div className="row">
            <div className="seg" role="group" aria-label="Velocidade">
              {SPEED_STEPS.map((s) => (
                <button
                  key={s}
                  type="button"
                  className={params.speed === s ? "on" : ""}
                  onClick={() => setSpeedText(toDecimalInput(s))}
                >
                  {formatSpeed(s)}
                </button>
              ))}
            </div>
            <input
              className={`input${errors.speed ? " invalid" : ""}`}
              style={{ width: 72 }}
              inputMode="decimal"
              aria-label="Velocidade livre"
              value={speedText}
              onChange={(e) => setSpeedText(e.target.value)}
            />
            <span className="muted">
              {Number.isFinite(params.speed) && !errors.speed ? formatSpeed(params.speed) : "x"}
            </span>
            {errors.speed ? <span className="field-error">{errors.speed}</span> : null}
          </div>
        </div>

        <Switch
          label="Ajustes automáticos"
          checked={flags.auto_adjust}
          onChange={(v) => setFlags({ ...flags, auto_adjust: v })}
        />

        <OptionalAdjustmentsForm optional={optional} errors={errors} onChange={setOptional} />

        <div>
          <Switch label="Reencode" checked={flags.reencode} onChange={(v) => setFlags({ ...flags, reencode: v })} />
          {forcesReencode(params) ? (
            <div className="field-hint" style={{ marginTop: 5 }}>
              Reencode será aplicado porque há transformações ativas
            </div>
          ) : null}
        </div>
        <button type="submit" hidden />
      </form>
    </Modal>
  );
}
