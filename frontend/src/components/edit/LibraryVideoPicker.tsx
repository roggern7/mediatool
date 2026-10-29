import { ArrowLeft, Trash2 } from "lucide-react";
import { useCallback, useState } from "react";
import { api, messageOf } from "../../api/client";
import type { EditFolder, Media, MediaDeleteResult, Paged } from "../../api/types";
import {
  chunkIds,
  deleteConfirmLines,
  deleteResultText,
  folderDeleteTotals,
  folderFullName,
  folderSubtitle,
  folderTitle,
  mediaDeleteTotals,
  mergeDeleteResults,
  versionCountText,
  versionsDeleteTotals,
  versionsDeletedText,
  videoCountText,
} from "../../lib/editFolders";
import { mediaName, pickerQueryKey } from "../../lib/editSettings";
import { formatCount, formatDate, formatDuration, formatResolution } from "../../lib/format";
import { useToast } from "../../lib/toast";
import { useDebounced } from "../../lib/useDebounced";
import { useQuery } from "../../lib/useQuery";
import { ConfirmDialog, Empty, ErrorLine, SkeletonRows } from "../ui";
import { EditFolderList } from "./EditFolderList";
import { VideoPreviewModal } from "./VideoPreviewModal";
import { VideoThumb } from "./VideoThumb";

const PAGE_SIZE = 50;
const ALL_PAGE_SIZE = 200;
const NO_PAGE: Paged<Media> = { items: [], total: 0, next_before_id: null };

type DeleteTarget =
  | { kind: "media"; items: Media[] }
  | { kind: "folder"; folder: EditFolder }
  | { kind: "versions"; media: Media };

/**
 * Vídeos da biblioteca por pasta (uma por importação/grupo, por dia de envio do computador e "Outros vídeos"). Na
 * lista de pastas, a busca procura na biblioteca inteira; dentro de uma pasta, só nela. Páginas keyset de 50 com
 * caixas de seleção, "Adicionar selecionados", prévia do vídeo e exclusão (vídeo, selecionados, pasta inteira ou só
 * as versões editadas), sempre com confirmação.
 */
export function LibraryVideoPicker({
  selectedIds,
  onAdd,
  onDeleted,
  version = 0,
}: {
  selectedIds: ReadonlySet<number>;
  onAdd: (items: Media[]) => void;
  /** Mídias apagadas da biblioteca (saem da seleção da edição). */
  onDeleted?: (ids: number[]) => void;
  /** Sobe quando um envio local fica pronto: recarrega pastas e a página atual (seleção e busca continuam). */
  version?: number;
}) {
  const toast = useToast();
  const [folder, setFolder] = useState<EditFolder | null>(null);
  const [search, setSearch] = useState("");
  // Pilha de cursores (before_id) por pasta + busca: null é a primeira página.
  const [pages, setPages] = useState<{ scope: string; stack: (number | null)[] }>({ scope: "", stack: [null] });
  const [checked, setChecked] = useState<Map<number, Media>>(() => new Map());
  const [durations, setDurations] = useState<ReadonlyMap<number, number>>(() => new Map());
  const [preview, setPreview] = useState<Media | null>(null);
  const [target, setTarget] = useState<DeleteTarget | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectingAll, setSelectingAll] = useState(false);
  const [summary, setSummary] = useState<string | null>(null);
  // Sobe depois de apagar: recarrega pastas e lista e volta à primeira página.
  const [refresh, setRefresh] = useState(0);
  const q = useDebounced(search.trim().replace(/^@/, ""), 300);

  const folders = useQuery(`edit-folders:${version}:${refresh}`, () => api.listEditFolders());
  // Pasta que ficou vazia (sumiu da lista recarregada): volta para a lista de pastas.
  const vanished =
    folder !== null &&
    folders.data !== undefined &&
    !folders.loading &&
    !folders.data.items.some((f) => f.key === folder.key);
  const openFolder = vanished ? null : (folders.data?.items.find((f) => f.key === folder?.key) ?? folder);
  const showFolders = openFolder === null && q === "";

  const scope = `${openFolder?.key ?? ""}|${q}|${refresh}`;
  const stack = pages.scope === scope ? pages.stack : [null];
  const cursor = stack[stack.length - 1] ?? null;
  const listKey = showFolders
    ? "edit-videos:off"
    : `${pickerQueryKey(q, cursor, version)}:${openFolder?.key ?? ""}:${refresh}`;
  const list = useQuery(listKey, () => {
    if (showFolders) return Promise.resolve(NO_PAGE);
    const page = { q: q || undefined, limit: PAGE_SIZE, before_id: cursor ?? undefined };
    return openFolder
      ? api.listEditFolderMedia(openFolder.key, page)
      : api.listMedia({ view: "all", media_type: "video", ...page });
  });
  const items = list.data?.items ?? [];
  const selectable = items.filter((m) => !selectedIds.has(m.id));
  const pageChecked = selectable.length > 0 && selectable.every((m) => checked.has(m.id));
  const next = list.data?.next_before_id ?? null;

  const rememberDuration = useCallback((id: number, seconds: number) => {
    setDurations((prev) => (prev.get(id) === seconds ? prev : new Map(prev).set(id, seconds)));
  }, []);

  function enter(next: EditFolder | null) {
    setFolder(next);
    setSearch("");
    setSummary(null);
  }

  function toggle(m: Media) {
    setChecked((prev) => {
      const copy = new Map(prev);
      if (copy.has(m.id)) copy.delete(m.id);
      else copy.set(m.id, m);
      return copy;
    });
  }

  function togglePage() {
    setChecked((prev) => {
      const copy = new Map(prev);
      for (const m of selectable) {
        if (pageChecked) copy.delete(m.id);
        else copy.set(m.id, m);
      }
      return copy;
    });
  }

  async function checkWholeFolder() {
    if (!openFolder || selectingAll) return;
    setSelectingAll(true);
    try {
      const all: Media[] = [];
      let before: number | undefined;
      for (;;) {
        const page = await api.listEditFolderMedia(openFolder.key, {
          q: q || undefined,
          limit: ALL_PAGE_SIZE,
          before_id: before,
        });
        all.push(...page.items);
        if (page.next_before_id === null || page.next_before_id === undefined) break;
        before = page.next_before_id;
      }
      setChecked((prev) => {
        const copy = new Map(prev);
        for (const m of all) if (!selectedIds.has(m.id)) copy.set(m.id, m);
        return copy;
      });
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setSelectingAll(false);
    }
  }

  function addChecked() {
    onAdd(Array.from(checked.values()));
    setChecked(new Map());
  }

  async function runDelete() {
    if (!target || busy) return;
    setBusy(true);
    try {
      if (target.kind === "versions") {
        let deleted = 0;
        const failures: string[] = [];
        for (const r of target.media.renditions) {
          try {
            await api.deleteRendition(r.id);
            deleted += 1;
          } catch (err) {
            failures.push(messageOf(err));
          }
        }
        setSummary(versionsDeletedText(deleted, failures));
      } else {
        const results: MediaDeleteResult[] = [];
        let error: string | null = null;
        try {
          if (target.kind === "folder") {
            results.push(await api.deleteMediaBulk({ folder: target.folder.key, delete_original: true }));
          } else {
            // Blocos de até 500 (teto do backend); os resumos são somados.
            for (const ids of chunkIds(target.items.map((m) => m.id))) {
              results.push(await api.deleteMediaBulk({ media_ids: ids, delete_original: true }));
            }
          }
        } catch (err) {
          if (results.length === 0) throw err;
          error = messageOf(err); // blocos anteriores já apagados: mostra o parcial e o erro
        }
        const result = mergeDeleteResults(results);
        const known = target.kind === "media" ? target.items : items;
        const text = deleteResultText(result, new Map(known.map((m) => [m.id, mediaName(m)])));
        setSummary(error ? `${text} Parou com erro: ${error}` : text);
        if (result.deleted_ids.length > 0) {
          const gone = new Set(result.deleted_ids);
          setChecked((prev) => new Map(Array.from(prev).filter(([id]) => !gone.has(id))));
          onDeleted?.(result.deleted_ids);
        }
      }
      setTarget(null);
      setRefresh((r) => r + 1);
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  function askDelete(next: DeleteTarget) {
    setPreview(null);
    setTarget(next);
  }

  const toolbar = (
    <div className="toolbar">
      <input
        className="input grow"
        style={{ minWidth: 180 }}
        placeholder={openFolder ? "Buscar nesta pasta" : "Buscar por perfil ou nome do arquivo"}
        aria-label={openFolder ? "Buscar vídeos nesta pasta" : "Buscar vídeos na biblioteca"}
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      {checked.size > 0 ? (
        <button
          type="button"
          className="btn btn-sm btn-danger"
          onClick={() => askDelete({ kind: "media", items: Array.from(checked.values()) })}
        >
          Apagar selecionados ({formatCount(checked.size)})
        </button>
      ) : null}
      <button type="button" className="btn btn-sm btn-primary" disabled={checked.size === 0} onClick={addChecked}>
        Adicionar selecionados{checked.size > 0 ? ` (${formatCount(checked.size)})` : ""}
      </button>
    </div>
  );

  return (
    <div>
      {openFolder ? (
        <div className="edit-folder-head">
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => enter(null)}>
            <ArrowLeft size={13} strokeWidth={1.75} aria-hidden="true" />
            Pastas
          </button>
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="edit-folder-title">{folderTitle(openFolder)}</div>
            <div className="muted small">{folderSubtitle(openFolder)}</div>
          </div>
          <button
            type="button"
            className="btn btn-sm"
            disabled={selectingAll || openFolder.video_count === 0}
            onClick={() => void checkWholeFolder()}
          >
            {selectingAll ? "Selecionando…" : "Selecionar todos da pasta"}
          </button>
          <button
            type="button"
            className="btn btn-sm btn-danger"
            onClick={() => askDelete({ kind: "folder", folder: openFolder })}
          >
            Apagar pasta
          </button>
        </div>
      ) : null}
      {toolbar}
      {summary ? (
        <div className="row between edit-delete-summary" role="status">
          <span className="small">{summary}</span>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => setSummary(null)}>
            Fechar
          </button>
        </div>
      ) : null}

      {showFolders ? (
        folders.data === undefined ? (
          folders.error ? (
            <ErrorLine message={folders.error} onRetry={folders.reload} />
          ) : (
            <SkeletonRows rows={4} />
          )
        ) : folders.data.items.length === 0 ? (
          <Empty title="Nenhum vídeo na biblioteca" />
        ) : (
          <>
            {folders.error ? <ErrorLine message={folders.error} onRetry={folders.reload} /> : null}
            <div className="edit-picker">
              <EditFolderList folders={folders.data.items} onOpen={enter} />
            </div>
          </>
        )
      ) : list.data === undefined ? (
        list.error ? (
          <ErrorLine message={list.error} onRetry={list.reload} />
        ) : (
          <SkeletonRows rows={4} />
        )
      ) : items.length === 0 ? (
        <Empty title={q ? "Nenhum vídeo encontrado" : "Nenhum vídeo nesta pasta"} />
      ) : (
        <>
          {list.error ? <ErrorLine message={list.error} onRetry={list.reload} /> : null}
          <div className="table-wrap edit-picker">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 28 }}>
                    <input
                      type="checkbox"
                      aria-label="Selecionar os vídeos desta página"
                      checked={pageChecked}
                      disabled={selectable.length === 0}
                      onChange={togglePage}
                    />
                  </th>
                  <th />
                  <th>Vídeo</th>
                  <th className="col-opt">Duração</th>
                  <th className="col-opt">Resolução</th>
                  <th className="actions" />
                </tr>
              </thead>
              <tbody>
                {items.map((m) => {
                  const inSelection = selectedIds.has(m.id);
                  const versions = m.renditions.length;
                  return (
                    <tr key={m.id} className="clickable" onClick={() => (inSelection ? undefined : toggle(m))}>
                      <td onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          aria-label={`Selecionar ${mediaName(m)}`}
                          checked={inSelection || checked.has(m.id)}
                          disabled={inSelection}
                          onChange={() => toggle(m)}
                        />
                      </td>
                      <td>
                        <VideoThumb media={m} size={48} onOpen={() => setPreview(m)} onDuration={rememberDuration} />
                      </td>
                      <td className="edit-video-name" title={mediaName(m)}>
                        {mediaName(m)}
                        <div className="muted small">
                          {m.source_profile ? `@${m.source_profile} · ` : ""}
                          {formatDate(m.created_at)}
                          {versions > 0 ? ` · ${versionCountText(versions)}` : ""}
                          {inSelection ? " · na seleção" : ""}
                        </div>
                      </td>
                      <td className="muted col-opt">{formatDuration(m.duration ?? durations.get(m.id))}</td>
                      <td className="muted nowrap col-opt">{formatResolution(m.width, m.height)}</td>
                      <td className="actions" onClick={(e) => e.stopPropagation()}>
                        <button
                          type="button"
                          className="btn btn-sm btn-ghost btn-icon"
                          aria-label={`Apagar ${mediaName(m)}`}
                          title="Apagar vídeo"
                          onClick={() => askDelete({ kind: "media", items: [m] })}
                        >
                          <Trash2 size={13} strokeWidth={1.75} aria-hidden="true" />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="row between" style={{ marginTop: 8 }}>
            <span className="muted small">{videoCountText(list.data.total)}</span>
            <div className="row">
              <button
                type="button"
                className="btn btn-sm"
                disabled={stack.length <= 1}
                onClick={() => setPages({ scope, stack: stack.slice(0, -1) })}
              >
                Anterior
              </button>
              <button
                type="button"
                className="btn btn-sm"
                disabled={next === null}
                onClick={() => next !== null && setPages({ scope, stack: [...stack, next] })}
              >
                Próxima
              </button>
            </div>
          </div>
        </>
      )}

      {preview ? (
        <VideoPreviewModal
          media={preview}
          inSelection={selectedIds.has(preview.id)}
          onClose={() => setPreview(null)}
          onAdd={() => onAdd([preview])}
          onDeleteMedia={() => askDelete({ kind: "media", items: [preview] })}
          onDeleteVersions={() => askDelete({ kind: "versions", media: preview })}
        />
      ) : null}
      {target ? <DeleteConfirm target={target} busy={busy} onConfirm={runDelete} onCancel={() => setTarget(null)} /> : null}
    </div>
  );
}

function DeleteConfirm({
  target,
  busy,
  onConfirm,
  onCancel,
}: {
  target: DeleteTarget;
  busy: boolean;
  onConfirm: () => Promise<void>;
  onCancel: () => void;
}) {
  let title: string;
  let lines: string[];
  if (target.kind === "folder") {
    title = "Apagar pasta inteira";
    lines = deleteConfirmLines(
      folderDeleteTotals(target.folder),
      folderFullName(target.folder),
      target.folder.shared_count,
    );
  } else if (target.kind === "versions") {
    title = "Apagar versões editadas";
    lines = deleteConfirmLines(versionsDeleteTotals(target.media));
  } else {
    const only = target.items.length === 1 ? target.items[0] : undefined;
    title = only ? `Apagar ${mediaName(only)}` : `Apagar ${formatCount(target.items.length)} vídeos`;
    lines = deleteConfirmLines(mediaDeleteTotals(target.items));
  }
  return (
    <ConfirmDialog
      title={title}
      confirmLabel={title}
      danger
      focusCancel
      busy={busy}
      onConfirm={() => void onConfirm()}
      onCancel={onCancel}
    >
      {lines.map((line) => (
        <p key={line} style={{ margin: "0 0 6px" }}>
          {line}
        </p>
      ))}
    </ConfirmDialog>
  );
}
