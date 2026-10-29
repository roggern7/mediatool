import { useState } from "react";
import { ExternalLink, FolderOpen, Trash2, Wand2, X } from "lucide-react";
import { api, messageOf } from "../api/client";
import type { ExportContent, Media, MediaType, MediaView, Preset, Rendition } from "../api/types";
import { Link } from "../components/Layout";
import { ExportContentSelect, ExportStatus } from "../components/ExportPanel";
import { PresetPickerDialog } from "../components/PresetPicker";
import { ConfirmDialog, Empty, ErrorLine, Pill, SkeletonRows, Spinner, Thumb } from "../components/ui";
import {
  formatBytes,
  formatCount,
  formatDate,
  formatDateTime,
  formatDuration,
  formatFps,
  formatResolution,
  formatVideoCodec,
} from "../lib/format";
import { ACTIVE_BATCH, JOB_STATUS_LABEL, TERMINAL_JOB, jobTone } from "../lib/labels";
import { MAX_BULK_PROCESS, canOfferSelectAll, processByFilterBody, withinBulkCap } from "../lib/filterSelection";
import {
  exportContentNote,
  exportSelectedLabel,
  initialLibrarySearch,
  isExportActive,
  librarySelectionSource,
  zipSelectedLabel,
} from "../lib/exportFolder";
import { useExportFlow } from "../lib/useExportFlow";
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
import { nextPollMs } from "../lib/polling";
import { ROUTES } from "../lib/router";
import { useDebounced } from "../lib/useDebounced";
import { useQuery } from "../lib/useQuery";
import { useToast } from "../lib/toast";

const PAGE_SIZE = 50;

const VIEWS: { value: MediaView; label: string }[] = [
  { value: "all", label: "Todos" },
  { value: "originals", label: "Originais" },
  { value: "processed", label: "Processados" },
];

type TypeFilter = "all" | MediaType;

type Dialog =
  | { kind: "process"; mediaIds: number[]; reprocess: boolean }
  | { kind: "process-filter-preset"; total: number }
  | { kind: "process-filter-confirm"; total: number; presetId: number }
  | { kind: "delete-media"; media: Media }
  | { kind: "delete-rendition"; rendition: Rendition };

function presetNames(m: Media): string {
  const names = Array.from(new Set(m.renditions.map((r) => r.preset_name ?? "Preset removido")));
  return names.length > 0 ? names.join(", ") : "-";
}

export function LibraryPage() {
  const toast = useToast();
  const [view, setView] = useState<MediaView>("all");
  // O que "Baixar ZIP"/"Exportar selecionados" levam; na visão "Originais" é sempre originais.
  const [exportContentChoice, setExportContentChoice] = useState<ExportContent>("originals");
  const exportContent: ExportContent = view === "originals" ? "originals" : exportContentChoice;
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  // "Ver na Biblioteca" de uma importação abre a tela já com a busca pelo perfil (?perfil=usuario).
  const [search, setSearch] = useState(() => initialLibrarySearch(window.location.search));
  const [pager, setPager] = useState<Pager>({ scope: "", stack: FIRST_PAGE });
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  // Todos os resultados do filtro atual (processados no servidor, sem carregar os ids).
  const [allByFilter, setAllByFilter] = useState(false);
  const [watchPoll, setWatchPoll] = useState<number>(() => nextPollMs(null, false));
  const [detailRef, setDetailRef] = useState<{ mediaId: number; renditionId: number | null } | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [busy, setBusy] = useState(false);
  const [pollMs, setPollMs] = useState<number | null>(null);
  // Retoma o ZIP/exportação da Biblioteca em andamento, ou volta a oferecer o ZIP pronto, ao reabrir a tela.
  const exportFlow = useExportFlow({ resumeFor: { kind: "library" } });

  const q = useDebounced(search.trim().replace(/^@/, ""), 300);
  const scope = JSON.stringify({ view, typeFilter, q });
  const stack = stackFor(pager, scope);
  const cursor = currentCursor(stack);
  const onFirstPage = stack.length === 1;
  const filterQuery = { view, media_type: typeFilter === "all" ? undefined : typeFilter, q: q || undefined };
  const list = useQuery(
    `media:${scope}:${JSON.stringify(cursor)}`,
    async () => {
      const page = await api.listMedia({ ...filterQuery, limit: PAGE_SIZE, ...cursorQuery(cursor) });
      const anyActive = page.items.some((m) => m.last_job && !TERMINAL_JOB.has(m.last_job.status));
      setPollMs(anyActive ? 4_000 : null);
      if (shouldResetToFirst(page, stack)) setPager({ scope, stack: FIRST_PAGE });
      return page;
    },
    pollMs,
  );
  // Durante importação ativa: na primeira página, atualiza a lista; nas outras, só conta as novas mídias.
  const watch = useQuery(
    `media-watch:${scope}:${onFirstPage}`,
    async () => {
      const batches = await api.listBatches(5, 0, "instagram_import");
      const importing = batches.items.some((b) => b.source === "instagram_import" && ACTIVE_BATCH.has(b.status));
      setWatchPoll((prev) => nextPollMs(prev, importing));
      if (!importing) return null;
      if (onFirstPage) {
        if (!list.loading) list.reload();
        return null;
      }
      const head = await api.listMedia({ ...filterQuery, limit: 1 });
      return head.total;
    },
    watchPoll,
  );
  const presets = useQuery("presets", () => api.listPresets());
  const settings = useQuery("settings", () => api.getSettings());

  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const detail = detailRef !== null ? (items.find((m) => m.id === detailRef.mediaId) ?? null) : null;
  const processedView = view === "processed";
  const rows: { media: Media; rendition: Rendition | null }[] = processedView
    ? items.flatMap((m) => m.renditions.map((r) => ({ media: m, rendition: r })))
    : items.map((m) => ({ media: m, rendition: null }));
  const filtered = view !== "all" || typeFilter !== "all" || q !== "";
  const selectedIds = items.filter((m) => selected.has(m.id)).map((m) => m.id);
  const allChecked = allByFilter || (items.length > 0 && selectedIds.length === items.length);
  const offerSelectAll = !allByFilter && !processedView && canOfferSelectAll(items.length, selectedIds.length, total);
  const nextPage = list.data ? nextCursor(list.data, stack, PAGE_SIZE) : null;
  const start = pageStart(stack, PAGE_SIZE);
  const exportActive = isExportActive(exportFlow.current);
  const newCount = typeof watch.data === "number" && list.data ? Math.max(0, watch.data - list.data.total) : 0;

  function goTo(nextStack: PageCursor[]) {
    setPager({ scope, stack: nextStack });
  }

  function clearSelection() {
    setSelected(new Set());
    setAllByFilter(false);
  }

  function changeFilter(fn: () => void) {
    fn();
    setPager({ scope: "", stack: FIRST_PAGE });
    clearSelection();
  }

  function toggle(id: number) {
    if (allByFilter) {
      // Sai do modo "todos os resultados" e mantém a página atual sem o item desmarcado.
      setAllByFilter(false);
      setSelected(new Set(items.map((m) => m.id).filter((x) => x !== id)));
      return;
    }
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function open(media: Media, target: "file" | "folder", rendition?: Rendition) {
    try {
      await api.openMedia(media.id, target, rendition?.id);
      toast.push("ok", target === "folder" ? "Pasta aberta no Explorer." : "Arquivo aberto.");
    } catch (err) {
      toast.push("err", messageOf(err));
    }
  }

  async function openLibraryFolder() {
    try {
      await api.openLibraryFolder();
      toast.push("ok", "Pasta da Biblioteca aberta no Explorer.");
    } catch (err) {
      toast.push("err", messageOf(err));
    }
  }

  /** Exporta a seleção atual (ids da página ou todos os resultados do filtro, resolvidos no servidor). */
  function exportSelected(mode: "pick" | "zip" = "pick") {
    const source = librarySelectionSource({ allByFilter, view, typeFilter, q, selectedIds });
    if (source) void exportFlow.start(source, mode, exportContent);
  }

  async function runDialog(presetId?: number) {
    if (!dialog) return;
    setBusy(true);
    try {
      if (dialog.kind === "process" && presetId !== undefined) {
        const [onlyId] = dialog.mediaIds;
        if (dialog.mediaIds.length === 1 && onlyId !== undefined) {
          await api.processMedia(onlyId, presetId);
          toast.push("ok", "Processamento adicionado à fila.");
        } else {
          await api.processMediaBatch(dialog.mediaIds, presetId);
          toast.push("ok", `${formatCount(dialog.mediaIds.length)} itens adicionados à fila.`);
          setSelected(new Set());
        }
        setPollMs(4_000);
      } else if (dialog.kind === "process-filter-confirm") {
        const batch = await api.processByFilter(processByFilterBody({ view, typeFilter, q }, dialog.presetId));
        toast.push("ok", `${formatCount(batch.found || dialog.total)} itens adicionados à fila.`);
        clearSelection();
        setPollMs(4_000);
      } else if (dialog.kind === "delete-media") {
        await api.deleteMedia(dialog.media.id);
        toast.push("ok", "Item excluído.");
        if (detailRef?.mediaId === dialog.media.id) setDetailRef(null);
        setSelected((prev) => {
          const next = new Set(prev);
          next.delete(dialog.media.id);
          return next;
        });
      } else if (dialog.kind === "delete-rendition") {
        await api.deleteRendition(dialog.rendition.id);
        toast.push("ok", "Arquivo processado excluído.");
      }
      setDialog(null);
      list.reload();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Biblioteca</h1>
        <div className="row">
          {list.loading && list.data ? <Spinner /> : null}
          <span className="muted small">
            {list.data ? (processedView ? `${formatCount(total)} mídias processadas` : `${formatCount(total)} itens`) : null}
          </span>
          <button
            type="button"
            className="btn btn-sm"
            title="Abre no Explorer a pasta onde os arquivos originais ficam guardados"
            onClick={() => void openLibraryFolder()}
          >
            <FolderOpen size={13} strokeWidth={1.75} aria-hidden="true" />
            Abrir pasta da Biblioteca
          </button>
        </div>
      </div>

      <div className="tabs" role="tablist">
        {VIEWS.map((v) => (
          <button
            key={v.value}
            type="button"
            role="tab"
            aria-selected={view === v.value}
            className={`tab${view === v.value ? " on" : ""}`}
            onClick={() => changeFilter(() => setView(v.value))}
          >
            {v.label}
          </button>
        ))}
      </div>

      <div className="toolbar">
        <div className="seg" role="group" aria-label="Tipo">
          {(
            [
              ["all", "Todos"],
              ["video", "Vídeos"],
              ["image", "Imagens"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={typeFilter === value ? "on" : ""}
              aria-pressed={typeFilter === value}
              onClick={() => changeFilter(() => setTypeFilter(value))}
            >
              {label}
            </button>
          ))}
        </div>
        <input
          className="input"
          style={{ width: 220 }}
          placeholder="Buscar por @perfil"
          value={search}
          onChange={(e) => changeFilter(() => setSearch(e.target.value))}
          spellCheck={false}
          aria-label="Buscar por perfil"
        />
      </div>

      {newCount > 0 ? (
        <div className="row" style={{ marginBottom: 10 }}>
          <span className="muted small">
            {newCount === 1 ? "1 nova mídia" : `${formatCount(newCount)} novas mídias`}
          </span>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => goTo(FIRST_PAGE)}>
            Ver as novas
          </button>
        </div>
      ) : null}

      {allByFilter ? (
        <div className="selection-bar">
          <span className="grow">
            Todos os {formatCount(total)} resultados selecionados
            {!withinBulkCap(total) ? (
              <span className="tone-warn small">
                {" "}
                · Acima do limite de {formatCount(MAX_BULK_PROCESS)} por vez; refine os filtros.
              </span>
            ) : null}
          </span>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            disabled={!withinBulkCap(total)}
            onClick={() => setDialog({ kind: "process-filter-preset", total })}
          >
            <Wand2 size={13} strokeWidth={1.75} aria-hidden="true" />
            Processar com preset (opcional)
          </button>
          {view !== "originals" ? (
            <ExportContentSelect value={exportContentChoice} onChange={setExportContentChoice} />
          ) : null}
          <button
            type="button"
            className="btn btn-sm"
            disabled={total === 0 || exportFlow.busy || exportActive}
            title="Prepara um arquivo .zip com todos os resultados do filtro e inicia o download"
            aria-label={zipSelectedLabel(total)}
            onClick={() => exportSelected("zip")}
          >
            Baixar ZIP
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={total === 0 || exportFlow.busy || exportActive}
            title={exportContentNote(exportContent)}
            aria-label={exportSelectedLabel(total)}
            onClick={() => exportSelected()}
          >
            Exportar selecionados
          </button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={clearSelection}>
            Limpar seleção
          </button>
        </div>
      ) : selectedIds.length > 0 ? (
        <div className="selection-bar">
          <span className="grow">
            {formatCount(selectedIds.length)} selecionados
            {offerSelectAll ? (
              <>
                {" "}
                <button type="button" className="btn btn-sm btn-ghost" onClick={() => setAllByFilter(true)}>
                  Selecionar todos os {formatCount(total)} resultados
                </button>
              </>
            ) : null}
          </span>
          <button
            type="button"
            className="btn btn-sm btn-primary"
            onClick={() => setDialog({ kind: "process", mediaIds: selectedIds, reprocess: false })}
          >
            <Wand2 size={13} strokeWidth={1.75} aria-hidden="true" />
            Processar com preset (opcional)
          </button>
          {view !== "originals" ? (
            <ExportContentSelect value={exportContentChoice} onChange={setExportContentChoice} />
          ) : null}
          <button
            type="button"
            className="btn btn-sm"
            disabled={exportFlow.busy || exportActive}
            title="Prepara um arquivo .zip com as mídias selecionadas e inicia o download"
            aria-label={zipSelectedLabel(selectedIds.length)}
            onClick={() => exportSelected("zip")}
          >
            Baixar ZIP
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={exportFlow.busy || exportActive}
            title={exportContentNote(exportContent)}
            aria-label={exportSelectedLabel(selectedIds.length)}
            onClick={() => exportSelected()}
          >
            Exportar selecionados
          </button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={clearSelection}>
            Limpar seleção
          </button>
        </div>
      ) : null}
      <ExportStatus flow={exportFlow} />

      <div className={`library${detail ? " with-detail" : ""}`}>
        <div style={{ minWidth: 0 }}>
          {list.data === undefined ? (
            list.error ? (
              <ErrorLine message={list.error} onRetry={list.reload} />
            ) : (
              <SkeletonRows rows={6} />
            )
          ) : rows.length === 0 ? (
            processedView && typeFilter === "all" && q === "" ? (
              <Empty title="Nenhuma mídia processada">
                <span className="muted small">
                  O processamento é opcional. Os originais baixados ficam na aba Originais.
                </span>
              </Empty>
            ) : filtered ? (
              <Empty title="Nada encontrado com esses filtros" />
            ) : (
              <Empty title="Biblioteca vazia">
                <Link to={ROUTES.import}>Baixar do Instagram</Link>
              </Empty>
            )
          ) : (
            <>
              {list.error ? <ErrorLine message={list.error} onRetry={list.reload} /> : null}
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      {processedView ? null : (
                        <th style={{ width: 28 }}>
                          <input
                            type="checkbox"
                            aria-label="Selecionar todos"
                            checked={allChecked}
                            onChange={() =>
                              allChecked ? clearSelection() : setSelected(new Set(items.map((m) => m.id)))
                            }
                          />
                        </th>
                      )}
                      <th />
                      <th className="col-opt">Tipo</th>
                      <th>Perfil</th>
                      <th className="col-opt">{processedView ? "Processado em" : "Data"}</th>
                      <th className="col-opt">Duração</th>
                      <th className="col-opt">Resolução</th>
                      <th className="num">Tamanho</th>
                      <th className="col-opt">{processedView ? "Preset" : "Presets"}</th>
                      <th>{processedView ? "SHA-256" : "Status"}</th>
                      <th className="actions" />
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map(({ media: m, rendition: r }) => {
                      const isOpen = detailRef?.mediaId === m.id && (r === null || detailRef.renditionId === r.id);
                      return (
                        <tr
                          key={r ? `r${r.id}` : `m${m.id}`}
                          className={`clickable${isOpen ? " selected" : ""}`}
                          onClick={() =>
                            setDetailRef(isOpen ? null : { mediaId: m.id, renditionId: r ? r.id : null })
                          }
                        >
                          {processedView ? null : (
                            <td onClick={(e) => e.stopPropagation()}>
                              <input
                                type="checkbox"
                                aria-label="Selecionar"
                                checked={allByFilter || selected.has(m.id)}
                                onChange={() => toggle(m.id)}
                              />
                            </td>
                          )}
                          <td>
                            <Thumb src={m.thumbnail_url} mediaType={m.media_type} size={36} />
                          </td>
                          <td className="muted col-opt">{m.media_type === "video" ? "Vídeo" : "Imagem"}</td>
                          <td className="cell-trunc" style={{ maxWidth: 160 }} title={m.source_profile ?? undefined}>
                            {m.source_profile ? `@${m.source_profile}` : "-"}
                          </td>
                          <td className="muted nowrap col-opt">
                            {r ? formatDateTime(r.created_at) : formatDate(m.taken_at ?? m.created_at)}
                          </td>
                          <td className="muted col-opt">
                            {m.media_type === "video" ? formatDuration(r ? r.duration : m.duration) : "-"}
                          </td>
                          <td className="muted nowrap col-opt">
                            {r ? formatResolution(r.width, r.height) : formatResolution(m.width, m.height)}
                          </td>
                          <td className="muted nowrap num">{formatBytes(r ? r.file_size : m.file_size)}</td>
                          {r ? (
                            <td className="cell-trunc col-opt" style={{ maxWidth: 160 }} title={r.preset_name ?? undefined}>
                              {r.preset_name ?? "Preset removido"}
                            </td>
                          ) : (
                            <td className="cell-trunc col-opt" style={{ maxWidth: 160 }} title={presetNames(m)}>
                              {presetNames(m)}
                            </td>
                          )}
                          {r ? (
                            <td className="mono dim nowrap" title={r.processed_sha256}>
                              {r.processed_sha256.slice(0, 12)}
                            </td>
                          ) : (
                            <td>
                              {m.last_job ? (
                                <Pill tone={jobTone(m.last_job.status)}>{JOB_STATUS_LABEL[m.last_job.status]}</Pill>
                              ) : (
                                <span className="dim">-</span>
                              )}
                            </td>
                          )}
                          <td className="actions" onClick={(e) => e.stopPropagation()}>
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm btn-icon"
                              title={r ? "Abrir arquivo processado" : "Abrir arquivo"}
                              aria-label={r ? "Abrir arquivo processado" : "Abrir arquivo"}
                              onClick={() => void open(m, "file", r ?? undefined)}
                            >
                              <ExternalLink size={14} strokeWidth={1.6} />
                            </button>
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm btn-icon"
                              title="Abrir pasta"
                              aria-label="Abrir pasta"
                              onClick={() => void open(m, "folder", r ?? undefined)}
                            >
                              <FolderOpen size={14} strokeWidth={1.6} />
                            </button>
                            {r ? (
                              <button
                                type="button"
                                className="btn btn-ghost btn-sm btn-icon btn-danger"
                                title="Excluir processado"
                                aria-label="Excluir processado"
                                onClick={() => setDialog({ kind: "delete-rendition", rendition: r })}
                              >
                                <Trash2 size={14} strokeWidth={1.6} />
                              </button>
                            ) : (
                              <button
                                type="button"
                                className="btn btn-ghost btn-sm"
                                title="Opcional: cria uma versão processada com um preset; o original é mantido"
                                onClick={() =>
                                  setDialog({ kind: "process", mediaIds: [m.id], reprocess: m.renditions.length > 0 })
                                }
                              >
                                {m.renditions.length > 0 ? "Reprocessar" : "Processar"}
                              </button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {stack.length > 1 || nextPage !== null ? (
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
                    disabled={nextPage === null}
                    onClick={() => goTo(pushPage(stack, nextPage))}
                  >
                    Próxima
                  </button>
                </div>
              ) : null}
            </>
          )}
        </div>

        {detail ? (
          <MediaDetail
            key={`${detail.id}:${detailRef?.renditionId ?? "o"}`}
            media={detail}
            initialPreview={detailRef?.renditionId ?? null}
            onClose={() => setDetailRef(null)}
            onOpen={(target, r) => void open(detail, target, r)}
            onProcess={() =>
              setDialog({ kind: "process", mediaIds: [detail.id], reprocess: detail.renditions.length > 0 })
            }
            onDeleteRendition={(r) => setDialog({ kind: "delete-rendition", rendition: r })}
            onDelete={() => setDialog({ kind: "delete-media", media: detail })}
          />
        ) : null}
      </div>

      {dialog?.kind === "process" ? (
        <PresetPickerDialog
          title={
            dialog.mediaIds.length > 1
              ? `Processar ${formatCount(dialog.mediaIds.length)} itens`
              : dialog.reprocess
                ? "Reprocessar com preset"
                : "Processar com preset"
          }
          presets={presets.data ?? ([] as Preset[])}
          defaultPresetId={settings.data?.default_preset_id ?? null}
          busy={busy}
          onConfirm={(id) => void runDialog(id)}
          onCancel={() => setDialog(null)}
        />
      ) : null}
      {dialog?.kind === "process-filter-preset" ? (
        <PresetPickerDialog
          title={`Processar ${formatCount(dialog.total)} resultados`}
          presets={presets.data ?? ([] as Preset[])}
          defaultPresetId={settings.data?.default_preset_id ?? null}
          busy={busy}
          onConfirm={(id) => setDialog({ kind: "process-filter-confirm", total: dialog.total, presetId: id })}
          onCancel={() => setDialog(null)}
        />
      ) : null}
      {dialog?.kind === "process-filter-confirm" ? (
        <ConfirmDialog
          title={`Processar ${formatCount(dialog.total)} resultados`}
          confirmLabel={`Processar ${formatCount(dialog.total)} itens`}
          busy={busy}
          onConfirm={() => void runDialog()}
          onCancel={() => setDialog(null)}
        >
          {formatCount(dialog.total)} itens do filtro atual vão para a fila com o preset{" "}
          {presets.data?.find((p) => p.id === dialog.presetId)?.name ?? "escolhido"}. O limite por vez é de{" "}
          {formatCount(MAX_BULK_PROCESS)} itens.
        </ConfirmDialog>
      ) : null}
      {dialog?.kind === "delete-media" ? (
        <ConfirmDialog
          title="Excluir item"
          confirmLabel="Excluir item e original"
          danger
          busy={busy}
          onConfirm={() => void runDialog()}
          onCancel={() => setDialog(null)}
        >
          O arquivo original será apagado do disco
          {dialog.media.renditions.length > 0
            ? `, junto com ${formatCount(dialog.media.renditions.length)} arquivo(s) processado(s)`
            : ""}
          . Esta ação não pode ser desfeita.
        </ConfirmDialog>
      ) : null}
      {dialog?.kind === "delete-rendition" ? (
        <ConfirmDialog
          title="Excluir processado"
          confirmLabel="Excluir processado"
          danger
          busy={busy}
          onConfirm={() => void runDialog()}
          onCancel={() => setDialog(null)}
        >
          O arquivo processado com {dialog.rendition.preset_name ?? "este preset"} será apagado. O original continua na
          biblioteca.
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

function MediaDetail({
  media,
  initialPreview,
  onClose,
  onOpen,
  onProcess,
  onDeleteRendition,
  onDelete,
}: {
  media: Media;
  initialPreview: number | null;
  onClose: () => void;
  onOpen: (target: "file" | "folder", rendition?: Rendition) => void;
  onProcess: () => void;
  onDeleteRendition: (r: Rendition) => void;
  onDelete: () => void;
}) {
  const [previewId, setPreviewId] = useState<number | "original">(initialPreview ?? "original");
  const rendition = previewId === "original" ? null : (media.renditions.find((r) => r.id === previewId) ?? null);
  const src = rendition ? rendition.file_url : media.original_url;
  const job = media.last_job;

  return (
    <aside className="detail" aria-label="Detalhes da mídia">
      <div className="row between">
        <strong>{media.source_profile ? `@${media.source_profile}` : "Mídia"}</strong>
        <button type="button" className="btn btn-ghost btn-sm btn-icon" aria-label="Fechar detalhes" onClick={onClose}>
          <X size={14} />
        </button>
      </div>

      {media.renditions.length > 0 ? (
        <div className="seg" style={{ marginTop: 8 }} role="group" aria-label="Pré-visualização">
          <button
            type="button"
            className={rendition === null ? "on" : ""}
            onClick={() => setPreviewId("original")}
          >
            Original
          </button>
          {media.renditions.map((r) => (
            <button
              key={r.id}
              type="button"
              className={rendition?.id === r.id ? "on" : ""}
              onClick={() => setPreviewId(r.id)}
            >
              {r.preset_name ?? `#${r.id}`}
            </button>
          ))}
        </div>
      ) : null}

      {media.media_type === "video" ? (
        <video key={src} className="preview" src={src} controls preload="metadata" />
      ) : (
        <img key={src} className="preview" src={src} alt="" />
      )}

      <dl className="kv small">
        <dt>Tipo</dt>
        <dd>{media.media_type === "video" ? "Vídeo" : "Imagem"}</dd>
        {media.source_post_id ? (
          <>
            <dt>Post</dt>
            <dd className="mono">
              {media.source_post_id}
              {media.source_media_index > 0 ? ` (item ${media.source_media_index + 1})` : ""}
            </dd>
          </>
        ) : null}
        <dt>Data</dt>
        <dd>{formatDateTime(media.taken_at ?? media.created_at)}</dd>
        <dt>Importado</dt>
        <dd>{formatDateTime(media.created_at)}</dd>
        <dt>Dimensões</dt>
        <dd>{formatResolution(media.width, media.height)}</dd>
        {media.media_type === "video" ? (
          <>
            <dt>Duração</dt>
            <dd>{formatDuration(media.duration)}</dd>
            <dt>FPS</dt>
            <dd>{formatFps(media.fps)}</dd>
            <dt>Áudio</dt>
            <dd>{media.has_audio == null ? "-" : media.has_audio ? "Sim" : "Não"}</dd>
            {media.video_codec ? (
              <>
                <dt>Codec</dt>
                <dd>{formatVideoCodec(media.video_codec)}</dd>
              </>
            ) : null}
          </>
        ) : null}
        <dt>Tamanho</dt>
        <dd>{formatBytes(media.file_size)}</dd>
        <dt>SHA-256</dt>
        <dd className="mono">{media.original_sha256}</dd>
        {job ? (
          <>
            <dt>Último job</dt>
            <dd>
              <Pill tone={jobTone(job.status)}>{JOB_STATUS_LABEL[job.status]}</Pill>
              {job.error_message ? <div className="tone-err small">{job.error_message}</div> : null}
            </dd>
          </>
        ) : null}
      </dl>

      <div className="row" style={{ marginTop: 12 }}>
        <button type="button" className="btn btn-sm" onClick={() => onOpen("file")}>
          Abrir arquivo
        </button>
        <button type="button" className="btn btn-sm" onClick={() => onOpen("folder")}>
          Abrir pasta
        </button>
        <button
          type="button"
          className="btn btn-sm"
          title="Opcional: cria uma versão processada com um preset; o original é mantido"
          onClick={onProcess}
        >
          {media.renditions.length > 0 ? "Reprocessar" : "Processar (opcional)"}
        </button>
      </div>

      <h3>Processados (opcional)</h3>
      {media.renditions.length === 0 ? (
        <div className="muted small">Somente o original, sem versão processada. O processamento é opcional.</div>
      ) : (
        media.renditions.map((r) => (
          <div key={r.id} className="rendition small">
            <div className="row between">
              <strong>{r.preset_name ?? "Preset removido"}</strong>
              <span className="muted">{formatDateTime(r.created_at)}</span>
            </div>
            <dl className="kv" style={{ marginTop: 4 }}>
              <dt>Dimensões</dt>
              <dd>{formatResolution(r.width, r.height)}</dd>
              {media.media_type === "video" ? (
                <>
                  <dt>Duração</dt>
                  <dd>{formatDuration(r.duration)}</dd>
                  <dt>FPS</dt>
                  <dd>{formatFps(r.fps)}</dd>
                  <dt>Áudio</dt>
                  <dd>{r.has_audio == null ? "-" : r.has_audio ? "Sim" : "Não"}</dd>
                </>
              ) : null}
              <dt>Tamanho</dt>
              <dd>{formatBytes(r.file_size)}</dd>
              <dt>SHA-256</dt>
              <dd className="mono">{r.processed_sha256}</dd>
            </dl>
            <div className="row" style={{ marginTop: 6 }}>
              <button type="button" className="btn btn-sm" onClick={() => onOpen("file", r)}>
                Abrir arquivo
              </button>
              <button type="button" className="btn btn-sm" onClick={() => onOpen("folder", r)}>
                Abrir pasta
              </button>
              <button type="button" className="btn btn-sm btn-ghost btn-danger" onClick={() => onDeleteRendition(r)}>
                Excluir processado
              </button>
            </div>
          </div>
        ))
      )}

      <div style={{ borderTop: "1px solid var(--border)", marginTop: 12, paddingTop: 12 }}>
        <button type="button" className="btn btn-sm btn-danger" onClick={onDelete}>
          <Trash2 size={13} strokeWidth={1.75} aria-hidden="true" />
          Excluir item
        </button>
      </div>
    </aside>
  );
}
