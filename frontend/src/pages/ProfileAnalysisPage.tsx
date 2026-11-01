import { memo, useCallback, useDeferredValue, useId, useMemo, useState, type FormEvent } from "react";
import { api, messageOf } from "../api/client";
import type { ProfileAnalysis, ProfileAnalysisItem } from "../api/types";
import { Link } from "../components/Layout";
import { ConfirmDialog, Empty, ErrorLine, Field, Pill, ProgressBar, SkeletonRows, Spinner } from "../components/ui";
import { formatCount } from "../lib/format";
import {
  ANALYSIS_STATUS_LABEL,
  EMPTY_CELL,
  analysisIdFromSearch,
  analysisIndicators,
  analysisPollMs,
  analysisReportUrl,
  analysisRoute,
  analysisTone,
  confirmedCount,
  clearedText,
  compactProfileText,
  countProfileLines,
  endNoticeText,
  estimateText,
  ignoredText,
  isAnalysisActive,
  itemCells,
  lineCountText,
  overLimitText,
  pageInfo,
  pageOffset,
  parsePage,
  progressFraction,
  progressText,
  startErrorText,
  waitingText,
  type Cell,
} from "../lib/profileAnalysis";
import { navigate, ROUTES, useSearch } from "../lib/router";
import { useToast } from "../lib/toast";
import { useQuery } from "../lib/useQuery";

const HISTORY_PAGE = 20;
const ITEMS_PAGE = 50;

export function ProfileAnalysisPage() {
  const urlId = analysisIdFromSearch(useSearch());
  // Análise aberta encontrada pelo histórico: continua na tela depois de terminar, mesmo sem ?id= na URL.
  const [watchedId, setWatchedId] = useState<number | null>(null);
  const [offset, setOffset] = useState(0);
  // Liga depois da primeira leitura: uma falha inicial (ou ?id= inexistente) não fica repetindo sozinha.
  const [pollMs, setPollMs] = useState<number | null>(null);
  const history = useQuery(
    `profile-analyses:${offset}`,
    async () => {
      const page = await api.listProfileAnalyses(HISTORY_PAGE, offset);
      if (page.open_id !== null) setWatchedId(page.open_id);
      setPollMs((prev) => analysisPollMs(prev, page.open_id !== null));
      return page;
    },
    pollMs,
  );
  const currentId = urlId ?? watchedId;
  const reloadHistory = history.reload;
  const toast = useToast();
  const [toDelete, setToDelete] = useState<ProfileAnalysis | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const [removing, setRemoving] = useState(false);

  /** A análise aberta no painel acabou de ser apagada: fecha o painel e volta à página sem `?id=`. */
  function closePanel() {
    setWatchedId(null);
    navigate(ROUTES.profiles);
  }

  async function removeOne(analysis: ProfileAnalysis) {
    setRemoving(true);
    try {
      await api.deleteProfileAnalysis(analysis.id);
      toast.push("ok", `Análise nº ${analysis.id} apagada.`);
      if (analysis.id === currentId) closePanel();
      // Era a única linha desta página do histórico: volta uma página.
      if (history.data?.items.length === 1 && offset > 0) setOffset(Math.max(0, offset - HISTORY_PAGE));
      else reloadHistory();
    } catch (err) {
      toast.push("err", messageOf(err));
      reloadHistory();
    } finally {
      setRemoving(false);
      setToDelete(null);
    }
  }

  async function removeAll() {
    setRemoving(true);
    try {
      const result = await api.clearProfileHistory();
      toast.push("ok", clearedText(result));
      // O painel de uma análise aberta volta sozinho pelo `open_id` do histórico.
      closePanel();
      if (offset > 0) setOffset(0);
      else reloadHistory();
    } catch (err) {
      toast.push("err", messageOf(err));
      reloadHistory();
    } finally {
      setRemoving(false);
      setConfirmClear(false);
    }
  }

  // Referência estável: o polling do histórico não redesenha o cartão de entrada (`memo`).
  const onStarted = useCallback(
    (id: number) => {
      setOffset(0);
      navigate(analysisRoute(id));
      reloadHistory();
    },
    [reloadHistory],
  );

  return (
    <div className="page">
      <div className="page-head">
        <h1>Análise de Perfis</h1>
      </div>
      <InputCard onStarted={onStarted} />
      {currentId !== null ? (
        <AnalysisPanel
          key={currentId}
          id={currentId}
          onChanged={history.reload}
          onClose={() => {
            setWatchedId(null);
            navigate(ROUTES.profiles);
          }}
        />
      ) : null}
      <section style={{ marginTop: 24 }}>
        <div className="section-head">
          <h2>Histórico</h2>
          {history.loading && history.data ? <Spinner /> : null}
          {history.data && history.data.total > 0 ? (
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              style={{ marginLeft: "auto" }}
              onClick={() => setConfirmClear(true)}
            >
              Limpar histórico
            </button>
          ) : null}
        </div>
        {history.error ? <ErrorLine message={history.error} onRetry={history.reload} /> : null}
        {history.data === undefined ? (
          history.error ? null : (
            <SkeletonRows rows={3} />
          )
        ) : history.data.total === 0 ? (
          <Empty title="Nenhuma análise ainda" />
        ) : (
          <>
            <HistoryTable items={history.data.items} currentId={currentId} onDelete={setToDelete} />
            <Pager start={offset} total={history.data.total} size={HISTORY_PAGE} onGo={setOffset} />
          </>
        )}
      </section>
      {toDelete ? (
        <ConfirmDialog
          title={`Apagar a análise nº ${toDelete.id}?`}
          confirmLabel="Apagar"
          danger
          focusCancel
          busy={removing}
          onConfirm={() => void removeOne(toDelete)}
          onCancel={() => setToDelete(null)}
        >
          Os resultados de {formatCount(toDelete.total)} perfis e o relatório desta análise serão apagados. Não dá para
          desfazer.
        </ConfirmDialog>
      ) : null}
      {confirmClear ? (
        <ConfirmDialog
          title="Limpar o histórico?"
          confirmLabel="Limpar histórico"
          danger
          focusCancel
          busy={removing}
          onConfirm={() => void removeAll()}
          onCancel={() => setConfirmClear(false)}
        >
          Todas as análises encerradas e seus relatórios serão apagados. Análise em andamento não é apagada. Não dá para
          desfazer.
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

const InputCard = memo(function InputCard({ onStarted }: { onStarted: (id: number) => void }) {
  const inputId = useId();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Contagem fora da digitação: com 10.000 linhas ela não pode travar o campo.
  const deferredText = useDeferredValue(text);
  const count = useMemo(() => countProfileLines(deferredText), [deferredText]);
  const overLimit = overLimitText(count);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await api.createProfileAnalysis(compactProfileText(text));
      setText("");
      onStarted(created.id);
    } catch (err) {
      setError(startErrorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel" onSubmit={(e) => void submit(e)}>
      <Field label="Cole os @ ou links, um por linha" htmlFor={inputId} error={error}>
        <textarea
          id={inputId}
          className="input textarea"
          rows={6}
          spellCheck={false}
          placeholder={"@usuario\nhttps://www.instagram.com/usuario/"}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setError(null);
          }}
        />
      </Field>
      <div className="row between" style={{ marginTop: 10 }}>
        <span className="muted small" aria-live="polite" style={overLimit ? { color: "var(--err)" } : undefined}>
          {lineCountText(count)}
          {overLimit ? ` · ${overLimit}` : null}
        </span>
        <button type="submit" className="btn btn-primary" disabled={busy || count.profiles === 0}>
          {busy ? <Spinner /> : null}
          Analisar
        </button>
      </div>
    </form>
  );
});

function AnalysisPanel({ id, onChanged, onClose }: { id: number; onChanged: () => void; onClose: () => void }) {
  const toast = useToast();
  const [offset, setOffset] = useState(0);
  const [pollMs, setPollMs] = useState<number | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [resuming, setResuming] = useState(false);
  const query = useQuery(
    `profile-analysis:${id}:${offset}`,
    async () => {
      // Nesta ordem: análise já encerrada garante que os itens lidos depois dela são os finais.
      const analysis = await api.getProfileAnalysis(id);
      const items = await api.listProfileAnalysisItems(id, ITEMS_PAGE, offset);
      setPollMs((prev) => analysisPollMs(prev, isAnalysisActive(analysis.status)));
      return { analysis, items, offset };
    },
    pollMs,
  );

  async function cancel() {
    setCancelling(true);
    try {
      await api.cancelProfileAnalysis(id);
      toast.push("ok", "Cancelamento solicitado.");
      query.reload();
      onChanged();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setCancelling(false);
    }
  }

  async function resume() {
    setResuming(true);
    try {
      await api.resumeProfileAnalysis(id);
      toast.push("ok", "Análise retomada.");
      query.reload();
      onChanged();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setResuming(false);
    }
  }

  if (query.data === undefined) {
    return (
      <div className="panel" style={{ marginTop: 14 }}>
        {query.error ? <ErrorLine message={query.error} onRetry={query.reload} /> : <SkeletonRows rows={2} />}
      </div>
    );
  }

  const { analysis, items } = query.data;
  const active = isAnalysisActive(analysis.status);
  const estimate = estimateText(analysis);
  const waiting = waitingText(analysis);
  const notice = endNoticeText(analysis);
  const ignored = ignoredText(analysis);

  return (
    <div className="panel" style={{ marginTop: 14 }}>
      <div className="row between">
        <div className="row">
          <strong style={{ fontSize: 14 }}>Análise nº {analysis.id}</strong>
          <Pill tone={analysisTone(analysis.status)}>{ANALYSIS_STATUS_LABEL[analysis.status]}</Pill>
        </div>
        <div className="row">
          {analysis.resumable ? (
            <button
              type="button"
              className="btn btn-sm btn-primary"
              disabled={resuming}
              onClick={() => void resume()}
            >
              {resuming ? <Spinner /> : null}
              Retomar
            </button>
          ) : null}
          {active ? (
            <button type="button" className="btn btn-sm" disabled={cancelling} onClick={() => void cancel()}>
              {cancelling ? <Spinner /> : null}
              Cancelar
            </button>
          ) : (
            <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>
              Fechar
            </button>
          )}
          <ReportLink analysis={analysis} label="Baixar relatório (ZIP)" />
        </div>
      </div>

      <div className="row small" role="group" aria-label="Indicadores" style={{ marginTop: 12 }}>
        {analysisIndicators(analysis).map((indicator) => (
          <span key={indicator.label} className="muted" title={indicator.title}>
            {indicator.label}: <strong>{formatCount(indicator.value)}</strong>
          </span>
        ))}
      </div>
      <div role="group" aria-label="Progresso geral" style={{ marginTop: 10 }}>
        <ProgressBar value={progressFraction(analysis)} />
      </div>
      <div className="row muted small" style={{ marginTop: 6 }} aria-live="polite">
        <span style={{ color: "var(--text)" }}>{progressText(analysis)}</span>
        {analysis.running_username ? <span>Consultando @{analysis.running_username}</span> : null}
        {estimate ? <span>Tempo estimado: {estimate}</span> : null}
        {active && analysis.cautious_pace ? <span>Ritmo reduzido por segurança</span> : null}
      </div>
      {waiting ? (
        <div className="alert warn small" role="status" style={{ marginTop: 10 }}>
          {waiting}
        </div>
      ) : null}
      {notice ? (
        <div className="alert warn small" role="status" style={{ marginTop: 10 }}>
          {notice}
        </div>
      ) : null}
      {ignored ? (
        <div className="dim small" style={{ marginTop: 6 }}>
          {ignored}
        </div>
      ) : null}

      {query.error ? (
        <div style={{ marginTop: 10 }}>
          <ErrorLine message={query.error} onRetry={query.reload} />
        </div>
      ) : null}
      <div className="table-wrap" style={{ marginTop: 12 }}>
        <table className="table">
          <thead>
            <tr>
              <th>Perfil</th>
              <th>Posts</th>
              <th>Views</th>
              <th>Stories</th>
              <th>Destaques</th>
              <th>Status</th>
              <th>Data</th>
              <th>Horário</th>
            </tr>
          </thead>
          <tbody>
            {items.items.map((item) => (
              <ItemRow key={item.position} item={item} />
            ))}
          </tbody>
        </table>
      </div>
      <Pager start={query.data.offset} total={items.total} size={ITEMS_PAGE} onGo={setOffset} />
    </div>
  );
}

function ItemRow({ item }: { item: ProfileAnalysisItem }) {
  const cells = itemCells(item);
  return (
    <tr>
      <td className="cell-trunc" title={item.display_name ?? undefined}>
        <div className="truncate">@{item.username}</div>
        {item.display_name ? <div className="dim small truncate">{item.display_name}</div> : null}
      </td>
      <td className="nowrap">
        <CellView cell={cells.posts} />
      </td>
      <td className="nowrap">
        <CellView cell={cells.views} />
      </td>
      <td>
        <CellView cell={cells.stories} />
      </td>
      <td>
        <CellView cell={cells.highlights} />
      </td>
      <td>
        <CellView cell={cells.status} />
      </td>
      <td className="muted nowrap">
        <CellView cell={cells.date} />
      </td>
      <td className="muted nowrap">
        <CellView cell={cells.time} />
      </td>
    </tr>
  );
}

function CellView({ cell }: { cell: Cell }) {
  if (cell.tone) {
    return (
      <span title={cell.title}>
        <Pill tone={cell.tone}>{cell.text}</Pill>
      </span>
    );
  }
  return (
    <span className={cell === EMPTY_CELL ? "dim" : undefined} title={cell.title}>
      {cell.text}
    </span>
  );
}

function ReportLink({ analysis, label }: { analysis: ProfileAnalysis; label: string }) {
  if (!analysis.report_ready) {
    return (
      <button type="button" className="btn btn-sm" disabled title="Disponível quando a análise terminar">
        {label}
      </button>
    );
  }
  return (
    <a className="btn btn-sm" href={analysisReportUrl(analysis.id)} download>
      {label}
    </a>
  );
}

function HistoryTable({
  items,
  currentId,
  onDelete,
}: {
  items: ProfileAnalysis[];
  currentId: number | null;
  onDelete: (analysis: ProfileAnalysis) => void;
}) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Nº</th>
            <th>Data</th>
            <th>Hora</th>
            <th className="num">Perfis</th>
            <th className="num">Confirmados</th>
            <th>Status</th>
            <th aria-label="Abrir" />
            <th aria-label="Baixar" />
            <th aria-label="Apagar" />
          </tr>
        </thead>
        <tbody>
          {items.map((analysis) => (
            <tr key={analysis.id} className={analysis.id === currentId ? "selected" : undefined}>
              <td className="muted">{analysis.id}</td>
              <td className="nowrap">{analysis.created_date ?? "—"}</td>
              <td className="muted nowrap">{analysis.created_time ?? "—"}</td>
              <td className="num">{formatCount(analysis.total)}</td>
              <td className="num">{formatCount(confirmedCount(analysis.summary))}</td>
              <td>
                <Pill tone={analysisTone(analysis.status)}>{ANALYSIS_STATUS_LABEL[analysis.status]}</Pill>
              </td>
              <td>
                <Link to={analysisRoute(analysis.id)} className="btn btn-sm btn-ghost">
                  Abrir
                </Link>
              </td>
              <td>
                <ReportLink analysis={analysis} label="Baixar" />
              </td>
              <td>
                {isAnalysisActive(analysis.status) ? null : (
                  <button type="button" className="btn btn-sm btn-ghost" onClick={() => onDelete(analysis)}>
                    Apagar
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Pager({
  start,
  total,
  size,
  onGo,
}: {
  start: number;
  total: number;
  size: number;
  onGo: (offset: number) => void;
}) {
  // Texto em edição no campo: o polling redesenha a linha sem apagar o que está sendo digitado.
  const [draft, setDraft] = useState<string | null>(null);
  if (total <= size) return null;
  const { page, pages } = pageInfo(start, size, total);

  // Troca de página volta ao topo da tela: a nova lista começa lá em cima, não onde o botão estava.
  function go(offset: number) {
    onGo(offset);
    document.querySelector(".main")?.scrollTo(0, 0);
  }

  function jump() {
    const target = draft === null ? null : parsePage(draft);
    setDraft(null);
    if (target !== null) go(pageOffset(target, size, total));
  }

  return (
    <div className="row" style={{ marginTop: 10, justifyContent: "flex-end" }}>
      <span className="muted small">
        Página {formatCount(page)} de {formatCount(pages)}
      </span>
      <button type="button" className="btn btn-sm" disabled={page === 1} onClick={() => go(0)}>
        Primeira
      </button>
      <button type="button" className="btn btn-sm" disabled={page === 1} onClick={() => go(start - size)}>
        Anterior
      </button>
      <button type="button" className="btn btn-sm" disabled={page === pages} onClick={() => go(start + size)}>
        Próxima
      </button>
      <button
        type="button"
        className="btn btn-sm"
        disabled={page === pages}
        onClick={() => go(pageOffset(pages, size, total))}
      >
        Última
      </button>
      <label className="row muted small">
        Ir para
        <input
          className="input"
          type="number"
          min={1}
          max={pages}
          style={{ width: 80 }}
          value={draft ?? String(page)}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={jump}
          onKeyDown={(e) => {
            if (e.key === "Enter") jump();
          }}
        />
      </label>
    </div>
  );
}
