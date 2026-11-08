import { useState, type ReactNode } from "react";
import { ApiError, api, messageOf } from "../api/client";
import type { CapCutImportSettings, EditSettings, MediaChoice, UnifiedImportResult } from "../api/types";
import { ConfirmDialog, ErrorLine, Field, Spinner } from "./ui";
import {
  AUTO_EXPORT_CAPCUT_TEXT,
  AUTO_EXPORT_CHOICES,
  AUTO_EXPORT_NEEDS_PROCESSING_TEXT,
  autoExportNeedsProcessing,
  CONFIRM_ALL_TEXT,
  MEDIA_CHOICES,
  PATTERNS_NEED_PROFILES_TEXT,
  QUANTITY_PRESETS,
  classificationText,
  entriesOf,
  needsConfirmAll,
  postProcessingApplies,
  postTimesPlanText,
  resolveQuantity,
  unifiedBody,
  viewsAllowed,
  type AutoExportChoice,
  type QuantityMode,
} from "../lib/importUnified";
import type { PostProcessing } from "../lib/importOptions";
import {
  PERIOD_PRESETS,
  VIEWS_FILTER_ENABLED,
  VIEWS_PRESETS,
  periodSummary,
  resolvePeriod,
  resolveViewsRange,
  viewsPresetLabel,
  type MaxViewsMode,
  type MinViewsMode,
  type PeriodMode,
} from "../lib/viewsFilter";
import { formatCount } from "../lib/format";
import { useDebounced } from "../lib/useDebounced";
import { useQuery } from "../lib/useQuery";

const CLASSIFY_DEBOUNCE_MS = 400;
const INVALID_SHOWN_MAX = 20;

function ViewsSelect({
  id,
  label,
  offLabel,
  mode,
  text,
  disabled,
  onMode,
  onText,
}: {
  id: string;
  label: string;
  offLabel: string;
  mode: MinViewsMode;
  text: string;
  disabled: boolean;
  onMode: (m: MinViewsMode) => void;
  onText: (t: string) => void;
}) {
  return (
    <Field label={label} htmlFor={id}>
      <div className="row">
        <select
          id={id}
          className="select"
          value={mode}
          disabled={disabled}
          onChange={(e) => onMode(e.target.value as MinViewsMode)}
        >
          <option value="off">{offLabel}</option>
          {VIEWS_PRESETS.map((n) => (
            <option key={n} value={String(n)}>
              {viewsPresetLabel(n)}
            </option>
          ))}
          <option value="custom">Personalizado…</option>
        </select>
        {mode === "custom" && !disabled ? (
          <input
            className="input"
            inputMode="numeric"
            aria-label={`${label} personalizado`}
            value={text}
            onChange={(e) => onText(e.target.value)}
            style={{ maxWidth: 140 }}
          />
        ) : null}
      </div>
    </Field>
  );
}

/**
 * Importação unificada: um campo para links de publicações e/ou perfis, tipo de mídia, quantidade por perfil, filtros
 * (período e visualizações, só perfis) e o pós-processamento existente (passado pela página em `processing`).
 */
export function ImportUnifiedForm({
  blocker,
  processing,
  post,
  presetId,
  edit,
  capcut = null,
  editProblem,
  onCreated,
  onError,
  onConsult,
  consultBusy,
  patternsMode = false,
}: {
  /** Motivo que impede iniciar vindo da página (sessão, pausa); null = pode iniciar. */
  blocker: string | null;
  processing: ReactNode;
  post: PostProcessing;
  presetId: number | null;
  edit: EditSettings | null;
  /** "Edição CapCut": configuração do lote (null nas outras escolhas). */
  capcut?: CapCutImportSettings | null;
  editProblem: string | null;
  onCreated: (result: UnifiedImportResult) => void;
  onError: (err: unknown) => void;
  /** Consulta de perfil único (prévia), como na aba "Por perfil". */
  onConsult: (username: string) => void;
  consultBusy: boolean;
  /** Aba "Consultar padrões": só perfis, mídia fixa "none" (nada é baixado), horário sempre ligado. */
  patternsMode?: boolean;
}) {
  const [text, setText] = useState("");
  const [media, setMedia] = useState<MediaChoice>(patternsMode ? "none" : "videos");
  const [postTimes, setPostTimes] = useState(false);
  // "Nada — só consultar padrões": horário ligado e travado; sem pós-processamento, ZIP automático nem visualizações.
  const patternsOnly = media === "none";
  const effectivePost: PostProcessing = patternsOnly ? "none" : post;
  // Capas e fotos nunca são processadas: sem versão editada para exportar. Na Edição CapCut, os editados ficam no
  // lote dela (não na exportação da importação).
  const noEdited = (post: string) => post === "none" || post === "capcut" || media === "covers" || media === "photos";
  const autoExportText = post === "capcut" ? AUTO_EXPORT_CAPCUT_TEXT : AUTO_EXPORT_NEEDS_PROCESSING_TEXT;
  const [quantityMode, setQuantityMode] = useState<QuantityMode>("50");
  const [quantityText, setQuantityText] = useState("");
  const [periodMode, setPeriodMode] = useState<PeriodMode>("any");
  const [periodText, setPeriodText] = useState("");
  const [minMode, setMinMode] = useState<MinViewsMode>("off");
  const [minText, setMinText] = useState("");
  const [maxMode, setMaxMode] = useState<MaxViewsMode>("off");
  const [maxText, setMaxText] = useState("");
  const [autoExport, setAutoExport] = useState<AutoExportChoice>("off");
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const key = entriesOf(text).join("\n");
  const requested = useDebounced(key, CLASSIFY_DEBOUNCE_MS);
  const classify = useQuery(`imports-classify:${requested}`, async () =>
    requested ? { key: requested, data: await api.classifyImport({ inputs: requested.split("\n") }) } : null,
  );
  const settled = classify.data && classify.data.key === key && requested === key ? classify.data.data : null;
  const checking = key !== "" && !settled && !classify.error;
  const hasProfiles = !!settled && settled.profiles.length > 0;
  const quantity = resolveQuantity(quantityMode, quantityText);
  const period = resolvePeriod(periodMode, periodText);
  const allowViews = viewsAllowed(media);
  const views = resolveViewsRange(allowViews, { mode: minMode, text: minText }, { mode: maxMode, text: maxText });
  const singleProfile = settled && settled.profiles.length === 1 && settled.links.length === 0 ? settled.profiles[0] : null;
  const onlyLinks = patternsOnly && !!settled && settled.profiles.length === 0 && settled.links.length > 0;
  const planText = postTimesPlanText(media, patternsOnly || postTimes, !!settled && settled.links.length > 0);

  const problem =
    blocker ??
    (key === "" ? "Cole ao menos um link de publicação ou perfil." : null) ??
    (checking ? "Conferindo a lista…" : null) ??
    (settled && settled.profiles.length + settled.links.length === 0 ? "Nenhuma entrada válida na lista." : null) ??
    (hasProfiles && !quantity.ok ? quantity.error : null) ??
    (hasProfiles && !period.ok ? period.error : null) ??
    (hasProfiles && !views.ok ? views.error : null) ??
    (onlyLinks ? PATTERNS_NEED_PROFILES_TEXT : null) ??
    (patternsOnly ? null : editProblem) ??
    (effectivePost === "preset" && presetId === null ? "Escolha um preset." : null) ??
    (!patternsOnly && noEdited(post) && autoExportNeedsProcessing(autoExport) ? autoExportText : null);

  async function send(confirmAll: boolean) {
    const body = unifiedBody({
      text,
      media,
      quantity: hasProfiles ? quantity : { ok: true, limit: 50 },
      confirmAll,
      periodDays: hasProfiles && period.ok ? period.days : null,
      minViews: hasProfiles && views.ok ? views.minViews : null,
      maxViews: hasProfiles && views.ok ? views.maxViews : null,
      post: effectivePost,
      presetId,
      edit,
      capcut,
      autoExport: patternsOnly ? "off" : autoExport,
      postTimes: patternsOnly || postTimes,
    });
    if (!body) return;
    setSubmitting(true);
    try {
      const result = await api.startImport(body);
      setText("");
      onCreated(result);
    } catch (err) {
      onError(err instanceof ApiError ? err : new Error(messageOf(err)));
    } finally {
      setSubmitting(false);
    }
  }

  function submit() {
    if (problem !== null || submitting) return;
    if (needsConfirmAll(settled, hasProfiles ? quantity : { ok: true, limit: 50 })) setConfirming(true);
    else void send(false);
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      {patternsMode ? (
        <div className="muted small" style={{ marginBottom: 10 }}>
          Lê o horário das publicações de cada perfil, sem baixar nenhuma mídia, e gera o relatório de padrões (.zip):
          tempo médio entre posts, rajadas, pausas, horários e dias preferidos e a sequência dos posts.
        </div>
      ) : null}
      <Field
        label={patternsMode ? "Perfis" : "Cole links de publicações e/ou perfis"}
        htmlFor="imp-unified-text"
        hint={
          patternsMode
            ? "Um por linha: usuario, @usuario ou link do perfil."
            : "Um por linha (vírgula e ponto e vírgula também separam; espaço só entre links e @usuario): links de /p/, /reel/, /tv/, links de perfil, @usuario ou usuario."
        }
      >
        <textarea
          id="imp-unified-text"
          className="input textarea"
          rows={5}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={patternsMode ? "@perfil\nusuario" : "https://www.instagram.com/reel/ABC123/\n@perfil"}
        />
      </Field>
      <div className="row small" aria-live="polite" style={{ marginTop: 6 }}>
        {checking ? <Spinner /> : null}
        {settled ? <span>{classificationText(settled)}</span> : null}
        {classify.error && key ? <ErrorLine message={classify.error} /> : null}
        {singleProfile ? (
          <button type="button" className="btn btn-sm" disabled={consultBusy} onClick={() => onConsult(singleProfile)}>
            Consultar perfil
          </button>
        ) : null}
      </div>
      {settled && settled.invalid.length ? (
        <ul className="small tone-err" style={{ marginTop: 4 }}>
          {settled.invalid.slice(0, INVALID_SHOWN_MAX).map((entry) => (
            <li key={entry.entry}>
              {entry.entry}: {entry.reason}
            </li>
          ))}
          {settled.invalid.length > INVALID_SHOWN_MAX ? (
            <li>e mais {formatCount(settled.invalid.length - INVALID_SHOWN_MAX)}</li>
          ) : null}
        </ul>
      ) : null}

      <div style={{ marginTop: 14 }} hidden={patternsMode}>
        <div className="field-label" id="imp-media-label" style={{ marginBottom: 6 }}>
          Tipo de mídia
        </div>
        <div className="radio-list" role="radiogroup" aria-labelledby="imp-media-label">
          {MEDIA_CHOICES.filter((choice) => choice.value !== "none").map((choice) => (
            <label key={choice.value} className="radio" title={choice.hint}>
              <input
                type="radio"
                name="imp-media"
                value={choice.value}
                checked={media === choice.value}
                onChange={() => setMedia(choice.value)}
              />
              <span>{choice.label}</span>
            </label>
          ))}
        </div>
        <div className="muted small">{MEDIA_CHOICES.find((c) => c.value === media)?.hint}</div>
        <label className="row" style={{ gap: 8, marginTop: 8 }}>
          <input
            type="checkbox"
            checked={patternsOnly || postTimes}
            disabled={patternsOnly}
            onChange={(e) => setPostTimes(e.target.checked)}
          />
          <span>Puxar horário dos posts (relatório de padrões)</span>
        </label>
        <div className="muted small">
          {patternsOnly
            ? "Sempre ligado em “Consultar padrões”."
            : "Guarda o horário de cada publicação dos perfis para baixar à parte um relatório .zip de padrões (tempo médio entre posts e sequências)."}
        </div>
        {onlyLinks ? (
          <div className="alert warn" role="status" style={{ marginTop: 8 }}>
            <span className="grow">{PATTERNS_NEED_PROFILES_TEXT}</span>
          </div>
        ) : null}
      </div>

      <fieldset disabled={!hasProfiles} style={{ marginTop: 14, border: "none", padding: 0, minWidth: 0 }}>
        <legend className="field-label">
          {patternsMode ? "Quais publicações analisar" : `Perfis ${hasProfiles ? "" : "(só vale quando a lista tem perfis)"}`}
        </legend>
        <div className="row wrap" style={{ gap: 12, alignItems: "flex-start" }}>
          <Field label="Quantidade por perfil" htmlFor="imp-quantity" error={hasProfiles && !quantity.ok ? quantity.error : null}>
            <div className="row">
              <select
                id="imp-quantity"
                className="select"
                value={quantityMode}
                onChange={(e) => setQuantityMode(e.target.value as QuantityMode)}
              >
                {QUANTITY_PRESETS.map((n) => (
                  <option key={n} value={String(n)}>
                    Últimas {n}
                  </option>
                ))}
                <option value="custom">Personalizada…</option>
                <option value="all">Todas</option>
              </select>
              {quantityMode === "custom" ? (
                <input
                  className="input"
                  inputMode="numeric"
                  aria-label="Quantidade personalizada"
                  value={quantityText}
                  onChange={(e) => setQuantityText(e.target.value)}
                  style={{ maxWidth: 120 }}
                />
              ) : null}
            </div>
          </Field>
          <Field label="Período de postagem" htmlFor="imp-unified-period" error={hasProfiles && !period.ok ? period.error : null}>
            <div className="row">
              <select
                id="imp-unified-period"
                className="select"
                value={periodMode}
                onChange={(e) => setPeriodMode(e.target.value as PeriodMode)}
              >
                <option value="any">Qualquer data</option>
                {PERIOD_PRESETS.map((d) => (
                  <option key={d} value={String(d)}>
                    {periodSummary(d)}
                  </option>
                ))}
                <option value="custom">Personalizado…</option>
              </select>
              {periodMode === "custom" ? (
                <input
                  className="input"
                  inputMode="numeric"
                  aria-label="Dias do período"
                  value={periodText}
                  onChange={(e) => setPeriodText(e.target.value)}
                  style={{ maxWidth: 100 }}
                />
              ) : null}
            </div>
          </Field>
          {VIEWS_FILTER_ENABLED && !patternsMode ? (
            <>
              <ViewsSelect
                id="imp-min-views"
                label="Mínimo de visualizações"
                offLabel="Sem mínimo"
                mode={minMode}
                text={minText}
                disabled={!allowViews}
                onMode={setMinMode}
                onText={setMinText}
              />
              <ViewsSelect
                id="imp-max-views"
                label="Máximo de visualizações"
                offLabel="Sem máximo"
                mode={maxMode}
                text={maxText}
                disabled={!allowViews}
                onMode={setMaxMode}
                onText={setMaxText}
              />
            </>
          ) : null}
        </div>
        {VIEWS_FILTER_ENABLED && !patternsMode ? (
          <div className={`small${hasProfiles && !views.ok ? " tone-err" : " muted"}`}>
            {patternsOnly
              ? "Só padrões não usa filtro de visualizações. Quantidade e período continuam valendo (a quantidade conta publicações)."
              : !allowViews
                ? "O filtro de visualizações só vale para vídeos: escolha “Somente vídeos”, “Somente capas” ou “Vídeos e capas”."
                : hasProfiles && !views.ok
                  ? views.error
                  : "Vídeos fora do intervalo ou sem contagem de visualizações não são baixados. A capa usa as visualizações do vídeo."}
          </div>
        ) : null}
      </fieldset>

      {patternsOnly ? null : processing}
      {!patternsOnly && !postProcessingApplies(media) && post !== "none" ? (
        <div className="muted small">
          Com “{MEDIA_CHOICES.find((c) => c.value === media)?.label}” as mídias são só baixadas (sem processamento ou
          edição).
        </div>
      ) : null}
      {media === "all" || media === "videos_covers" ? (
        post !== "none" ? (
          <div className="muted small">Só os vídeos são processados ou editados; fotos e capas são só baixadas.</div>
        ) : null
      ) : null}

      {patternsOnly ? null : (
        <div style={{ marginTop: 12 }}>
          <Field label="Exportar ZIP automaticamente ao concluir" htmlFor="imp-auto-export">
            <select
              id="imp-auto-export"
              value={autoExport}
              onChange={(e) => setAutoExport(e.target.value as AutoExportChoice)}
            >
              {AUTO_EXPORT_CHOICES.map((choice) => (
                <option
                  key={choice.value}
                  value={choice.value}
                  disabled={noEdited(post) && autoExportNeedsProcessing(choice.value)}
                >
                  {choice.label}
                </option>
              ))}
            </select>
          </Field>
          {post === "none" || post === "capcut" ? (
            <div className={`small${autoExportNeedsProcessing(autoExport) ? " tone-err" : " muted"}`}>
              {autoExportText}
            </div>
          ) : null}
          {autoExport !== "off" ? (
            <div className="muted small">
              Quando a importação terminar (e o processamento dela também), um único ZIP é preparado; o botão de
              baixar aparece no painel da importação.
            </div>
          ) : null}
        </div>
      )}

      {planText ? (
        <div className="small" role="status" style={{ marginTop: 12 }}>
          {planText}
        </div>
      ) : null}

      <div className="row" style={{ marginTop: 16 }}>
        <button type="submit" className="btn btn-primary" disabled={problem !== null || submitting}>
          {submitting ? <Spinner /> : null} {patternsOnly ? "Consultar padrões" : "Iniciar importação"}
        </button>
        {problem && key !== "" ? <span className="muted small">{problem}</span> : null}
      </div>

      {confirming ? (
        <ConfirmDialog
          title="Importar todas as publicações?"
          confirmLabel="Importar todas"
          busy={submitting}
          onConfirm={() => {
            setConfirming(false);
            void send(true);
          }}
          onCancel={() => setConfirming(false)}
        >
          <p>{CONFIRM_ALL_TEXT}</p>
        </ConfirmDialog>
      ) : null}
    </form>
  );
}
