import { Film } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { Media } from "../../api/types";
import { mediaName } from "../../lib/editSettings";
import { Thumb } from "../ui";

// Quadro capturado e duração lida por mídia: trocar de página ou de pasta não baixa o vídeo de novo.
const frames = new Map<number, string>();
const durations = new Map<number, number>();

const FRAME_WIDTH = 96;

/**
 * Miniatura real do vídeo. Com `thumbnail_url`, a imagem; sem, um `<video preload="metadata">` do original
 * (`#t=0.5`) criado só quando a linha aparece na tela (IntersectionObserver). Assim que o quadro chega, ele vira
 * uma imagem e o `<video>` solta o arquivo (não fica uma conexão aberta por linha). Com `onOpen`, vira um botão.
 */
export function VideoThumb({
  media,
  size = 40,
  onOpen,
  onDuration,
}: {
  media: Media;
  size?: number;
  onOpen?: () => void;
  /** Duração lida do arquivo (segundos), avisada uma vez por mídia. */
  onDuration?: (id: number, seconds: number) => void;
}) {
  const boxRef = useRef<HTMLSpanElement>(null);
  const captured = useRef(false);
  const [visible, setVisible] = useState(() => typeof IntersectionObserver === "undefined");
  const [frame, setFrame] = useState<string | null>(() => frames.get(media.id) ?? null);
  const useVideo = media.thumbnail_url === null && media.media_type === "video";

  useEffect(() => {
    const known = durations.get(media.id);
    if (known !== undefined) onDuration?.(media.id, known);
  }, [media.id, onDuration]);

  useEffect(() => {
    const el = boxRef.current;
    if (!useVideo || frame !== null || visible || !el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "120px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [useVideo, frame, visible]);

  function onMetadata(video: HTMLVideoElement) {
    if (Number.isFinite(video.duration) && video.duration > 0) {
      durations.set(media.id, video.duration);
      onDuration?.(media.id, video.duration);
    }
    // Sem suporte a #t: pula para um quadro com imagem (o primeiro costuma ser preto).
    if (video.currentTime < 0.1 && video.duration > 0.2) video.currentTime = Math.min(0.5, video.duration / 2);
  }

  function capture(video: HTMLVideoElement) {
    if (captured.current || video.videoWidth === 0) return;
    captured.current = true;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = FRAME_WIDTH;
      canvas.height = Math.max(1, Math.round((FRAME_WIDTH * video.videoHeight) / video.videoWidth));
      canvas.getContext("2d")?.drawImage(video, 0, 0, canvas.width, canvas.height);
      const url = canvas.toDataURL("image/jpeg", 0.7);
      frames.set(media.id, url);
      setFrame(url);
      // Solta o arquivo: pausa antes de tirar o src (o navegador ainda pode fechar a conexão no meio da resposta).
      video.pause();
      video.removeAttribute("src");
      video.load();
    } catch {
      // Sem captura: o próprio <video> continua mostrando o quadro.
    }
  }

  let content;
  if (!useVideo) {
    content = <Thumb src={media.thumbnail_url} mediaType={media.media_type} size={size} />;
  } else if (frame !== null) {
    content = (
      <span className="thumb" style={{ width: size, height: size }}>
        <img src={frame} alt="" />
      </span>
    );
  } else {
    content = (
      <span className="thumb" ref={boxRef} style={{ width: size, height: size }}>
        {visible ? (
          <video
            className="thumb-video"
            src={`${media.original_url}#t=0.5`}
            preload="metadata"
            muted
            playsInline
            aria-hidden="true"
            tabIndex={-1}
            onLoadedMetadata={(e) => onMetadata(e.currentTarget)}
            onLoadedData={(e) => e.currentTarget.currentTime >= 0.1 && capture(e.currentTarget)}
            onSeeked={(e) => capture(e.currentTarget)}
          />
        ) : (
          <Film size={16} strokeWidth={1.5} aria-hidden="true" />
        )}
      </span>
    );
  }
  if (!onOpen) return content;
  return (
    <button
      type="button"
      className="thumb-btn"
      aria-label={`Ver prévia de ${mediaName(media)}`}
      title="Ver prévia"
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
    >
      {content}
    </button>
  );
}
