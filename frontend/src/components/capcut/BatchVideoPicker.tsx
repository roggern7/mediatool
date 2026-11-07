import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { CapCutBatchFilter, CapCutCaptioned, CapCutLibraryItem } from "../../api/types";
import {
  type BatchSelection,
  CAPTIONED_OPTIONS,
  EMPTY_SELECTION,
  captionedText,
  cleanFilter,
  folderProfiles,
  isSelected,
  pageState,
  selectAllFiltered,
  selectionCount,
  toggleOne,
  togglePage,
} from "../../lib/capcutBatch";
import { folderTitle, videoCountText } from "../../lib/editFolders";
import { mediaName } from "../../lib/editSettings";
import { formatCount, formatDate, formatDuration } from "../../lib/format";
import { useDebounced } from "../../lib/useDebounced";
import { useQuery } from "../../lib/useQuery";
import { Empty, ErrorLine, Pill, SkeletonRows } from "../ui";
import { VideoThumb } from "../edit/VideoThumb";

const PAGE_SIZE = 50;

/**
 * Vídeos originais da biblioteca (GET /api/capcut/library) com busca, perfil, pasta do Editar e "Já legendados".
 * Lista ou grade com miniatura; "Legendado (n)" nos já processados. Marca um a um, a página inteira ou "todos os N
 * filtrados" (vai como `filter` no pedido). Mudar um filtro desfaz o "todos os filtrados".
 */
export function BatchVideoPicker({
  selection,
  onChange,
  version = 0,
}: {
  selection: BatchSelection;
  onChange: (next: BatchSelection) => void;
  /** Sobe quando um envio local fica pronto: recarrega a lista. */
  version?: number;
}) {
  const [search, setSearch] = useState("");
  const [profile, setProfile] = useState("");
  const [folder, setFolder] = useState("");
  const [captioned, setCaptioned] = useState<CapCutCaptioned>("any");
  const [view, setView] = useState<"list" | "grid">("list");
  const q = useDebounced(search.trim().replace(/^@/, ""), 300);
  const filter: CapCutBatchFilter = cleanFilter({ q, profile, folder, captioned });
  const filterKey = JSON.stringify(filter);
  // Pilha de cursores (before_id) por filtro: null é a primeira página.
  const [pages, setPages] = useState<{ scope: string; stack: (number | null)[] }>({ scope: "", stack: [null] });
  const stack = pages.scope === filterKey ? pages.stack : [null];
  const cursor = stack[stack.length - 1] ?? null;

  const folders = useQuery(`edit-folders:${version}`, () => api.listEditFolders());
  const list = useQuery(`capcut-library:${filterKey}:${cursor ?? ""}:${version}`, () =>
    api.capcutLibrary({ ...filter, limit: PAGE_SIZE, before_id: cursor ?? undefined }),
  );
  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const next = list.data?.next_before_id ?? null;
  const pageIds = items.map((m) => m.id);
  const page = pageState(selection, pageIds);
  const profiles = folderProfiles(folders.data?.items ?? []);
  const allActive = selection.all !== null;

  // "Todos os filtrados" vale para o filtro em que foi escolhido; outro filtro volta à seleção vazia.
  function changeFilter(apply: () => void) {
    apply();
    if (allActive) onChange(EMPTY_SELECTION);
  }

  const pageBox = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (pageBox.current) pageBox.current.indeterminate = page === "some";
  }, [page]);

  function toggle(item: CapCutLibraryItem) {
    onChange(toggleOne(selection, item.id));
  }

  return (
    <div>
      <div className="toolbar">
        <input
          className="input grow"
          style={{ minWidth: 180 }}
          placeholder="Buscar por perfil ou nome do arquivo"
          aria-label="Buscar vídeos na biblioteca"
          value={search}
          onChange={(e) => changeFilter(() => setSearch(e.target.value))}
        />
        <select
          className="select"
          style={{ width: "auto", maxWidth: 200 }}
          aria-label="Filtrar por perfil"
          value={profile}
          onChange={(e) => changeFilter(() => setProfile(e.target.value))}
        >
          <option value="">Todos os perfis</option>
          {profiles.map((p) => (
            <option key={p} value={p}>
              @{p}
            </option>
          ))}
        </select>
        <select
          className="select"
          style={{ width: "auto", maxWidth: 240 }}
          aria-label="Filtrar por pasta"
          value={folder}
          onChange={(e) => changeFilter(() => setFolder(e.target.value))}
        >
          <option value="">Todas as pastas</option>
          {(folders.data?.items ?? []).map((f) => (
            <option key={f.key} value={f.key}>
              {folderTitle(f)} ({formatCount(f.video_count)})
            </option>
          ))}
        </select>
        <select
          className="select"
          style={{ width: "auto" }}
          aria-label="Já legendados"
          value={captioned}
          onChange={(e) => changeFilter(() => setCaptioned(e.target.value as CapCutCaptioned))}
        >
          {CAPTIONED_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              Já legendados: {o.label.toLowerCase()}
            </option>
          ))}
        </select>
        <div className="seg" role="group" aria-label="Exibição">
          <button type="button" className={view === "list" ? "on" : ""} aria-pressed={view === "list"} onClick={() => setView("list")}>
            Lista
          </button>
          <button type="button" className={view === "grid" ? "on" : ""} aria-pressed={view === "grid"} onClick={() => setView("grid")}>
            Grade
          </button>
        </div>
      </div>

      <div className="selection-bar cc-pick-bar">
        <label className="row" style={{ gap: 6 }}>
          <input
            ref={pageBox}
            type="checkbox"
            checked={page === "all"}
            disabled={allActive || items.length === 0}
            onChange={() => onChange(togglePage(selection, pageIds))}
          />
          <span className="small">Marcar a página</span>
        </label>
        {allActive ? (
          <span className="small" role="status">
            Todos os {formatCount(selection.all?.total ?? 0)} vídeos filtrados estão selecionados.
          </span>
        ) : total > 0 ? (
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => onChange(selectAllFiltered(filter, total))}>
            Selecionar todos os {formatCount(total)} filtrados
          </button>
        ) : null}
        <span className="grow" />
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          disabled={selectionCount(selection) === 0}
          onClick={() => onChange(EMPTY_SELECTION)}
        >
          Limpar seleção
        </button>
      </div>

      {list.data === undefined ? (
        list.error ? (
          <ErrorLine message={list.error} onRetry={list.reload} />
        ) : (
          <SkeletonRows rows={4} />
        )
      ) : items.length === 0 ? (
        <Empty title={Object.keys(filter).length > 0 ? "Nenhum vídeo encontrado" : "Nenhum vídeo na biblioteca"} />
      ) : (
        <>
          {list.error ? <ErrorLine message={list.error} onRetry={list.reload} /> : null}
          {view === "list" ? (
            <div className="table-wrap edit-picker">
              <table className="table">
                <thead>
                  <tr>
                    <th style={{ width: 28 }} aria-label="Seleção" />
                    <th aria-label="Miniatura" />
                    <th>Vídeo</th>
                    <th className="col-opt">Duração</th>
                    <th>Legendas</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((m) => (
                    <tr
                      key={m.id}
                      className={`clickable${isSelected(selection, m.id) ? " selected" : ""}`}
                      onClick={() => toggle(m)}
                    >
                      <td onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          aria-label={`Selecionar ${mediaName(m)}`}
                          checked={isSelected(selection, m.id)}
                          disabled={allActive}
                          onChange={() => toggle(m)}
                        />
                      </td>
                      <td>
                        <VideoThumb media={m} size={44} />
                      </td>
                      <td className="edit-video-name" title={mediaName(m)}>
                        {mediaName(m)}
                        <div className="muted small">
                          {m.source_profile ? `@${m.source_profile} · ` : ""}
                          {formatDate(m.created_at)}
                        </div>
                      </td>
                      <td className="muted col-opt">{formatDuration(m.duration)}</td>
                      <td>
                        <CaptionMark item={m} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <ul className="cc-grid edit-picker" aria-label="Vídeos">
              {items.map((m) => {
                const on = isSelected(selection, m.id);
                return (
                  <li key={m.id} className={`cc-card${on ? " on" : ""}`}>
                    <label>
                      <VideoThumb media={m} size={112} />
                      <span className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
                        <input type="checkbox" checked={on} disabled={allActive} onChange={() => toggle(m)} />
                        <span className="truncate small" title={mediaName(m)}>
                          {mediaName(m)}
                        </span>
                      </span>
                    </label>
                    <span className="muted small">
                      {formatDuration(m.duration)}
                      {m.source_profile ? ` · @${m.source_profile}` : ""}
                    </span>
                    <CaptionMark item={m} />
                  </li>
                );
              })}
            </ul>
          )}
          <div className="row between" style={{ marginTop: 8 }}>
            <span className="muted small">{videoCountText(total)}</span>
            <div className="row">
              <button
                type="button"
                className="btn btn-sm"
                disabled={stack.length <= 1}
                onClick={() => setPages({ scope: filterKey, stack: stack.slice(0, -1) })}
              >
                Anterior
              </button>
              <button
                type="button"
                className="btn btn-sm"
                disabled={next === null}
                onClick={() => next !== null && setPages({ scope: filterKey, stack: [...stack, next] })}
              >
                Próxima
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function CaptionMark({ item }: { item: CapCutLibraryItem }) {
  const done = captionedText(item);
  if (!done && !item.capcut.open) return <span className="muted small">—</span>;
  return (
    <span className="row" style={{ gap: 4 }}>
      {done ? <Pill tone="ok">{done}</Pill> : null}
      {item.capcut.open ? <Pill tone="accent">Na fila</Pill> : null}
    </span>
  );
}
