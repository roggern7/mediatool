import {
  ChevronLeft,
  ChevronRight,
  Pause,
  Play,
  Repeat,
  SkipBack,
  SkipForward,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { type KeyboardEvent as ReactKeyboardEvent, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { Media } from "../../api/types";
import { formatEditDuration, formatEditSpeed, mediaName } from "../../lib/editSettings";
import { formatFps } from "../../lib/format";
import {
  TIMECODE_INVALID_TEXT,
  type Trim,
  ZOOM_LEVELS,
  formatTimecode,
  formatTrimLength,
  frameStartAt,
  fullTrim,
  isFullTrim,
  knownFps,
  normalizeTrim,
  parseTimecode,
  previewTime,
  setTrimEnd,
  setTrimStart,
  snapTime,
  stepFrame,
  stepSeconds,
  trimLength,
} from "../../lib/trim";
import { Spinner } from "../ui";
import { type DragTarget, TrimTimeline } from "./TrimTimeline";

/** Campo de tempo editável: mostra mm:ss.mmm; ao confirmar (Enter ou sair do campo) aplica o valor digitado. */
function TimeField({
  label,
  value,
  disabled,
  onCommit,
}: {
  label: string;
  value: number;
  disabled: boolean;
  onCommit: (seconds: number) => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState(false);

  function commit() {
    if (draft === null) return;
    if (draft.trim() === formatTimecode(value)) {
      setDraft(null); // nada digitado: não mexe no recorte nem na agulha
      setError(false);
      return;
    }
    const parsed = parseTimecode(draft);
    if (parsed === null) {
      setError(true);
      return;
    }
    setError(false);
    setDraft(null);
    onCommit(parsed);
  }

  function onKeyDown(e: ReactKeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation(); // Esc no campo desfaz a digitação (não fecha o editor)
      setDraft(null);
      setError(false);
    }
  }

  return (
    <div className="trim-field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        className={`input mono${error ? " invalid" : ""}`}
        inputMode="decimal"
        autoComplete="off"
        spellCheck={false}
        disabled={disabled}
        value={draft ?? formatTimecode(value)}
        aria-invalid={error || undefined}
        aria-describedby={error ? `${id}-err` : undefined}
        onFocus={(e) => {
          setDraft(e.currentTarget.value);
          e.currentTarget.select();
        }}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          commit();
          setDraft(null);
        }}
        onKeyDown={onKeyDown}
      />
      {error ? (
        <div id={`${id}-err`} className="field-hint err" role="alert">
          {TIMECODE_INVALID_TEXT}
        </div>
      ) : null}
    </div>
  );
}

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), [tabindex="0"]';

/**
 * Editor de recorte de um vídeo (tela cheia): player grande, linha do tempo com miniaturas, puxadores de início e
 * fim, agulha arrastável e tempos em mm:ss.mmm. "Aplicar" devolve o recorte (ou null quando ele cobre o vídeo
 * inteiro); Esc e "Cancelar" fecham sem aplicar.
 */
export function TrimEditor({
  media,
  initial,
  speed,
  onApply,
  onClose,
}: {
  media: Media;
  initial: Trim | null;
  /** Velocidade geral da edição: só para mostrar a duração final do trecho. */
  speed: number;
  onApply: (trim: Trim | null) => void;
  onClose: () => void;
}) {
  const titleId = useId();
  const helpId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const fps = knownFps(media.fps);
  const [duration, setDuration] = useState<number | null>(null);
  const [trim, setTrim] = useState<Trim | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [loop, setLoop] = useState(false);
  const [regionPlaying, setRegionPlaying] = useState(false);
  const [zoom, setZoom] = useState<number>(1);
  const [loadError, setLoadError] = useState(false);
  // Um seek por vez: enquanto o vídeo busca, só o último pedido fica guardado (varredura fluida).
  const seek = useRef<{ pending: number | null; busy: boolean }>({ pending: null, busy: false });
  // Estado lido pelo laço de reprodução e pelos atalhos (sempre o atual).
  // `stopAt`: "Tocar só o recorte" chegou ao fim; `keep`: a pausa veio de um movimento da agulha (não sobrescrever).
  const live = useRef({
    trim: null as Trim | null,
    region: false,
    loop: false,
    stopAt: null as number | null,
    keep: false,
  });
  const keyHandler = useRef<(e: KeyboardEvent) => void>(() => undefined);
  const ready = duration !== null && trim !== null;

  const [returnFocus] = useState(() =>
    typeof document !== "undefined" && document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );
  useLayoutEffect(() => {
    dialogRef.current?.focus();
    return () => returnFocus?.focus();
  }, [returnFocus]);

  useEffect(() => {
    live.current.trim = trim;
    live.current.loop = loop;
  }, [trim, loop]);

  function pump() {
    const v = videoRef.current;
    const s = seek.current;
    if (!v || s.busy || s.pending === null || v.readyState < 1) return;
    const t = s.pending;
    s.pending = null;
    s.busy = true;
    v.currentTime = t;
  }

  function show(t: number, mode: "at" | "start" | "end" = "at") {
    if (duration === null) return;
    seek.current.pending = previewTime(t, fps, duration, mode);
    pump();
  }

  function pause() {
    live.current.region = false;
    setRegionPlaying(false);
    videoRef.current?.pause();
  }

  function moveTo(t: number, mode: "at" | "start" | "end" = "at") {
    if (duration === null) return;
    if (videoRef.current && !videoRef.current.paused) {
      live.current.keep = true;
      pause();
    }
    setPlayhead(t);
    show(t, mode);
  }

  function changeStart(value: number) {
    if (trim === null || duration === null) return;
    const next = setTrimStart(trim, value, duration);
    setTrim(next);
    moveTo(next.start, "start");
  }

  function changeEnd(value: number) {
    if (trim === null || duration === null) return;
    const next = setTrimEnd(trim, value, duration);
    setTrim(next);
    moveTo(next.end, "end");
  }

  function togglePlay() {
    const v = videoRef.current;
    if (!v || duration === null) return;
    if (playing) {
      pause();
      return;
    }
    live.current.region = false;
    setRegionPlaying(false);
    if (playhead >= duration - 0.01) v.currentTime = 0;
    v.play().catch(() => undefined);
  }

  function playRegion() {
    const v = videoRef.current;
    if (!v || trim === null) return;
    live.current.region = true;
    setRegionPlaying(true);
    seek.current.pending = null;
    v.currentTime = trim.start;
    v.play().catch(() => undefined);
  }

  function stepFrames(direction: number) {
    if (duration !== null) moveTo(stepFrame(playhead, direction, fps, duration));
  }

  function stepBySeconds(delta: number) {
    if (duration !== null) moveTo(stepSeconds(playhead, delta, fps, duration));
  }

  function onDrag(target: DragTarget, time: number) {
    if (duration === null) return;
    const t = snapTime(time, fps, duration);
    if (target === "start") changeStart(t);
    else if (target === "end") changeEnd(t);
    else moveTo(t);
  }

  function onNudge(target: DragTarget, direction: number, unit: "frames" | "seconds") {
    if (trim === null || duration === null) return;
    const from = target === "start" ? trim.start : target === "end" ? trim.end : playhead;
    const to =
      unit === "frames" ? stepFrame(from, direction, fps, duration) : stepSeconds(from, direction, fps, duration);
    onDrag(target, to);
  }

  function apply() {
    if (trim === null || duration === null) return;
    onApply(isFullTrim(trim, duration) ? null : trim);
  }

  useEffect(() => {
    keyHandler.current = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      const dialog = dialogRef.current;
      if (e.key === "Tab" && dialog) {
        const items = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE));
        const first = items[0];
        const last = items[items.length - 1];
        if (!first || !last) return;
        const active = document.activeElement;
        if (!dialog.contains(active) || (e.shiftKey && (active === first || active === dialog))) {
          e.preventDefault();
          (e.shiftKey ? last : first).focus();
        } else if (!e.shiftKey && active === last) {
          e.preventDefault();
          first.focus();
        }
        return;
      }
      const el = e.target;
      const typing =
        el instanceof HTMLTextAreaElement ||
        el instanceof HTMLSelectElement ||
        (el instanceof HTMLInputElement && el.type !== "checkbox");
      if (typing || e.ctrlKey || e.metaKey || e.altKey || trim === null) return;
      switch (e.key) {
        case " ":
          e.preventDefault();
          if (!e.repeat) togglePlay();
          break;
        case "i":
        case "I":
          e.preventDefault();
          changeStart(playhead);
          break;
        case "o":
        case "O":
          e.preventDefault();
          changeEnd(playhead);
          break;
        case "ArrowLeft":
        case "ArrowRight": {
          e.preventDefault();
          const direction = e.key === "ArrowLeft" ? -1 : 1;
          if (e.shiftKey) stepBySeconds(direction);
          else stepFrames(direction);
          break;
        }
        case "Home":
          e.preventDefault();
          moveTo(trim.start, "start");
          break;
        case "End":
          e.preventDefault();
          moveTo(trim.end, "end");
          break;
      }
    };
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => keyHandler.current(e);
    // Espaço num botão focado não deve também "clicar" nele (o clique por Espaço acontece ao soltar a tecla).
    const onKeyUp = (e: KeyboardEvent) => {
      if (e.key === " " && e.target instanceof HTMLButtonElement) e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("keyup", onKeyUp);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("keyup", onKeyUp);
    };
  }, []);

  // Durante a reprodução, a agulha acompanha o vídeo a cada quadro de tela; "Tocar só o recorte" para (ou volta
  // ao início, com "Repetir") no fim do recorte.
  useEffect(() => {
    if (!playing) return;
    let frame = 0;
    const tick = () => {
      const v = videoRef.current;
      const state = live.current;
      if (v && !v.paused) {
        if (state.region && state.trim && v.currentTime >= state.trim.end) {
          if (state.loop) {
            v.currentTime = state.trim.start;
          } else {
            state.stopAt = state.trim.end;
            state.region = false;
            v.pause();
            return;
          }
        }
        setPlayhead(v.currentTime);
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing]);

  function onLoadedMetadata() {
    const v = videoRef.current;
    if (!v) return;
    const measured = Number.isFinite(v.duration) && v.duration > 0 ? v.duration : null;
    const stored = media.duration !== null && media.duration > 0 ? media.duration : null;
    // A menor das duas: o fim nunca passa da duração que o backend conhece nem do que o navegador consegue tocar.
    const d = measured !== null && stored !== null ? Math.min(measured, stored) : (measured ?? stored);
    if (d === null) {
      setLoadError(true);
      return;
    }
    const first = normalizeTrim(initial ?? fullTrim(d), d);
    setDuration(d);
    setTrim(first);
    setPlayhead(first.start);
    seek.current.pending = previewTime(first.start, fps, d, "start");
    pump();
  }

  function onPause() {
    const v = videoRef.current;
    setPlaying(false);
    setRegionPlaying(false);
    if (!v || duration === null) return;
    const { stopAt, keep } = live.current;
    live.current.stopAt = null;
    live.current.keep = false;
    if (keep) return; // a agulha já foi levada para onde o dono pediu
    if (stopAt !== null) {
      moveTo(stopAt, "end");
    } else {
      setPlayhead(frameStartAt(v.currentTime, fps, duration));
    }
  }

  const length = trim ? trimLength(trim) : 0;
  const zoomIndex = ZOOM_LEVELS.indexOf(zoom as (typeof ZOOM_LEVELS)[number]);

  return (
    <div className="overlay trim-overlay">
      <div
        className="trim-editor"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={helpId}
        ref={dialogRef}
        tabIndex={-1}
      >
        <div className="trim-head">
          <h2 id={titleId} className="cell-trunc" title={mediaName(media)}>
            Recortar: {mediaName(media)}
          </h2>
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            aria-label="Fechar sem aplicar (Esc)"
            title="Fechar sem aplicar (Esc)"
            onClick={onClose}
          >
            <X size={15} strokeWidth={1.75} aria-hidden="true" />
          </button>
        </div>

        <div className="trim-stage">
          <video
            ref={videoRef}
            className="trim-video"
            src={media.original_url}
            preload="auto"
            playsInline
            aria-label={`Vídeo ${mediaName(media)}`}
            onLoadedMetadata={onLoadedMetadata}
            onSeeked={() => {
              seek.current.busy = false;
              pump();
            }}
            onPlay={() => setPlaying(true)}
            onPause={onPause}
            onError={() => setLoadError(true)}
            onClick={togglePlay}
          />
          {!ready ? (
            <div className="trim-loading">
              {loadError ? <span>Não foi possível abrir este vídeo.</span> : <Spinner />}
            </div>
          ) : null}
        </div>

        <div className="trim-readout">
          <span>
            Agulha <strong className="mono">{formatTimecode(playhead)}</strong>
            <span className="muted"> / {duration !== null ? formatTimecode(duration) : "--:--.---"}</span>
          </span>
          {trim ? (
            <>
              <span>
                Início <strong className="mono">{formatTimecode(trim.start)}</strong>
              </span>
              <span>
                Fim <strong className="mono">{formatTimecode(trim.end)}</strong>
              </span>
              <span>
                Recorte <strong className="mono">{formatTimecode(length)}</strong>
                <span className="muted"> ({formatTrimLength(length)})</span>
              </span>
              {speed !== 1 ? (
                <span className="muted">
                  Final com {formatEditSpeed(speed)}: {formatEditDuration(length / speed)}
                </span>
              ) : null}
            </>
          ) : null}
        </div>

        {duration !== null && trim !== null ? (
          <TrimTimeline
            src={media.original_url}
            duration={duration}
            trim={trim}
            playhead={playhead}
            zoom={zoom}
            onDrag={onDrag}
            onDragEnd={() => undefined}
            onNudge={onNudge}
          />
        ) : (
          <div className="trim-timeline-scroll trim-timeline-empty" />
        )}

        <div className="trim-toolbar">
          <div className="trim-group" role="group" aria-label="Reprodução">
            <button
              type="button"
              className="btn btn-sm btn-icon"
              disabled={!ready}
              aria-label="Ir para o início do recorte (Home)"
              title="Ir para o início do recorte (Home)"
              onClick={() => trim && moveTo(trim.start, "start")}
            >
              <SkipBack size={14} strokeWidth={1.75} aria-hidden="true" />
            </button>
            <button type="button" className="btn btn-sm" disabled={!ready} title="Voltar 1 s (Shift+←)"
              onClick={() => stepBySeconds(-1)}>
              −1 s
            </button>
            <button
              type="button"
              className="btn btn-sm btn-icon"
              disabled={!ready}
              aria-label="Voltar 1 quadro (←)"
              title="Voltar 1 quadro (←)"
              onClick={() => stepFrames(-1)}
            >
              <ChevronLeft size={14} strokeWidth={1.75} aria-hidden="true" />
            </button>
            <button
              type="button"
              className="btn btn-sm btn-primary trim-play"
              disabled={!ready}
              aria-label={playing ? "Pausar (Espaço)" : "Tocar (Espaço)"}
              title={playing ? "Pausar (Espaço)" : "Tocar (Espaço)"}
              onClick={togglePlay}
            >
              {playing ? (
                <Pause size={14} strokeWidth={1.75} aria-hidden="true" />
              ) : (
                <Play size={14} strokeWidth={1.75} aria-hidden="true" />
              )}
            </button>
            <button
              type="button"
              className="btn btn-sm btn-icon"
              disabled={!ready}
              aria-label="Avançar 1 quadro (→)"
              title="Avançar 1 quadro (→)"
              onClick={() => stepFrames(1)}
            >
              <ChevronRight size={14} strokeWidth={1.75} aria-hidden="true" />
            </button>
            <button type="button" className="btn btn-sm" disabled={!ready} title="Avançar 1 s (Shift+→)"
              onClick={() => stepBySeconds(1)}>
              +1 s
            </button>
            <button
              type="button"
              className="btn btn-sm btn-icon"
              disabled={!ready}
              aria-label="Ir para o fim do recorte (End)"
              title="Ir para o fim do recorte (End)"
              onClick={() => trim && moveTo(trim.end, "end")}
            >
              <SkipForward size={14} strokeWidth={1.75} aria-hidden="true" />
            </button>
          </div>

          <div className="trim-group" role="group" aria-label="Marcar recorte">
            <button type="button" className="btn btn-sm" disabled={!ready} onClick={() => changeStart(playhead)}>
              Marcar início (I)
            </button>
            <button type="button" className="btn btn-sm" disabled={!ready} onClick={() => changeEnd(playhead)}>
              Marcar fim (O)
            </button>
            <button
              type="button"
              className="btn btn-sm"
              disabled={!ready}
              aria-pressed={regionPlaying}
              onClick={() => (regionPlaying ? pause() : playRegion())}
            >
              {regionPlaying ? "Parar" : "Tocar só o recorte"}
            </button>
            <label className="trim-check" title="Repetir o recorte ao tocar só o recorte">
              <input type="checkbox" checked={loop} disabled={!ready} onChange={(e) => setLoop(e.target.checked)} />
              <Repeat size={13} strokeWidth={1.75} aria-hidden="true" />
              Repetir
            </label>
          </div>

          <div className="trim-group" role="group" aria-label="Zoom da linha do tempo">
            <button
              type="button"
              className="btn btn-sm btn-icon"
              disabled={!ready || zoomIndex <= 0}
              aria-label="Diminuir zoom da linha do tempo"
              title="Diminuir zoom"
              onClick={() => setZoom(ZOOM_LEVELS[Math.max(zoomIndex - 1, 0)] ?? 1)}
            >
              <ZoomOut size={14} strokeWidth={1.75} aria-hidden="true" />
            </button>
            <span className="muted small mono trim-zoom" aria-live="polite">
              {zoom}x
            </span>
            <button
              type="button"
              className="btn btn-sm btn-icon"
              disabled={!ready || zoomIndex >= ZOOM_LEVELS.length - 1}
              aria-label="Aumentar zoom da linha do tempo"
              title="Aumentar zoom"
              onClick={() => setZoom(ZOOM_LEVELS[Math.min(zoomIndex + 1, ZOOM_LEVELS.length - 1)] ?? zoom)}
            >
              <ZoomIn size={14} strokeWidth={1.75} aria-hidden="true" />
            </button>
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              disabled={!ready || (trim !== null && duration !== null && isFullTrim(trim, duration))}
              onClick={() => duration !== null && setTrim(fullTrim(duration))}
            >
              Limpar recorte
            </button>
          </div>
        </div>

        <div className="trim-fields">
          <TimeField label="Início" value={trim?.start ?? 0} disabled={!ready} onCommit={changeStart} />
          <TimeField label="Fim" value={trim?.end ?? 0} disabled={!ready} onCommit={changeEnd} />
          <p id={helpId} className="muted small trim-help">
            Arraste os puxadores ou a agulha na linha do tempo. Espaço toca/pausa · I marca o início e O o fim na
            agulha · ← / → 1 quadro{fps ? ` (${formatFps(fps)})` : " (1/30 s)"} · Shift+← / → 1 s · Home / End vão ao
            início / fim do recorte · Esc fecha sem aplicar. O fim é exclusivo: o quadro que começa no fim fica de
            fora. Mínimo de 0,1 s.
          </p>
        </div>

        <div className="modal-actions trim-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancelar
          </button>
          <button type="button" className="btn btn-primary" disabled={!ready} onClick={apply}>
            Aplicar recorte
          </button>
        </div>
      </div>
    </div>
  );
}
