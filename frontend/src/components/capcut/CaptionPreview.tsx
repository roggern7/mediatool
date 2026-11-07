import { useEffect, useMemo, useState, type CSSProperties } from "react";
import type { CapCutDesign } from "../../api/types";
import {
  FRAME_H,
  FRAME_W,
  captionLayout,
  chunkCaption,
  previewFrameAt,
  rgbaCss,
  textShadowCss,
} from "../../lib/captionDesign";

const DISPLAY_W = 270;
const TICK_MS = 40;

/**
 * Prévia instantânea (aproximada) da legenda num quadro 9:16: o texto é desenhado em CSS sobre um palco de 1080x1920
 * (o sistema de coordenadas do CapCut) reduzido para caber no painel.
 */
export function CaptionPreview({
  design,
  text,
  fontStack,
  backgroundUrl,
  maxWords,
  maxChars,
}: {
  design: CapCutDesign;
  text: string;
  /** Pilha CSS de fontes (a real carregada por @font-face ou uma aproximada). */
  fontStack: string;
  backgroundUrl: string | null;
  maxWords?: number;
  maxChars?: number;
}) {
  const [elapsed, setElapsed] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [failedBg, setFailedBg] = useState<string | null>(null);

  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => setElapsed((e) => e + TICK_MS), TICK_MS);
    return () => window.clearInterval(timer);
  }, [playing]);

  const chunks = useMemo(() => {
    const c = chunkCaption(text, maxWords, maxChars);
    return c.length > 0 ? c : [["Legenda"]];
  }, [text, maxWords, maxChars]);
  const frame = previewFrameAt(chunks, elapsed, design.animation);
  const layout = captionLayout(design);
  const words = chunks[Math.min(frame.chunk, chunks.length - 1)] ?? [];
  const fill = rgbaCss(design.color, design.opacity);
  const highlight = rgbaCss(design.highlight.color, design.opacity);
  const ratio = DISPLAY_W / FRAME_W;
  const showBg = backgroundUrl !== null && failedBg !== backgroundUrl;

  const boxStyle: CSSProperties = {
    left: layout.centerX,
    top: layout.centerY,
    maxWidth: layout.wrapWidth,
    transform: `translate(-50%, -50%) scale(${layout.scale * frame.scale}) translateY(${frame.shiftEm}em)`,
    opacity: frame.opacity,
    fontFamily: fontStack,
    fontSize: layout.fontPx,
    fontWeight: design.bold ? 700 : 400,
    fontStyle: design.italic ? "italic" : "normal",
    textDecoration: design.underline ? "underline" : "none",
    textTransform: design.uppercase ? "uppercase" : "none",
    textAlign: design.align,
    letterSpacing: layout.letterSpacingPx,
    lineHeight: layout.lineHeight,
    color: fill,
    // Contorno por fora das letras: traço com o dobro da espessura, pintado antes do preenchimento.
    WebkitTextStroke: layout.strokePx > 0 ? `${layout.strokePx * 2}px ${design.stroke.color}` : undefined,
    paintOrder: "stroke fill",
    textShadow: textShadowCss(layout),
    ...(layout.background
      ? {
          background: layout.background.color,
          padding: `${layout.background.padY}px ${layout.background.padX}px`,
          borderRadius: layout.background.radius,
        }
      : {}),
  };

  return (
    <div>
      <div className="caption-frame" style={{ width: DISPLAY_W, height: (DISPLAY_W * FRAME_H) / FRAME_W }}>
        {showBg ? (
          <img className="caption-frame-bg" src={backgroundUrl} alt="" onError={() => setFailedBg(backgroundUrl)} />
        ) : null}
        <div className="caption-stage" style={{ transform: `scale(${ratio})` }} aria-hidden="true">
          <div className="caption-box" style={boxStyle}>
            {words.map((word, i) => (
              <span key={i} style={design.highlight.enabled && i === frame.word ? { color: highlight } : undefined}>
                {word}
                {i < words.length - 1 ? " " : ""}
              </span>
            ))}
          </div>
        </div>
      </div>
      <div className="row" style={{ marginTop: 6, width: DISPLAY_W }}>
        <span className="muted small grow">Prévia aproximada (fonte, tamanho e animações podem variar um pouco).</span>
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => setPlaying((p) => !p)}>
          {playing ? "Pausar" : "Animar"}
        </button>
      </div>
    </div>
  );
}
