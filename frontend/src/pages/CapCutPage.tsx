import { useEffect, useRef, useState } from "react";
import { Play } from "lucide-react";
import { ApiError, api, messageOf } from "../api/client";
import type { CapCutBatchCreated, CapCutBatchMode, Media } from "../api/types";
import { BatchConfigForm } from "../components/capcut/BatchConfigForm";
import { BatchHistory } from "../components/capcut/BatchHistory";
import { BatchMonitor } from "../components/capcut/BatchMonitor";
import { BatchVideoPicker } from "../components/capcut/BatchVideoPicker";
import { CapCutJobsTable } from "../components/capcut/CapCutJobsTable";
import { CapCutSettingsForm } from "../components/capcut/CapCutSettingsForm";
import { CapCutStatusLine } from "../components/capcut/CapCutStatusLine";
import { CaptionStylesSection } from "../components/capcut/CaptionStylesSection";
import { Collapsible } from "../components/capcut/Collapsible";
import { LocalFilesDrop } from "../components/edit/LocalFilesDrop";
import { ErrorLine, PageHeader, SkeletonRows, Spinner } from "../components/ui";
import {
  type BatchEditForm,
  type BatchExportForm,
  type BatchPlan,
  type BatchSelection,
  BATCH_MODE_LABEL,
  DEFAULT_BATCH_EDIT,
  EMPTY_SELECTION,
  addIds,
  buildBatchRequest,
  estimateText,
  needsCaptionFallback,
  exportFormFrom,
  planBlocker,
  planLines,
  processButtonText,
  resolveEstimate,
  selectedText,
  selectionCount,
  skippedSummary,
  toggleOne,
  videoCount,
} from "../lib/capcutBatch";
import { nextPollMs } from "../lib/polling";
import { useDebounced } from "../lib/useDebounced";
import { useQuery } from "../lib/useQuery";
import { useToast } from "../lib/toast";
import { useUploadQueue } from "../lib/useUploadQueue";

/** `undefined` = mostrar o lote mais recente que ainda anda; `null` = nenhum. */
type Shown = number | null | undefined;

const PAGE_TITLE = "Edição CapCut";

/**
 * Edição CapCut em etapas: 1. escolher vídeos (biblioteca ou computador), 2. configurar, 3. resumo, 4. acompanhar
 * resultados. A barra de ação fixa no rodapé mostra a seleção, o modo e a estimativa e tem o botão "Processar N
 * vídeos". Modelos de legenda, configurações avançadas e a lista geral de jobs ficam em seções recolhíveis abaixo.
 */
export function CapCutPage() {
  const toast = useToast();
  useEffect(() => {
    const previous = document.title;
    document.title = `${PAGE_TITLE} · Media Tool`;
    return () => {
      document.title = previous;
    };
  }, []);
  const [statusPoll, setStatusPoll] = useState<number>(() => nextPollMs(null, false));
  const status = useQuery(
    "capcut-status",
    () =>
      api.capcutStatus().then((s) => {
        setStatusPoll((p) => nextPollMs(p, s.queue.running + s.queue.pending + s.queue.waiting > 0));
        return s;
      }),
    statusPoll,
  );
  const settings = useQuery("capcut-settings", () => api.capcutSettings());
  const [stylesVersion, setStylesVersion] = useState(0);
  const styles = useQuery(`capcut-styles:${stylesVersion}`, () => api.capcutStyles());
  const presets = useQuery("presets", () => api.listPresets());

  // Etapa 1: seleção. A ref acompanha o estado para os retornos assíncronos dos envios.
  const [tab, setTab] = useState<"library" | "local">("library");
  const [selection, setSelection] = useState<BatchSelection>(EMPTY_SELECTION);
  const selectionRef = useRef<BatchSelection>(EMPTY_SELECTION);
  const [libraryVersion, setLibraryVersion] = useState(0);
  const [uploadedCount, setUploadedCount] = useState(0);

  function commitSelection(next: BatchSelection) {
    selectionRef.current = next;
    setSelection(next);
  }

  const uploads = useUploadQueue((media: Media, duplicate: boolean) => {
    // Duplicata de um vídeo que saiu da lista ("Limpar lista"): volta a aparecer nela.
    if (duplicate) {
      api
        .showInCapcutLibrary([media.id])
        .then(() => setLibraryVersion((v) => v + 1))
        .catch((err: unknown) => toast.push("err", messageOf(err)));
    }
    const result = addIds(selectionRef.current, [media.id]);
    commitSelection(result.selection);
    if (result.leftFilter) {
      toast.push("info", "A seleção \"todos os filtrados\" virou a lista de vídeos marcados, com os enviados do computador.");
    }
    setUploadedCount((n) => n + 1);
    setLibraryVersion((v) => v + 1);
  });

  function removeUpload(key: string) {
    const media = uploads.remove(key);
    const sel = selectionRef.current;
    if (media && !sel.all && sel.ids.has(media.id)) commitSelection(toggleOne(sel, media.id));
  }

  // Etapa 2: configuração.
  const [mode, setMode] = useState<CapCutBatchMode>("caption");
  const [styleId, setStyleId] = useState<number | null>(null);
  const [edit, setEdit] = useState<BatchEditForm>(DEFAULT_BATCH_EDIT);
  const [exportDraft, setExportDraft] = useState<BatchExportForm | null>(null);
  const exportForm = exportDraft ?? (settings.data ? exportFormFrom(settings.data) : null);
  const [includeDone, setIncludeDone] = useState(false);

  // Etapa 3: resumo; o botão fica na barra de ação.
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [result, setResult] = useState<{ text: string; skipped: string | null; error: boolean } | null>(null);

  // Etapa 4: lote em destaque e histórico.
  const [shown, setShown] = useState<Shown>(undefined);
  const [newBatchId, setNewBatchId] = useState<number | null>(null);
  const resultsRef = useRef<HTMLElement>(null);
  const [historyVersion, setHistoryVersion] = useState(0);
  const latest = useQuery(`capcut-batch-latest:${historyVersion}`, () => api.listCapcutBatches({ limit: 1 }));
  const latestOpen = latest.data?.items.find((b) => b.status === "running" || b.status === "paused");
  const shownId = shown === undefined ? (latestOpen?.id ?? null) : shown;

  // Seções recolhíveis.
  const [stylesOpen, setStylesOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [jobsOpen, setJobsOpen] = useState(false);
  const stylesRef = useRef<HTMLDetailsElement>(null);

  const count = selectionCount(selection);
  const estimateCount = useDebounced(count, 400);
  const estimate = useQuery(`capcut-estimate:${mode}:${estimateCount}`, () =>
    estimateCount > 0 ? api.capcutEstimate(mode, estimateCount) : Promise.resolve(undefined),
  );
  // Editar e legendar sem histórico próprio: aproxima pela estimativa de "Legendar".
  const wantFallback = estimateCount > 0 && !estimate.loading && needsCaptionFallback(mode, estimate.data);
  const captionEstimate = useQuery(wantFallback ? `capcut-estimate:caption:${estimateCount}` : "capcut-estimate:off", () =>
    wantFallback ? api.capcutEstimate("caption", estimateCount) : Promise.resolve(undefined),
  );
  const shownEstimate = resolveEstimate(estimate.data, wantFallback ? captionEstimate.data : undefined);

  const plan: BatchPlan = {
    mode,
    selection,
    name,
    includeDone,
    styleId,
    exportForm,
    edit,
  };
  const styleName =
    styleId === null
      ? `padrão: ${styles.data?.find((s) => s.is_default)?.name ?? "embutido"}`
      : (styles.data?.find((s) => s.id === styleId)?.name ?? `#${styleId}`);
  const presetName = edit.presetId === null ? null : (presets.data?.find((p) => p.id === edit.presetId)?.name ?? null);
  const blocker =
    (count === 0 ? "Selecione vídeos na lista acima" : null) ??
    planBlocker(plan) ??
    (uploads.busy
      ? "Aguarde os envios do computador terminarem."
      : mode !== "edit" && !exportForm
        ? "Carregando as configurações de exportação…"
        : null);

  function openStyles() {
    setStylesOpen(true);
    window.requestAnimationFrame(() => stylesRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }

  async function process() {
    if (blocker || creating) return;
    setCreating(true);
    setResult(null);
    try {
      const created: CapCutBatchCreated = await api.createCapcutBatch(buildBatchRequest(plan));
      const total = created.batch.counts.total;
      setResult({
        text: `Lote "${created.batch.name}" criado com ${videoCount(total)}.`,
        skipped: skippedSummary(created.skipped),
        error: false,
      });
      toast.push("ok", `Processamento iniciado para ${videoCount(total)}.`);
      commitSelection(EMPTY_SELECTION);
      setName("");
      setShown(created.batch.id);
      setNewBatchId(created.batch.id);
      window.requestAnimationFrame(() => resultsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
      setHistoryVersion((v) => v + 1);
      setLibraryVersion((v) => v + 1);
      status.reload();
    } catch (err) {
      const skipped =
        err instanceof ApiError && Array.isArray(err.payload?.skipped)
          ? skippedSummary(err.payload.skipped as CapCutBatchCreated["skipped"])
          : null;
      setResult({ text: messageOf(err), skipped, error: true });
    } finally {
      setCreating(false);
    }
  }

  const estimateLine =
    estimateCount === count ? estimateText(shownEstimate.estimate, shownEstimate.approximate) : "calculando estimativa…";

  return (
    <div className="page">
      <PageHeader title={PAGE_TITLE} subtitle="Transcreva e legende vídeos em lote com o CapCut." />

      <CapCutStatusLine status={status.data} error={status.error} onReload={status.reload} />

      <section className="section cc-step" aria-labelledby="cc-step-1">
        <StepHead id="cc-step-1" n={1} title="Escolher vídeos" />
        <div className="tabs" role="tablist" aria-label="Origem dos vídeos">
          <button
            type="button"
            role="tab"
            id="cc-tab-library"
            aria-selected={tab === "library"}
            aria-controls="cc-panel-library"
            className={`tab${tab === "library" ? " on" : ""}`}
            onClick={() => setTab("library")}
          >
            Biblioteca
          </button>
          <button
            type="button"
            role="tab"
            id="cc-tab-local"
            aria-selected={tab === "local"}
            aria-controls="cc-panel-local"
            className={`tab${tab === "local" ? " on" : ""}`}
            onClick={() => setTab("local")}
          >
            Do computador
            {uploads.items.length > 0 ? <span className="count">{uploads.items.length}</span> : null}
          </button>
        </div>
        <div id="cc-panel-library" role="tabpanel" aria-labelledby="cc-tab-library" hidden={tab !== "library"}>
          <BatchVideoPicker selection={selection} onChange={commitSelection} version={libraryVersion} />
        </div>
        <div id="cc-panel-local" role="tabpanel" aria-labelledby="cc-tab-local" hidden={tab !== "local"}>
          <LocalFilesDrop
            queue={uploads}
            onRemove={removeUpload}
            onIgnored={(n) =>
              toast.push("info", n === 1 ? "Um arquivo já estava na lista." : `${n} arquivos já estavam na lista.`)
            }
            onFolderOthers={(n) =>
              toast.push("info", n === 1 ? "1 arquivo da pasta não é vídeo e ficou de fora." : `${n} arquivos da pasta não são vídeo e ficaram de fora.`)
            }
          />
          {uploadedCount > 0 ? (
            <p className="muted small" style={{ marginBottom: 0 }}>
              Os vídeos enviados entram na biblioteca e na seleção.
            </p>
          ) : null}
        </div>
      </section>

      <section className="section cc-step" aria-labelledby="cc-step-2">
        <StepHead id="cc-step-2" n={2} title="Configurar" />
        {styles.error ? <ErrorLine message={styles.error} onRetry={styles.reload} /> : null}
        {settings.error ? <ErrorLine message={settings.error} onRetry={settings.reload} /> : null}
        <BatchConfigForm
          mode={mode}
          onMode={setMode}
          styles={styles.data}
          styleId={styleId}
          onStyle={setStyleId}
          onEditStyles={openStyles}
          presets={presets.data}
          edit={edit}
          onEdit={setEdit}
          exportForm={exportForm}
          onExport={setExportDraft}
          includeDone={includeDone}
          onIncludeDone={setIncludeDone}
        />
      </section>

      <section className="section cc-step" aria-labelledby="cc-step-3">
        <StepHead id="cc-step-3" n={3} title="Resumo" />
        <ul className="cc-plan">
          {planLines(plan, styleName, presetName).map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <div className="field" style={{ maxWidth: 360 }}>
          <label className="field-label" htmlFor="cc-batch-name">
            Nome do lote (opcional)
          </label>
          <input
            id="cc-batch-name"
            className="input"
            maxLength={80}
            placeholder="Automático: Lote N · data e hora"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
      </section>

      <section ref={resultsRef} className="section cc-step" aria-labelledby="cc-step-4">
        <StepHead id="cc-step-4" n={4} title="Acompanhar resultados" />
        <div aria-live="polite">
          {result && !result.error ? (
            <div className="alert" style={{ marginBottom: 10 }}>
              <span className="grow">
                {result.text}
                {result.skipped ? (
                  <>
                    <br />
                    <span className="muted small">{result.skipped}</span>
                  </>
                ) : null}
              </span>
              <button type="button" className="btn btn-sm btn-ghost" onClick={() => setResult(null)}>
                Fechar
              </button>
            </div>
          ) : null}
        </div>
        {shownId !== null ? (
          <BatchMonitor
            key={shownId}
            highlight={shownId === newBatchId}
            batchId={shownId}
            onChanged={() => {
              // Fixa o lote mostrado: um lote cancelado ou concluído não some do painel ao recarregar.
              setShown((s) => (s === undefined ? shownId : s));
              setHistoryVersion((v) => v + 1);
              status.reload();
            }}
            onRemoved={() => setShown(null)}
          />
        ) : latest.data === undefined && !latest.error ? (
          <SkeletonRows rows={2} />
        ) : (
          <p className="muted small" style={{ marginTop: 0 }}>
            Nenhum lote em andamento. Abra um lote do histórico para ver os resultados.
          </p>
        )}
        <h3 style={{ margin: "16px 0 8px" }}>Histórico de lotes</h3>
        <BatchHistory
          openId={shownId}
          onOpen={(id) => setShown(id)}
          onRemoved={(ids) => {
            if (shownId !== null && ids.includes(shownId)) setShown(null);
          }}
          version={historyVersion}
        />
      </section>

      <Collapsible ref={stylesRef} title="Modelos de legenda" open={stylesOpen} onToggle={setStylesOpen}>
        <CaptionStylesSection settings={settings.data} onChanged={() => setStylesVersion((v) => v + 1)} />
      </Collapsible>

      <Collapsible title="Configurações avançadas" open={settingsOpen} onToggle={setSettingsOpen}>
        {settings.data ? (
          <CapCutSettingsForm
            key={JSON.stringify(settings.data)}
            initial={settings.data}
            models={status.data?.models}
            onSaved={() => {
              settings.reload();
              status.reload();
            }}
          />
        ) : settings.error ? (
          <ErrorLine message={settings.error} onRetry={settings.reload} />
        ) : (
          <SkeletonRows rows={4} />
        )}
      </Collapsible>

      <Collapsible title="Todos os jobs" open={jobsOpen} onToggle={setJobsOpen}>
        <CapCutJobsTable onChanged={status.reload} />
      </Collapsible>

      <div className="cc-actionbar" role="region" aria-label="Processar vídeos">
        <div className="cc-actionbar-info" aria-live="polite">
          <span>
            <strong>{count === 0 ? "Selecione vídeos na lista acima" : selectedText(count)}</strong> ·{" "}
            {BATCH_MODE_LABEL[mode]}
          </span>
          {count > 0 ? (
            <span className="small" style={blocker ? { color: "var(--warn)" } : { color: "var(--text-muted)" }}>
              {blocker ?? estimateLine}
            </span>
          ) : null}
          {result?.error ? (
            <span className="small" role="alert" style={{ color: "var(--err)" }}>
              {result.text}
              {result.skipped ? ` ${result.skipped}` : ""}
            </span>
          ) : null}
        </div>
        <div className="cc-actionbar-buttons">
          {count > 0 ? (
            <button type="button" className="btn btn-ghost" onClick={() => commitSelection(EMPTY_SELECTION)}>
              Limpar seleção
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn-primary cc-process-btn"
            disabled={blocker !== null || creating}
            onClick={() => void process()}
          >
            {creating ? <Spinner /> : <Play size={16} strokeWidth={1.75} aria-hidden="true" />}
            {processButtonText(count)}
          </button>
        </div>
      </div>
    </div>
  );
}

function StepHead({ id, n, title }: { id: string; n: number; title: string }) {
  return (
    <div className="section-head">
      <h2 id={id}>
        <span className="cc-step-n" aria-hidden="true">
          {n}
        </span>
        {title}
      </h2>
    </div>
  );
}
