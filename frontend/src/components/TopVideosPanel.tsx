import { type ReactNode, useEffect, useState } from "react";
import { Download, ExternalLink, Search, Trophy } from "lucide-react";
import { ApiError, api, messageOf } from "../api/client";
import type { Batch, CapCutImportSettings, EditSettings, TopVideoItem, TopVideosSearch } from "../api/types";
import { formatDate } from "../lib/format";
import type { PostProcessing } from "../lib/importOptions";
import { interpretLinksStartError } from "../lib/importLinks";
import {
  SCAN_DEFAULT,
  SCAN_OPTIONS,
  TOP_DEFAULT,
  TOP_MAX,
  TOP_POLL_MS,
  buildTopDownloadRequest,
  buildTopRequest,
  formatViews,
  isRunning,
  resolveTopCount,
  scanProgressText,
  topDownloadBlocker,
  topDownloadHint,
  topDownloadMessage,
  topResultSummary,
  topResultView,
  viewsLabel,
} from "../lib/topVideos";
import { parseProfileInput } from "../lib/profile";
import { useToast } from "../lib/toast";
import { Empty, ErrorLine, Field, Pill, Spinner, Thumb } from "./ui";

/**
 * Aba "Top vídeos": busca os Reels com mais visualizações de um perfil (aba Reels sem login, no backend) e baixa os
 * escolhidos como uma importação por links, com o mesmo bloco de Processamento da página.
 */
export function TopVideosPanel({
  sessionOk,
  paused,
  post,
  process,
  presetId,
  edit,
  capcut,
  editProblem,
  processing,
  searchId,
  onSearchId,
  onCreated,
  onFailed,
}: {
  sessionOk: boolean;
  /** Pausa global do Instagram vigente. */
  paused: boolean;
  post: PostProcessing;
  process: boolean;
  presetId: number | null;
  edit: EditSettings | null;
  capcut: CapCutImportSettings | null;
  editProblem: string | null;
  /** Bloco "Processamento" da tela, o mesmo dos outros modos. */
  processing: ReactNode;
  /** Id da última busca, guardado pela página: voltar para a aba recarrega o resultado pelo GET. */
  searchId: string | null;
  /** Precisa ser estável (ex.: o `set` de `useState`). */
  onSearchId: (id: string | null) => void;
  onCreated: (batch: Batch & { capcut_batch_id?: number | null }) => void;
  /** Precisa ser estável (ex.: `reload` de `useQuery`): também é chamado quando a busca falha. */
  onFailed: () => void;
}) {
  const toast = useToast();
  const [profileText, setProfileText] = useState("");
  const [topText, setTopText] = useState(String(TOP_DEFAULT));
  const [scanLimit, setScanLimit] = useState<number>(SCAN_DEFAULT);
  const [search, setSearch] = useState<TopVideosSearch | null>(null);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [pollError, setPollError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [submitting, setSubmitting] = useState(false);

  const choice = buildTopRequest(profileText, topText, scanLimit);
  const profileProblem = profileText.trim() === "" ? null : parseProfileInput(profileText);
  const countChoice = resolveTopCount(topText);
  const running = starting || isRunning(search);

  // Andamento: lê a busca a cada segundo enquanto ela estiver rodando.
  useEffect(() => {
    if (searchId === null) return undefined;
    let stopped = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const next = await api.topVideos(searchId);
        if (stopped) return;
        setSearch(next);
        setPollError(null);
        if (next.status === "running") timer = window.setTimeout(() => void tick(), TOP_POLL_MS);
        else if (next.status === "failed") onFailed();
      } catch (err) {
        if (stopped) return;
        if (err instanceof ApiError && err.status === 404) {
          // A busca sumiu (o Media Tool reiniciou): volta para o formulário, sem erro.
          setPollError(null);
          setSearch(null);
          onSearchId(null);
          return;
        }
        setPollError(messageOf(err));
        timer = window.setTimeout(() => void tick(), TOP_POLL_MS * 3);
      }
    };
    void tick();
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [searchId, onFailed, onSearchId]);

  async function startSearch() {
    if (!choice.ok || running || paused) return;
    setStarting(true);
    setPollError(null);
    try {
      const created = await api.startTopVideos(choice.body);
      setSelected(new Set());
      setSearch(null);
      onSearchId(created.id);
    } catch (err) {
      toast.push("err", messageOf(err));
      onFailed();
    } finally {
      setStarting(false);
    }
  }

  async function cancelSearch() {
    if (searchId === null) return;
    setCancelling(true);
    try {
      setSearch(await api.cancelTopVideos(searchId));
      // A busca termina na próxima requisição; o laço de andamento continua até o fim.
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setCancelling(false);
    }
  }

  const items = search?.items ?? [];
  const view = topResultView(search);
  const chosen = items.filter((item) => selected.has(item.code));
  const blockerFor = (list: TopVideoItem[]) =>
    topDownloadBlocker({ sessionOk, paused, submitting, items: list, process, presetId, editProblem });
  const hint = topDownloadHint(blockerFor(items));

  async function download(list: TopVideoItem[]) {
    const body = buildTopDownloadRequest(list, { processAfterDownload: process, presetId, edit, capcut });
    if (blockerFor(list) !== null || !body) return;
    setSubmitting(true);
    try {
      const batch = await api.createLinksImport(body);
      toast.push("ok", topDownloadMessage(body.links.length, post));
      onCreated(batch);
    } catch (err) {
      toast.push("err", interpretLinksStartError(err instanceof ApiError ? err : null).error);
      onFailed();
    } finally {
      setSubmitting(false);
    }
  }

  function toggle(code: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  }

  const allChosen = items.length > 0 && chosen.length === items.length;

  return (
    <div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void startSearch();
        }}
      >
        <div className="top-form">
          <Field
            label="Perfil"
            htmlFor="imp-top-profile"
            hint="usuario, @usuario ou o link do perfil."
            error={profileProblem && !profileProblem.ok ? profileProblem.error : null}
          >
            <input
              id="imp-top-profile"
              className="input mono"
              value={profileText}
              onChange={(e) => setProfileText(e.target.value)}
              placeholder="https://www.instagram.com/usuario/"
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
            />
          </Field>
          <Field
            label="Quantos"
            htmlFor="imp-top-count"
            hint={`De 1 a ${TOP_MAX}.`}
            error={countChoice.ok ? null : countChoice.error}
          >
            <input
              id="imp-top-count"
              className={`input${countChoice.ok ? "" : " invalid"}`}
              style={{ width: 80 }}
              inputMode="numeric"
              value={topText}
              onChange={(e) => setTopText(e.target.value)}
            />
          </Field>
          <Field label="Analisar os últimos" htmlFor="imp-top-scan">
            <select
              id="imp-top-scan"
              className="select"
              value={scanLimit}
              onChange={(e) => setScanLimit(Number(e.target.value))}
            >
              {SCAN_OPTIONS.map((n) => (
                <option key={n} value={n}>
                  {n} Reels
                </option>
              ))}
            </select>
          </Field>
        </div>
        <div className="muted small" style={{ marginTop: 8 }}>
          Lê a aba Reels do perfil sem usar a sua sessão, do mais novo para o mais antigo (uma consulta a cada 12
          Reels), e mostra os que têm mais visualizações. Reels fixados entram e aparecem marcados.
        </div>
        <div className="row" style={{ marginTop: 12, flexWrap: "wrap" }}>
          <button type="submit" className="btn btn-primary" disabled={running || paused || !choice.ok}>
            {starting ? <Spinner /> : <Search size={14} strokeWidth={1.75} aria-hidden="true" />}
            Buscar top vídeos
          </button>
          {paused ? (
            <span className="muted small">Instagram em pausa: a busca fica disponível quando a pausa terminar.</span>
          ) : null}
        </div>
      </form>

      {processing}

      <div className="top-results" role="region" aria-label="Top vídeos do perfil" aria-live="polite">
        {search && search.status === "running" ? (
          <div className="row" style={{ flexWrap: "wrap" }} role="status">
            <Spinner />
            <span className="grow">
              @{search.profile}: {scanProgressText(search.scanned)}
            </span>
            <button type="button" className="btn btn-sm" disabled={cancelling} onClick={() => void cancelSearch()}>
              {cancelling ? <Spinner /> : null}
              Cancelar
            </button>
          </div>
        ) : null}
        {pollError ? <ErrorLine message={pollError} /> : null}
        {view.error ? <ErrorLine message={view.error} /> : null}
        {search && view.showEmpty ? (
          <Empty title="Nenhum vídeo para mostrar" icon={Trophy}>
            <span>{topResultSummary(search)}</span>
          </Empty>
        ) : null}
        {search && view.showList ? (
            <>
              <div className="row top-toolbar">
                <span className="grow small">
                  <strong>@{search.profile}</strong> · {topResultSummary(search)}
                </span>
                <label className="row small" style={{ gap: 6 }}>
                  <input
                    type="checkbox"
                    checked={allChosen}
                    onChange={() => setSelected(allChosen ? new Set() : new Set(items.map((item) => item.code)))}
                  />
                  Selecionar todos
                </label>
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={chosen.length === 0 || blockerFor(chosen) !== null}
                  onClick={() => void download(chosen)}
                >
                  Baixar selecionados ({chosen.length})
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  disabled={blockerFor(items) !== null}
                  onClick={() => void download(items)}
                >
                  {submitting ? <Spinner /> : <Download size={14} strokeWidth={1.75} aria-hidden="true" />}
                  Baixar todos
                </button>
              </div>
              {hint ? <div className="muted small">{hint}</div> : null}
              <ol className="top-list">
                {items.map((item, index) => (
                  <li key={item.code} className={`top-item${selected.has(item.code) ? " selected" : ""}`}>
                    <input
                      type="checkbox"
                      aria-label={`Selecionar o vídeo #${index + 1}`}
                      checked={selected.has(item.code)}
                      onChange={() => toggle(item.code)}
                    />
                    <span className="top-rank">#{index + 1}</span>
                    <Thumb src={item.thumbnail_url} mediaType="video" size={56} />
                    <div className="top-info">
                      <strong title={viewsLabel(item.views)}>{formatViews(item.views)} visualizações</strong>
                      <span className="muted small">
                        {item.posted_at ? formatDate(item.posted_at) : "Data desconhecida"}
                        {item.pinned ? " · " : ""}
                        {item.pinned ? <Pill tone="accent">Fixado</Pill> : null}
                      </span>
                      <a className="small" href={item.url} target="_blank" rel="noopener noreferrer">
                        <ExternalLink size={12} strokeWidth={1.75} aria-hidden="true" /> Abrir no Instagram
                      </a>
                    </div>
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={blockerFor([item]) !== null}
                      onClick={() => void download([item])}
                    >
                      Baixar
                    </button>
                  </li>
                ))}
              </ol>
            </>
        ) : null}
      </div>
    </div>
  );
}
