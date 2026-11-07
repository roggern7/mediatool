import { useState } from "react";
import { api, messageOf } from "../../api/client";
import type {
  CapCutBitrate,
  CapCutExportMode,
  CapCutFps,
  CapCutResolution,
  CapCutReviewMode,
  CapCutSettings,
  CapCutWhisperModel,
} from "../../api/types";
import {
  CAPCUT_BITRATES,
  CAPCUT_EXPORT_MODES,
  CAPCUT_FPS_OPTIONS,
  CAPCUT_RESOLUTIONS,
  CAPCUT_REVIEW_MODES,
  CAPCUT_WHISPER_MODELS,
  capcutSettingsPatch,
  exportModeHelp,
  whisperModelText,
} from "../../lib/capcut";
import { useToast } from "../../lib/toast";
import { Field, Spinner, Switch } from "../ui";

/** Configurações das legendas (formulário salvo em /api/capcut/settings). */
export function CapCutSettingsForm({
  initial,
  models,
  onSaved,
}: {
  initial: CapCutSettings;
  models: Record<string, boolean> | undefined;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [draft, setDraft] = useState<CapCutSettings>(initial);
  const [saving, setSaving] = useState(false);
  const set = <K extends keyof CapCutSettings>(key: K, value: CapCutSettings[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));
  const patch = capcutSettingsPatch(initial, draft);
  const dirty = Object.keys(patch).length > 0;

  async function save() {
    setSaving(true);
    try {
      await api.updateCapcutSettings(patch);
      toast.push("ok", "Configurações das legendas salvas.");
      onSaved();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))" }}>
        <Field
          label="Pasta de saída"
          htmlFor="cc-output"
          hint={initial.output_dir_is_default ? "Padrão do Media Tool. Vazio volta ao padrão." : "Vazio volta ao padrão."}
        >
          <input
            id="cc-output"
            className="input"
            value={draft.output_dir}
            onChange={(e) => set("output_dir", e.target.value)}
            spellCheck={false}
          />
        </Field>
        <Field label="Resolução" htmlFor="cc-resolution">
          <select
            id="cc-resolution"
            className="select"
            value={draft.resolution}
            onChange={(e) => set("resolution", e.target.value as CapCutResolution)}
          >
            {CAPCUT_RESOLUTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Qualidade (taxa de bits)" htmlFor="cc-bitrate">
          <select
            id="cc-bitrate"
            className="select"
            value={draft.bitrate}
            onChange={(e) => set("bitrate", e.target.value as CapCutBitrate)}
          >
            {CAPCUT_BITRATES.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Modo de exportação" htmlFor="cc-export-mode" hint={exportModeHelp(draft.export_mode)}>
          <select
            id="cc-export-mode"
            className="select"
            value={draft.export_mode}
            onChange={(e) => set("export_mode", e.target.value as CapCutExportMode)}
          >
            {CAPCUT_EXPORT_MODES.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Quadros por segundo" htmlFor="cc-fps">
          <select
            id="cc-fps"
            className="select"
            value={draft.fps}
            onChange={(e) => set("fps", e.target.value as CapCutFps)}
          >
            {CAPCUT_FPS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Palavras por legenda" htmlFor="cc-words">
          <input
            id="cc-words"
            className="input"
            type="number"
            min={1}
            max={10}
            value={draft.max_words}
            onChange={(e) => set("max_words", Number(e.target.value))}
          />
        </Field>
        <Field label="Caracteres por legenda" htmlFor="cc-chars">
          <input
            id="cc-chars"
            className="input"
            type="number"
            min={8}
            max={80}
            value={draft.max_chars}
            onChange={(e) => set("max_chars", Number(e.target.value))}
          />
        </Field>
        <Field label="Modelo de transcrição" htmlFor="cc-model" hint={whisperModelText(models, draft.whisper_model)}>
          <select
            id="cc-model"
            className="select"
            value={draft.whisper_model}
            onChange={(e) => set("whisper_model", e.target.value as CapCutWhisperModel)}
          >
            {CAPCUT_WHISPER_MODELS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Revisar legendas antes de exportar"
          htmlFor="cc-review"
          hint="Você ouve os trechos, corrige o texto e os tempos são recalculados."
        >
          <select
            id="cc-review"
            className="select"
            value={draft.review_mode}
            onChange={(e) => set("review_mode", e.target.value as CapCutReviewMode)}
          >
            {CAPCUT_REVIEW_MODES.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Tentativas por vídeo" htmlFor="cc-attempts">
          <select
            id="cc-attempts"
            className="select"
            value={draft.max_attempts}
            onChange={(e) => set("max_attempts", Number(e.target.value))}
          >
            {[1, 2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Cor do destaque"
          htmlFor="cc-color"
          hint="Formato #RRGGBB. Vazio usa a cor do modelo (ou amarelo)."
        >
          <input
            id="cc-color"
            className="input"
            value={draft.highlight_color ?? ""}
            disabled={!draft.highlight}
            onChange={(e) => set("highlight_color", e.target.value)}
            placeholder="#FFD400"
            spellCheck={false}
          />
        </Field>
      </div>
      <div className="row" style={{ gap: 18, flexWrap: "wrap", marginTop: 12 }}>
        <Switch checked={draft.highlight} onChange={(v) => set("highlight", v)} label="Destacar a palavra falada" />
        <Switch
          checked={draft.keep_original_audio}
          onChange={(v) => set("keep_original_audio", v)}
          label="Manter o áudio original"
        />
        <Switch
          checked={draft.keep_draft}
          onChange={(v) => set("keep_draft", v)}
          label="Manter o projeto no CapCut"
        />
        <Switch
          checked={draft.auto_after_download}
          onChange={(v) => set("auto_after_download", v)}
          label="Legendar automaticamente após o download"
        />
      </div>
      <div className="row" style={{ marginTop: 14 }}>
        <button type="submit" className="btn btn-primary" disabled={saving || !dirty}>
          {saving ? <Spinner /> : null}
          Salvar
        </button>
        {dirty ? (
          <button type="button" className="btn btn-ghost" disabled={saving} onClick={() => setDraft(initial)}>
            Descartar
          </button>
        ) : null}
      </div>
    </form>
  );
}
