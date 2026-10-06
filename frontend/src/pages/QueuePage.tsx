import { useState } from "react";
import { api, messageOf } from "../api/client";
import { describeErrorCode } from "../api/errors";
import type { Batch, Job, JobStatus } from "../api/types";
import { BatchNotice } from "../components/BatchNotice";
import { listingDroppedSuffix } from "../lib/batchState";
import { Empty, ErrorLine, Pill, ProgressBar, SkeletonRows, Spinner, Thumb } from "../components/ui";
import { formatCount, formatDateTime, formatDuration, formatPercent } from "../lib/format";
import {
  ACTIVE_BATCH,
  JOB_STAGE_LABEL,
  JOB_STATUS_LABEL,
  TERMINAL_JOB,
  batchLabel,
  batchTitle,
  batchToneOf,
  isDownloadOnly,
  jobKindLabel,
  jobTone,
  showDuplicate,
} from "../lib/labels";
import { batchIdFromSearch } from "../lib/batchRoute";
import {
  FIRST_PAGE,
  type PageCursor,
  type Pager,
  cursorQuery,
  currentCursor,
  nextCursor,
  pageStart,
  popPage,
  pushPage,
  shouldResetToFirst,
  stackFor,
} from "../lib/keyset";
import { POLL_STEPS_MS, nextPollMs } from "../lib/polling";
import { navigate, ROUTES, useSearch } from "../lib/router";
import { useQuery } from "../lib/useQuery";
import { useToast } from "../lib/toast";

const PAGE_SIZE = 100;

type FilterId = "all" | "pending" | "running" | "completed" | "failed" | "cancelled";

const FILTERS: { id: FilterId; label: string; statuses: JobStatus[] }[] = [
  { id: "all", label: "Todos", statuses: [] },
  { id: "pending", label: "Pendentes", statuses: ["pending"] },
  { id: "running", label: "Processando", statuses: ["downloading", "downloaded", "processing"] },
  { id: "completed", label: "Concluídos", statuses: ["completed"] },
  { id: "failed", label: "Erros", statuses: ["failed"] },
  { id: "cancelled", label: "Cancelados", statuses: ["cancelled"] },
];

const FAST_POLL_MS = POLL_STEPS_MS[0];

const ACTIVE_STATUSES: JobStatus[] = ["pending", "downloading", "downloaded", "processing"];

function origin(job: Job): string {
  const parts: string[] = [];
  if (job.source_profile) parts.push(`@${job.source_profile}`);
  if (job.source_post_id) parts.push(job.source_post_id);
  return parts.length > 0 ? parts.join(" · ") : job.kind === "process" ? "Biblioteca" : "-";
}

export function QueuePage() {
  const toast = useToast();
  const batchId = batchIdFromSearch(useSearch());
  const [filter, setFilter] = useState<FilterId>("all");
  const [pager, setPager] = useState<Pager>({ scope: "", stack: FIRST_PAGE });
  const [pollMs, setPollMs] = useState<number>(FAST_POLL_MS);
  const [busyId, setBusyId] = useState<number | null>(null);

  const statuses = FILTERS.find((f) => f.id === filter)?.statuses ?? [];
  const scope = `${batchId ?? "all"}:${filter}`;
  const stack = stackFor(pager, scope);
  const cursor = currentCursor(stack);
  const jobs = useQuery(
    `jobs:${scope}:${JSON.stringify(cursor)}`,
    async () => {
      const page = await api.listJobs({
        status: statuses,
        batch_id: batchId ?? undefined,
        limit: PAGE_SIZE,
        ...cursorQuery(cursor),
      });
      const activeByCounts = ACTIVE_STATUSES.some((s) => (page.counts[s] ?? 0) > 0);
      const activeByItems = page.items.some((j) => !TERMINAL_JOB.has(j.status));
      setPollMs((prev) => nextPollMs(prev, activeByCounts || activeByItems));
      if (shouldResetToFirst(page, stack)) setPager({ scope, stack: FIRST_PAGE });
      return page;
    },
    pollMs,
  );
  // Só o resumo do lote (sem jobs), no mesmo ritmo da lista.
  const batch = useQuery<Batch | null>(
    `queue-batch:${batchId ?? "none"}`,
    () => (batchId === null ? Promise.resolve(null) : api.getBatch(batchId)),
    batchId === null ? null : pollMs,
  );

  async function act(job: Job, kind: "cancel" | "retry") {
    setBusyId(job.id);
    try {
      if (kind === "cancel") {
        await api.cancelJob(job.id);
        toast.push("ok", "Cancelamento solicitado.");
      } else {
        await api.retryJob(job.id);
        toast.push("ok", "Job voltou para a fila.");
        setPollMs(FAST_POLL_MS);
      }
      jobs.reload();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusyId(null);
    }
  }

  // Lote só de download: sem coluna de preset e sem rótulo de processamento.
  const downloadOnly = batchId !== null && batch.data != null && isDownloadOnly(batch.data);
  const items = jobs.data?.items ?? [];
  const total = jobs.data?.total ?? 0;
  const next = jobs.data ? nextCursor(jobs.data, stack, PAGE_SIZE) : null;
  const start = pageStart(stack, PAGE_SIZE);
  const goTo = (nextStack: PageCursor[]) => {
    setPager({ scope, stack: nextStack });
    setPollMs(FAST_POLL_MS);
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1>Fila</h1>
        <div className="row">
          {jobs.loading && jobs.data ? <Spinner /> : null}
          {jobs.data ? <span className="muted small">{formatCount(total)} jobs</span> : null}
        </div>
      </div>

      {batchId !== null ? (
        <BatchHeader
          batchId={batchId}
          batch={batch.data ?? null}
          error={batch.error}
          onRetry={batch.reload}
          onClear={() => navigate(ROUTES.queue)}
        />
      ) : null}

      <div className="tabs" role="tablist">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            role="tab"
            aria-selected={filter === f.id}
            className={`tab${filter === f.id ? " on" : ""}`}
            onClick={() => {
              setFilter(f.id);
              setPager({ scope: "", stack: FIRST_PAGE });
              setPollMs(FAST_POLL_MS);
            }}
          >
            {f.id === "running" && downloadOnly ? "Baixando" : f.label}
            {jobs.data ? (
              <span className="count">
                {formatCount(
                  (f.statuses.length > 0 ? f.statuses : (Object.keys(jobs.data.counts) as JobStatus[])).reduce(
                    (sum, s) => sum + (jobs.data?.counts[s] ?? 0),
                    0,
                  ),
                )}
              </span>
            ) : null}
          </button>
        ))}
      </div>

      {jobs.data === undefined ? (
        jobs.error ? (
          <ErrorLine message={jobs.error} onRetry={jobs.reload} />
        ) : (
          <SkeletonRows rows={6} />
        )
      ) : items.length === 0 ? (
        <Empty
          title={
            filter !== "all"
              ? "Nenhum job neste filtro"
              : batchId !== null
                ? "Nenhum job nesta importação"
                : "Fila vazia"
          }
        />
      ) : (
        <>
          {jobs.error ? <ErrorLine message={jobs.error} onRetry={jobs.reload} /> : null}
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th />
                  <th>Origem</th>
                  {downloadOnly ? null : <th>Preset</th>}
                  <th>Status</th>
                  <th>Etapa</th>
                  <th>Progresso</th>
                  <th>Tempo</th>
                  <th>Tentativas</th>
                  <th>Erro</th>
                  <th className="actions" />
                </tr>
              </thead>
              <tbody>
                {items.map((job) => {
                  const terminal = TERMINAL_JOB.has(job.status);
                  const canRetry = job.status === "failed" || job.status === "cancelled";
                  return (
                    <tr key={job.id}>
                      <td>
                        <Thumb src={job.thumbnail_url} mediaType={job.media_type} size={34} />
                      </td>
                      <td className="cell-trunc" title={origin(job)}>
                        <div className="truncate">{origin(job)}</div>
                        <div className="dim small truncate">
                          {jobKindLabel(job.kind)} · {formatDateTime(job.created_at)}
                        </div>
                      </td>
                      {downloadOnly ? null : (
                        <td className="muted cell-trunc" style={{ maxWidth: 140 }} title={job.preset_name ?? undefined}>
                          {job.preset_name ?? "-"}
                        </td>
                      )}
                      <td>
                        <div className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
                          <Pill tone={jobTone(job.status)}>{JOB_STATUS_LABEL[job.status]}</Pill>
                          {showDuplicate(job) ? (
                            <span
                              className="tag"
                              title={
                                job.duplicate_reason === "sha256"
                                  ? "Arquivo idêntico já existe na biblioteca"
                                  : "Mídia já importada antes"
                              }
                            >
                              Duplicado
                            </span>
                          ) : null}
                        </div>
                      </td>
                      <td className="muted nowrap">
                        {job.stage ? JOB_STAGE_LABEL[job.stage] : "-"}
                        {job.stage === "waiting_retry" && job.next_attempt_at ? (
                          <div className="dim small">às {formatDateTime(job.next_attempt_at)}</div>
                        ) : null}
                      </td>
                      <td className="nowrap">
                        {job.progress !== null && !terminal ? (
                          <>
                            <ProgressBar value={job.progress} inline />
                            <span className="muted small">{formatPercent(job.progress)}</span>
                          </>
                        ) : (
                          <span className="dim">-</span>
                        )}
                      </td>
                      <td className="muted nowrap">{formatDuration(job.elapsed_seconds)}</td>
                      <td className="muted">
                        {job.attempts}/{job.max_attempts}
                      </td>
                      <td className="cell-trunc">
                        {job.error_code || job.error_message ? (
                          <span
                            className="tone-err small"
                            title={describeErrorCode(job.error_code, job.error_message)}
                          >
                            {describeErrorCode(job.error_code, job.error_message)}
                          </span>
                        ) : (
                          <span className="dim">-</span>
                        )}
                      </td>
                      <td className="actions">
                        {!terminal ? (
                          <button
                            type="button"
                            className="btn btn-sm btn-ghost"
                            disabled={busyId === job.id}
                            onClick={() => void act(job, "cancel")}
                          >
                            Cancelar
                          </button>
                        ) : null}
                        {canRetry ? (
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={busyId === job.id}
                            onClick={() => void act(job, "retry")}
                          >
                            Tentar novamente
                          </button>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {stack.length > 1 || next !== null ? (
            <div className="row" style={{ marginTop: 10, justifyContent: "flex-end" }}>
              <span className="muted small">
                {formatCount(start + 1)} a {formatCount(start + items.length)} de {formatCount(total)}
              </span>
              {stack.length > 2 ? (
                <button type="button" className="btn btn-sm btn-ghost" onClick={() => goTo(FIRST_PAGE)}>
                  Primeira página
                </button>
              ) : null}
              <button
                type="button"
                className="btn btn-sm"
                disabled={stack.length <= 1}
                onClick={() => goTo(popPage(stack))}
              >
                Anterior
              </button>
              <button
                type="button"
                className="btn btn-sm"
                disabled={next === null}
                onClick={() => goTo(pushPage(stack, next))}
              >
                Próxima
              </button>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}

const BATCH_COUNTS: { label: string; statuses: JobStatus[] }[] = [
  { label: "Pendentes", statuses: ["pending"] },
  { label: "Processando", statuses: ["downloading", "downloaded", "processing"] },
  { label: "Concluídos", statuses: ["completed"] },
  { label: "Erros", statuses: ["failed"] },
  { label: "Cancelados", statuses: ["cancelled"] },
];

function BatchHeader({
  batchId,
  batch,
  error,
  onRetry,
  onClear,
}: {
  batchId: number;
  batch: Batch | null;
  error: string | null;
  onRetry: () => void;
  onClear: () => void;
}) {
  return (
    <div className="panel" style={{ marginBottom: 14 }}>
      <div className="row between">
        <div className="row">
          <strong style={{ fontSize: 14 }}>{batch ? batchTitle(batch) : `Importação ${batchId}`}</strong>
          {batch ? <Pill tone={batchToneOf(batch)}>{batchLabel(batch)}</Pill> : null}
          {batch && ACTIVE_BATCH.has(batch.status) ? <Spinner /> : null}
        </div>
        <button type="button" className="btn btn-sm btn-ghost" onClick={onClear}>
          Ver toda a fila
        </button>
      </div>
      {batch ? (
        <div className="muted small" style={{ marginTop: 6 }}>
          {isDownloadOnly(batch) ? "Somente originais · " : ""}
          {BATCH_COUNTS.map(
            ({ label, statuses }) =>
              `${isDownloadOnly(batch) && label === "Processando" ? "Baixando" : label}: ${formatCount(
                statuses.reduce((sum, s) => sum + (batch.counts[s] ?? 0), 0),
              )}`,
          ).join(" · ")}
          {listingDroppedSuffix(batch)}
        </div>
      ) : error ? (
        <div style={{ marginTop: 6 }}>
          <ErrorLine message={error} onRetry={onRetry} />
        </div>
      ) : null}
      {batch ? <BatchNotice batch={batch} onResumed={onRetry} /> : null}
    </div>
  );
}
