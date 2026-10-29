import { useState } from "react";
import { api } from "../../api/client";
import type { Batch, Job } from "../../api/types";
import { queueForBatch } from "../../lib/batchRoute";
import { describeErrorCode } from "../../api/errors";
import { isExportActive } from "../../lib/exportFolder";
import { formatCount } from "../../lib/format";
import { ACTIVE_BATCH, BATCH_STATUS_LABEL, JOB_STAGE_LABEL, JOB_STATUS_LABEL, batchTone, jobTone } from "../../lib/labels";
import { nextPollMs } from "../../lib/polling";
import { ROUTES } from "../../lib/router";
import { useExportFlow } from "../../lib/useExportFlow";
import { useQuery } from "../../lib/useQuery";
import { ExportStatus } from "../ExportPanel";
import { Link } from "../Layout";
import { ErrorLine, Pill, ProgressBar, SkeletonRows, SplitBar, Thumb } from "../ui";

function jobLine(job: Job): string {
  if (job.status === "completed") {
    return job.stage === "skipped" ? "Já existia com estas configurações (reaproveitado)" : "Salvo na biblioteca";
  }
  if (job.status === "failed") return describeErrorCode(job.error_code, job.error_message);
  if (job.status === "cancelled") return "Cancelado";
  return job.stage ? JOB_STAGE_LABEL[job.stage] : JOB_STATUS_LABEL[job.status];
}

/** Progresso do lote de edição: contadores, barra do lote e, até 500 itens, a lista por vídeo. */
export function EditProgressPanel({
  batchId,
  names,
  onClose,
}: {
  batchId: number;
  names: ReadonlyMap<number, string>;
  onClose: () => void;
}) {
  const [pollMs, setPollMs] = useState<number | null>(() => nextPollMs(null, true));
  const query = useQuery(
    `edit-batch:${batchId}`,
    async () => {
      const batch = await api.getBatch(batchId, true);
      setPollMs((prev) => (ACTIVE_BATCH.has(batch.status) ? nextPollMs(prev, true) : null));
      return batch;
    },
    pollMs,
  );
  const batch: Batch | undefined = query.data;
  // Exportação das versões editadas deste lote (`content: "edited"`): ZIP com download automático ou pasta.
  const flow = useExportFlow({ resumeFor: { batchId } });
  const source = { kind: "batch" as const, id: batchId };
  const exportBusy = flow.busy || (flow.current !== null && isExportActive(flow.current));
  const canExport =
    batch !== undefined && !ACTIVE_BATCH.has(batch.status) && batch.progress.processed + batch.progress.skipped > 0;

  return (
    <div className="panel edit-progress" aria-live="polite">
      <div className="section-head">
        <h2>Lote de edição #{batchId}</h2>
        <div className="row">
          {batch ? <Pill tone={batchTone(batch.status)}>{BATCH_STATUS_LABEL[batch.status]}</Pill> : null}
          <Link to={queueForBatch(batchId)} className="btn btn-sm">
            Ver na Fila
          </Link>
          <Link to={ROUTES.library} className="btn btn-sm">
            Abrir Biblioteca
          </Link>
          <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>
            Fechar
          </button>
        </div>
      </div>
      {batch === undefined ? (
        query.error ? (
          <ErrorLine message={query.error} onRetry={query.reload} />
        ) : (
          <SkeletonRows rows={3} />
        )
      ) : (
        <BatchBody batch={batch} names={names} error={query.error} onRetry={query.reload} />
      )}
      {canExport ? (
        <div className="row" style={{ flexWrap: "wrap", marginTop: 12 }} role="group" aria-label="Versões editadas">
          <button
            type="button"
            className="btn btn-sm btn-primary"
            disabled={exportBusy}
            title="Prepara um arquivo .zip com as versões editadas deste lote e inicia o download"
            onClick={() => void flow.start(source, "zip", "edited")}
          >
            Baixar ZIP dos editados
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={exportBusy}
            title="Copia as versões editadas deste lote para uma pasta escolhida"
            onClick={() => void flow.start(source, "pick", "edited")}
          >
            Exportar editados para pasta
          </button>
        </div>
      ) : null}
      <ExportStatus flow={flow} />
    </div>
  );
}

function BatchBody({
  batch,
  names,
  error,
  onRetry,
}: {
  batch: Batch;
  names: ReadonlyMap<number, string>;
  error: string | null;
  onRetry: () => void;
}) {
  const p = batch.progress;
  const total = Math.max(batch.found, p.processed + p.skipped + p.failed + p.pending + p.active, 1);
  const jobs = [...(batch.jobs ?? [])].sort((a, b) => a.id - b.id);
  const counters: [string, number][] = [
    ["Vídeos", batch.found],
    ["Concluídos", p.processed],
    ["Reaproveitados", p.skipped],
    ["Com erro", p.failed],
    ["Em andamento", p.active],
    ["Na fila", p.pending],
  ];
  return (
    <>
      {error ? <ErrorLine message={error} onRetry={onRetry} /> : null}
      <SplitBar ok={(p.processed + p.skipped) / total} err={p.failed / total} />
      <div className="counters">
        {counters.map(([label, n]) => (
          <div key={label} className="counter">
            <div className="n">{formatCount(n)}</div>
            <div className="l">{label}</div>
          </div>
        ))}
      </div>
      {batch.error_message ? <div className="alert err">{batch.error_message}</div> : null}
      {batch.jobs_truncated || jobs.length === 0 ? (
        <p className="muted small">
          {batch.jobs_truncated
            ? "Lote grande: aqui aparecem só os contadores. Veja cada vídeo na Fila."
            : "Preparando os itens do lote."}
        </p>
      ) : (
        <ul className="edit-job-list">
          {jobs.map((job) => (
            <li key={job.id} className="edit-job">
              <Thumb src={job.thumbnail_url} mediaType={job.media_type ?? "video"} size={32} />
              <div className="grow" style={{ minWidth: 0 }}>
                <div className="row between">
                  <span className="truncate">
                    {job.media_id !== null ? (names.get(job.media_id) ?? `Vídeo ${job.media_id}`) : `Item ${job.id}`}
                  </span>
                  <Pill tone={jobTone(job.status)}>{JOB_STATUS_LABEL[job.status]}</Pill>
                </div>
                {job.status === "processing" || job.status === "pending" ? (
                  <div style={{ marginTop: 4 }}>
                    <ProgressBar value={job.progress ?? 0} />
                  </div>
                ) : null}
                <div className={`small ${job.status === "failed" ? "field-error" : "muted"}`}>{jobLine(job)}</div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
