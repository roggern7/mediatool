import { useDeferredValue, useMemo, useRef, useState } from "react";
import { BadgeCheck, Download, Lock, Search, UserRound } from "lucide-react";
import { ApiError, api, messageOf } from "../api/client";
import { describeErrorCode } from "../api/errors";
import type {
  Batch,
  CapCutBatchMode,
  ContentType,
  HistoryHideResult,
  ImportGroup,
  Preset,
  ProfileInfo,
  UnifiedImportResult,
} from "../api/types";
import { ConfirmDialog, Field, Empty, ErrorLine, PageHeader, Pill, SkeletonRows, SplitBar, Spinner } from "../components/ui";
import {
  DELETE_WARNING,
  type HistoryTarget,
  deleteMediaLabel,
  historyConfirmText,
  historyConfirmTitle,
  historyHideBody,
  historyResultMessage,
  removeBlockedHint,
  sharedWarning,
} from "../lib/importHistory";
import { formatCount, formatDateTime } from "../lib/format";
import {
  ACTIVE_BATCH,
  CONTENT_LABEL,
  batchLabel,
  batchTitle,
  batchToneOf,
  batchOriginLabel,
  isDownloadOnly,
  isLinksBatch,
  isQueuedInGroup,
} from "../lib/labels";
import {
  CONTENT_CHOICES,
  DEFAULT_LIMIT_TEXT,
  LIMIT_SHORTCUTS,
  type LimitMode,
  POST_PROCESSING_DEFAULT,
  POST_PROCESSING_OPTIONS,
  type PostProcessing,
  editProblemOf,
  entriesSummary,
  invalidCountText,
  profilesCountText,
  resolveLimit,
  postProcessingOptions,
  startLabel,
} from "../lib/importOptions";
import {
  entriesKey,
  interpretGroupCreated,
  interpretStartError,
  planFromPreview,
  providerBlocker,
  settledPreview,
  startBlocker,
  startSummary,
} from "../lib/importStart";
import {
  type RecentEntry,
  groupDroppedText,
  groupProfileCounts,
  groupStatusLabel,
  groupTitle,
  mergeRecent,
  profileEndText,
} from "../lib/importGroup";
import { compactProfileEntries, parseProfileList } from "../lib/profile";
import {
  MIN_VIEWS_DISABLED_HINT,
  MIN_VIEWS_HINT,
  MIN_VIEWS_PRESETS,
  type MinViewsMode,
  PERIOD_HINT,
  PERIOD_PRESETS,
  type PeriodMode,
  minViewsAllowed,
  periodSummary,
  resolveMinViews,
  resolvePeriod,
  VIEWS_FILTER_ENABLED,
  viewsFilterText,
  visibleFilter,
} from "../lib/viewsFilter";
import { describeSessionTest } from "../lib/sessionTest";
import { isPaused, useNow } from "../lib/pause";
import { PauseBanner } from "../components/PauseBanner";
import { BatchNotice } from "../components/BatchNotice";
import { BatchLinksPanel } from "../components/BatchLinksPanel";
import { ImportGroupPanel } from "../components/ImportGroupPanel";
import { ImportLinksForm } from "../components/ImportLinksForm";
import { ImportUnifiedForm } from "../components/ImportUnifiedForm";
import { alreadyDownloadedNote, analyzedPostsText, createdText, isPatternsOnly } from "../lib/importUnified";
import { PostTimesReportButton } from "../components/PostTimesReportButton";
import { EditSettingsForm } from "../components/edit/EditSettingsForm";
import { DEFAULT_EDIT_FORM, type EditForm } from "../lib/editSettings";
import { ImportExportBar } from "../components/ExportPanel";
import { batchExportOffer, libraryPathForProfile } from "../lib/exportFolder";
import { listingDropped, listingDroppedSuffix, needsAttention } from "../lib/batchState";
import { navigate, ROUTES } from "../lib/router";
import { capcutForBatch, queueForBatch } from "../lib/batchRoute";
import { BatchConfigForm } from "../components/capcut/BatchConfigForm";
import {
  type BatchConfigPlan,
  type BatchEditForm,
  type BatchExportForm,
  DEFAULT_BATCH_EDIT,
  buildImportCapcut,
  configBlocker,
  exportFormFrom,
} from "../lib/capcutBatch";
import { POLL_STEPS_MS, nextPollMs } from "../lib/polling";
import { useDebounced } from "../lib/useDebounced";
import { useQuery } from "../lib/useQuery";
import { useToast } from "../lib/toast";

/** Espera depois da última tecla antes de pedir a pré-validação ao backend. */
const PREVIEW_DEBOUNCE_MS = 400;
/** Entradas inválidas listadas na tela (as demais entram em "e mais N"). */
const INVALID_SHOWN_MAX = 100;
const RECENT_ROWS = 8;

type Recent = RecentEntry<ImportGroup, Batch>;
type Selection = { kind: "batch" | "group"; id: number };
type ImportMode = "unified" | "profile" | "links" | "patterns";

// "Links e perfis" (importação unificada) é a tela padrão; "Por perfil" e "Por link" continuam como antes.
const IMPORT_MODES: { id: ImportMode; label: string }[] = [
  { id: "unified", label: "Links e perfis" },
  { id: "profile", label: "Por perfil" },
  { id: "links", label: "Por link" },
  { id: "patterns", label: "Consultar padrões" },
];

function defaultPresetId(presets: Preset[] | undefined, configured: number | null | undefined): number | null {
  if (!presets || presets.length === 0) return null;
  if (configured != null && presets.some((p) => p.id === configured)) return configured;
  const anti = presets.find((p) => p.name === "Anti-duplicidade");
  return (anti ?? presets[0])?.id ?? null;
}

function selectionOf(entry: Recent): Selection {
  return { kind: entry.kind, id: entry.kind === "group" ? entry.group.id : entry.batch.id };
}

function isActiveEntry(entry: Recent): boolean {
  return entry.kind === "group"
    ? entry.group.status === "active" || entry.group.status === "attention"
    : ACTIVE_BATCH.has(entry.batch.status);
}

export function ImportPage() {
  const toast = useToast();
  const settings = useQuery("settings", () => api.getSettings());
  const presets = useQuery("presets", () => api.listPresets());

  // Só estado da tela: toda visita começa em "Links e perfis"; os rascunhos ficam ao trocar de modo.
  const [mode, setMode] = useState<ImportMode>("unified");
  const [linksText, setLinksText] = useState("");
  const [profileText, setProfileText] = useState("");
  const [content, setContent] = useState<ContentType>("all");
  const [limitMode, setLimitMode] = useState<LimitMode>("last");
  const [limitText, setLimitText] = useState(DEFAULT_LIMIT_TEXT);
  // Filtro de visualizações mínimas (só Reels/Vídeos): toda visita começa em "Sem filtro".
  const [minViewsMode, setMinViewsMode] = useState<MinViewsMode>("off");
  const [minViewsText, setMinViewsText] = useState("");
  // Período de postagem (perfil e vários perfis): toda visita começa em "Qualquer data".
  const [periodMode, setPeriodMode] = useState<PeriodMode>("any");
  const [periodText, setPeriodText] = useState("");
  const [presetChoice, setPresetChoice] = useState<number | null>(null);
  // Só estado da tela: toda visita começa em "Só baixar".
  const [post, setPost] = useState<PostProcessing>(POST_PROCESSING_DEFAULT);
  const [editForm, setEditForm] = useState<EditForm>(DEFAULT_EDIT_FORM);
  // "Edição CapCut": a mesma configuração (e os mesmos padrões) da página Edição CapCut.
  const [ccMode, setCcMode] = useState<CapCutBatchMode>("caption");
  const [ccStyleId, setCcStyleId] = useState<number | null>(null);
  const [ccEdit, setCcEdit] = useState<BatchEditForm>(DEFAULT_BATCH_EDIT);
  const [ccExportDraft, setCcExportDraft] = useState<BatchExportForm | null>(null);
  const [ccName, setCcName] = useState("");
  // Lote da Edição CapCut da última importação criada com "Edição CapCut" (aviso com o link).
  const [capcutBatchId, setCapcutBatchId] = useState<number | null>(null);
  const processAfter = post === "preset";
  const [submitting, setSubmitting] = useState(false);
  const [lookup, setLookup] = useState<{ username: string; info: ProfileInfo } | null>(null);
  const [lookupBusy, setLookupBusy] = useState(false);
  const [lookupError, setLookupError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Selection | null>(null);
  // Última importação ativa vista na lista: continua aberta depois de concluir, até ser fechada ou trocada.
  const [auto, setAuto] = useState<Selection | null>(null);
  // Lote de um perfil aberto a partir da lista de um grupo (vale só enquanto aquele grupo está aberto).
  const [child, setChild] = useState<{ groupId: number; batchId: number } | null>(null);
  // Aviso da última criação (perfis pulados); `groupId` nulo = nada foi criado.
  const [startNotice, setStartNotice] = useState<{ groupId: number | null; text: string } | null>(null);
  const [showAllBatches, setShowAllBatches] = useState(false);
  // Remover do histórico / limpar histórico: alvo, contagem prévia (dry_run) e a opção de apagar os vídeos.
  const [historyTarget, setHistoryTarget] = useState<HistoryTarget | null>(null);
  const [historyPreview, setHistoryPreview] = useState<HistoryHideResult | null>(null);
  const [historyDelete, setHistoryDelete] = useState(false);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyVersion, setHistoryVersion] = useState(0);
  const panelRef = useRef<HTMLDivElement>(null);

  // A lista do campo é recalculada sem travar a digitação (valor adiado) e conferida no backend com debounce.
  // A estimativa local é só o número provisório; válidos, inválidos e duplicatas exibidos e enviados vêm do backend.
  const deferredText = useDeferredValue(profileText);
  const draft = useMemo(
    () => ({
      key: entriesKey(compactProfileEntries(deferredText)),
      estimate: parseProfileList(deferredText).valid.length,
    }),
    [deferredText],
  );
  const requestedKey = useDebounced(draft.key, PREVIEW_DEBOUNCE_MS);
  const previewQuery = useQuery(`profiles-preview:${requestedKey}`, async () =>
    requestedKey ? { key: requestedKey, data: await api.previewProfiles(requestedKey.split("\n")) } : null,
  );
  const hasText = profileText.trim() !== "";
  const typing = deferredText !== profileText;
  const checking = typing || requestedKey !== draft.key || previewQuery.loading;
  const preview = hasText
    ? settledPreview({
        currentKey: draft.key,
        requestedKey,
        typing,
        loading: previewQuery.loading,
        result: previewQuery.data,
      })
    : null;
  const previewError = hasText && !checking && !preview ? previewQuery.error : null;
  const invalidCount = preview ? preview.invalid.length : 0;
  const singleProfile = preview && preview.valid.length === 1 ? (preview.valid[0] ?? null) : null;

  const limitChoice = resolveLimit(limitMode, limitText);
  const minViewsChoice = VIEWS_FILTER_ENABLED
    ? resolveMinViews(content, minViewsMode, minViewsText)
    : ({ ok: true, minViews: null } as const);
  const minViewsOn = minViewsAllowed(content);
  const periodChoice = resolvePeriod(periodMode, periodText);
  const sessionOk = settings.data?.instagram.session_configured ?? false;
  const now = useNow();
  const igStatus = settings.data?.instagram ?? null;
  const paused = isPaused(igStatus?.paused_until, now);
  const lastTest = settings.data?.instagram.last_test ?? null;
  const lastView = lastTest ? describeSessionTest(lastTest) : null;
  const presetId = presetChoice ?? defaultPresetId(presets.data, settings.data?.default_preset_id);
  const wantCapcut = post === "capcut";
  const ccSettings = useQuery(wantCapcut ? "capcut-settings" : "capcut-settings:off", () =>
    wantCapcut ? api.capcutSettings() : Promise.resolve(undefined),
  );
  const ccStyles = useQuery(wantCapcut ? "capcut-styles:import" : "capcut-styles:off", () =>
    wantCapcut ? api.capcutStyles() : Promise.resolve(undefined),
  );
  const ccPlan: BatchConfigPlan = {
    mode: ccMode,
    name: ccName,
    styleId: ccStyleId,
    exportForm: ccExportDraft ?? (ccSettings.data ? exportFormFrom(ccSettings.data) : null),
    edit: ccEdit,
  };
  const capcutProblem = wantCapcut ? configBlocker(ccPlan) : null;
  // O problema da edição ou da Edição CapCut bloqueia o envio nos três modos (como o de "Editar após baixar").
  const editProblem = editProblemOf(post, editForm) ?? capcutProblem;
  const postOptions = {
    ...postProcessingOptions(post, presetId, editForm),
    capcut: wantCapcut ? buildImportCapcut(ccPlan) : null,
  };
  const providerProblem = providerBlocker(
    settings.data?.instagram_provider,
    content,
    limitMode === "all",
    minViewsChoice.ok ? minViewsChoice.minViews : null,
    periodChoice.ok ? periodChoice.days : null,
  );
  const blocker = startBlocker({
    sessionOk,
    paused,
    submitting,
    preview,
    limit: limitChoice,
    minViews: minViewsChoice,
    period: periodChoice,
    process: processAfter,
    presetId,
    editProblem,
    providerProblem,
  });
  const canSubmit = blocker === null;
  const summary = startSummary({ preview, limit: limitChoice, content, minViews: minViewsChoice, period: periodChoice });

  const [recentPoll, setRecentPoll] = useState<number | null>(5_000);
  const recent = useQuery(
    "recent-imports",
    async () => {
      // Grupos (uma linha cada) + lotes avulsos e antigos; os filhos de grupo não entram na lista.
      const [groups, batches] = await Promise.all([
        api.listImportGroups(RECENT_ROWS, undefined, true),
        api.listBatches(RECENT_ROWS, 0, "instagram_import", { ungrouped: true, history: true }),
      ]);
      const items = mergeRecent(
        groups.items,
        batches.items.filter((b) => b.source === "instagram_import"),
        RECENT_ROWS,
      );
      const live = items.find(isActiveEntry);
      // Sem nenhuma ativa, a última aberta automaticamente continua na tela (ex.: grupo que acabou de concluir).
      if (live) setAuto(selectionOf(live));
      setRecentPoll(live ? 5_000 : null);
      return items;
    },
    recentPoll,
  );
  const recentItems = recent.data ?? [];
  const shown = selected ?? auto;
  const shownLive = shown !== null && recentItems.some((e) => isActiveEntry(e) && sameSelection(e, shown));
  const shownBatchId =
    shown?.kind === "batch" ? shown.id : shown && child?.groupId === shown.id ? child.batchId : null;

  function closeShown() {
    setSelected(null);
    // A importação ainda ativa volta a aparecer sozinha; a encerrada some de vez.
    if (!shownLive) setAuto(null);
  }

  function revealCreated() {
    recent.reload();
    window.requestAnimationFrame(() => {
      panelRef.current?.focus({ preventScroll: true });
      panelRef.current?.scrollIntoView({ block: "nearest" });
    });
  }

  async function submit() {
    const plan = planFromPreview(preview, {
      content,
      limit: limitChoice,
      ...postOptions,
      minViews: minViewsChoice,
      period: periodChoice,
    });
    if (!canSubmit || !plan) return;
    setSubmitting(true);
    setStartNotice(null);
    try {
      if (plan.kind === "single") {
        const batch = await api.createImport(plan.body);
        setSelected({ kind: "batch", id: batch.id });
        setCapcutBatchId(batch.capcut_batch_id ?? null);
        toast.push(
          "ok",
          post === "capcut"
            ? `Download de @${plan.body.profile} iniciado; os vídeos vão para a Edição CapCut.`
            : post === "edit"
            ? `Download e edição de @${plan.body.profile} iniciados.`
            : processAfter
            ? `Download e processamento de @${plan.body.profile} iniciados.`
            : `Download dos originais de @${plan.body.profile} iniciado.`,
        );
      } else {
        const detail = await api.createImportGroup(plan.body);
        const created = interpretGroupCreated(detail);
        setCapcutBatchId(detail.capcut_batch_id ?? null);
        setSelected({ kind: "group", id: created.groupId });
        if (created.notice) setStartNotice({ groupId: created.groupId, text: created.notice });
        toast.push("ok", created.message);
      }
      // Lista enviada: o campo é limpo (tipo, quantidade e processamento ficam) e o painel criado recebe o foco.
      setProfileText("");
      revealCreated();
    } catch (err) {
      const failure = interpretStartError(err instanceof ApiError ? err : null);
      if (failure.notice) setStartNotice({ groupId: null, text: failure.notice });
      if (failure.error) toast.push("err", failure.error);
      if (failure.refreshPreview) previewQuery.reload();
      if (failure.reloadRecent) recent.reload();
      settings.reload(); // pode ter entrado em pausa; atualiza a faixa
    } finally {
      setSubmitting(false);
    }
  }

  function openHistory(target: HistoryTarget) {
    setHistoryTarget(target);
    setHistoryDelete(false);
    setHistoryPreview(null);
    api
      .hideImportHistory(historyHideBody(target, false, true))
      .then(setHistoryPreview)
      .catch(() => undefined);
  }

  async function confirmHistory() {
    if (historyTarget === null) return;
    setHistoryBusy(true);
    try {
      const result = await api.hideImportHistory(historyHideBody(historyTarget, historyDelete));
      toast.push("ok", historyResultMessage(result));
      if (historyTarget.kind === "all" || (shown && shown.kind === historyTarget.kind && shown.id === historyTarget.id)) {
        setSelected(null);
        setAuto(null);
      }
      setHistoryTarget(null);
      setHistoryVersion((v) => v + 1);
      recent.reload();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setHistoryBusy(false);
    }
  }

  async function consult(target?: string) {
    const username = target ?? singleProfile;
    if (username === null) return;
    setLookupBusy(true);
    setLookupError(null);
    try {
      const info = await api.lookupProfile(username);
      setLookup({ username, info });
    } catch (err) {
      setLookup(null);
      setLookupError(messageOf(err));
      settings.reload();
    } finally {
      setLookupBusy(false);
    }
  }

  const lookupShown =
    lookup && (lookup.username === singleProfile || mode === "unified") ? lookup.info : null;

  function unifiedCreated(result: UnifiedImportResult) {
    // Grupo (2+ perfis) ou o primeiro lote criado abre no acompanhamento; o aviso lista o que foi criado junto.
    const first = result.group_id ?? result.batch_ids[0] ?? null;
    if (first !== null) setSelected({ kind: result.group_id !== null ? "group" : "batch", id: first });
    setStartNotice({ groupId: null, text: createdText(result) });
    setCapcutBatchId(result.capcut_batch_id ?? null);
    toast.push("ok", "Importação iniciada.");
    setLookup(null);
    revealCreated();
  }

  function unifiedFailed(err: unknown) {
    const failure = interpretStartError(err instanceof ApiError ? err : null);
    if (failure.notice) setStartNotice({ groupId: null, text: failure.notice });
    toast.push("err", failure.error ?? messageOf(err));
    if (failure.reloadRecent) recent.reload();
    settings.reload();
  }

  // Vale para os dois modos (por perfil e por link). Uma escolha só: o lote tem um único pós-processamento.
  const processingBlock = (
    <div style={{ marginTop: 16 }}>
      <div className="field-label" id="imp-post-label" style={{ marginBottom: 6 }}>
        Processamento
      </div>
      <div className="radio-list" role="radiogroup" aria-labelledby="imp-post-label">
        {POST_PROCESSING_OPTIONS.map((option) => (
          <label key={option.value} className="radio">
            <input
              type="radio"
              name="imp-post"
              value={option.value}
              checked={post === option.value}
              onChange={() => setPost(option.value)}
            />
            <span>{option.label}</span>
          </label>
        ))}
      </div>
      {processAfter ? (
        <div style={{ marginTop: 10, maxWidth: 280 }}>
          <Field
            label="Preset do processamento"
            htmlFor="imp-preset"
            error={
              presets.error ??
              (presets.data && presets.data.length === 0 ? "Nenhum preset disponível." : null)
            }
          >
            <select
              id="imp-preset"
              className="select"
              value={presetId ?? ""}
              disabled={!presets.data || presets.data.length === 0}
              onChange={(e) => setPresetChoice(Number(e.target.value))}
            >
              {presets.data === undefined ? <option value="">Carregando</option> : null}
              {presets.data?.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
      ) : null}
      {wantCapcut ? (
        <div style={{ marginTop: 10 }}>
          {ccSettings.error ? <ErrorLine message={ccSettings.error} onRetry={ccSettings.reload} /> : null}
          {ccStyles.error ? <ErrorLine message={ccStyles.error} onRetry={ccStyles.reload} /> : null}
          <BatchConfigForm
            mode={ccMode}
            onMode={setCcMode}
            styles={ccStyles.data}
            styleId={ccStyleId}
            onStyle={setCcStyleId}
            onEditStyles={() => navigate(ROUTES.captions)}
            presets={presets.data}
            edit={ccEdit}
            onEdit={setCcEdit}
            exportForm={ccPlan.exportForm}
            onExport={setCcExportDraft}
          />
          <div style={{ marginTop: 10, maxWidth: 360 }}>
            <Field label="Nome do lote (opcional)" htmlFor="imp-capcut-name" hint="Vazio: “Importação” com a data e a hora.">
              <input
                id="imp-capcut-name"
                className="input"
                maxLength={120}
                value={ccName}
                onChange={(e) => setCcName(e.target.value)}
              />
            </Field>
          </div>
          {capcutProblem ? (
            <div className="field-error" role="alert" style={{ marginTop: 8 }}>
              {capcutProblem}
            </div>
          ) : null}
        </div>
      ) : null}
      {post === "edit" ? (
        <div style={{ marginTop: 10 }}>
          <EditSettingsForm form={editForm} onChange={setEditForm} />
          {editProblem ? (
            <div className="field-error" role="alert" style={{ marginTop: 8 }}>
              {editProblem}
            </div>
          ) : null}
        </div>
      ) : null}
      <div className="muted small" style={{ marginTop: 8 }}>
        {wantCapcut
          ? "Os originais são baixados e cada vídeo novo entra num lote da Edição CapCut, criado agora com estas configurações. Fotos e capas são só baixadas. Acompanhe na página Edição CapCut."
          : post === "edit"
          ? "Os originais são baixados e cada vídeo ganha uma versão editada com estas configurações, como na página Editar. Fotos são só baixadas. Os originais ficam guardados."
          : processAfter
            ? "Os originais são baixados e, em seguida, processados com o preset escolhido. Os originais ficam guardados."
            : "Somente os arquivos originais serão baixados, sem nenhum processamento. Se quiser, processe depois pela Biblioteca."}
      </div>
    </div>
  );

  return (
    <div className="page">
      <PageHeader title="Importar do Instagram" subtitle="Baixe publicações e perfis para a biblioteca." />

      <div className="session-line">
        {settings.data === undefined && settings.loading ? (
          <Spinner />
        ) : settings.error && settings.data === undefined ? (
          <span className="tone-err grow">{settings.error}</span>
        ) : (
          <>
            <span className="grow">
              Instagram ·{" "}
              {sessionOk ? (
                <span className="tone-ok">Sessão configurada</span>
              ) : (
                <span className="tone-warn">Sessão não configurada</span>
              )}
              {sessionOk && lastView && lastTest ? (
                <span className="muted small">
                  {" "}
                  · Último teste: <span className={`tone-${lastView.tone}`}>{lastView.label}</span>,{" "}
                  {formatDateTime(lastTest.tested_at)}
                </span>
              ) : null}
            </span>
            {!sessionOk ? (
              <button type="button" className="btn btn-sm" onClick={() => navigate(ROUTES.settings)}>
                Configurar em Configurações
              </button>
            ) : null}
          </>
        )}
      </div>

      {igStatus && paused ? <PauseBanner status={igStatus} now={now} onReleased={settings.reload} /> : null}

      <div className="tabs" role="tablist" aria-label="Modo de importação">
        {IMPORT_MODES.map((m) => (
          <button
            key={m.id}
            type="button"
            role="tab"
            id={`imp-mode-${m.id}`}
            aria-selected={mode === m.id}
            aria-controls="imp-mode-panel"
            className={`tab${mode === m.id ? " on" : ""}`}
            onClick={() => setMode(m.id)}
          >
            {m.label}
          </button>
        ))}
      </div>

      <div id="imp-mode-panel" className="section" role="tabpanel" aria-labelledby={`imp-mode-${mode}`}>
        {mode === "unified" || mode === "patterns" ? (
          <ImportUnifiedForm
            key={mode}
            patternsMode={mode === "patterns"}
            blocker={!sessionOk ? "Configure a sessão do Instagram em Configurações." : paused ? "Instagram em pausa." : null}
            processing={processingBlock}
            post={post}
            presetId={presetId}
            edit={postOptions.edit}
            capcut={postOptions.capcut}
            editProblem={editProblem}
            onCreated={unifiedCreated}
            onError={unifiedFailed}
            onConsult={(username) => void consult(username)}
            consultBusy={lookupBusy || !sessionOk || paused}
          />
        ) : mode === "profile" ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (canSubmit) void submit();
            }}
          >
            <div className="import-form">
              <div className="span-all">
                <Field
                  label="Perfis"
                  htmlFor="imp-profiles"
                  hint="Um perfil por linha: usuario, @usuario ou a URL do perfil. Linhas vazias e repetidas são ignoradas."
                >
                  <textarea
                    id="imp-profiles"
                    className="input textarea mono"
                    rows={5}
                    value={profileText}
                    onChange={(e) => setProfileText(e.target.value)}
                    placeholder={"usuario\n@usuario\nhttps://www.instagram.com/usuario/"}
                    autoComplete="off"
                    autoCapitalize="off"
                    spellCheck={false}
                    aria-describedby="imp-profiles-status"
                  />
                </Field>
              </div>
              <Field label="Tipo de mídia" htmlFor="imp-content">
                <select
                  id="imp-content"
                  className="select"
                  value={content}
                  onChange={(e) => {
                    const next = e.target.value as ContentType;
                    setContent(next);
                    // O filtro de visualizações só vale para vídeos: outro conteúdo volta a "Sem filtro".
                    if (!minViewsAllowed(next)) setMinViewsMode("off");
                  }}
                >
                  {CONTENT_CHOICES.map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                label="Quantidade por perfil"
                htmlFor="imp-limit-mode"
                error={limitChoice.ok ? null : limitChoice.error}
              >
                <div className="row" style={{ flexWrap: "wrap" }}>
                  <select
                    id="imp-limit-mode"
                    className="select"
                    value={limitMode}
                    onChange={(e) => setLimitMode(e.target.value as LimitMode)}
                  >
                    <option value="last">Últimos N</option>
                    <option value="all">Todos</option>
                  </select>
                  {limitMode === "last" ? (
                    <>
                      <input
                        id="imp-limit"
                        className={`input${limitChoice.ok ? "" : " invalid"}`}
                        style={{ width: 96 }}
                        inputMode="numeric"
                        aria-label="Quantidade de mídias por perfil"
                        aria-invalid={!limitChoice.ok}
                        value={limitText}
                        onChange={(e) => setLimitText(e.target.value)}
                        autoComplete="off"
                      />
                      <div className="seg" role="group" aria-label="Atalhos de quantidade">
                        {LIMIT_SHORTCUTS.map((n) => (
                          <button
                            key={n}
                            type="button"
                            className={limitChoice.ok && limitChoice.limit === n ? "on" : ""}
                            aria-pressed={limitChoice.ok && limitChoice.limit === n}
                            onClick={() => setLimitText(String(n))}
                          >
                            {formatCount(n)}
                          </button>
                        ))}
                      </div>
                    </>
                  ) : null}
                </div>
              </Field>
              {VIEWS_FILTER_ENABLED ? (
              <Field
                label="Filtro: visualizações mínimas (só vídeos)"
                htmlFor="imp-min-views"
                error={minViewsChoice.ok ? null : minViewsChoice.error}
                hint={minViewsOn ? MIN_VIEWS_HINT : MIN_VIEWS_DISABLED_HINT}
              >
                <div className="row" style={{ flexWrap: "wrap" }}>
                  <select
                    id="imp-min-views"
                    className="select"
                    value={minViewsOn ? minViewsMode : "off"}
                    disabled={!minViewsOn}
                    onChange={(e) => setMinViewsMode(e.target.value as MinViewsMode)}
                  >
                    <option value="off">Sem filtro</option>
                    {MIN_VIEWS_PRESETS.map((n) => (
                      <option key={n} value={String(n)}>
                        {formatCount(n)}
                      </option>
                    ))}
                    <option value="custom">Personalizado…</option>
                  </select>
                  {minViewsOn && minViewsMode === "custom" ? (
                    <input
                      id="imp-min-views-custom"
                      className={`input${minViewsChoice.ok ? "" : " invalid"}`}
                      style={{ width: 120 }}
                      inputMode="numeric"
                      aria-label="Mínimo de visualizações por vídeo"
                      aria-invalid={!minViewsChoice.ok}
                      value={minViewsText}
                      onChange={(e) => setMinViewsText(e.target.value)}
                      autoComplete="off"
                    />
                  ) : null}
                </div>
              </Field>
              ) : null}
              <Field
                label="Filtro: período de postagem"
                htmlFor="imp-period"
                error={periodChoice.ok ? null : periodChoice.error}
                hint={PERIOD_HINT}
              >
                <div className="row" style={{ flexWrap: "wrap" }}>
                  <select
                    id="imp-period"
                    className="select"
                    value={periodMode}
                    onChange={(e) => setPeriodMode(e.target.value as PeriodMode)}
                  >
                    <option value="any">Qualquer data</option>
                    {PERIOD_PRESETS.map((n) => (
                      <option key={n} value={String(n)}>
                        {periodSummary(n)}
                      </option>
                    ))}
                    <option value="custom">Personalizado…</option>
                  </select>
                  {periodMode === "custom" ? (
                    <input
                      id="imp-period-custom"
                      className={`input${periodChoice.ok ? "" : " invalid"}`}
                      style={{ width: 90 }}
                      inputMode="numeric"
                      aria-label="Últimos N dias"
                      aria-invalid={!periodChoice.ok}
                      placeholder="dias"
                      value={periodText}
                      onChange={(e) => setPeriodText(e.target.value)}
                      autoComplete="off"
                    />
                  ) : null}
                </div>
              </Field>
            </div>

            {processingBlock}

            <div id="imp-profiles-status" style={{ marginTop: 16 }}>
              {!hasText ? null : preview ? (
                <div className="small" role="status">
                  {entriesSummary({
                    valid: preview.valid.length,
                    duplicates: preview.duplicates,
                    invalid: preview.invalid.length,
                  })}
                </div>
              ) : previewError ? (
                // Sem conferência não há contagem válida para mostrar.
                <ErrorLine message={previewError} onRetry={previewQuery.reload} />
              ) : !checking ? (
                <div className="muted small" role="status">
                  Nenhum perfil na lista.
                </div>
              ) : (
                <div className="muted small" role="status">
                  Conferindo a lista. Estimativa provisória: cerca de {profilesCountText(draft.estimate)}; o número final
                  aparece quando a conferência terminar.
                </div>
              )}
              {preview && preview.invalid.length > 0 ? (
                <div className="alert warn" role="alert" style={{ marginTop: 8, flexDirection: "column" }}>
                  <strong id="imp-invalid-title">
                    {invalidCountText(preview.invalid.length)}:{" "}
                    {preview.valid.length > 0
                      ? "ficam de fora; a importação começa só com os perfis válidos."
                      : "nenhum perfil válido na lista."}
                  </strong>
                  <ul className="invalid-list" aria-labelledby="imp-invalid-title">
                    {preview.invalid.slice(0, INVALID_SHOWN_MAX).map((item, i) => (
                      <li key={`${i}:${item.entry}`}>
                        <span className="mono">{item.entry}</span>: {item.reason}
                      </li>
                    ))}
                    {preview.invalid.length > INVALID_SHOWN_MAX ? (
                      <li>e mais {formatCount(preview.invalid.length - INVALID_SHOWN_MAX)}</li>
                    ) : null}
                  </ul>
                </div>
              ) : null}
              {summary?.kind === "problem" ? (
                <div className="field-error" role="alert" style={{ marginTop: 8 }}>
                  {summary.text}
                </div>
              ) : null}
              {summary?.kind === "summary" ? (
                <>
                  <div style={{ marginTop: 8 }}>
                    <strong>{summary.text}</strong>
                  </div>
                  {summary.hints.map((hint) => (
                    <div key={hint} className="muted small" style={{ marginTop: 4 }}>
                      {hint}
                    </div>
                  ))}
                </>
              ) : null}
              {providerProblem ? (
                <div id="imp-provider-note" className="alert warn" role="status" style={{ marginTop: 8 }}>
                  <span className="grow">{providerProblem}</span>
                  <button type="button" className="btn btn-sm" onClick={() => navigate(ROUTES.settings)}>
                    Abrir Configurações
                  </button>
                </div>
              ) : null}
            </div>

            <div className="row" style={{ marginTop: 16, flexWrap: "wrap" }}>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={!canSubmit}
                title={providerProblem ?? undefined}
                aria-describedby={providerProblem ? "imp-provider-note" : undefined}
              >
                {submitting ? <Spinner /> : null}
                {startLabel({ process: processAfter, invalid: invalidCount, edit: post === "edit" || post === "capcut" })}
              </button>
              <span className="grow" />
              <button
                type="button"
                className="btn btn-ghost"
                title={singleProfile === null ? "Disponível com um único perfil na lista" : undefined}
                disabled={singleProfile === null || !sessionOk || lookupBusy || paused}
                onClick={() => void consult()}
              >
                {lookupBusy ? <Spinner /> : <Search size={14} strokeWidth={1.75} aria-hidden="true" />}
                Consultar perfil
              </button>
            </div>
          </form>
        ) : (
          <ImportLinksForm
            text={linksText}
            onTextChange={setLinksText}
            sessionOk={sessionOk}
            paused={paused}
            process={processAfter}
            presetId={presetId}
            edit={postOptions.edit}
            capcut={postOptions.capcut}
            editProblem={editProblem}
            processing={processingBlock}
            onCreated={(batch) => {
              // Lista enviada: o campo é limpo e o lote criado abre no acompanhamento, como no modo por perfil.
              setSelected({ kind: "batch", id: batch.id });
              setCapcutBatchId(batch.capcut_batch_id ?? null);
              setLinksText("");
              revealCreated();
            }}
            onFailed={settings.reload}
          />
        )}
      </div>

      {capcutBatchId !== null ? (
        <div className="alert ok" role="status" style={{ marginTop: 12 }}>
          <span className="grow">Os vídeos vão para a Edição CapCut (lote {capcutBatchId}).</span>
          <a
            className="btn btn-sm"
            href={capcutForBatch(capcutBatchId)}
            onClick={(e) => {
              e.preventDefault();
              navigate(capcutForBatch(capcutBatchId));
            }}
          >
            Abrir na Edição CapCut
          </a>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => setCapcutBatchId(null)}>
            Fechar
          </button>
        </div>
      ) : null}
      {mode !== "links" && startNotice && startNotice.groupId === null ? (
        <div className="alert warn" role="status" style={{ marginTop: 12 }}>
          <span className="grow">{startNotice.text}</span>
        </div>
      ) : null}
      {mode !== "links" && lookupError ? (
        <div style={{ marginTop: 12 }}>
          <ErrorLine message={lookupError} />
        </div>
      ) : null}
      {mode !== "links" && lookupShown ? <ProfilePreview info={lookupShown} /> : null}

      <div ref={panelRef} tabIndex={-1} role="region" aria-label="Acompanhamento da importação">
        {shown?.kind === "group" ? (
          <ImportGroupPanel
            key={shown.id}
            groupId={shown.id}
            notice={startNotice && startNotice.groupId === shown.id ? startNotice.text : null}
            openBatchId={shownBatchId}
            onOpenBatch={(batchId) => setChild({ groupId: shown.id, batchId })}
            onChanged={recent.reload}
            onClose={selected !== null || !shownLive ? closeShown : undefined}
          />
        ) : null}
        {shown !== null && shownBatchId !== null ? (
          <BatchPanel
            key={shownBatchId}
            batchId={shownBatchId}
            onChanged={recent.reload}
            onFinished={(id) => {
              if (shown.kind === "batch") setSelected((prev) => prev ?? { kind: "batch", id });
            }}
            onClose={
              shown.kind === "group"
                ? () => setChild(null)
                : selected !== null || !shownLive
                  ? closeShown
                  : undefined
            }
          />
        ) : null}
      </div>

      <section className="section">
        <div className="section-head">
          <h2>{showAllBatches ? "Todas as importações" : "Importações recentes"}</h2>
          {recent.loading && recent.data ? <Spinner /> : null}
          <span className="grow" />
          {recentItems.length > 0 ? (
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => openHistory({ kind: "all" })}>
              Limpar histórico
            </button>
          ) : null}
          {showAllBatches || recentItems.length > 0 ? (
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => setShowAllBatches((v) => !v)}>
              {showAllBatches ? "ver recentes" : "ver todas"}
            </button>
          ) : null}
        </div>
        {showAllBatches ? (
          <>
            <AllGroups key={`g${historyVersion}`} selected={shown} onSelect={setSelected} />
            <AllBatches key={`b${historyVersion}`} selected={shown} onSelect={setSelected} />
          </>
        ) : recent.data === undefined ? (
          recent.error ? (
            <ErrorLine message={recent.error} onRetry={recent.reload} />
          ) : (
            <SkeletonRows rows={3} />
          )
        ) : recentItems.length === 0 ? (
          <Empty title="Nenhuma importação ainda" icon={Download}>
            <span>Cole links ou perfis acima e clique em Iniciar importação.</span>
          </Empty>
        ) : (
          <RecentTable items={recentItems} selected={shown} onSelect={setSelected} onRemove={openHistory} />
        )}
      </section>
      {historyTarget ? (
        <ConfirmDialog
          title={historyConfirmTitle(historyTarget)}
          confirmLabel={historyDelete ? `${historyConfirmTitle(historyTarget)} e apagar os vídeos` : historyConfirmTitle(historyTarget)}
          danger={historyDelete}
          focusCancel
          busy={historyBusy}
          onConfirm={() => void confirmHistory()}
          onCancel={() => setHistoryTarget(null)}
        >
          <p style={{ margin: "0 0 10px" }}>{historyConfirmText(historyTarget, historyPreview, historyDelete)}</p>
          <label className="row" style={{ gap: 8, alignItems: "flex-start" }}>
            <input
              type="checkbox"
              checked={historyDelete}
              disabled={historyBusy}
              onChange={(e) => setHistoryDelete(e.target.checked)}
            />
            <span>{deleteMediaLabel(historyTarget, historyPreview)}</span>
          </label>
          {historyDelete ? (
            <p className="tone-err" style={{ margin: "8px 0 0" }}>
              {DELETE_WARNING} Vídeos com job ou exportação em andamento são pulados.
              {sharedWarning(historyPreview) ? ` ${sharedWarning(historyPreview)}` : ""}
            </p>
          ) : null}
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

function ProfilePreview({ info }: { info: ProfileInfo }) {
  const [avatarFailed, setAvatarFailed] = useState(false);
  return (
    <div className="profile-preview">
      {info.avatar_url && !avatarFailed ? (
        <img
          className="avatar"
          src={info.avatar_url}
          alt=""
          referrerPolicy="no-referrer"
          onError={() => setAvatarFailed(true)}
        />
      ) : (
        <span className="avatar thumb" style={{ borderRadius: "50%" }}>
          <UserRound size={18} strokeWidth={1.5} aria-hidden="true" />
        </span>
      )}
      <div>
        <div className="row" style={{ gap: 6 }}>
          <strong>@{info.username}</strong>
          {info.is_verified ? <BadgeCheck size={14} strokeWidth={1.75} aria-label="Verificado" /> : null}
          {info.is_private ? (
            <span className="tag">
              <Lock size={10} strokeWidth={2} aria-hidden="true" /> Privado
            </span>
          ) : null}
        </div>
        <div className="muted small">
          {[info.full_name, info.post_count != null ? `${formatCount(info.post_count)} posts` : null]
            .filter(Boolean)
            .join(" · ") || "Sem detalhes públicos"}
        </div>
      </div>
    </div>
  );
}

function BatchPanel({
  batchId,
  onChanged,
  onFinished,
  onClose,
}: {
  batchId: number;
  onChanged: () => void;
  onFinished: (id: number) => void;
  onClose?: () => void;
}) {
  const toast = useToast();
  const [pollMs, setPollMs] = useState<number | null>(1_500);
  const [busy, setBusy] = useState<"cancel" | "retry" | null>(null);
  // Leituras seguidas em estado terminal e sem jobs ativos; após a segunda, o polling para.
  const settled = useRef(0);
  const query = useQuery(
    `batch:${batchId}`,
    async () => {
      const b = await api.getBatch(batchId);
      const moving = ACTIVE_BATCH.has(b.status) || b.progress.active > 0;
      settled.current = moving ? 0 : settled.current + 1;
      if (settled.current === 1) {
        onChanged();
        onFinished(b.id);
      }
      setPollMs(moving || settled.current < 2 ? 1_500 : null);
      return b;
    },
    pollMs,
  );
  const batch = query.data;

  async function act(kind: "cancel" | "retry") {
    setBusy(kind);
    try {
      if (kind === "cancel") {
        await api.cancelBatch(batchId);
        toast.push("ok", "Importação cancelada.");
      } else {
        await api.retryBatchFailed(batchId);
        toast.push("ok", "Itens com erro voltaram para a fila.");
      }
      settled.current = 0;
      setPollMs(1_500);
      query.reload();
      onChanged();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(null);
    }
  }

  if (!batch) {
    return (
      <div className="panel" style={{ marginTop: 20 }}>
        {query.error ? <ErrorLine message={query.error} onRetry={query.reload} /> : <SkeletonRows rows={2} />}
      </div>
    );
  }

  const exportOffer = batchExportOffer(batch);
  return (
    <div className="panel" style={{ marginTop: 20 }} aria-live="polite">
      <BatchSummary batch={batch} />
      <BatchNotice
        batch={batch}
        onResumed={() => {
          settled.current = 0;
          setPollMs(1_500);
          query.reload();
          onChanged();
        }}
      />
      {isLinksBatch(batch) && batch.links ? <BatchLinksPanel batchId={batch.id} links={batch.links} /> : null}
      <div className="row" style={{ marginTop: 12 }}>
        {ACTIVE_BATCH.has(batch.status) ? (
          <button type="button" className="btn btn-sm" disabled={busy !== null} onClick={() => void act("cancel")}>
            {busy === "cancel" ? <Spinner /> : null}
            Cancelar
          </button>
        ) : null}
        {batch.progress.failed > 0 && batch.status !== "listing" ? (
          <button type="button" className="btn btn-sm" disabled={busy !== null} onClick={() => void act("retry")}>
            {busy === "retry" ? <Spinner /> : null}
            Tentar falhas novamente
          </button>
        ) : null}
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => navigate(queueForBatch(batch.id))}>
          Ver na Fila
        </button>
        <PostTimesReportButton
          target={{ kind: "batch", id: batch.id }}
          postTimes={batch.post_times}
          postTimesCount={batch.post_times_count}
        />
        <span className="grow" />
        {onClose ? (
          <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>
            Fechar
          </button>
        ) : null}
      </div>
      {isPatternsOnly(batch) ? null : (
        <ImportExportBar
          source={{ kind: "batch", id: batch.id }}
          finished={exportOffer.offer}
          note={exportOffer.note}
          lastExport={batch.last_export}
          libraryPath={libraryPathForProfile(batch.source_profile)}
          contentChoice={batch.process}
          onChanged={() => {
            query.reload();
            onChanged();
          }}
        />
      )}
    </div>
  );
}

function BatchSummary({ batch }: { batch: Batch }) {
  const p = batch.progress;
  const listingDone = batch.status !== "listing";
  const done = Math.max(0, p.found - p.pending - p.active);
  const errors = Math.min(p.failed, done);
  const cancelled = Math.min(batch.counts.cancelled ?? 0, done - errors);
  const succeeded = done - errors - cancelled;
  // Lote só de padrões: nada é baixado; o que conta são as publicações com horário gravado.
  const patternsOnly = isPatternsOnly(batch);
  const showBar = p.found > 0 && listingDone && !patternsOnly;
  const downloadOnly = isDownloadOnly(batch);
  const byLinks = isLinksBatch(batch);
  const profileEnd = batch.error_code ? null : profileEndText(batch);
  const counters: [string, number][] = patternsOnly
    ? [["Publicações analisadas", batch.post_times_count ?? 0]]
    : [
        ["Encontrados", p.found],
        ["Baixados", p.downloaded],
        // Lote só de download não tem etapa de processamento para contar.
        ...(downloadOnly ? [] : ([["Processados", p.processed]] as [string, number][])),
        // Já existentes na Biblioteca (não baixados de novo, mas parte do lote) e filtrados por tipo/período/
        // visualizações (ausentes em backends antigos, que só mandam "Ignorados").
        ...(typeof p.duplicates === "number"
          ? ([["Já baixados antes", p.duplicates]] as [string, number][]).concat(
              p.skipped > p.duplicates ? [["Ignorados", p.skipped - p.duplicates]] : [],
            )
          : ([["Ignorados", p.skipped]] as [string, number][])),
        ...(typeof p.filtered === "number" ? ([["Filtrados", p.filtered]] as [string, number][]) : []),
        ["Erros", p.failed],
        ["Pendentes", p.pending],
      ];
  return (
    <>
      <div className="row between">
        <div className="row">
          <strong style={{ fontSize: 15 }}>{batchTitle(batch)}</strong>
          <Pill tone={batchToneOf(batch)}>{batchLabel(batch)}</Pill>
          {ACTIVE_BATCH.has(batch.status) && !isQueuedInGroup(batch) ? <Spinner /> : null}
        </div>
        <span className="muted small">
          {[
            // Lote por link: tipo de mídia e quantidade não se aplicam.
            ...(byLinks
              ? ["Por link"]
              : [
                  batch.content_type ? CONTENT_LABEL[batch.content_type] : null,
                  batch.requested_limit ? `Últimos ${formatCount(batch.requested_limit)}` : "Todos",
                ]),
            patternsOnly ? "Só padrões" : batch.process && batch.preset_name ? batch.preset_name : "Somente originais",
          ]
            .filter(Boolean)
            .join(" · ")}
        </span>
      </div>
      <div className="counters">
        {counters.map(([label, n]) => (
          <div key={label} className="counter">
            <div className={`n${label === "Erros" && n > 0 ? " tone-err" : ""}`}>{formatCount(n)}</div>
            <div className="l">{label}</div>
          </div>
        ))}
      </div>
      {showBar ? (
        <div>
          <SplitBar ok={succeeded / p.found} err={errors / p.found} />
          <div className="muted small" style={{ marginTop: 5 }}>
            {formatCount(succeeded)} de {formatCount(p.found)} {downloadOnly ? "originais salvos" : "concluídos"}
            {errors > 0 ? ` · ${formatCount(errors)} com erro` : ""}
            {cancelled > 0 ? ` · ${formatCount(cancelled)} cancelados` : ""}
            {p.active > 0 ? ` · ${formatCount(p.active)} em execução` : ""}
            {listingDroppedSuffix(batch)}
          </div>
        </div>
      ) : isQueuedInGroup(batch) ? (
        <div className="muted small">
          Aguardando na fila: a listagem deste perfil começa quando chegar a vez dele na importação.
        </div>
      ) : batch.status === "listing" ? (
        <div className="muted small">
          {byLinks
            ? "Resolvendo os links, um de cada vez."
            : patternsOnly
              ? "Lendo o horário das publicações do perfil."
              : "Coletando a lista de mídias do perfil."}
        </div>
      ) : null}
      {!patternsOnly && alreadyDownloadedNote(p) ? (
        <div className="muted small" style={{ marginTop: 5 }}>
          {alreadyDownloadedNote(p)}
        </div>
      ) : null}
      {viewsFilterText(visibleFilter(batch.views_filter), p.downloaded) ? (
        <div className="muted small" style={{ marginTop: 5 }}>
          {viewsFilterText(visibleFilter(batch.views_filter), p.downloaded)}
        </div>
      ) : null}
      {profileEnd ? (
        // Fim real do perfil antes da quantidade pedida: informação, não erro.
        <div className="muted small" style={{ marginTop: 8 }}>
          O {profileEnd}.
        </div>
      ) : (batch.error_code || batch.error_message) && !needsAttention(batch) ? (
        <div style={{ marginTop: 10 }}>
          <ErrorLine message={describeErrorCode(batch.error_code, batch.error_message)} />
        </div>
      ) : null}
    </>
  );
}

function sameSelection(entry: Recent, selected: Selection | null): boolean {
  if (selected === null || selected.kind !== entry.kind) return false;
  return selected.id === (entry.kind === "group" ? entry.group.id : entry.batch.id);
}

function RecentTable({
  items,
  selected,
  onSelect,
  onRemove,
}: {
  items: Recent[];
  selected: Selection | null;
  onSelect: (selection: Selection) => void;
  /** Só na lista de recentes: abre a confirmação de remover o item do histórico. */
  onRemove?: (target: HistoryTarget) => void;
}) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Origem</th>
            <th>Conteúdo</th>
            <th>Estado</th>
            <th className="num">Encontrados</th>
            <th className="num">Baixados</th>
            <th className="num">Processados</th>
            <th className="num">Já baixados</th>
            <th className="num">Erros</th>
            <th>Criado em</th>
            {onRemove ? <th aria-label="Ações" /> : null}
          </tr>
        </thead>
        <tbody>
          {items.map((entry) => {
            const isGroup = entry.kind === "group";
            const item = isGroup ? entry.group : entry.batch;
            const selection: Selection = { kind: entry.kind, id: item.id };
            const tone = isGroup ? groupStatusLabel(entry.group) : null;
            const downloadOnly = isGroup ? !entry.group.process : isDownloadOnly(entry.batch);
            // Só padrões: nada baixado; a linha mostra as publicações analisadas no lugar das mídias.
            const patternsOnly = isPatternsOnly(item);
            const analyzed = patternsOnly ? analyzedPostsText(item.post_times_count ?? 0) : null;
            const note = isGroup
              ? [...groupProfileCounts(entry.group.profiles), analyzed, groupDroppedText(entry.group)]
                  .filter(Boolean)
                  .join(" • ")
              : [analyzed, listingDropped(entry.batch)?.summary].filter(Boolean).join(" • ");
            const p = item.progress;
            return (
              <tr
                key={`${entry.kind}:${item.id}`}
                className={`clickable${sameSelection(entry, selected) ? " selected" : ""}`}
                onClick={() => onSelect(selection)}
              >
                <td className="truncate" style={{ maxWidth: 240 }}>
                  <button
                    type="button"
                    className="link-btn"
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelect(selection);
                    }}
                  >
                    {isGroup ? groupTitle(entry.group) : batchOriginLabel(entry.batch)}
                  </button>
                </td>
                <td className="muted">
                  {!isGroup && isLinksBatch(entry.batch)
                    ? "-"
                    : patternsOnly
                      ? "Só padrões"
                      : item.content_type
                      ? CONTENT_LABEL[item.content_type]
                      : "-"}
                </td>
                <td>
                  {isGroup && tone ? (
                    <Pill tone={tone.tone}>{tone.label}</Pill>
                  ) : !isGroup ? (
                    <Pill tone={batchToneOf(entry.batch)}>{batchLabel(entry.batch)}</Pill>
                  ) : null}
                  {note ? <div className="muted small">{note}</div> : null}
                </td>
                <td className="num">{formatCount(patternsOnly ? (item.post_times_count ?? 0) : p.found)}</td>
                <td className="num muted">{patternsOnly ? "-" : formatCount(p.downloaded)}</td>
                <td className="num muted" title={downloadOnly ? "Somente originais" : undefined}>
                  {downloadOnly || patternsOnly ? "-" : formatCount(p.processed)}
                </td>
                <td className="num muted">{patternsOnly ? "-" : formatCount(p.duplicates ?? p.skipped)}</td>
                <td className={`num ${p.failed > 0 ? "tone-err" : "muted"}`}>{formatCount(p.failed)}</td>
                <td className="muted nowrap">{formatDateTime(item.created_at)}</td>
                {onRemove ? (
                  <td className="nowrap">
                    <button
                      type="button"
                      className="btn btn-sm btn-ghost"
                      disabled={isActiveEntry(entry)}
                      title={removeBlockedHint(isActiveEntry(entry)) ?? "Remover do histórico"}
                      onClick={(e) => {
                        e.stopPropagation();
                        onRemove({
                          kind: entry.kind,
                          id: item.id,
                          label: isGroup ? groupTitle(entry.group) : batchOriginLabel(entry.batch),
                        });
                      }}
                    >
                      Remover
                    </button>
                  </td>
                ) : null}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const ALL_BATCHES_PAGE = 20;

/** Importações de vários perfis, uma linha por grupo (keyset `before_id`). Some quando não há nenhuma. */
function AllGroups({ selected, onSelect }: { selected: Selection | null; onSelect: (s: Selection) => void }) {
  // Pilha de cursores `before_id`; vazia = primeira página.
  const [cursors, setCursors] = useState<number[]>([]);
  const before = cursors[cursors.length - 1];
  const [pollMs, setPollMs] = useState<number>(POLL_STEPS_MS[0]);
  const list = useQuery(
    `all-groups:${before ?? "first"}`,
    async () => {
      const page = await api.listImportGroups(ALL_BATCHES_PAGE, before, true);
      setPollMs((prev) => nextPollMs(prev, page.items.some((g) => g.status === "active")));
      return { ...page, before };
    },
    pollMs,
  );
  if (list.data === undefined) {
    return list.error ? <ErrorLine message={list.error} onRetry={list.reload} /> : null;
  }
  const { items, total, next_before_id: next } = list.data;
  if (total === 0) return null;
  const ready = list.data.before === before;
  return (
    <div style={{ marginBottom: 18 }}>
      <h3 className="muted small" style={{ marginBottom: 6 }}>
        Vários perfis · {formatCount(total)}
      </h3>
      {list.error ? <ErrorLine message={list.error} onRetry={list.reload} /> : null}
      <RecentTable
        items={items.map((group) => ({ kind: "group" as const, group }))}
        selected={selected}
        onSelect={onSelect}
      />
      {cursors.length > 0 || next !== null ? (
        <div className="row" style={{ marginTop: 10, justifyContent: "flex-end" }}>
          <button
            type="button"
            className="btn btn-sm"
            disabled={cursors.length === 0 || !ready}
            onClick={() => setCursors(cursors.slice(0, -1))}
          >
            Anterior
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={next === null || !ready}
            onClick={() => {
              if (next !== null) setCursors([...cursors, next]);
            }}
          >
            Próxima
          </button>
        </div>
      ) : null}
    </div>
  );
}

/** Lotes avulsos e antigos (um perfil ou por link); os filhos de grupo ficam dentro do grupo. */
function AllBatches({ selected, onSelect }: { selected: Selection | null; onSelect: (s: Selection) => void }) {
  const [offset, setOffset] = useState(0);
  const [pollMs, setPollMs] = useState<number>(POLL_STEPS_MS[0]);
  const list = useQuery(
    `all-batches:${offset}`,
    async () => {
      const page = await api.listBatches(ALL_BATCHES_PAGE, offset, "instagram_import", {
        ungrouped: true,
        history: true,
      });
      const items = page.items.filter((b) => b.source === "instagram_import");
      setPollMs((prev) => nextPollMs(prev, items.some((b) => ACTIVE_BATCH.has(b.status))));
      if (page.items.length === 0 && page.total > 0 && offset > 0) setOffset(0);
      return { ...page, items };
    },
    pollMs,
  );
  if (list.data === undefined) {
    return list.error ? <ErrorLine message={list.error} onRetry={list.reload} /> : <SkeletonRows rows={3} />;
  }
  const { items, total } = list.data;
  if (items.length === 0) return <Empty title="Nenhuma importação de um perfil ou por link" />;
  return (
    <>
      <h3 className="muted small" style={{ marginBottom: 6 }}>
        Um perfil ou por link · {formatCount(total)}
      </h3>
      {list.error ? <ErrorLine message={list.error} onRetry={list.reload} /> : null}
      <RecentTable
        items={items.map((batch) => ({ kind: "batch" as const, batch }))}
        selected={selected}
        onSelect={onSelect}
      />
      {total > ALL_BATCHES_PAGE ? (
        <div className="row" style={{ marginTop: 10, justifyContent: "flex-end" }}>
          <span className="muted small">
            {formatCount(offset + 1)} a {formatCount(Math.min(offset + ALL_BATCHES_PAGE, total))} de{" "}
            {formatCount(total)}
          </span>
          <button
            type="button"
            className="btn btn-sm"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - ALL_BATCHES_PAGE))}
          >
            Anterior
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={offset + ALL_BATCHES_PAGE >= total}
            onClick={() => setOffset(offset + ALL_BATCHES_PAGE)}
          >
            Próxima
          </button>
        </div>
      ) : null}
    </>
  );
}
