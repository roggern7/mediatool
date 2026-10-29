"""Verificação pura (sem FFmpeg) da duração da saída quando há corte por duração alvo (max_duration) ou recorte
manual (cut_end)."""

from __future__ import annotations

from .commands import StreamSpan
from .probe import ProbeResult, _float, audio_stream, parse_rate, video_stream

# Vídeo e container: ±1 quadro + esta folga (s). O vídeo é quantizado por quadro.
VIDEO_SLACK = 0.005
# Áudio: |fim - fim esperado| <= isto (s). Um quadro AAC de 1024 amostras a 48 kHz é 21,3 ms.
AUDIO_TOLERANCE = 0.025
# Início de cada stream: |início - início esperado| <= isto (s).
MAX_START = 0.05
# Duração de um quadro quando o fps é desconhecido (s).
UNKNOWN_FRAME = 0.1

NAMES = {"video": "vídeo", "audio": "áudio"}


def _frame(vs: dict | None, fps_hint: float | None) -> float:
    fps = None
    if vs is not None:
        fps = parse_rate(vs.get("avg_frame_rate")) or parse_rate(vs.get("r_frame_rate"))
    fps = fps or fps_hint
    return 1.0 / fps if fps and fps > 0 else UNKNOWN_FRAME


def check_output_duration(out_probe: ProbeResult, spans: dict[str, StreamSpan], fps_hint: float | None) -> str | None:
    """None se a saída bate com os spans esperados (commands.expected_spans); senão uma mensagem em pt-BR.

    Tudo na linha do tempo de saída (frame = 1/fps do vídeo de saída, ou `fps_hint`, ou 0,1 s):
    - fim do vídeo a no máximo frame + 5 ms do esperado; fim do áudio a no máximo 25 ms;
    - fim do container (start + duração) a no máximo frame + 5 ms do maior fim esperado;
    - início de cada stream a no máximo 50 ms do esperado (vídeo: max(50 ms, frame + 5 ms), porque depois de um
      corte inicial fora da grade de quadros o primeiro quadro começa até um quadro depois) e o deslocamento
      vídeo - áudio a no máximo frame + 25 ms do deslocamento esperado (sincronia preservada);
    - stream cuja duração a origem não informa: só o limite superior do fim.
    Duração ausente de uma stream de saída usa o fim do container.
    """
    raw = out_probe.raw or {}
    fmt = raw.get("format") or {}
    vs = video_stream(raw)
    frame = _frame(vs, fps_hint)
    origin = _float(fmt.get("start_time")) or 0.0
    container = _float(fmt.get("duration"))
    if container is None:
        container = out_probe.duration
    if container is None:
        return "duração da saída desconhecida"
    container_end = origin + container

    actual: dict[str, tuple[float, float]] = {}
    for kind, stream in (("video", vs), ("audio", audio_stream(raw))):
        if kind not in spans:
            continue
        if stream is None:
            return f"saída sem stream de {NAMES[kind]}"
        start = _float(stream.get("start_time"))
        start = origin if start is None else start
        duration = _float(stream.get("duration"))
        actual[kind] = (start, container_end if duration is None else start + duration)

    for kind, (start, end) in actual.items():
        span = spans[kind]
        tol = frame + VIDEO_SLACK if kind == "video" else AUDIO_TOLERANCE
        low = span.end - tol if span.end_known else start
        if not low <= end <= span.end + tol:
            return f"{NAMES[kind]} termina em {end:.3f}s, esperado {span.end:.3f}s (±{tol:.3f}s)"
        if abs(start - span.start) > (max(MAX_START, frame + VIDEO_SLACK) if kind == "video" else MAX_START):
            return f"{NAMES[kind]} começa em {start:.3f}s, esperado {span.start:.3f}s"

    expected_end = max(span.end for span in spans.values())
    tol = frame + VIDEO_SLACK
    low = expected_end - tol if all(span.end_known for span in spans.values()) else 0.0
    if not low <= container_end <= expected_end + tol:
        return f"arquivo termina em {container_end:.3f}s, esperado {expected_end:.3f}s (±{tol:.3f}s)"

    if "video" in actual and "audio" in actual:
        offset = actual["video"][0] - actual["audio"][0]
        expected = spans["video"].start - spans["audio"].start
        if abs(offset - expected) > frame + AUDIO_TOLERANCE:
            return (f"vídeo e áudio dessincronizados: deslocamento {offset * 1000:.0f} ms, "
                    f"esperado {expected * 1000:.0f} ms")
    return None
