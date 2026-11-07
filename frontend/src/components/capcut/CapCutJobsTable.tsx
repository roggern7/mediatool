import { useState } from "react";
import { api, capcutJobFileUrl, messageOf } from "../../api/client";
import type { CapCutJob, CapCutJobStatus } from "../../api/types";
import {
  CAPCUT_STATUS_LABEL,
  capcutPollMs,
  capcutTone,
  exportModeHelp,
  isCapcutActive,
  isHeldJob,
  jobExportModeText,
  jobNote,
  jobProgress,
  jobStatusText,
  stageLabel,
} from "../../lib/capcut";
import { formatCount, formatDateTime } from "../../lib/format";
import { nextPollMs } from "../../lib/polling";
import { useQuery } from "../../lib/useQuery";
import { useToast } from "../../lib/toast";
import { Empty, ErrorLine, Pill, ProgressBar, SkeletonRows, Spinner, Thumb } from "../ui";

const PAGE_SIZE = 50;
const STATUS_FILTERS: { value: CapCutJobStatus | ""; label: string }[] = [
  { value: "", label: "Todos" },
  { value: "pending", label: CAPCUT_STATUS_LABEL.pending },
  { value: "running", label: CAPCUT_STATUS_LABEL.running },
  { value: "waiting", label: CAPCUT_STATUS_LABEL.waiting },
  { value: "completed", label: CAPCUT_STATUS_LABEL.completed },
  { value: "failed", label: CAPCUT_STATUS_LABEL.failed },
  { value: "cancelled", label: CAPCUT_STATUS_LABEL.cancelled },
];

/** Lista geral de jobs de legenda (avulsos, automáticos e de lotes), com filtro por status e coluna Modo. */
export function CapCutJobsTable({ onChanged }: { onChanged: () => void }) {
  const toast = useToast();
  const [filter, setFilter] = useState<CapCutJobStatus | "">("");
  const [offset, setOffset] = useState(0);
  const [pollMs, setPollMs] = useState<number>(() => nextPollMs(null, false));
  const [busyId, setBusyId] = useState<number | null>(null);
  const jobs = useQuery(
    `capcut-jobs:${filter}:${offset}`,
    () =>
      api.listCapcutJobs({ status: filter || null, limit: PAGE_SIZE, offset }).then((page) => {
        setPollMs((p) => capcutPollMs(page.items, p));
        return page;
      }),
    pollMs,
  );

  async function act(job: CapCutJob, action: "cancel" | "retry" | "delete" | "reveal") {
    setBusyId(job.id);
    try {
      if (action === "cancel") await api.cancelCapcutJob(job.id);
      else if (action === "retry") await api.retryCapcutJob(job.id);
      else if (action === "delete") await api.deleteCapcutJob(job.id);
      else await api.revealCapcutJob(job.id);
      if (action === "reveal") toast.push("ok", "Pasta aberta no Explorer.");
      else {
        setPollMs(nextPollMs(null, true));
        jobs.reload();
        onChanged();
      }
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusyId(null);
    }
  }

  const page = jobs.data;
  return (
    <div>
      <div className="row" style={{ justifyContent: "flex-end", marginBottom: 10 }}>
        {jobs.loading && page ? <Spinner /> : null}
        <select
          className="select"
          style={{ width: "auto" }}
          aria-label="Filtrar por status"
          value={filter}
          onChange={(e) => {
            setFilter(e.target.value as CapCutJobStatus | "");
            setOffset(0);
          }}
        >
          {STATUS_FILTERS.map((o) => (
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
      ) : page.total === 0 ? (
        <Empty title="Nenhum vídeo na fila de legendas">
          <span className="muted small">Use "Processar vídeos" acima ou, na Biblioteca, "Legendar no CapCut".</span>
        </Empty>
      ) : (
        <>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Vídeo</th>
                  <th>Status</th>
                  <th>Modo</th>
                  <th>Etapa</th>
                  <th>Progresso</th>
                  <th>Tentativas</th>
                  <th>Observação</th>
                  <th>Saída</th>
                  <th aria-label="Ações" />
                </tr>
              </thead>
              <tbody>
                {page.items.map((job) => (
                  <JobRow key={job.id} job={job} busy={busyId === job.id} onAction={(a) => void act(job, a)} />
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
    </div>
  );
}

function JobRow({
  job,
  busy,
  onAction,
}: {
  job: CapCutJob;
  busy: boolean;
  onAction: (action: "cancel" | "retry" | "delete" | "reveal") => void;
}) {
  const active = isCapcutActive(job);
  const note = jobNote(job);
  return (
    <tr>
      <td>
        <span className="row" style={{ gap: 8, flexWrap: "nowrap" }}>
          <Thumb src={job.thumbnail_url} mediaType={job.media_type ?? "video"} size={36} />
          <span style={{ minWidth: 0 }}>
            <span className="small">{job.media_filename ?? "—"}</span>
            <br />
            <span className="muted small">
              {job.style_name ?? "—"} · {formatDateTime(job.created_at)}
              {job.origin === "auto" ? " · automático" : ""}
            </span>
          </span>
        </span>
      </td>
      <td>
        <Pill tone={isHeldJob(job) ? "warn" : capcutTone(job.status)}>{jobStatusText(job)}</Pill>
      </td>
      <td className="muted small nowrap" title={job.export_mode ? exportModeHelp(job.export_mode) : "Ainda não exportado"}>
        {jobExportModeText(job.export_mode)}
      </td>
      <td className="muted small">{job.status === "running" ? stageLabel(job.stage) : "—"}</td>
      <td style={{ minWidth: 120 }}>
        <ProgressBar value={jobProgress(job)} />
      </td>
      <td className="muted nowrap">
        {formatCount(job.attempts)} de {formatCount(job.max_attempts)}
      </td>
      <td className="muted small">{note || "—"}</td>
      <td className="nowrap">
        {job.status === "completed" && job.output_exists ? (
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
        ) : (
          <span className="muted small">—</span>
        )}
      </td>
      <td className="nowrap" style={{ textAlign: "right" }}>
        {active ? (
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            disabled={busy || job.cancel_requested}
            onClick={() => onAction("cancel")}
          >
            Cancelar
          </button>
        ) : null}
        {job.status === "failed" || job.status === "cancelled" ? (
          <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={() => onAction("retry")}>
            Tentar de novo
          </button>
        ) : null}
        {!active ? (
          <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={() => onAction("delete")}>
            Remover do histórico
          </button>
        ) : null}
      </td>
    </tr>
  );
}
