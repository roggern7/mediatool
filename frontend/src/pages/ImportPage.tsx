import { useRef, useState } from "react";
import { BadgeCheck, Lock, Search, UserRound } from "lucide-react";
import { ApiError, api, messageOf } from "../api/client";
import { describeErrorCode } from "../api/errors";
import type { Batch, ContentType, Preset, ProfileInfo } from "../api/types";
import { Field, Empty, ErrorLine, Pill, SkeletonRows, SplitBar, Spinner, Switch } from "../components/ui";
import { formatCount, formatDateTime } from "../lib/format";
import { ACTIVE_BATCH, CONTENT_LABEL, batchLabel, batchTitle, batchToneOf, isDownloadOnly } from "../lib/labels";
import {
  LIMIT_OPTIONS,
  PROCESS_AFTER_DOWNLOAD_DEFAULT,
  buildImportRequest,
  canSubmitImport,
} from "../lib/importOptions";
import { parseProfileInput } from "../lib/profile";
import { describeSessionTest } from "../lib/sessionTest";
import { isPaused, useNow } from "../lib/pause";
import { PauseBanner } from "../components/PauseBanner";
import { BatchNotice } from "../components/BatchNotice";
import { listingDropped, listingDroppedSuffix, needsAttention } from "../lib/batchState";
import { navigate, ROUTES } from "../lib/router";
import { queueForBatch } from "../lib/batchRoute";
import { POLL_STEPS_MS, nextPollMs } from "../lib/polling";
import { useQuery } from "../lib/useQuery";
import { useToast } from "../lib/toast";

const CONTENT_OPTIONS: ContentType[] = ["reels", "posts", "videos", "images", "all"];

function defaultPresetId(presets: Preset[] | undefined, configured: number | null | undefined): number | null {
  if (!presets || presets.length === 0) return null;
  if (configured != null && presets.some((p) => p.id === configured)) return configured;
  const anti = presets.find((p) => p.name === "Anti-duplicidade");
  return (anti ?? presets[0])?.id ?? null;
}

export function ImportPage() {
  const toast = useToast();
  const settings = useQuery("settings", () => api.getSettings());
  const presets = useQuery("presets", () => api.listPresets());

  const [profileText, setProfileText] = useState("");
  const [content, setContent] = useState<ContentType>("all");
  const [limitIdx, setLimitIdx] = useState(0);
  const [presetChoice, setPresetChoice] = useState<number | null>(null);
  // Só estado da tela: toda visita começa em "somente baixar originais".
  const [processAfter, setProcessAfter] = useState(PROCESS_AFTER_DOWNLOAD_DEFAULT);
  const [submitting, setSubmitting] = useState(false);
  const [lookup, setLookup] = useState<{ username: string; info: ProfileInfo } | null>(null);
  const [lookupBusy, setLookupBusy] = useState(false);
  const [lookupError, setLookupError] = useState<string | null>(null);
  const [selectedBatchId, setSelectedBatchId] = useState<number | null>(null);
  const [showAllBatches, setShowAllBatches] = useState(false);

  const parsed = parseProfileInput(profileText);
  const sessionOk = settings.data?.instagram.session_configured ?? false;
  const now = useNow();
  const igStatus = settings.data?.instagram ?? null;
  const paused = isPaused(igStatus?.paused_until, now);
  const lastTest = settings.data?.instagram.last_test ?? null;
  const lastView = lastTest ? describeSessionTest(lastTest) : null;
  const presetId = presetChoice ?? defaultPresetId(presets.data, settings.data?.default_preset_id);
  const canSubmit =
    sessionOk && parsed.ok && !submitting && !paused && canSubmitImport(processAfter, presetId);

  const [recentPoll, setRecentPoll] = useState<number | null>(5_000);
  const recent = useQuery(
    "batches",
    async () => {
      const page = await api.listBatches(8, 0, "instagram_import");
      // Filtro também no cliente, caso a API ainda não filtre por source.
      const items = page.items.filter((b) => b.source === "instagram_import");
      setRecentPoll(items.some((b) => ACTIVE_BATCH.has(b.status)) ? 5_000 : null);
      return { ...page, items };
    },
    recentPoll,
  );
  const recentItems = recent.data?.items ?? [];
  const autoBatch = recentItems.find((b) => b.source === "instagram_import" && ACTIVE_BATCH.has(b.status));
  const shownBatchId = selectedBatchId ?? autoBatch?.id ?? null;

  async function submit() {
    if (!parsed.ok || !canSubmitImport(processAfter, presetId)) return;
    setSubmitting(true);
    try {
      const batch = await api.createImport(
        buildImportRequest({
          profile: parsed.username,
          content,
          limitIndex: limitIdx,
          processAfterDownload: processAfter,
          presetId,
        }),
      );
      setSelectedBatchId(batch.id);
      recent.reload();
      toast.push(
        "ok",
        processAfter
          ? `Download e processamento de @${parsed.username} iniciados.`
          : `Download dos originais de @${parsed.username} iniciado.`,
      );
    } catch (err) {
      toast.push("err", messageOf(err));
      // Perfil já em importação: atualiza a lista para mostrar a importação em andamento.
      if (err instanceof ApiError && err.code === "profile_already_importing") recent.reload();
      settings.reload(); // pode ter entrado em pausa; atualiza a faixa
    } finally {
      setSubmitting(false);
    }
  }

  async function consult() {
    if (!parsed.ok) return;
    const username = parsed.username;
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

  const lookupShown = lookup && parsed.ok && lookup.username === parsed.username ? lookup.info : null;

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
          <Field
            label="Perfil"
            htmlFor="imp-profile"
            error={profileText.trim() && !parsed.ok ? parsed.error : null}
            hint={parsed.ok ? `@${parsed.username}` : "@usuario ou URL do perfil"}
          >
            <input
              id="imp-profile"
              className={`input${profileText.trim() && !parsed.ok ? " invalid" : ""}`}
              value={profileText}
              onChange={(e) => setProfileText(e.target.value)}
              placeholder="@perfil"
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <Field label="Conteúdo" htmlFor="imp-content">
            <select
              id="imp-content"
              className="select"
              value={content}
              onChange={(e) => setContent(e.target.value as ContentType)}
            >
              {CONTENT_OPTIONS.map((c) => (
                <option key={c} value={c}>
                  {CONTENT_LABEL[c]}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Quantidade" htmlFor="imp-limit">
            <select
              id="imp-limit"
              className="select"
              value={limitIdx}
              onChange={(e) => setLimitIdx(Number(e.target.value))}
            >
              {LIMIT_OPTIONS.map((o, i) => (
                <option key={o.label} value={i} disabled={o.disabled}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <div style={{ marginTop: 16 }}>
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

        <div className="row" style={{ marginTop: 16 }}>
          <button type="submit" className="btn btn-primary" disabled={!canSubmit}>
            {submitting ? <Spinner /> : null}
            {processAfter ? "Baixar e processar" : "Baixar originais"}
          </button>
          <span className="grow" />
          <button
            type="button"
            className="btn btn-ghost"
            disabled={!parsed.ok || !sessionOk || lookupBusy || paused}
            onClick={() => void consult()}
          >
            {lookupBusy ? <Spinner /> : <Search size={14} strokeWidth={1.75} aria-hidden="true" />}
            Consultar perfil
          </button>
        </div>
      </form>

      {lookupError ? (
        <div style={{ marginTop: 12 }}>
          <ErrorLine message={lookupError} />
        </div>
      ) : null}
      {lookupShown ? <ProfilePreview info={lookupShown} /> : null}

      {shownBatchId !== null ? (
        <BatchPanel
          key={shownBatchId}
          batchId={shownBatchId}
          onChanged={recent.reload}
          onFinished={(id) => setSelectedBatchId((prev) => prev ?? id)}
          onClose={selectedBatchId !== null ? () => setSelectedBatchId(null) : undefined}
        />
      ) : null}

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
          <AllBatches selectedId={shownBatchId} onSelect={setSelectedBatchId} />
        ) : recent.data === undefined ? (
          recent.error ? (
            <ErrorLine message={recent.error} onRetry={recent.reload} />
          ) : (
            <SkeletonRows rows={3} />
          )
        ) : recentItems.length === 0 ? (
          <Empty title="Nenhuma importação ainda" />
        ) : (
          <RecentTable items={recentItems} selectedId={shownBatchId} onSelect={setSelectedBatchId} />
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
        {!ACTIVE_BATCH.has(batch.status) ? (
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => navigate(ROUTES.library)}>
            Abrir Biblioteca
          </button>
        ) : null}
        <span className="grow" />
        {onClose ? (
          <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>
            Fechar
          </button>
        ) : null}
      </div>
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
          {ACTIVE_BATCH.has(batch.status) ? <Spinner /> : null}
        </div>
        <span className="muted small">
          {[
            batch.content_type ? CONTENT_LABEL[batch.content_type] : null,
            batch.requested_limit ? `Últimos ${batch.requested_limit}` : "Todos",
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
      ) : batch.status === "listing" ? (
        <div className="muted small">Coletando a lista de mídias do perfil.</div>
      ) : null}
      {(batch.error_code || batch.error_message) && !needsAttention(batch) ? (
        <div style={{ marginTop: 10 }}>
          <ErrorLine message={describeErrorCode(batch.error_code, batch.error_message)} />
        </div>
      ) : null}
    </>
  );
}

function RecentTable({
  items,
  selectedId,
  onSelect,
}: {
  items: Batch[];
  selectedId: number | null;
  onSelect: (id: number) => void;
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
          {items.map((b) => (
            <tr
              key={b.id}
              className={`clickable${b.id === selectedId ? " selected" : ""}`}
              onClick={() => onSelect(b.id)}
            >
              <td className="truncate" style={{ maxWidth: 200 }}>{`@${b.source_profile ?? "?"}`}</td>
              <td className="muted">{b.content_type ? CONTENT_LABEL[b.content_type] : "-"}</td>
              <td>
                <Pill tone={batchToneOf(b)}>{batchLabel(b)}</Pill>
                {listingDropped(b) ? <div className="muted small">{listingDropped(b)?.summary}</div> : null}
              </td>
              <td className="num">{formatCount(b.progress.found)}</td>
              <td className="num muted">{formatCount(b.progress.downloaded)}</td>
              <td className="num muted" title={isDownloadOnly(b) ? "Somente originais" : undefined}>
                {isDownloadOnly(b) ? "-" : formatCount(b.progress.processed)}
              </td>
              <td className="num muted">{formatCount(b.progress.skipped)}</td>
              <td className={`num ${b.progress.failed > 0 ? "tone-err" : "muted"}`}>{formatCount(b.progress.failed)}</td>
              <td className="muted nowrap">{formatDateTime(b.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const ALL_BATCHES_PAGE = 20;

function AllBatches({ selectedId, onSelect }: { selectedId: number | null; onSelect: (id: number) => void }) {
  const [offset, setOffset] = useState(0);
  const [pollMs, setPollMs] = useState<number>(POLL_STEPS_MS[0]);
  const list = useQuery(
    `all-batches:${offset}`,
    async () => {
      const page = await api.listBatches(ALL_BATCHES_PAGE, offset, "instagram_import");
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
  if (items.length === 0) return <Empty title="Nenhuma importação ainda" />;
  return (
    <>
      {list.error ? <ErrorLine message={list.error} onRetry={list.reload} /> : null}
      <RecentTable items={items} selectedId={selectedId} onSelect={onSelect} />
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
