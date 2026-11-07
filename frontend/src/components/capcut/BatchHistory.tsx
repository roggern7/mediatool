import { useState } from "react";
import { api } from "../../api/client";
import type { CapCutBatchMode, CapCutBatchStatus } from "../../api/types";
import {
  BATCH_MODE_LABEL,
  BATCH_MODE_OPTIONS,
  BATCH_STATUS_LABEL,
  batchPollMs,
  batchProgress,
  batchSummaryText,
  batchTone,
} from "../../lib/capcutBatch";
import { formatCount, formatDateTime } from "../../lib/format";
import { useDebounced } from "../../lib/useDebounced";
import { useQuery } from "../../lib/useQuery";
import { Empty, ErrorLine, Pill, ProgressBar, SkeletonRows } from "../ui";

const PAGE_SIZE = 20;
const STATUS_OPTIONS = Object.entries(BATCH_STATUS_LABEL) as [CapCutBatchStatus, string][];

/** Histórico de lotes (sem os removidos) com filtros de status, modo e nome; "Abrir" mostra os resultados do lote. */
export function BatchHistory({
  openId,
  onOpen,
  version,
}: {
  openId: number | null;
  onOpen: (id: number) => void;
  /** Sobe quando um lote é criado ou muda: recarrega. */
  version: number;
}) {
  const [status, setStatus] = useState<CapCutBatchStatus | "">("");
  const [mode, setMode] = useState<CapCutBatchMode | "">("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [pollMs, setPollMs] = useState<number>(() => batchPollMs(undefined, null));
  const q = useDebounced(search.trim(), 300);
  const list = useQuery(
    `capcut-batches:${status}:${mode}:${q}:${offset}:${version}`,
    () =>
      api
        .listCapcutBatches({ status: status || null, mode: mode || null, q: q || undefined, limit: PAGE_SIZE, offset })
        .then((page) => {
          setPollMs((p) => batchPollMs(page.items, p));
          return page;
        }),
    pollMs,
  );
  const page = list.data;

  return (
    <div>
      <div className="toolbar">
        <input
          className="input grow"
          style={{ minWidth: 160 }}
          placeholder="Buscar pelo nome do lote"
          aria-label="Buscar lote pelo nome"
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setOffset(0);
          }}
        />
        <select
          className="select"
          style={{ width: "auto" }}
          aria-label="Filtrar lotes por status"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value as CapCutBatchStatus | "");
            setOffset(0);
          }}
        >
          <option value="">Todos os status</option>
          {STATUS_OPTIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <select
          className="select"
          style={{ width: "auto" }}
          aria-label="Filtrar lotes por modo"
          value={mode}
          onChange={(e) => {
            setMode(e.target.value as CapCutBatchMode | "");
            setOffset(0);
          }}
        >
          <option value="">Todos os modos</option>
          {BATCH_MODE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      {list.error ? <ErrorLine message={list.error} onRetry={list.reload} /> : null}
      {page === undefined ? (
        list.error ? null : (
          <SkeletonRows rows={3} />
        )
      ) : page.items.length === 0 ? (
        <Empty title={status || mode || q ? "Nenhum lote encontrado" : "Nenhum lote ainda"} />
      ) : (
        <>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Lote</th>
                  <th>Status</th>
                  <th>Progresso</th>
                  <th className="col-opt">Configurações</th>
                  <th aria-label="Ações" />
                </tr>
              </thead>
              <tbody>
                {page.items.map((b) => (
                  <tr key={b.id} className={b.id === openId ? "selected" : undefined}>
                    <td>
                      <span className="small">{b.name}</span>
                      <div className="muted small">
                        {BATCH_MODE_LABEL[b.mode]} · {formatDateTime(b.created_at)}
                      </div>
                    </td>
                    <td>
                      <Pill tone={batchTone(b.status)}>{BATCH_STATUS_LABEL[b.status]}</Pill>
                    </td>
                    <td style={{ minWidth: 120 }}>
                      <ProgressBar value={batchProgress(b)} />
                      <div className="muted small">
                        {formatCount(b.counts.completed)} de {formatCount(b.counts.total)}
                        {b.counts.failed > 0 ? ` · ${formatCount(b.counts.failed)} com erro` : ""}
                      </div>
                    </td>
                    <td className="muted small col-opt">{batchSummaryText(b)}</td>
                    <td className="nowrap" style={{ textAlign: "right" }}>
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        aria-pressed={b.id === openId}
                        onClick={() => onOpen(b.id)}
                      >
                        {b.id === openId ? "Aberto" : "Abrir"}
                      </button>
                    </td>
                  </tr>
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
