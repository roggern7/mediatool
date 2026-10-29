import { type KeyboardEvent, type PointerEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  filmstripCount,
  filmstripTimes,
  formatTimecode,
  fractionToTime,
  rulerLabel,
  rulerStep,
  rulerTicks,
  timeToFraction,
  type Trim,
} from "../../lib/trim";

export type DragTarget = "start" | "end" | "playhead";

/** Margem interna da linha do tempo (px) de cada lado, igual à largura do puxador (CSS `.trim-timeline`): com início
 * em 0 ou fim na duração o puxador fica inteiro dentro da área visível e clicável, sem rolagem lateral a 1x. */
const HANDLE_PAD = 16;

/**
 * Tira de miniaturas gerada no navegador: um `<video>` escondido vai de quadro em quadro (um seek por vez, sem travar
 * a tela) e cada quadro vira uma imagem num canvas. Devolve as imagens já prontas (null = ainda carregando).
 */
function useFilmstrip(src: string, duration: number, count: number): (string | null)[] {
  const key = `${src}|${duration}|${count}`;
  const [state, setState] = useState<{ key: string; frames: (string | null)[] }>({ key: "", frames: [] });
  useEffect(() => {
    if (duration <= 0 || count <= 0) return;
    let cancelled = false;
    const video = document.createElement("video");
    video.muted = true;
    video.preload = "auto";
    video.playsInline = true;
    const canvas = document.createElement("canvas");
    const frames: (string | null)[] = new Array<string | null>(count).fill(null);
    // Esperas em andamento: ao fechar, cada uma solta o timer e os listeners na hora.
    const pending = new Set<() => void>();
    const wait = (event: string) =>
      new Promise<void>((resolve, reject) => {
        const done = (ok: boolean) => {
          window.clearTimeout(timer);
          video.removeEventListener(event, onOk);
          video.removeEventListener("error", onFail);
          pending.delete(onFail);
          if (ok) resolve();
          else reject(new Error(event));
        };
        const onOk = () => done(true);
        const onFail = () => done(false);
        const timer = window.setTimeout(onFail, 15000);
        pending.add(onFail);
        video.addEventListener(event, onOk);
        video.addEventListener("error", onFail);
      });
    void (async () => {
      const loaded = wait("loadeddata");
      video.src = src;
      await loaded;
      const height = 72;
      const ratio = video.videoWidth > 0 && video.videoHeight > 0 ? video.videoWidth / video.videoHeight : 16 / 9;
      canvas.width = Math.max(16, Math.round(height * ratio));
      canvas.height = height;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      for (const [i, t] of filmstripTimes(duration, count).entries()) {
        if (cancelled) return;
        const seeked = wait("seeked");
        video.currentTime = t;
        await seeked;
        if (cancelled) return;
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        frames[i] = canvas.toDataURL("image/jpeg", 0.7);
        setState({ key, frames: frames.slice() });
        await new Promise((resolve) => window.setTimeout(resolve, 0)); // deixa a tela respirar entre quadros
      }
    })().catch(() => undefined);
    return () => {
      cancelled = true;
      for (const cancel of [...pending]) cancel();
      video.removeAttribute("src");
      video.load(); // solta o arquivo
    };
  }, [key, src, duration, count]);
  return state.key === key ? state.frames : [];
}

/**
 * Linha do tempo do editor de recorte: régua, tira de miniaturas, região do recorte com dois puxadores e a agulha.
 * Arrastar em qualquer ponto leva a agulha até lá (varredura); arrastar um puxador muda início/fim. As posições são
 * lidas do ponteiro uma vez por quadro de tela (requestAnimationFrame). Com zoom, a linha fica mais larga e rola.
 */
export function TrimTimeline({
  src,
  duration,
  trim,
  playhead,
  zoom,
  onDrag,
  onDragEnd,
  onNudge,
}: {
  src: string;
  duration: number;
  trim: Trim;
  playhead: number;
  zoom: number;
  /** Novo tempo (s) do alvo arrastado, já preso a [0, duração]. */
  onDrag: (target: DragTarget, time: number) => void;
  onDragEnd: (target: DragTarget) => void;
  /** Teclado num puxador/agulha: `frames` (±1) ou `seconds` (±1 com Shift). */
  onNudge: (target: DragTarget, direction: number, unit: "frames" | "seconds") => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ target: DragTarget; x: number; frame: number | null } | null>(null);
  const [width, setWidth] = useState(0);
  const [count, setCount] = useState(0);
  const [dragging, setDragging] = useState(false);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => {
      setWidth(el.clientWidth);
      setCount((c) => c || filmstripCount(el.clientWidth)); // fixado na primeira medida (não regera ao redimensionar)
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const frames = useFilmstrip(src, duration, count);
  const inner = width * zoom;
  const trackWidth = Math.max(inner - 2 * HANDLE_PAD, 0);

  // Com zoom, mantém a agulha à vista (fora do arraste, para não brigar com o ponteiro).
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || zoom <= 1 || dragging) return;
    const x = HANDLE_PAD + timeToFraction(playhead, duration) * Math.max(el.scrollWidth - 2 * HANDLE_PAD, 0);
    if (x < el.scrollLeft + 16 || x > el.scrollLeft + el.clientWidth - 16) {
      el.scrollLeft = Math.max(0, x - el.clientWidth / 2);
    }
  }, [playhead, zoom, duration, dragging]);

  useEffect(
    () => () => {
      if (drag.current?.frame) cancelAnimationFrame(drag.current.frame);
    },
    [],
  );

  function apply(x: number) {
    const el = trackRef.current;
    const current = drag.current;
    if (!el || !current) return;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return;
    onDrag(current.target, fractionToTime((x - rect.left) / rect.width, duration));
  }

  function onPointerDown(e: PointerEvent<HTMLDivElement>) {
    if (e.button !== 0 || duration <= 0) return;
    const handle = (e.target as HTMLElement).closest("[data-drag]")?.getAttribute("data-drag");
    const target: DragTarget = handle === "start" || handle === "end" ? handle : "playhead";
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    (e.target as HTMLElement).closest<HTMLElement>("[data-drag]")?.focus();
    drag.current = { target, x: e.clientX, frame: null };
    setDragging(true);
    apply(e.clientX);
  }

  function onPointerMove(e: PointerEvent<HTMLDivElement>) {
    const current = drag.current;
    if (!current) return;
    current.x = e.clientX;
    if (current.frame === null) {
      current.frame = requestAnimationFrame(() => {
        if (!drag.current) return;
        drag.current.frame = null;
        apply(drag.current.x);
      });
    }
  }

  function onPointerEnd(e: PointerEvent<HTMLDivElement>) {
    const current = drag.current;
    if (!current) return;
    if (current.frame !== null) cancelAnimationFrame(current.frame);
    apply(e.clientX);
    drag.current = null;
    setDragging(false);
    onDragEnd(current.target);
  }

  function keyHandler(target: DragTarget) {
    return (e: KeyboardEvent<HTMLDivElement>) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      e.stopPropagation(); // o atalho global de ← / → move a agulha; aqui move o próprio puxador
      onNudge(target, e.key === "ArrowLeft" ? -1 : 1, e.shiftKey ? "seconds" : "frames");
    };
  }

  const pct = (t: number) => `${timeToFraction(t, duration) * 100}%`;
  const step = rulerStep(duration, trackWidth);
  const ticks = rulerTicks(duration, step);
  const minorPx = duration > 0 ? (step / 5) * (trackWidth / duration) : 0;

  return (
    <div className="trim-timeline-scroll" ref={scrollRef}>
      <div
        className={`trim-timeline${dragging ? " dragging" : ""}`}
        style={{ width: inner > 0 ? inner : "100%" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerEnd}
        onPointerCancel={onPointerEnd}
      >
        <div className="trim-track" ref={trackRef}>
          <div
            className="trim-ruler"
            aria-hidden="true"
            style={minorPx >= 4 ? { backgroundSize: `${minorPx}px 5px` } : { backgroundImage: "none" }}
          >
            {ticks.map((t) => (
              <span
                key={t}
                className={`trim-tick${timeToFraction(t, duration) > 0.96 ? " last" : ""}`}
                style={{ left: pct(t) }}
              >
                {rulerLabel(t, step)}
              </span>
            ))}
          </div>
          <div className="trim-strip" aria-hidden="true">
            {Array.from({ length: count }, (_, i) => (
              <div key={i} className="trim-strip-cell" style={{ width: `${100 / count}%` }}>
                {frames[i] ? <img src={frames[i] ?? undefined} alt="" draggable={false} /> : null}
              </div>
            ))}
            <div className="trim-shade" style={{ left: 0, width: pct(trim.start) }} />
            <div className="trim-shade" style={{ left: pct(trim.end), right: 0 }} />
            <div
              className="trim-region"
              style={{ left: pct(trim.start), right: `${100 - timeToFraction(trim.end, duration) * 100}%` }}
            />
            <div
              className="trim-handle start"
              data-drag="start"
              role="slider"
              tabIndex={0}
              aria-label="Início do recorte"
              aria-valuemin={0}
              aria-valuemax={duration}
              aria-valuenow={trim.start}
              aria-valuetext={formatTimecode(trim.start)}
              style={{ left: pct(trim.start) }}
              onKeyDown={keyHandler("start")}
            />
            <div
              className="trim-handle end"
              data-drag="end"
              role="slider"
              tabIndex={0}
              aria-label="Fim do recorte"
              aria-valuemin={0}
              aria-valuemax={duration}
              aria-valuenow={trim.end}
              aria-valuetext={formatTimecode(trim.end)}
              style={{ left: pct(trim.end) }}
              onKeyDown={keyHandler("end")}
            />
          </div>
          <div
            className="trim-playhead"
            data-drag="playhead"
            role="slider"
            tabIndex={0}
            aria-label="Agulha"
            aria-valuemin={0}
            aria-valuemax={duration}
            aria-valuenow={playhead}
            aria-valuetext={formatTimecode(playhead)}
            style={{ left: pct(playhead) }}
            onKeyDown={keyHandler("playhead")}
          />
        </div>
      </div>
    </div>
  );
}
