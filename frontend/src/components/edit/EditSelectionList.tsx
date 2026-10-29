import { Scissors, X } from "lucide-react";
import { Fragment, useCallback, useState } from "react";
import type { Media } from "../../api/types";
import {
  type EditForm,
  formatEditDuration,
  mediaName,
  predictedDuration,
  TRIM_IGNORES_DURATION_TEXT,
} from "../../lib/editSettings";
import { formatCount, formatDuration } from "../../lib/format";
import { type Trim, trimLabel } from "../../lib/trim";
import { Empty } from "../ui";
import { TrimEditor } from "./TrimEditor";
import { VideoPreviewModal } from "./VideoPreviewModal";
import { VideoThumb } from "./VideoThumb";

/** Prévia da seleção (biblioteca e arquivos locais): miniatura com player, duração original e prevista, recorte
 * manual por vídeo, remover e limpar. Sem duração no banco, usa a lida do arquivo. */
export function EditSelectionList({
  items,
  form,
  trims,
  onTrim,
  onRemove,
  onClear,
}: {
  items: Media[];
  form: EditForm;
  /** Recorte manual por vídeo (id da mídia). */
  trims: ReadonlyMap<number, Trim>;
  /** Guarda (ou remove, com null) o recorte de um vídeo. */
  onTrim: (id: number, trim: Trim | null) => void;
  onRemove: (id: number) => void;
  onClear: () => void;
}) {
  const [preview, setPreview] = useState<Media | null>(null);
  const [trimming, setTrimming] = useState<Media | null>(null);
  const [durations, setDurations] = useState<ReadonlyMap<number, number>>(() => new Map());
  const rememberDuration = useCallback((id: number, seconds: number) => {
    setDurations((prev) => (prev.get(id) === seconds ? prev : new Map(prev).set(id, seconds)));
  }, []);
  if (items.length === 0) {
    return (
      <Empty title="Nenhum vídeo selecionado">
        <span className="muted small">Adicione vídeos da biblioteca ou envie arquivos do computador.</span>
      </Empty>
    );
  }
  return (
    <div>
      <div className="row between" style={{ marginBottom: 8 }}>
        <span className="muted small">{formatCount(items.length)} de 500 vídeos no máximo</span>
        <button type="button" className="btn btn-sm btn-ghost" onClick={onClear}>
          Limpar tudo
        </button>
      </div>
      <div className="table-wrap edit-selection">
        <table className="table">
          <thead>
            <tr>
              <th />
              <th>Vídeo</th>
              <th className="col-opt">Origem</th>
              <th className="num">Duração</th>
              <th className="num">Prevista</th>
              <th className="actions" />
            </tr>
          </thead>
          <tbody>
            {items.map((m) => {
              const duration = m.duration ?? durations.get(m.id) ?? null;
              const trim = trims.get(m.id) ?? null;
              return (
                <Fragment key={m.id}>
                  <tr className={trim ? "edit-row-trimmed" : undefined}>
                    <td>
                      <VideoThumb media={m} size={40} onOpen={() => setPreview(m)} onDuration={rememberDuration} />
                    </td>
                    <td className="cell-trunc" style={{ maxWidth: 200 }} title={mediaName(m)}>
                      {mediaName(m)}
                    </td>
                    <td className="muted col-opt nowrap">
                      {m.source_platform === "local" ? "Arquivo local" : "Biblioteca"}
                    </td>
                    <td className="muted num nowrap">{formatDuration(duration)}</td>
                    <td className="num nowrap">{formatEditDuration(predictedDuration(duration, form, trim))}</td>
                    <td className="actions nowrap">
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        aria-label={`Recortar ${mediaName(m)}`}
                        title="Recortar (editor de recorte)"
                        onClick={() => setTrimming(m)}
                      >
                        <Scissors size={13} strokeWidth={1.75} aria-hidden="true" />
                        Recortar
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost btn-icon"
                        aria-label={`Remover ${mediaName(m)} da seleção`}
                        title="Remover"
                        onClick={() => onRemove(m.id)}
                      >
                        <X size={13} strokeWidth={1.75} aria-hidden="true" />
                      </button>
                    </td>
                  </tr>
                  {trim ? (
                    <tr className="edit-trim-row">
                      <td />
                      <td colSpan={5}>
                        <div className="edit-trim-info">
                          <Scissors size={12} strokeWidth={1.75} aria-hidden="true" />
                          <span className="mono">{trimLabel(trim)}</span>
                          {form.durationMode !== "original" ? (
                            <span className="muted small">{TRIM_IGNORES_DURATION_TEXT}</span>
                          ) : null}
                          <span className="grow" />
                          <button
                            type="button"
                            className="btn btn-sm btn-ghost"
                            aria-label={`Editar recorte de ${mediaName(m)}`}
                            onClick={() => setTrimming(m)}
                          >
                            Editar recorte
                          </button>
                          <button
                            type="button"
                            className="btn btn-sm btn-ghost"
                            aria-label={`Remover recorte de ${mediaName(m)}`}
                            onClick={() => onTrim(m.id, null)}
                          >
                            Remover recorte
                          </button>
                        </div>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      {preview ? (
        <VideoPreviewModal
          media={preview}
          inSelection
          onClose={() => setPreview(null)}
          onTrim={() => {
            setPreview(null);
            setTrimming(preview);
          }}
        />
      ) : null}
      {trimming ? (
        <TrimEditor
          media={trimming}
          initial={trims.get(trimming.id) ?? null}
          speed={form.speed}
          onClose={() => setTrimming(null)}
          onApply={(trim) => {
            onTrim(trimming.id, trim);
            setTrimming(null);
          }}
        />
      ) : null}
    </div>
  );
}
