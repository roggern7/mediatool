import { useEffect, useRef, useState } from "react";
import { Upload, X } from "lucide-react";
import { formatBytes } from "../../lib/format";
import type { Tone } from "../../lib/labels";
import {
  UPLOAD_ACCEPT,
  UPLOAD_STATUS_LABEL,
  queueSummary,
  splitVideoFiles,
  type UploadStatus,
} from "../../lib/uploadQueue";
import type { UploadQueueApi } from "../../lib/useUploadQueue";
import { Pill, ProgressBar } from "../ui";

const STATUS_TONE: Record<UploadStatus, Tone> = {
  waiting: "muted",
  uploading: "accent",
  analyzing: "accent",
  done: "ok",
  duplicate: "warn",
  error: "err",
};

/**
 * Zona de arrastar e soltar + botão de escolher arquivos; lista de envios com progresso, erro e remover. Com
 * `onFolderOthers`, mostra também "Escolher pasta" (`webkitdirectory`): só os vídeos da pasta entram na fila e a
 * quantidade de outros arquivos é avisada.
 */
export function LocalFilesDrop({
  queue,
  onIgnored,
  onRemove,
  onFolderOthers,
}: {
  queue: UploadQueueApi;
  onIgnored: (count: number) => void;
  onRemove: (key: string) => void;
  onFolderOthers?: (count: number) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const summary = queueSummary(queue.items);
  const withFolder = onFolderOthers !== undefined;

  useEffect(() => {
    // `webkitdirectory` não está nos tipos do React: vai como atributo.
    folderInput.current?.setAttribute("webkitdirectory", "");
  }, [withFolder]);

  function accept(list: FileList | null) {
    if (!list || list.length === 0) return;
    const ignored = queue.add(Array.from(list));
    if (ignored > 0) onIgnored(ignored);
  }

  function acceptFolder(list: FileList | null) {
    if (!list || list.length === 0) return;
    const { videos, others } = splitVideoFiles(Array.from(list));
    if (others > 0) onFolderOthers?.(others);
    if (videos.length === 0) return;
    const ignored = queue.add(videos);
    if (ignored > 0) onIgnored(ignored);
  }

  return (
    <div>
      <div
        className={`drop-zone${over ? " over" : ""}`}
        onDragOver={(e) => {
          if (!Array.from(e.dataTransfer.types).includes("Files")) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          accept(e.dataTransfer.files);
        }}
      >
        <Upload size={18} strokeWidth={1.5} aria-hidden="true" />
        <span>Arraste vídeos do computador para cá ou</span>
        <button type="button" className="btn btn-sm" onClick={() => input.current?.click()}>
          Escolher arquivos
        </button>
        <input
          ref={input}
          type="file"
          multiple
          accept={UPLOAD_ACCEPT}
          className="sr-only"
          tabIndex={-1}
          aria-label="Escolher vídeos do computador"
          onChange={(e) => {
            accept(e.target.files);
            e.target.value = "";
          }}
        />
        {withFolder ? (
          <>
            <button type="button" className="btn btn-sm" onClick={() => folderInput.current?.click()}>
              Escolher pasta
            </button>
            <input
              ref={folderInput}
              type="file"
              multiple
              className="sr-only"
              tabIndex={-1}
              aria-label="Escolher uma pasta de vídeos do computador"
              onChange={(e) => {
                acceptFolder(e.target.files);
                e.target.value = "";
              }}
            />
          </>
        ) : null}
        <span className="muted small">
          MP4, MOV, WebM ou MKV de até 2 GB. Os vídeos entram na biblioteca (2 envios por vez); o mesmo conteúdo
          já existente é reaproveitado.
        </span>
      </div>

      {queue.items.length > 0 ? (
        <>
          <div className="muted small" style={{ margin: "10px 0 6px" }}>
            {summary.ready} de {summary.total} prontos
            {summary.failed > 0 ? `, ${summary.failed} com erro` : ""}
          </div>
          <ul className="upload-list">
            {queue.items.map((item) => (
              <li key={item.key} className="upload-item">
                <div className="row between">
                  <span className="truncate grow" title={item.name}>
                    {item.name}
                  </span>
                  <span className="muted small nowrap">{formatBytes(item.size)}</span>
                  <Pill tone={STATUS_TONE[item.status]}>{UPLOAD_STATUS_LABEL[item.status]}</Pill>
                  <button
                    type="button"
                    className="btn btn-sm btn-ghost btn-icon"
                    aria-label={`Remover ${item.name}`}
                    title="Remover"
                    onClick={() => onRemove(item.key)}
                  >
                    <X size={13} strokeWidth={1.75} aria-hidden="true" />
                  </button>
                </div>
                {item.status === "uploading" || item.status === "analyzing" ? (
                  <div style={{ marginTop: 5 }}>
                    <ProgressBar value={item.progress} />
                  </div>
                ) : null}
                {item.error ? <div className="field-error">{item.error}</div> : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}
