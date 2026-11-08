import { useState } from "react";
import { Download } from "lucide-react";
import { api, capcutBatchZipUrl, capcutJobFileUrl, messageOf } from "../../api/client";
import type { CapCutBatch, CapCutJob, CapCutJobStatus } from "../../api/types";
import {
  CAPCUT_STATUS_LABEL,
  capcutTone,
  isHeldJob,
  jobNote,
  jobProgress,
  needsReview,
  REVIEW_STATUS_LABEL,
} from "../../lib/capcut";
import {
  BATCH_MODE_LABEL,
  BATCH_STATUS_LABEL,
  batchSourceText,
  batchActions,
  batchCountsText,
  batchEtaText,
  batchJobActions,
  batchJobStep,
  batchPollMs,
  batchProgress,
  batchSummaryText,
  batchTone,
  zipLabel,
  formatEta,
  isBatchRunning,
  openCount,
} from "../../lib/capcutBatch";
import { nextReviewJob, reviewWaitingText } from "../../lib/capcutReview";
import { formatCount, formatDateTime } from "../../lib/format";
import { useQuery } from "../../lib/useQuery";
import { useToast } from "../../lib/toast";
import { ConfirmDialog, Empty, ErrorLine, Pill, ProgressBar, SkeletonRows, Spinner, Thumb } from "../ui";
import { ReviewDialog } from "./ReviewDialog";

const PAGE_SIZE = 50;
const JOB_FILTERS: { value: CapCutJobStatus | ""; label: string }[] = [
  { value: "", label: "Todos" },
  { value: "pending", label: CAPCUT_STATUS_LABEL.pending },
  { value: "running", label: CAPCUT_STATUS_LABEL.running },
  { value: "completed", label: CAPCUT_STATUS_LABEL.completed },
  { value: "failed", label: CAPCUT_STATUS_LABEL.failed },
  { value: "cancelled", label: CAPCUT_STATUS_LABEL.cancelled },
];

type BatchAction = "pause" | "resume" | "cancel" | "retry-failed" | "reveal" | "remove";
type JobAction = "hold" | "release" | "cancel" | "retry" | "reveal" | "review";

/** Etapa 4: lote em destaque (progresso, contagens, tempo restante, vídeo atual), ações do lote e lista dos jobs. */
export function BatchMonitor({
  batchId,
  onChanged,
  onRemoved,
  highlight = false,
}: {
  batchId: number;
  /** Lote que acabou de ser criado: borda em destaque e o selo "Novo". */
  highlight?: boolean;
  /** Algo mudou (status global e histórico recarregam). */
  onChanged: () => void;
  onRemoved: () => void;
}) {
  const toast = useToast();
  const [pollMs, setPollMs] = useState<number>(() => batchPollMs(undefined, null));
  const [filter, setFilter] = useState<CapCutJobStatus | "">("");
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [reviewing, setReviewing] = useState<number | null>(null);
  const batch = useQuery(
    `capcut-batch:${batchId}`,
    () =>
      api.getCapcutBatch(batchId).then((b) => {
        setPollMs((p) => batchPollMs([b], p));
        return b;
      }),
    pollMs,
  );
  const jobs = useQuery(
    `capcut-batch-jobs:${batchId}:${filter}:${offset}`,
    () => api.listCapcutBatchJobs(batchId, { status: filter || null, limit: PAGE_SIZE, offset }),
    pollMs,
  );

  function refresh() {
    setPollMs(batchPollMs([{ status: "running" }], null));
    batch.reload();
    jobs.reload();
    onChanged();
  }

  async function runBatch(action: BatchAction) {
    setBusy(`batch:${action}`);
    try {
      if (action === "pause") await api.pauseCapcutBatch(batchId);
      else if (action === "resume") await api.resumeCapcutBatch(batchId);
      else if (action === "cancel") await api.cancelCapcutBatch(batchId);
      else if (action === "retry-failed") await api.retryFailedCapcutBatch(batchId);
      else if (action === "reveal") await api.revealCapcutBatch(batchId);
      else await api.deleteCapcutBatch(batchId);
      if (action === "reveal") toast.push("ok", "Pasta do lote aberta no Explorer.");
      else if (action === "remove") {
        toast.push("ok", "Lote removido do histórico. Os arquivos continuam na pasta.");
        onChanged();
        onRemoved();
        return;
      } else {
        if (action === "cancel") setConfirmCancel(false);
        refresh();
      }
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(null);
    }
  }

  async function runJob(job: CapCutJob, action: JobAction) {
    if (action === "review") {
      setReviewing(job.id);
      return;
    }
    setBusy(`job:${job.id}`);
    try {
      if (action === "hold") await api.holdCapcutJob(job.id);
      else if (action === "release") await api.releaseCapcutJob(job.id);
      else if (action === "cancel") await api.cancelCapcutJob(job.id);
      else if (action === "retry") await api.retryCapcutJob(job.id);
      else await api.revealCapcutJob(job.id);
      if (action === "reveal") toast.push("ok", "Pasta aberta no Explorer.");
      else refresh();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(null);
    }
  }

  const b = batch.data;
  // Editar e legendar sem histórico: o servidor não estima; aproxima pela média de "Legendar" para os abertos.
  const openNow = b ? openCount(b.counts) : 0;
  const wantApprox = b !== undefined && b.mode === "edit_caption" && b.eta_seconds === null && openNow > 0 && b.status !== "cancelled";
  const approx = useQuery(wantApprox ? `capcut-batch-eta:${batchId}:${openNow}` : "capcut-batch-eta:off", () =>
    wantApprox ? api.capcutEstimate("caption", openNow) : Promise.resolve(undefined),
  );
  const etaText = b ? batchEtaText(b.eta_seconds, wantApprox ? approx.data : undefined) : "—";
  // Lote com vídeos aguardando a revisão das legendas: o próximo a revisar ("Revisar próximo").
  const reviewCount = b?.counts.review ?? 0;
  const waitingJobs = useQuery(
    reviewCount > 0 ? `capcut-batch-review:${batchId}:${reviewCount}` : "capcut-batch-review:off",
    () =>
      reviewCount > 0
        ? api.listCapcutBatchJobs(batchId, { status: "waiting", limit: 200, offset: 0 })
        : Promise.resolve(undefined),
  );
  const nextReview = reviewCount > 0 ? nextReviewJob(waitingJobs.data?.items ?? []) : null;
  if (b === undefined) {
    return (
      <div className="panel">
        {batch.error ? <ErrorLine message={batch.error} onRetry={batch.reload} /> : <SkeletonRows rows={4} />}
      </div>
    );
  }
  const can = batchActions(b);
  const page = jobs.data;

  return (
    <div className={`panel cc-monitor${highlight ? " cc-monitor-new" : ""}`}>
      <div className="row between">
        <div style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 8 }}>
            <strong>{b.name}</strong>
            {highlight ? <Pill tone="accent">Novo</Pill> : null}
            <Pill tone={batchTone(b.status)}>{BATCH_STATUS_LABEL[b.status]}</Pill>
            <span className="muted small">{BATCH_MODE_LABEL[b.mode]}</span>
            {batch.loading ? <Spinner label="Atualizando" /> : null}
          </div>
          <div className="muted small">
            {formatDateTime(b.created_at)} · {batchSummaryText(b)}
            {batchSourceText(b) ? ` · ${batchSourceText(b)}` : ""}
          </div>
        </div>
        <BatchButtons batch={b} can={can} busy={busy} onAction={(a) => (a === "cancel" ? setConfirmCancel(true) : void runBatch(a))} />
      </div>

      {batch.error ? <ErrorLine message={batch.error} onRetry={batch.reload} /> : null}

      <div style={{ marginTop: 12 }}>
        <ProgressBar value={batchProgress(b)} />
      </div>
      <p className="small" aria-live="polite" style={{ margin: "6px 0 0" }}>
        {batchCountsText(b.counts)}
        {isBatchRunning(b) ? ` · tempo restante ${etaText}` : ""}
        {b.cancel_requested && b.status !== "cancelled" ? " · cancelando…" : ""}
      </p>

      <div className="counters">
        <Counter n={b.counts.completed} label="Concluídos" />
        <Counter n={b.counts.pending + b.counts.waiting} label="Na fila" />
        <Counter n={b.counts.running} label="Em andamento" />
        <Counter n={b.counts.failed} label="Com erro" />
        <Counter n={b.counts.held} label="Pausados" />
        {reviewCount > 0 ? <Counter n={reviewCount} label={REVIEW_STATUS_LABEL} /> : null}
        <div className="counter">
          <div className="n">{isBatchRunning(b) || b.paused ? etaText : formatEta(null)}</div>
          <div className="l">
            Tempo restante
            {b.per_video_seconds !== null ? ` (${formatEta(b.per_video_seconds)} por vídeo)` : ""}
          </div>
        </div>
      </div>

      {reviewCount > 0 ? (
        <div className="selection-bar" role="status">
          <span className="grow small">
            <strong>{reviewWaitingText(reviewCount)}</strong>
            <span className="muted"> · confira as legendas antes de exportar</span>
          </span>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            disabled={nextReview === null}
            onClick={() => nextReview && setReviewing(nextReview.id)}
          >
            Revisar próximo
          </button>
        </div>
      ) : null}
      {b.current_job ? (
        <div className="selection-bar" role="status">
          <Thumb src={b.current_job.thumbnail_url} mediaType={b.current_job.media_type ?? "video"} size={32} />
          <span className="truncate grow small">
            Agora: {b.current_job.media_filename ?? `Vídeo ${b.current_job.media_id ?? ""}`}
          </span>
          <span className="muted small nowrap">{batchJobStep(b.current_job)}</span>
          <span style={{ width: 120 }}>
            <ProgressBar value={jobProgress(b.current_job)} />
          </span>
        </div>
      ) : null}
      {b.paused ? (
        <p className="muted small" style={{ margin: "4px 0 10px" }}>
          Lote pausado: o vídeo em andamento termina e os próximos esperam. A edição dos vídeos (modos com edição)
          continua.
        </p>
      ) : null}

      <div className="row between" style={{ marginTop: 6, marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>Vídeos do lote</h3>
        <select
          className="select"
          style={{ width: "auto" }}
          aria-label="Filtrar os vídeos do lote por status"
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value as CapCutJobStatus | "");
            setOffset(0);
          }}
        >
          {JOB_FILTERS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      {jobs.error ? <ErrorLine message={jobs.error} onRetry={jobs.reload} /> : null}
      {page === undefined ? (
        jobs.error ? null : (
          <SkeletonRows rows={3} />
        )
      ) : page.items.length === 0 ? (
        <Empty title="Nenhum vídeo com este status" />
      ) : (
        <>
          <div className="table-wrap edit-picker">
            <table className="table">
              <thead>
                <tr>
                  <th>Vídeo</th>
                  <th>Status</th>
                  <th>Etapa</th>
                  <th>Progresso</th>
                  <th className="col-opt">Observação</th>
                  <th aria-label="Ações" />
                </tr>
              </thead>
              <tbody>
                {page.items.map((job) => (
                  <BatchJobRow
                    key={job.id}
                    job={job}
                    busy={busy === `job:${job.id}`}
                    onAction={(a) => void runJob(job, a)}
                  />
                ))}
              </tbody>
            </table>
          </div>
          {page.total > PAGE_SIZE ? (
            <div className="row" style={{ marginTop: 10, justifyContent: "flex-end" }}>
              <span className="muted small">
                {formatCount(offset + 1)}–{formatCount(Math.min(offset + PAGE_SIZE, page.total))} de{" "}
                {formatCount(page.total)}
              </span>
              <button
                type="button"
                className="btn btn-sm"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Anterior
              </button>
              <button
                type="button"
                className="btn btn-sm"
                disabled={offset + PAGE_SIZE >= page.total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Próxima
              </button>
            </div>
          ) : null}
        </>
      )}

      {reviewing !== null ? (
        <ReviewDialog
          jobId={reviewing}
          onClose={() => setReviewing(null)}
          onApproved={() => {
            setReviewing(null);
            waitingJobs.reload();
            refresh();
          }}
        />
      ) : null}
      {confirmCancel ? (
        <ConfirmDialog
          title="Cancelar lote"
          confirmLabel="Cancelar lote"
          cancelLabel="Voltar"
          focusCancel
          danger
          busy={busy === "batch:cancel"}
          onConfirm={() => void runBatch("cancel")}
          onCancel={() => setConfirmCancel(false)}
        >
          <p>
            Os vídeos que ainda não terminaram serão cancelados (o que está em andamento para com segurança). Os já
            concluídos ficam na pasta do lote.
          </p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

function Counter({ n, label }: { n: number; label: string }) {
  return (
    <div className="counter">
      <div className="n">{formatCount(n)}</div>
      <div className="l">{label}</div>
    </div>
  );
}

function BatchButtons({
  batch,
  can,
  busy,
  onAction,
}: {
  batch: CapCutBatch;
  can: ReturnType<typeof batchActions>;
  busy: string | null;
  onAction: (action: BatchAction) => void;
}) {
  const off = busy !== null && busy.startsWith("batch:");
  return (
    <div className="row" style={{ gap: 6 }}>
      {can.pause ? (
        <button type="button" className="btn btn-sm" disabled={off} onClick={() => onAction("pause")}>
          Pausar
        </button>
      ) : null}
      {can.resume ? (
        <button type="button" className="btn btn-sm" disabled={off} onClick={() => onAction("resume")}>
          Retomar
        </button>
      ) : null}
      {can.retryFailed ? (
        <button type="button" className="btn btn-sm" disabled={off} onClick={() => onAction("retry-failed")}>
          Reprocessar falhas ({formatCount(batch.counts.failed + batch.counts.cancelled)})
        </button>
      ) : null}
      {can.reveal ? (
        <button type="button" className="btn btn-sm btn-ghost" disabled={off} onClick={() => onAction("reveal")}>
          Abrir pasta
        </button>
      ) : null}
      {can.zip ? (
        <a className="btn btn-sm btn-primary" href={capcutBatchZipUrl(batch.id)} download={`lote-${batch.id}.zip`}>
          <Download size={14} strokeWidth={2} aria-hidden="true" />
          {zipLabel(batch)}
        </a>
      ) : null}
      {can.cancel ? (
        <button type="button" className="btn btn-sm btn-danger" disabled={off} onClick={() => onAction("cancel")}>
          Cancelar lote
        </button>
      ) : null}
      {can.remove ? (
        <button type="button" className="btn btn-sm btn-ghost" disabled={off} onClick={() => onAction("remove")}>
          Remover do histórico
        </button>
      ) : null}
    </div>
  );
}

function BatchJobRow({
  job,
  busy,
  onAction,
}: {
  job: CapCutJob;
  busy: boolean;
  onAction: (action: JobAction) => void;
}) {
  const can = batchJobActions(job);
  const note = jobNote(job);
  return (
    <tr>
      <td>
        <span className="row" style={{ gap: 8, flexWrap: "nowrap" }}>
          <Thumb src={job.thumbnail_url} mediaType={job.media_type ?? "video"} size={36} />
          <span className="small truncate" style={{ minWidth: 0 }} title={job.media_filename ?? undefined}>
            {job.media_filename ?? "—"}
          </span>
        </span>
      </td>
      <td>
        <Pill tone={isHeldJob(job) || needsReview(job) ? "warn" : capcutTone(job.status)}>
          {needsReview(job) ? REVIEW_STATUS_LABEL : isHeldJob(job) ? "Pausado" : CAPCUT_STATUS_LABEL[job.status]}
        </Pill>
      </td>
      <td className="muted small">{batchJobStep(job)}</td>
      <td style={{ minWidth: 110 }}>
        <ProgressBar value={jobProgress(job)} />
      </td>
      <td className="muted small col-opt">{note || "—"}</td>
      <td className="nowrap" style={{ textAlign: "right" }}>
        {can.review ? (
          <button type="button" className="btn btn-sm btn-primary" disabled={busy} onClick={() => onAction("review")}>
            Revisar
          </button>
        ) : null}
        {can.hold ? (
          <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={() => onAction("hold")}>
            Pausar
          </button>
        ) : null}
        {can.release ? (
          <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={() => onAction("release")}>
            Retomar
          </button>
        ) : null}
        {can.cancel ? (
          <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={() => onAction("cancel")}>
            Cancelar
          </button>
        ) : null}
        {can.retry ? (
          <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={() => onAction("retry")}>
            Tentar de novo
          </button>
        ) : null}
        {can.download ? (
          <>
            <a className="btn btn-sm btn-ghost" href={capcutJobFileUrl(job.id)} download={job.output_name ?? undefined}>
              Baixar
            </a>
            <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={() => onAction("reveal")}>
              Mostrar na pasta
            </button>
          </>
        ) : job.status === "completed" ? (
          <span className="muted small">Arquivo removido</span>
        ) : null}
      </td>
    </tr>
  );
}
