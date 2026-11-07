import { useEffect, useId, useRef, useState } from "react";
import { api, messageOf } from "../../api/client";
import type {
  CapCutAnimationItem,
  CapCutAnimationType,
  CapCutDesign,
  CapCutFontItem,
  CapCutFontSource,
  CapCutStyle,
} from "../../api/types";
import {
  ANIMATION_TYPES,
  ANIMATION_TYPE_LABEL,
  DESIGN_LIMITS,
  FONT_SOURCE_LABEL,
  POSITION_PRESETS,
  STYLE_NAME_MAX,
  clamp,
  colorInputValue,
  designsEqual,
  designWarnings,
  fontKey,
  normalizeDesign,
  normalizeHex,
  patchDesign,
  positionPresetOf,
  previewFontStack,
  validateDesign,
  validateStyleName,
  type DesignPatch,
} from "../../lib/captionDesign";
import { useCaptionFont } from "../../lib/useCaptionFont";
import { useDebounced } from "../../lib/useDebounced";
import { useQuery } from "../../lib/useQuery";
import { useToast } from "../../lib/toast";
import { ConfirmDialog, ErrorLine, Field, Pill, Spinner, Switch } from "../ui";
import { CaptionPreview } from "./CaptionPreview";
import { RealPreviewPanel } from "./RealPreviewPanel";

const DEFAULT_SAMPLE = "Exemplo de legenda no seu estilo com destaque";
const PRO_WARNING =
  "Recursos Pro podem fazer o CapCut bloquear a exportação nesta conta. Prefira fontes e animações gratuitas.";

/** Editor visual de um modelo de legenda (novo ou criado pelo editor), com prévia aproximada e prévia real. */
export function CaptionEditor({
  style,
  initialDesign,
  initialName,
  maxWords,
  maxChars,
  onClose,
  onSaved,
}: {
  /** Modelo editado (criado pelo editor); null = novo modelo. */
  style: CapCutStyle | null;
  initialDesign: CapCutDesign;
  initialName: string;
  maxWords?: number;
  maxChars?: number;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const rootRef = useRef<HTMLDivElement>(null);
  const [saved, setSaved] = useState<{ id: number | null; name: string; design: CapCutDesign }>({
    id: style?.id ?? null,
    name: style ? initialName : "",
    design: initialDesign,
  });
  const [design, setDesign] = useState<CapCutDesign>(initialDesign);
  const [name, setName] = useState(initialName);
  const [showPro, setShowPro] = useState(false);
  const [proChosen, setProChosen] = useState<Record<string, boolean>>({});
  const [fontPick, setFontPick] = useState<{ key: string; previewable: boolean } | null>(null);
  const [showErrors, setShowErrors] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [sample, setSample] = useState(DEFAULT_SAMPLE);
  const [mediaId, setMediaId] = useState<number | null>(null);

  useEffect(() => {
    rootRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  const media = useQuery("capcut-editor-media", () =>
    api.listMedia({ view: "originals", media_type: "video", role: "original", limit: 20 }),
  );
  const currentKey = fontKey(design.font);
  const font = useCaptionFont(
    fontPick && fontPick.key === currentKey ? fontPick.key : currentKey,
    fontPick && fontPick.key === currentKey ? fontPick.previewable : true,
  );
  const errors = validateDesign(design);
  const warnings = designWarnings(design);
  const nameError = validateStyleName(name);
  const hasErrors = Object.keys(errors).length > 0;
  const dirty = saved.id === null || name.trim() !== saved.name || !designsEqual(design, saved.design);
  const set = (patch: DesignPatch) => setDesign((d) => patchDesign(d, patch));
  const err = (key: string) => (showErrors ? (errors[key] ?? null) : null);
  const usesPro =
    (currentKey !== null && proChosen[`font:${currentKey}`]) ||
    ANIMATION_TYPES.some((t) => design.animation[t] && proChosen[`anim:${design.animation[t]?.id}`]);
  const chosenMedia = media.data?.items.find((m) => m.id === mediaId) ?? null;
  const backgroundUrl = chosenMedia ? (chosenMedia.thumbnail_url ?? `/api/media/${chosenMedia.id}/thumbnail`) : null;

  async function save(mode: "new" | "update" | "default") {
    setShowErrors(true);
    if (hasErrors || nameError) {
      toast.push("err", nameError ?? "Confira os campos destacados no modelo.");
      return;
    }
    setSaving(true);
    try {
      const body = { name: name.trim(), design };
      let result: CapCutStyle;
      if (mode === "new" || saved.id === null) result = await api.createCapcutStyle(body);
      else result = await api.updateCapcutStyle(saved.id, body);
      if (mode === "default") await api.setDefaultCapcutStyle(result.id);
      const next = result.design ? normalizeDesign(result.design) : design;
      setSaved({ id: result.id, name: result.name, design: next });
      setDesign(next);
      setName(result.name);
      toast.push(
        "ok",
        mode === "default"
          ? `"${result.name}" salvo e definido como padrão.`
          : mode === "new" || saved.id === null
            ? `Modelo "${result.name}" criado.`
            : "Alterações salvas.",
      );
      onSaved();
    } catch (e) {
      toast.push("err", messageOf(e));
    } finally {
      setSaving(false);
    }
  }

  function cancel() {
    if (dirty && (saved.id !== null || !designsEqual(design, initialDesign) || name.trim() !== initialName.trim())) {
      setConfirmDiscard(true);
    } else {
      onClose();
    }
  }

  const ids = useId();
  const id = (s: string) => `${ids}-${s}`;
  const presetKey = positionPresetOf(design.position.y);

  return (
    <div ref={rootRef} className="panel" style={{ marginTop: 14 }}>
      <div className="section-head" style={{ marginBottom: 10 }}>
        <h2>{saved.id === null ? "Novo modelo de legenda" : `Editar modelo: ${saved.name}`}</h2>
      </div>
      <div className="edit-layout">
        <div>
          <Field label="Nome do modelo" htmlFor={id("name")} error={showErrors ? nameError : null}>
            <input
              id={id("name")}
              className={`input${showErrors && nameError ? " invalid" : ""}`}
              value={name}
              maxLength={STYLE_NAME_MAX}
              onChange={(e) => setName(e.target.value)}
              placeholder="Ex.: Amarelo com contorno"
            />
          </Field>

          <section className="edit-group" style={{ marginTop: 12 }}>
            <h3>Fonte</h3>
            <FontPicker
              design={design}
              showPro={showPro}
              error={err("font")}
              onPick={(item) => {
                setFontPick({ key: item.key, previewable: item.previewable });
                setProChosen((p) => ({ ...p, [`font:${item.key}`]: item.is_pro }));
                set({ font: { source: item.source, id: item.id, path: item.path, name: item.name } });
              }}
            />
            <div style={{ marginTop: 8 }}>
              <Switch checked={showPro} onChange={setShowPro} label="Mostrar recursos Pro (fontes e animações)" />
            </div>
            {showPro || usesPro ? (
              <div className="alert warn" style={{ marginTop: 8 }}>
                <span className="grow small">
                  {usesPro ? "Este modelo usa recurso Pro. " : ""}
                  {PRO_WARNING}
                </span>
              </div>
            ) : null}
          </section>

          <section className="edit-group">
            <h3>Tamanho e escala</h3>
            <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}>
              <RangeField
                id={id("size")}
                label="Tamanho do texto"
                limits={DESIGN_LIMITS.size}
                step={0.5}
                value={design.size}
                error={err("size")}
                onChange={(size) => set({ size })}
              />
              <RangeField
                id={id("scale")}
                label="Escala"
                limits={DESIGN_LIMITS.scale}
                step={0.01}
                value={design.scale}
                error={err("scale")}
                onChange={(scale) => set({ scale })}
              />
            </div>
          </section>

          <section className="edit-group">
            <h3>Estilo e alinhamento</h3>
            <div className="row" style={{ gap: 16 }}>
              <Switch checked={design.bold} onChange={(bold) => set({ bold })} label="Negrito" />
              <Switch checked={design.italic} onChange={(italic) => set({ italic })} label="Itálico" />
              <Switch checked={design.underline} onChange={(underline) => set({ underline })} label="Sublinhado" />
              <Switch checked={design.uppercase} onChange={(uppercase) => set({ uppercase })} label="Maiúsculas" />
            </div>
            <div className="row" style={{ marginTop: 10 }}>
              <span className="muted small">Alinhamento</span>
              <div className="seg" role="group" aria-label="Alinhamento">
                {(
                  [
                    ["left", "Esquerda"],
                    ["center", "Centro"],
                    ["right", "Direita"],
                  ] as const
                ).map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    className={design.align === value ? "on" : undefined}
                    aria-pressed={design.align === value}
                    onClick={() => set({ align: value })}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          </section>

          <section className="edit-group">
            <h3>Cor</h3>
            <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}>
              <ColorField id={id("color")} label="Cor do texto" value={design.color} error={err("color")} onChange={(color) => set({ color })} />
              <RangeField
                id={id("opacity")}
                label="Opacidade"
                limits={DESIGN_LIMITS.opacity}
                step={0.01}
                percent
                value={design.opacity}
                error={err("opacity")}
                onChange={(opacity) => set({ opacity })}
              />
            </div>
          </section>

          <section className="edit-group">
            <h3>Contorno</h3>
            <Switch checked={design.stroke.enabled} onChange={(enabled) => set({ stroke: { enabled } })} label="Usar contorno" />
            {design.stroke.enabled ? (
              <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", marginTop: 8 }}>
                <ColorField
                  id={id("stroke-color")}
                  label="Cor do contorno"
                  value={design.stroke.color}
                  error={err("stroke.color")}
                  onChange={(color) => set({ stroke: { color } })}
                />
                <RangeField
                  id={id("stroke-width")}
                  label="Espessura"
                  limits={DESIGN_LIMITS.strokeWidth}
                  step={1}
                  value={design.stroke.width}
                  error={err("stroke.width")}
                  onChange={(width) => set({ stroke: { width } })}
                />
              </div>
            ) : null}
          </section>

          <section className="edit-group">
            <h3>Sombra</h3>
            <Switch checked={design.shadow.enabled} onChange={(enabled) => set({ shadow: { enabled } })} label="Usar sombra" />
            {design.shadow.enabled ? (
              <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", marginTop: 8 }}>
                <ColorField
                  id={id("shadow-color")}
                  label="Cor da sombra"
                  value={design.shadow.color}
                  error={err("shadow.color")}
                  onChange={(color) => set({ shadow: { color } })}
                />
                <RangeField
                  id={id("shadow-opacity")}
                  label="Opacidade"
                  limits={DESIGN_LIMITS.shadowOpacity}
                  step={0.01}
                  percent
                  value={design.shadow.opacity}
                  error={err("shadow.opacity")}
                  onChange={(opacity) => set({ shadow: { opacity } })}
                />
                <RangeField
                  id={id("shadow-blur")}
                  label="Desfoque"
                  limits={DESIGN_LIMITS.shadowBlur}
                  step={1}
                  value={design.shadow.blur}
                  error={err("shadow.blur")}
                  onChange={(blur) => set({ shadow: { blur } })}
                />
                <RangeField
                  id={id("shadow-distance")}
                  label="Distância"
                  limits={DESIGN_LIMITS.shadowDistance}
                  step={1}
                  value={design.shadow.distance}
                  error={err("shadow.distance")}
                  onChange={(distance) => set({ shadow: { distance } })}
                />
                <RangeField
                  id={id("shadow-angle")}
                  label="Ângulo (graus)"
                  limits={DESIGN_LIMITS.shadowAngle}
                  step={1}
                  value={design.shadow.angle}
                  error={err("shadow.angle")}
                  onChange={(angle) => set({ shadow: { angle } })}
                />
              </div>
            ) : null}
          </section>

          <section className="edit-group">
            <h3>Fundo</h3>
            <Switch
              checked={design.background.enabled}
              onChange={(enabled) => set({ background: { enabled } })}
              label="Usar caixa de fundo"
            />
            {design.background.enabled ? (
              <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", marginTop: 8 }}>
                <ColorField
                  id={id("bg-color")}
                  label="Cor do fundo"
                  value={design.background.color}
                  error={err("background.color")}
                  onChange={(color) => set({ background: { color } })}
                />
                <RangeField
                  id={id("bg-opacity")}
                  label="Opacidade"
                  limits={DESIGN_LIMITS.backgroundOpacity}
                  step={0.01}
                  percent
                  value={design.background.opacity}
                  error={err("background.opacity")}
                  onChange={(opacity) => set({ background: { opacity } })}
                />
                <RangeField
                  id={id("bg-radius")}
                  label="Arredondamento"
                  limits={DESIGN_LIMITS.backgroundRadius}
                  step={0.01}
                  percent
                  value={design.background.radius}
                  error={err("background.radius")}
                  onChange={(radius) => set({ background: { radius } })}
                />
              </div>
            ) : null}
          </section>

          <section className="edit-group">
            <h3>Espaçamento</h3>
            <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}>
              <RangeField
                id={id("letter")}
                label="Entre letras"
                limits={DESIGN_LIMITS.letterSpacing}
                step={1}
                value={design.letter_spacing}
                error={err("letter_spacing")}
                onChange={(letter_spacing) => set({ letter_spacing })}
              />
              <RangeField
                id={id("line")}
                label="Entre linhas"
                limits={DESIGN_LIMITS.lineSpacing}
                step={1}
                value={design.line_spacing}
                error={err("line_spacing")}
                onChange={(line_spacing) => set({ line_spacing })}
              />
            </div>
          </section>

          <section className="edit-group">
            <h3>Posição</h3>
            <div className="seg" role="group" aria-label="Atalhos de posição">
              {POSITION_PRESETS.map((p) => (
                <button
                  key={p.key}
                  type="button"
                  className={presetKey === p.key && design.position.x === 0 ? "on" : undefined}
                  aria-pressed={presetKey === p.key && design.position.x === 0}
                  onClick={() => set({ position: { x: 0, y: p.y } })}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", marginTop: 8 }}>
              <RangeField
                id={id("pos-x")}
                label="Horizontal (−1 esquerda, 1 direita)"
                limits={DESIGN_LIMITS.position}
                step={0.01}
                value={design.position.x}
                error={err("position.x")}
                onChange={(x) => set({ position: { x } })}
              />
              <RangeField
                id={id("pos-y")}
                label="Vertical (−1 base, 1 topo)"
                limits={DESIGN_LIMITS.position}
                step={0.01}
                value={design.position.y}
                error={err("position.y")}
                onChange={(y) => set({ position: { y } })}
              />
              <RangeField
                id={id("max-width")}
                label="Largura máxima da linha"
                limits={DESIGN_LIMITS.maxLineWidth}
                step={0.01}
                percent
                value={design.max_line_width}
                error={err("max_line_width")}
                onChange={(max_line_width) => set({ max_line_width })}
              />
            </div>
          </section>

          <section className="edit-group">
            <h3>Destaque da palavra falada</h3>
            <Switch
              checked={design.highlight.enabled}
              onChange={(enabled) => set({ highlight: { enabled } })}
              label="Destacar a palavra falada"
            />
            {design.highlight.enabled ? (
              <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", marginTop: 8 }}>
                <ColorField
                  id={id("hl-color")}
                  label="Cor do destaque"
                  value={design.highlight.color}
                  error={err("highlight.color")}
                  onChange={(color) => set({ highlight: { color } })}
                />
              </div>
            ) : null}
          </section>

          <section className="edit-group">
            <h3>Animações</h3>
            <div style={{ display: "grid", gap: 12 }}>
              {ANIMATION_TYPES.map((type) => (
                <AnimationPicker
                  key={type}
                  type={type}
                  design={design}
                  showPro={showPro}
                  error={err(`animation.${type}`)}
                  onChange={(value, item) => {
                    if (item) setProChosen((p) => ({ ...p, [`anim:${item.id}`]: item.is_pro }));
                    set({ animation: { [type]: value } });
                  }}
                />
              ))}
            </div>
          </section>

          {warnings.map((w) => (
            <div key={w} className="alert warn" role="status" style={{ marginTop: 8 }}>
              <span className="grow small">{w}</span>
            </div>
          ))}

          {showErrors && (hasErrors || nameError) ? (
            <div className="alert err" role="alert" style={{ marginTop: 8 }}>
              <span className="grow small">
                Corrija antes de salvar: {[nameError, ...Object.values(errors)].filter(Boolean).join(" ")}
              </span>
            </div>
          ) : null}

          <div className="row" style={{ marginTop: 14 }}>
            <button type="button" className="btn btn-primary" disabled={saving} onClick={() => void save("new")}>
              {saving ? <Spinner /> : null}
              Salvar como novo
            </button>
            {saved.id !== null ? (
              <button type="button" className="btn" disabled={saving || !dirty} onClick={() => void save("update")}>
                Salvar alterações
              </button>
            ) : null}
            <button
              type="button"
              className="btn"
              disabled={saving}
              title={saved.id === null ? "Salva como novo modelo e o define como padrão." : "Salva as alterações e define como padrão."}
              onClick={() => void save("default")}
            >
              Usar como padrão
            </button>
            <button type="button" className="btn btn-ghost" disabled={saving} onClick={cancel}>
              {dirty ? "Cancelar" : "Fechar"}
            </button>
          </div>
        </div>

        <aside className="edit-aside">
          <section className="edit-group">
            <h3>Prévia</h3>
            <CaptionPreview
              design={design}
              text={sample}
              fontStack={previewFontStack(font.family, design.font.name)}
              backgroundUrl={backgroundUrl}
              maxWords={maxWords}
              maxChars={maxChars}
            />
            {font.approximate ? (
              <div className="field-hint">
                A prévia usa fonte aproximada (não há arquivo local desta fonte
                {design.font.source === "capcut" ? "; o CapCut baixa a fonte ao abrir o projeto" : ""}).
              </div>
            ) : font.loading ? (
              <div className="field-hint">Carregando a fonte…</div>
            ) : null}
            <div style={{ display: "grid", gap: 8, marginTop: 10 }}>
              <Field label="Texto de exemplo" htmlFor={id("sample")}>
                <input
                  id={id("sample")}
                  className="input"
                  value={sample}
                  maxLength={200}
                  onChange={(e) => setSample(e.target.value)}
                />
              </Field>
              <Field label="Fundo do quadro" htmlFor={id("media")}>
                <select
                  id={id("media")}
                  className="select"
                  value={mediaId ?? ""}
                  onChange={(e) => setMediaId(e.target.value ? Number(e.target.value) : null)}
                >
                  <option value="">Fundo neutro</option>
                  {(media.data?.items ?? []).map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.original_filename ?? `Vídeo ${m.id}`}
                      {m.source_profile ? ` (@${m.source_profile})` : ""}
                    </option>
                  ))}
                </select>
              </Field>
              {media.error ? <div className="field-error">{media.error}</div> : null}
            </div>
          </section>
          <section className="edit-group">
            <h3>Prévia real</h3>
            <RealPreviewPanel
              design={design}
              mediaId={mediaId}
              blockedReason={hasErrors ? "Corrija os campos do modelo para gerar a prévia real." : null}
            />
          </section>
        </aside>
      </div>
      {confirmDiscard ? (
        <ConfirmDialog
          title="Descartar alterações"
          confirmLabel="Descartar"
          cancelLabel="Continuar editando"
          danger
          onConfirm={() => {
            setConfirmDiscard(false);
            onClose();
          }}
          onCancel={() => setConfirmDiscard(false)}
        >
          <p>As alterações deste modelo ainda não foram salvas. Descartar?</p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

/* ---------------- Controles ---------------- */

function formatNumber(value: number, percent: boolean): string {
  if (!Number.isFinite(value)) return "";
  return percent ? String(Math.round(value * 100)) : String(Math.round(value * 1000) / 1000);
}

/** Controle deslizante com campo numérico (porcentagem quando `percent`: o design guarda 0..1). */
function RangeField({
  id,
  label,
  limits,
  step,
  value,
  percent = false,
  error,
  onChange,
}: {
  id: string;
  label: string;
  limits: readonly [number, number];
  step: number;
  value: number;
  percent?: boolean;
  error: string | null;
  onChange: (value: number) => void;
}) {
  const [min, max] = limits;
  return (
    <Field label={percent ? `${label} (%)` : label} htmlFor={id} error={error}>
      <div className="caption-range">
        <input
          type="range"
          className="range"
          aria-label={label}
          min={min}
          max={max}
          step={step}
          value={Number.isFinite(value) ? clamp(value, min, max) : min}
          onChange={(e) => onChange(Number(e.target.value))}
        />
        <input
          id={id}
          className={`input${error ? " invalid" : ""}`}
          type="number"
          min={percent ? min * 100 : min}
          max={percent ? max * 100 : max}
          step={percent ? 1 : step}
          value={formatNumber(value, percent)}
          onChange={(e) => {
            const n = e.target.value === "" ? Number.NaN : Number(e.target.value);
            onChange(percent ? n / 100 : n);
          }}
        />
      </div>
    </Field>
  );
}

/** Cor #RRGGBB: seletor do sistema + campo de texto (normalizado ao sair do campo). */
function ColorField({
  id,
  label,
  value,
  error,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  error: string | null;
  onChange: (value: string) => void;
}) {
  return (
    <Field label={label} htmlFor={id} error={error}>
      <div className="caption-color">
        <input
          type="color"
          aria-label={`${label} (seletor)`}
          value={colorInputValue(value)}
          onChange={(e) => onChange(e.target.value.toUpperCase())}
        />
        <input
          id={id}
          className={`input${error ? " invalid" : ""}`}
          style={{ width: 110 }}
          value={value}
          maxLength={7}
          spellCheck={false}
          onChange={(e) => onChange(e.target.value.toUpperCase())}
          onBlur={() => onChange(normalizeHex(value) ?? value)}
        />
      </div>
    </Field>
  );
}

const FONT_SOURCES: { value: CapCutFontSource | ""; label: string }[] = [
  { value: "", label: "Todas as origens" },
  { value: "capcut", label: FONT_SOURCE_LABEL.capcut },
  { value: "windows", label: FONT_SOURCE_LABEL.windows },
  { value: "bundled", label: FONT_SOURCE_LABEL.bundled },
];

function FontPicker({
  design,
  showPro,
  error,
  onPick,
}: {
  design: CapCutDesign;
  showPro: boolean;
  error: string | null;
  onPick: (item: CapCutFontItem) => void;
}) {
  const [open, setOpen] = useState(false);
  const f = design.font;
  return (
    <div>
      <div className="row">
        <strong>{f.name || "Nenhuma fonte"}</strong>
        <Pill tone="muted">{FONT_SOURCE_LABEL[f.source]}</Pill>
        <button type="button" className="btn btn-sm" style={{ marginLeft: "auto" }} onClick={() => setOpen((o) => !o)}>
          {open ? "Fechar busca" : "Trocar fonte"}
        </button>
      </div>
      {error ? <div className="field-error">{error}</div> : null}
      {open ? (
        <FontSearch
          currentKey={fontKey(f)}
          showPro={showPro}
          onPick={(item) => {
            onPick(item);
            setOpen(false);
          }}
        />
      ) : null}
    </div>
  );
}

function FontSearch({
  currentKey,
  showPro,
  onPick,
}: {
  currentKey: string | null;
  showPro: boolean;
  onPick: (item: CapCutFontItem) => void;
}) {
  const [q, setQ] = useState("");
  const [source, setSource] = useState<CapCutFontSource | "">("");
  const query = useDebounced(q.trim(), 300);
  const fonts = useQuery(`capcut-fonts:${query}:${source}:${showPro}`, () =>
    api.capcutFonts({ q: query, source: source || null, include_pro: showPro, limit: 40 }),
  );
  return (
    <div style={{ marginTop: 8 }}>
      <div className="row" style={{ flexWrap: "nowrap" }}>
        <input
          className="input grow"
          type="search"
          aria-label="Buscar fonte"
          placeholder="Buscar fonte (ex.: Montserrat)"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select
          className="select"
          style={{ width: "auto" }}
          aria-label="Origem da fonte"
          value={source}
          onChange={(e) => setSource(e.target.value as CapCutFontSource | "")}
        >
          {FONT_SOURCES.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      {fonts.error ? (
        <div style={{ marginTop: 6 }}>
          <ErrorLine message={fonts.error} onRetry={fonts.reload} />
        </div>
      ) : fonts.data === undefined ? (
        <div className="muted small" style={{ marginTop: 6 }}>
          <Spinner /> Carregando fontes…
        </div>
      ) : fonts.data.items.length === 0 ? (
        <div className="muted small" style={{ marginTop: 6 }}>
          Nenhuma fonte encontrada{showPro ? "" : " entre as gratuitas"}.
        </div>
      ) : (
        <>
          <div className="caption-options" role="listbox" aria-label="Fontes">
            {fonts.data.items.map((item) => (
              <button
                key={item.key}
                type="button"
                role="option"
                aria-selected={item.key === currentKey}
                className={`caption-option${item.key === currentKey ? " on" : ""}`}
                onClick={() => onPick(item)}
              >
                <span className="grow">{item.name}</span>
                <span className="muted small">{FONT_SOURCE_LABEL[item.source]}</span>
                {item.is_pro ? <Pill tone="warn">Pro</Pill> : null}
                {item.source === "capcut" && !item.downloaded ? (
                  <span className="muted small" title="O CapCut baixa a fonte ao abrir o projeto.">
                    baixa no 1º uso
                  </span>
                ) : null}
              </button>
            ))}
          </div>
          {fonts.data.total > fonts.data.items.length ? (
            <div className="field-hint">
              Mostrando {fonts.data.items.length} de {fonts.data.total}; refine a busca.
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

function AnimationPicker({
  type,
  design,
  showPro,
  error,
  onChange,
}: {
  type: CapCutAnimationType;
  design: CapCutDesign;
  showPro: boolean;
  error: string | null;
  onChange: (value: CapCutDesign["animation"][CapCutAnimationType], item?: CapCutAnimationItem) => void;
}) {
  const [open, setOpen] = useState(false);
  const current = design.animation[type];
  const durationId = useId();
  return (
    <div>
      <div className="row">
        <span style={{ minWidth: 120 }}>{ANIMATION_TYPE_LABEL[type]}</span>
        <strong>{current ? current.name : "Nenhuma"}</strong>
        {current ? (
          <span className="row" style={{ gap: 6 }}>
            <label className="muted small" htmlFor={durationId}>
              Duração (ms)
            </label>
            <input
              id={durationId}
              className={`input${error ? " invalid" : ""}`}
              type="number"
              style={{ width: 90 }}
              min={DESIGN_LIMITS.animationMs[0]}
              max={DESIGN_LIMITS.animationMs[1]}
              step={50}
              value={Number.isFinite(current.duration_ms) ? current.duration_ms : ""}
              onChange={(e) =>
                onChange({ ...current, duration_ms: e.target.value === "" ? Number.NaN : Number(e.target.value) })
              }
            />
          </span>
        ) : null}
        <span className="row" style={{ marginLeft: "auto", gap: 4 }}>
          {current ? (
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => onChange(null)}>
              Remover
            </button>
          ) : null}
          <button type="button" className="btn btn-sm" onClick={() => setOpen((o) => !o)}>
            {open ? "Fechar busca" : current ? "Trocar" : "Escolher"}
          </button>
        </span>
      </div>
      {error ? <div className="field-error">{error}</div> : null}
      {open ? (
        <AnimationSearch
          type={type}
          currentId={current?.id ?? null}
          showPro={showPro}
          onPick={(item) => {
            const [min, max] = DESIGN_LIMITS.animationMs;
            onChange(
              { id: item.id, name: item.name, duration_ms: clamp(item.default_duration_ms || 500, min, max) },
              item,
            );
            setOpen(false);
          }}
        />
      ) : null}
    </div>
  );
}

function AnimationSearch({
  type,
  currentId,
  showPro,
  onPick,
}: {
  type: CapCutAnimationType;
  currentId: string | null;
  showPro: boolean;
  onPick: (item: CapCutAnimationItem) => void;
}) {
  const [q, setQ] = useState("");
  const query = useDebounced(q.trim(), 300);
  const anims = useQuery(`capcut-anims:${type}:${query}:${showPro}`, () =>
    api.capcutAnimations({ type, q: query, include_pro: showPro, limit: 40 }),
  );
  const label = ANIMATION_TYPE_LABEL[type].toLowerCase();
  return (
    <div style={{ marginTop: 6 }}>
      <input
        className="input"
        style={{ width: "100%" }}
        type="search"
        aria-label={`Buscar animação de ${label}`}
        placeholder={`Buscar animação de ${label} (ex.: Fade)`}
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      {anims.error ? (
        <div style={{ marginTop: 6 }}>
          <ErrorLine message={anims.error} onRetry={anims.reload} />
        </div>
      ) : anims.data === undefined ? (
        <div className="muted small" style={{ marginTop: 6 }}>
          <Spinner /> Carregando animações…
        </div>
      ) : anims.data.items.length === 0 ? (
        <div className="muted small" style={{ marginTop: 6 }}>
          Nenhuma animação encontrada{showPro ? "" : " entre as gratuitas"}.
        </div>
      ) : (
        <div className="caption-options" role="listbox" aria-label={`Animações de ${label}`}>
          {anims.data.items.map((item) => (
            <button
              key={item.id}
              type="button"
              role="option"
              aria-selected={item.id === currentId}
              className={`caption-option${item.id === currentId ? " on" : ""}`}
              onClick={() => onPick(item)}
            >
              <span className="grow">{item.name}</span>
              {item.is_pro ? <Pill tone="warn">Pro</Pill> : null}
              {!item.downloaded ? <span className="muted small">baixa no 1º uso</span> : null}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
