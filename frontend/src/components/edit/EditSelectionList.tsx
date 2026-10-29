import { X } from "lucide-react";
import { useCallback, useState } from "react";
import type { Media } from "../../api/types";
import { type EditForm, formatEditDuration, mediaName, predictedDuration } from "../../lib/editSettings";
import { formatCount, formatDuration } from "../../lib/format";
import { Empty } from "../ui";
import { VideoPreviewModal } from "./VideoPreviewModal";
import { VideoThumb } from "./VideoThumb";

/** Prévia da seleção (biblioteca e arquivos locais): miniatura com player, duração original e prevista, remover e
 * limpar. Sem duração no banco, usa a lida do arquivo. */
export function EditSelectionList({
  items,
  form,
  onRemove,
  onClear,
}: {
  items: Media[];
  form: EditForm;
  onRemove: (id: number) => void;
  onClear: () => void;
}) {
  const [preview, setPreview] = useState<Media | null>(null);
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
              return (
                <tr key={m.id}>
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
                  <td className="num nowrap">{formatEditDuration(predictedDuration(duration, form))}</td>
                  <td className="actions">
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
              );
            })}
          </tbody>
        </table>
      </div>
      {preview ? <VideoPreviewModal media={preview} inSelection onClose={() => setPreview(null)} /> : null}
    </div>
  );
}
