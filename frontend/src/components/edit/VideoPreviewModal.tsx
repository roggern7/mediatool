import { Scissors } from "lucide-react";
import { useLayoutEffect, useState } from "react";
import type { Media } from "../../api/types";
import { versionCountText } from "../../lib/editFolders";
import { mediaName } from "../../lib/editSettings";
import { formatBytes, formatDateTime, formatDuration, formatResolution } from "../../lib/format";
import { Modal } from "../ui";

/**
 * Player maior de um vídeo (original, com controles e som), com nome, perfil e data. Pode adicionar à edição e
 * apagar (vídeo inteiro ou só as versões editadas) quando os callbacks vêm; com `onTrim`, abre o editor de recorte.
 * Esc ou clique fora fecham; o foco volta para onde estava.
 */
export function VideoPreviewModal({
  media,
  inSelection,
  onClose,
  onAdd,
  onDeleteMedia,
  onDeleteVersions,
  onTrim,
}: {
  media: Media;
  inSelection: boolean;
  onClose: () => void;
  onAdd?: () => void;
  onDeleteMedia?: () => void;
  onDeleteVersions?: () => void;
  onTrim?: () => void;
}) {
  const [duration, setDuration] = useState<number | null>(media.duration);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const versions = media.renditions.length;

  // Elemento com foco ANTES de o modal montar (lido no render, antes do autoFocus de "Fechar"). Ao fechar, o foco
  // volta para ele; no layout effect para não roubar o foco de um diálogo aberto logo em seguida (ex.: confirmar).
  const [returnFocus] = useState(() =>
    typeof document !== "undefined" && document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );
  useLayoutEffect(() => () => returnFocus?.focus(), [returnFocus]);

  return (
    <Modal
      title={mediaName(media)}
      wide
      onClose={onClose}
      actions={
        <>
          {onDeleteVersions && versions > 0 ? (
            <button type="button" className="btn btn-danger" onClick={onDeleteVersions}>
              Apagar versões editadas ({versions})
            </button>
          ) : null}
          {onDeleteMedia ? (
            <button type="button" className="btn btn-danger" onClick={onDeleteMedia}>
              Apagar vídeo
            </button>
          ) : null}
          <span className="grow" />
          {onTrim ? (
            <button type="button" className="btn" onClick={onTrim}>
              <Scissors size={14} strokeWidth={1.75} aria-hidden="true" />
              Recortar
            </button>
          ) : null}
          <button type="button" className="btn" onClick={onClose} autoFocus>
            Fechar
          </button>
          {onAdd ? (
            <button type="button" className="btn btn-primary" disabled={inSelection} onClick={onAdd}>
              {inSelection ? "Já está na seleção" : "Adicionar à edição"}
            </button>
          ) : null}
        </>
      }
    >
      <video
        className="edit-preview-video"
        src={media.original_url}
        controls
        autoPlay
        playsInline
        aria-label={`Prévia de ${mediaName(media)}`}
        onLoadedMetadata={(e) => {
          const v = e.currentTarget;
          if (media.duration === null && Number.isFinite(v.duration)) setDuration(v.duration);
          if (v.videoWidth > 0) setSize({ w: v.videoWidth, h: v.videoHeight });
        }}
      />
      <dl className="edit-preview-meta">
        <dt>Perfil</dt>
        <dd>{media.source_profile ? `@${media.source_profile}` : media.source_platform === "local" ? "Arquivo local" : "-"}</dd>
        <dt>Adicionado em</dt>
        <dd>{formatDateTime(media.created_at)}</dd>
        {media.taken_at ? (
          <>
            <dt>Publicado em</dt>
            <dd>{formatDateTime(media.taken_at)}</dd>
          </>
        ) : null}
        <dt>Duração</dt>
        <dd>{formatDuration(duration)}</dd>
        <dt>Resolução</dt>
        <dd>{formatResolution(media.width ?? size?.w, media.height ?? size?.h)}</dd>
        <dt>Tamanho</dt>
        <dd>{formatBytes(media.file_size)}</dd>
        <dt>Versões editadas</dt>
        <dd>{versions > 0 ? versionCountText(versions) : "Nenhuma"}</dd>
      </dl>
    </Modal>
  );
}
