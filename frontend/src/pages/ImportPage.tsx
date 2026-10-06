import { useDeferredValue, useMemo, useRef, useState } from "react";
import { BadgeCheck, Lock, Search, UserRound } from "lucide-react";
import { ApiError, api, messageOf } from "../api/client";
import { describeErrorCode } from "../api/errors";
import type { Batch, ContentType, ImportGroup, Preset, ProfileInfo } from "../api/types";
import { Field, Empty, ErrorLine, Pill, SkeletonRows, SplitBar, Spinner, Switch } from "../components/ui";
import { formatCount, formatDateTime } from "../lib/format";
import {
  ACTIVE_BATCH,
  CONTENT_LABEL,
  batchLabel,
  batchTitle,
  batchToneOf,
  isDownloadOnly,
  isQueuedInGroup,
} from "../lib/labels";
import {
  CONTENT_CHOICES,
  DEFAULT_LIMIT_TEXT,
  LIMIT_SHORTCUTS,
  type LimitMode,
  PROCESS_AFTER_DOWNLOAD_DEFAULT,
  entriesSummary,
  invalidCountText,
  profilesCountText,
  resolveLimit,
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
import { describeSessionTest } from "../lib/sessionTest";
import { isPaused, useNow } from "../lib/pause";
import { PauseBanner } from "../components/PauseBanner";
import { BatchNotice } from "../components/BatchNotice";
import { ImportGroupPanel } from "../components/ImportGroupPanel";
import { ImportExportBar } from "../components/ExportPanel";
import { batchExportOffer, libraryPathForProfile } from "../lib/exportFolder";
import { listingDropped, listingDroppedSuffix, needsAttention } from "../lib/batchState";
import { navigate, ROUTES } from "../lib/router";
import { queueForBatch } from "../lib/batchRoute";
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

  const [profileText, setProfileText] = useState("");
  const [content, setContent] = useState<ContentType>("all");
  const [limitMode, setLimitMode] = useState<LimitMode>("last");
  const [limitText, setLimitText] = useState(DEFAULT_LIMIT_TEXT);
  const [presetChoice, setPresetChoice] = useState<number | null>(null);
  // Só estado da tela: toda visita começa em "somente baixar originais".
  const [processAfter, setProcessAfter] = useState(PROCESS_AFTER_DOWNLOAD_DEFAULT);
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
  const sessionOk = settings.data?.instagram.session_configured ?? false;
  const now = useNow();
  const igStatus = settings.data?.instagram ?? null;
  const paused = isPaused(igStatus?.paused_until, now);
  const lastTest = settings.data?.instagram.last_test ?? null;
  const lastView = lastTest ? describeSessionTest(lastTest) : null;
  const presetId = presetChoice ?? defaultPresetId(presets.data, settings.data?.default_preset_id);
  const providerProblem = providerBlocker(settings.data?.instagram_provider, content, limitMode === "all");
  const blocker = startBlocker({
    sessionOk,
    paused,
    submitting,
    preview,
    limit: limitChoice,
    process: processAfter,
    presetId,
    providerProblem,
  });
  const canSubmit = blocker === null;
  const summary = startSummary({ preview, limit: limitChoice, content });

  const [recentPoll, setRecentPoll] = useState<number | null>(5_000);
  const recent = useQuery(
    "recent-imports",
    async () => {
      // Grupos (uma linha cada) + lotes avulsos e antigos; os filhos de grupo não entram na lista.
      const [groups, batches] = await Promise.all([
        api.listImportGroups(RECENT_ROWS),
        api.listBatches(RECENT_ROWS, 0, "instagram_import", { ungrouped: true }),
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

  async function submit() {
    const plan = planFromPreview(preview, {
      content,
      limit: limitChoice,
      processAfterDownload: processAfter,
      presetId,
    });
    if (!canSubmit || !plan) return;
    setSubmitting(true);
    setStartNotice(null);
    try {
      if (plan.kind === "single") {
        const batch = await api.createImport(plan.body);
        setSelected({ kind: "batch", id: batch.id });
        toast.push(
          "ok",
          processAfter
            ? `Download e processamento de @${plan.body.profile} iniciados.`
            : `Download dos originais de @${plan.body.profile} iniciado.`,
        );
      } else {
        const created = interpretGroupCreated(await api.createImportGroup(plan.body));
        setSelected({ kind: "group", id: created.groupId });
        if (created.notice) setStartNotice({ groupId: created.groupId, text: created.notice });
        toast.push("ok", created.message);
      }
      // Lista enviada: o campo é limpo (tipo, quantidade e processamento ficam) e o painel criado recebe o foco.
      setProfileText("");
      recent.reload();
      window.requestAnimationFrame(() => {
        panelRef.current?.focus({ preventScroll: true });
        panelRef.current?.scrollIntoView({ block: "nearest" });
      });
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

  async function consult() {
    if (singleProfile === null) return;
    const username = singleProfile;
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

  const lookupShown = lookup && lookup.username === singleProfile ? lookup.info : null;

  return (
    <div className="page">
      <div className="page-head">
        <h1>Importar do Instagram</h1>
      </div>

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
              onChange={(e) => setContent(e.target.value as ContentType)}
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
        </div>

        <div style={{ marginTop: 16 }}>
          <div className="field-label" style={{ marginBottom: 6 }}>
            Processamento
          </div>
          <Switch checked={processAfter} onChange={setProcessAfter} label="Processar após baixar (opcional)" />
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
          <div className="muted small" style={{ marginTop: 8 }}>
            {processAfter
              ? "Os originais são baixados e, em seguida, processados com o preset escolhido. Os originais ficam guardados."
              : "Somente os arquivos originais serão baixados, sem nenhum processamento. Se quiser, processe depois pela Biblioteca."}
          </div>
        </div>

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
            {startLabel({ process: processAfter, invalid: invalidCount })}
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

      {startNotice && startNotice.groupId === null ? (
        <div className="alert warn" role="status" style={{ marginTop: 12 }}>
          <span className="grow">{startNotice.text}</span>
        </div>
      ) : null}
      {lookupError ? (
        <div style={{ marginTop: 12 }}>
          <ErrorLine message={lookupError} />
        </div>
      ) : null}
      {lookupShown ? <ProfilePreview info={lookupShown} /> : null}

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

      <section className="section" style={{ marginTop: 22 }}>
        <div className="section-head">
          <h2>{showAllBatches ? "Todas as importações" : "Importações recentes"}</h2>
          {recent.loading && recent.data ? <Spinner /> : null}
          <span className="grow" />
          {showAllBatches || recentItems.length > 0 ? (
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => setShowAllBatches((v) => !v)}>
              {showAllBatches ? "ver recentes" : "ver todas"}
            </button>
          ) : null}
        </div>
        {showAllBatches ? (
          <>
            <AllGroups selected={shown} onSelect={setSelected} />
            <AllBatches selected={shown} onSelect={setSelected} />
          </>
        ) : recent.data === undefined ? (
          recent.error ? (
            <ErrorLine message={recent.error} onRetry={recent.reload} />
          ) : (
            <SkeletonRows rows={3} />
          )
        ) : recentItems.length === 0 ? (
          <Empty title="Nenhuma importação ainda" />
        ) : (
          <RecentTable items={recentItems} selected={shown} onSelect={setSelected} />
        )}
      </section>
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
        <span className="grow" />
        {onClose ? (
          <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>
            Fechar
          </button>
        ) : null}
      </div>
      <ImportExportBar
        source={{ kind: "batch", id: batch.id }}
        finished={exportOffer.offer}
        note={exportOffer.note}
        lastExport={batch.last_export}
        libraryPath={libraryPathForProfile(batch.source_profile)}
        onChanged={() => {
          query.reload();
          onChanged();
        }}
      />
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
  const showBar = p.found > 0 && listingDone;
  const downloadOnly = isDownloadOnly(batch);
  const profileEnd = batch.error_code ? null : profileEndText(batch);
  const counters: [string, number][] = [
    ["Encontrados", p.found],
    ["Baixados", p.downloaded],
    // Lote só de download não tem etapa de processamento para contar.
    ...(downloadOnly ? [] : ([["Processados", p.processed]] as [string, number][])),
    ["Ignorados", p.skipped],
    ["Erros", p.failed],
    ["Pendentes", p.pending],
  ];
  return (
    <>
      <div className="row between">
        <div className="row">
          <strong style={{ fontSize: 14 }}>{batchTitle(batch)}</strong>
          <Pill tone={batchToneOf(batch)}>{batchLabel(batch)}</Pill>
          {ACTIVE_BATCH.has(batch.status) && !isQueuedInGroup(batch) ? <Spinner /> : null}
        </div>
        <span className="muted small">
          {[
            batch.content_type ? CONTENT_LABEL[batch.content_type] : null,
            batch.requested_limit ? `Últimos ${formatCount(batch.requested_limit)}` : "Todos",
            batch.process && batch.preset_name ? batch.preset_name : "Somente originais",
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
        <div className="muted small">Coletando a lista de mídias do perfil.</div>
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
}: {
  items: Recent[];
  selected: Selection | null;
  onSelect: (selection: Selection) => void;
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
            <th className="num">Ignorados</th>
            <th className="num">Erros</th>
            <th>Criado em</th>
          </tr>
        </thead>
        <tbody>
          {items.map((entry) => {
            const isGroup = entry.kind === "group";
            const item = isGroup ? entry.group : entry.batch;
            const selection: Selection = { kind: entry.kind, id: item.id };
            const tone = isGroup ? groupStatusLabel(entry.group) : null;
            const downloadOnly = isGroup ? !entry.group.process : isDownloadOnly(entry.batch);
            const note = isGroup
              ? [...groupProfileCounts(entry.group.profiles), groupDroppedText(entry.group)].filter(Boolean).join(" • ")
              : (listingDropped(entry.batch)?.summary ?? "");
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
                    {isGroup ? groupTitle(entry.group) : `@${entry.batch.source_profile ?? "?"}`}
                  </button>
                </td>
                <td className="muted">{item.content_type ? CONTENT_LABEL[item.content_type] : "-"}</td>
                <td>
                  {isGroup && tone ? (
                    <Pill tone={tone.tone}>{tone.label}</Pill>
                  ) : !isGroup ? (
                    <Pill tone={batchToneOf(entry.batch)}>{batchLabel(entry.batch)}</Pill>
                  ) : null}
                  {note ? <div className="muted small">{note}</div> : null}
                </td>
                <td className="num">{formatCount(p.found)}</td>
                <td className="num muted">{formatCount(p.downloaded)}</td>
                <td className="num muted" title={downloadOnly ? "Somente originais" : undefined}>
                  {downloadOnly ? "-" : formatCount(p.processed)}
                </td>
                <td className="num muted">{formatCount(p.skipped)}</td>
                <td className={`num ${p.failed > 0 ? "tone-err" : "muted"}`}>{formatCount(p.failed)}</td>
                <td className="muted nowrap">{formatDateTime(item.created_at)}</td>
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
      const page = await api.listImportGroups(ALL_BATCHES_PAGE, before);
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

/** Lotes avulsos e antigos (um perfil), como sempre; os filhos de grupo ficam dentro do grupo. */
function AllBatches({ selected, onSelect }: { selected: Selection | null; onSelect: (s: Selection) => void }) {
  const [offset, setOffset] = useState(0);
  const [pollMs, setPollMs] = useState<number>(POLL_STEPS_MS[0]);
  const list = useQuery(
    `all-batches:${offset}`,
    async () => {
      const page = await api.listBatches(ALL_BATCHES_PAGE, offset, "instagram_import", { ungrouped: true });
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
  if (items.length === 0) return <Empty title="Nenhuma importação de um perfil" />;
  return (
    <>
      <h3 className="muted small" style={{ marginBottom: 6 }}>
        Um perfil · {formatCount(total)}
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
