import { useState } from "react";
import { api } from "../../api/client";
import type { Media } from "../../api/types";
import { mediaName, pickerQueryKey } from "../../lib/editSettings";
import { formatCount, formatDuration, formatResolution } from "../../lib/format";
import { useDebounced } from "../../lib/useDebounced";
import { useQuery } from "../../lib/useQuery";
import { Empty, ErrorLine, SkeletonRows, Thumb } from "../ui";

const PAGE_SIZE = 50;

/** Vídeos da biblioteca em páginas (keyset, 50 por vez) com caixas de seleção e "Adicionar selecionados". */
export function LibraryVideoPicker({
  selectedIds,
  onAdd,
  version = 0,
}: {
  selectedIds: ReadonlySet<number>;
  onAdd: (items: Media[]) => void;
  /** Sobe quando um envio local fica pronto: recarrega a página atual (seleção e busca continuam). */
  version?: number;
}) {
  const [search, setSearch] = useState("");
  // Pilha de cursores (before_id) por busca: null é a primeira página.
  const [pages, setPages] = useState<{ scope: string; stack: (number | null)[] }>({ scope: "", stack: [null] });
  const [checked, setChecked] = useState<Map<number, Media>>(() => new Map());
  const q = useDebounced(search.trim().replace(/^@/, ""), 300);
  const stack = pages.scope === q ? pages.stack : [null];
  const cursor = stack[stack.length - 1] ?? null;

  const list = useQuery(pickerQueryKey(q, cursor, version), () =>
    api.listMedia({ view: "all", media_type: "video", q: q || undefined, limit: PAGE_SIZE, before_id: cursor ?? undefined }),
  );
  const items = list.data?.items ?? [];
  const selectable = items.filter((m) => !selectedIds.has(m.id));
  const pageChecked = selectable.length > 0 && selectable.every((m) => checked.has(m.id));
  const next = list.data?.next_before_id ?? null;

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

  function addChecked() {
    onAdd(Array.from(checked.values()));
    setChecked(new Map());
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
          onChange={(e) => setSearch(e.target.value)}
        />
        <button type="button" className="btn btn-sm btn-primary" disabled={checked.size === 0} onClick={addChecked}>
          Adicionar selecionados{checked.size > 0 ? ` (${formatCount(checked.size)})` : ""}
        </button>
      </div>
      {list.data === undefined ? (
        list.error ? (
          <ErrorLine message={list.error} onRetry={list.reload} />
        ) : (
          <SkeletonRows rows={4} />
        )
      ) : items.length === 0 ? (
        <Empty title={q ? "Nenhum vídeo encontrado" : "Nenhum vídeo na biblioteca"} />
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
                </tr>
              </thead>
              <tbody>
                {items.map((m) => {
                  const inSelection = selectedIds.has(m.id);
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
                        <Thumb src={m.thumbnail_url} mediaType={m.media_type} size={32} />
                      </td>
                      <td className="cell-trunc" style={{ maxWidth: 220 }} title={mediaName(m)}>
                        {mediaName(m)}
                        {inSelection ? <span className="muted small"> · na seleção</span> : null}
                      </td>
                      <td className="muted col-opt">{formatDuration(m.duration)}</td>
                      <td className="muted nowrap col-opt">{formatResolution(m.width, m.height)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="row between" style={{ marginTop: 8 }}>
            <span className="muted small">{formatCount(list.data.total)} vídeos</span>
            <div className="row">
              <button
                type="button"
                className="btn btn-sm"
                disabled={stack.length <= 1}
                onClick={() => setPages({ scope: q, stack: stack.slice(0, -1) })}
              >
                Anterior
              </button>
              <button
                type="button"
                className="btn btn-sm"
                disabled={next === null}
                onClick={() => next !== null && setPages({ scope: q, stack: [...stack, next] })}
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
