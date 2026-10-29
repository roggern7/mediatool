"""Construção pura (sem executar nada) dos argumentos do ffmpeg."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .options import ProcessingOptions
from .probe import PACKETS_DURATION_KEY, ProbeResult, _float, audio_stream, parse_rate, video_stream

X264_PRESET = "medium"
X264_CRF = "20"
AAC_BITRATE = "160k"
JPEG_QUALITY = "2"
MAX_FPS = 60.0
EPS = 1e-6
# O corte final (trim_end) é ignorado quando sobraria menos que isto (s) depois dos dois cortes: vídeo curto não
# falha nem fica curto demais; o resto do preset é aplicado normalmente.
MIN_DURATION_AFTER_TRIM_END = 1.0

# Stream copy (só cleanup) sempre remuxa para mp4: é o único container em que o cleanup fica completo
# (matroska/webm gravam MuxingApp "Lavf", exposto como tag encoder; mov grava vendor_id "FFMP").
# Codecs fora destas listas -> reencode forçado.
MP4_COPY_VIDEO = {"h264", "hevc", "av1", "vp9", "mpeg4"}
MP4_COPY_AUDIO = {"aac", "mp3", "opus", "flac", "alac", "ac3", "eac3"}

ALPHA_HINTS = ("yuva", "rgba", "argb", "bgra", "abgr", "gbrap", "ya8", "ya16")

# Tags de stream zeradas no cleanup (o muxer mp4 deixa de gravar a tag quando vazia)
CLEARED_STREAM_TAGS = ("encoder",)

# Nitidez (sharpen): Contrast Adaptive Sharpening só na luma (não realça ruído de croma).
SHARPEN_FILTER = "cas=strength=0.25:planes=1"


@dataclass(frozen=True)
class Plan:
    kind: str  # "encode" | "copy" | "image"
    suffix: str
    muxer: str
    forced_reencode: bool
    expected_duration: float | None  # duração esperada da saída (para progresso)


def _num(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _speed_active(opts: ProcessingOptions) -> bool:
    return abs(opts.speed - 1.0) > EPS


def _color_args(opts: ProcessingOptions) -> list[str]:
    """Argumentos do `eq`, só os que não são neutros."""
    args: list[str] = []
    if abs(opts.brightness) > EPS:
        args.append(f"brightness={_num(opts.brightness)}")
    if abs(opts.contrast - 1.0) > EPS:
        args.append(f"contrast={_num(opts.contrast)}")
    if abs(opts.saturation - 1.0) > EPS:
        args.append(f"saturation={_num(opts.saturation)}")
    return args


def _look_active(opts: ProcessingOptions) -> bool:
    """Zoom, cor ou granulação ligados (valem para vídeo e imagem)."""
    return opts.zoom > EPS or bool(_color_args(opts)) or opts.grain > EPS


def effective_trim_end(probe: ProbeResult, opts: ProcessingOptions) -> float:
    """Corte final realmente aplicado: 0 em imagens e quando o vídeo ficaria curto demais (proteção)."""
    if probe.media_type != "video" or opts.trim_end <= EPS or not probe.duration:
        return 0.0
    if probe.duration - opts.trim_start - opts.trim_end < MIN_DURATION_AFTER_TRIM_END:
        return 0.0
    return opts.trim_end


def effective_max_duration(probe: ProbeResult, opts: ProcessingOptions) -> float | None:
    """Duração alvo realmente aplicada à saída, ou None sem corte.

    None em imagens, com max_duration desligado e quando a duração natural da saída (depois dos cortes e da
    velocidade) já cabe no alvo: vídeo menor não é prolongado nem recortado.
    """
    if probe.media_type != "video" or opts.max_duration <= EPS or not probe.duration:
        return None
    natural = (probe.duration - opts.trim_start - effective_trim_end(probe, opts)) / opts.speed
    if natural <= opts.max_duration + EPS:
        return None
    return opts.max_duration


@dataclass(frozen=True)
class StreamSpan:
    """Início e fim esperados de uma stream na linha do tempo de saída (s). `end_known` False: a origem não informa
    a duração da stream e o fim só tem limite superior."""

    start: float
    end: float
    end_known: bool


def expected_spans(probe: ProbeResult, opts: ProcessingOptions) -> dict[str, StreamSpan] | None:
    """Spans esperados de vídeo e áudio quando há corte por duração alvo (None sem corte).

    Cada stream mantém o próprio início (o ffmpeg só desconta o start_time do container, então o deslocamento entre
    as streams é preservado) e termina em min(alvo, fim natural daquela stream): nenhuma é prolongada.
    """
    target = effective_max_duration(probe, opts)
    if target is None:
        return None
    raw = probe.raw or {}
    origin = _float((raw.get("format") or {}).get("start_time")) or 0.0
    limit = (probe.duration or 0.0) - effective_trim_end(probe, opts)  # fim de origem após trim_end (relativo)
    spans: dict[str, StreamSpan] = {}
    streams = [("video", video_stream(raw))]
    if probe.has_audio:
        streams.append(("audio", audio_stream(raw)))
    for kind, stream in streams:
        if stream is None:
            continue
        # sem audio_sync_start o atempo mantém o início do áudio (não é dividido pela velocidade)
        scale = 1.0 if kind == "audio" and not opts.audio_sync_start else opts.speed
        start = _float(stream.get("start_time"))
        start = 0.0 if start is None else max(start - origin, 0.0)
        duration = _float(stream.get("duration"))
        end = min(start + duration, limit) if duration else limit
        out_start = max(start - opts.trim_start, 0.0) / scale
        out_end = max(end - opts.trim_start, 0.0) / opts.speed
        spans[kind] = StreamSpan(min(out_start, target), min(out_end, target), bool(duration))
    return spans


def _trim_args(probe: ProbeResult, opts: ProcessingOptions) -> str:
    """Argumentos de trim/atrim (`start=..`, `end=..`), iguais para vídeo e áudio; vazio sem corte."""
    args: list[str] = []
    if opts.trim_start > 0:
        args.append(f"start={_num(opts.trim_start)}")
    trim_end = effective_trim_end(probe, opts)
    if trim_end > 0:
        # fim na linha do tempo da origem (a mesma do corte inicial): duração - trim_end
        args.append(f"end={_num(probe.duration - trim_end)}")
    return ":".join(args)


def _pitch_factor(probe: ProbeResult, opts: ProcessingOptions) -> tuple[float, int] | None:
    """(fator de tom, sample rate de origem) quando o tom está ligado e há áudio com sample rate conhecido."""
    if abs(opts.pitch_shift) <= EPS or not probe.has_audio:
        return None
    try:
        rate = int((audio_stream(probe.raw) or {}).get("sample_rate"))
    except (TypeError, ValueError):
        return None
    return (1.0 + opts.pitch_shift / 100.0, rate) if rate > 0 else None


def _sar_is_square(vs: dict) -> bool:
    sar = vs.get("sample_aspect_ratio")
    if not sar or sar in ("0:1", "N/A", "1:1"):
        return True
    num, _, den = str(sar).partition(":")
    try:
        n, d = int(num), int(den)
    except ValueError:
        return True
    return n == d or n == 0 or d == 0


def _odd_dims(vs: dict) -> bool:
    return bool((vs.get("width") or 0) % 2 or (vs.get("height") or 0) % 2)


def _needs_adjust(probe: ProbeResult) -> bool:
    """auto_adjust só exige reencode se há algo a corrigir na origem."""
    vs = video_stream(probe.raw) or {}
    pix = vs.get("pix_fmt")
    return bool(
        probe.rotation
        or _odd_dims(vs)
        or not _sar_is_square(vs)
        or (pix and pix not in ("yuv420p", "yuvj420p"))
    )


def _frame_size(probe: ProbeResult, opts: ProcessingOptions) -> tuple[int, int] | None:
    """Dimensões do frame no ponto da cadeia em que o zoom entra (depois de rotação, SAR e recorte par)."""
    vs = video_stream(probe.raw) or {}
    image = probe.media_type == "image"
    if image or opts.auto_adjust:
        w, h = probe.width, probe.height  # o autorotate do ffmpeg já aplicou a rotação
    else:
        w, h = vs.get("width"), vs.get("height")  # -noautorotate: dimensões codificadas
    if not isinstance(w, int) or not isinstance(h, int) or w < 2 or h < 2:
        return None
    if image:
        return w, h
    if opts.auto_adjust and not _sar_is_square(vs):
        num, _, den = str(vs.get("sample_aspect_ratio")).partition(":")
        n, d = int(num), int(den)
        if probe.rotation in (90, 270):
            n, d = d, n
        # mesma conta do ffmpeg (racional já invertido pela rotação): 1/(n/d) difere de d/n no último bit e o
        # trunc cairia em outro par
        w = int(w * (n / d) / 2) * 2
    return (w // 2 * 2, h // 2 * 2) if w >= 2 else None


def _zoom_filters(probe: ProbeResult, opts: ProcessingOptions) -> list[str]:
    """Recorte central de 1/(1+zoom/100) e reescala de volta ao tamanho que o frame teria sem o zoom."""
    size = _frame_size(probe, opts) if opts.zoom > EPS else None
    if size is None:
        return []
    w, h = size
    factor = 1.0 + opts.zoom / 100.0
    # recorte em dimensões pares, nunca abaixo de 2 px nem acima do frame (mídia minúscula não quebra)
    cw = min(max(2, round(w / factor / 2) * 2), w)
    ch = min(max(2, round(h / factor / 2) * 2), h)
    if cw == w and ch == h:
        return []
    return [f"crop=w={cw}:h={ch}", f"scale=w={w}:h={h}:flags=lanczos", *_zoom_sar_filters(probe, opts)]


def _zoom_sar_filters(probe: ProbeResult, opts: ProcessingOptions) -> list[str]:
    """`setsar` que devolve ao frame o SAR que ele tinha antes do zoom.

    O recorte arredondado para par tem proporção um pouco diferente da do frame, e o `scale` de volta ao tamanho
    original compensa a diferença alterando o SAR (ele preserva o DAR). Sem isto a saída sairia com SAR diferente
    do que teria sem o zoom.
    """
    image = probe.media_type == "image"
    if not image and opts.auto_adjust:
        return []  # o setsar=1 do fim da cadeia já cobre
    num, _, den = str((video_stream(probe.raw) or {}).get("sample_aspect_ratio") or "").partition(":")
    try:
        n, d = int(num), int(den)
    except ValueError:
        return []
    if n <= 0 or d <= 0:
        return []  # SAR desconhecido: o scale não o altera
    if n == d:
        return ["setsar=1"]
    if image and probe.rotation in (90, 270):
        n, d = d, n  # o autorotate do ffmpeg já girou o frame (e inverteu o SAR)
    # max: o setsar aproxima a razão com termos até 100 por padrão
    return [f"setsar=r={n}/{d}:max={max(n, d, 100)}"]


def _look_filters(probe: ProbeResult, opts: ProcessingOptions) -> list[str]:
    """Zoom, cor e granulação, nesta ordem (vídeo e imagem)."""
    chain = _zoom_filters(probe, opts)
    color = _color_args(opts)
    if color:
        chain.append("eq=" + ":".join(color))
    if opts.sharpen and probe.media_type == "video":
        # depois da cor e antes da granulação (a granulação não deve ser realçada); ignorado em imagens
        chain.append(SHARPEN_FILTER)
    if opts.grain > EPS:
        g = _num(opts.grain)
        pix = str((video_stream(probe.raw) or {}).get("pix_fmt") or "")
        if probe.media_type == "image" and any(h in pix for h in ALPHA_HINTS):
            # imagem com transparência: granulação só nos componentes de cor (o canal alpha fica intacto)
            chain.append(f"noise=c0s={g}:c0f=t:c1s={g}:c1f=t:c2s={g}:c2f=t")
        else:
            chain.append(f"noise=alls={g}:allf=t")
    return chain


def plan(probe: ProbeResult, opts: ProcessingOptions) -> Plan:
    if probe.media_type == "image":
        # Imagens sempre são recodificadas (JPEG q2 ou PNG sem perda): é o jeito de garantir saída sem EXIF
        # e com a orientação aplicada. Com reencode=False isso conta como reencode forçado.
        # PNG continua PNG; WebP com alpha vira PNG (preserva transparência); o resto vira JPEG.
        pix = str((video_stream(probe.raw) or {}).get("pix_fmt") or "")
        png = probe.video_codec in ("png", "apng") or (
            probe.video_codec == "webp" and any(h in pix for h in ALPHA_HINTS)
        )
        return Plan("image", ".png" if png else ".jpg", "image2", not opts.reencode, None)

    duration = probe.duration or 0.0
    trim_end = effective_trim_end(probe, opts)
    target = effective_max_duration(probe, opts)
    filters_active = (
        opts.trim_start > 0
        or opts.mirror
        or _speed_active(opts)
        or (opts.auto_adjust and _needs_adjust(probe))
        or _look_active(opts)
        or trim_end > 0
        or _pitch_factor(probe, opts) is not None
        or opts.sharpen
        or target is not None  # corte exato exige recodificar (stream copy só corta em keyframe)
    )
    encoded_duration = max(duration - opts.trim_start - trim_end, 0.0) / opts.speed
    if target is not None:
        encoded_duration = min(encoded_duration, target)
    if opts.reencode:
        return Plan("encode", ".mp4", "mp4", False, encoded_duration)
    if filters_active:
        return Plan("encode", ".mp4", "mp4", True, encoded_duration)
    if probe.video_codec in MP4_COPY_VIDEO and (not probe.has_audio or probe.audio_codec in MP4_COPY_AUDIO):
        return Plan("copy", ".mp4", "mp4", False, duration)
    # codecs que não podem ir para mp4 sem recodificar
    return Plan("encode", ".mp4", "mp4", True, encoded_duration)


def atempo_chain(speed: float) -> list[str]:
    """atempo aceita 0.5..2 por instância; encadeia fora disso."""
    parts: list[str] = []
    s = speed
    while s > 2.0 + EPS:
        parts.append("atempo=2")
        s /= 2.0
    while s < 0.5 - EPS:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={_num(s)}")
    return parts


def video_filters(probe: ProbeResult, opts: ProcessingOptions) -> list[str]:
    vs = video_stream(probe.raw) or {}
    chain: list[str] = []
    cut = _trim_args(probe, opts)
    if cut:
        # trim por filtro na linha do tempo da entrada (já relativa ao start_time do container)
        chain.append(f"trim={cut}")
    if opts.auto_adjust and not _sar_is_square(vs):
        # pixels quadrados: corrige a largura pelo SAR mantendo a altura (sem deformar a imagem exibida)
        chain.append("scale=w=trunc(iw*sar/2)*2:h=trunc(ih/2)*2")
    elif _odd_dims(vs):
        # yuv420p exige dimensões pares: recorta 1 px na borda (não reescala, não deforma)
        chain.append("crop=w=trunc(iw/2)*2:h=trunc(ih/2)*2:x=0:y=0")
    if opts.mirror:
        chain.append("hflip")
    chain += _look_filters(probe, opts)
    # Mesmo deslocamento constante (trim_start) em vídeo e áudio: preserva o alinhamento relativo das streams.
    # PTS-STARTPTS não serve aqui porque normalizaria cada stream pelo seu próprio primeiro frame.
    shifted = f"(PTS-{_num(opts.trim_start)}/TB)" if opts.trim_start > 0 else "PTS"
    if _speed_active(opts):
        chain.append(f"setpts={shifted}/{_num(opts.speed)}")
    elif opts.trim_start > 0:
        chain.append(f"setpts={shifted}")
    if _speed_active(opts):
        if vs.get(PACKETS_DURATION_KEY) and probe.fps:
            source = _num(probe.fps)  # fps medido pelos pacotes (stream sem duração no container)
        else:
            avg = vs.get("avg_frame_rate")
            source = avg if parse_rate(avg) else vs.get("r_frame_rate")
        rate = parse_rate(source)
        if rate:
            # mantém o fps de origem (teto 60) em vez de inflar para fps*speed
            chain.append(f"fps={source if rate <= MAX_FPS else _num(MAX_FPS)}")
    target = effective_max_duration(probe, opts)
    if target is not None:
        # corte na linha do tempo de saída (PTS já começam em 0 e já passaram pela velocidade)
        chain.append(f"trim=end={_num(target)}")
    if opts.auto_adjust:
        chain.append("setsar=1")
    return chain


def audio_filters(probe: ProbeResult, opts: ProcessingOptions) -> list[str]:
    chain: list[str] = []
    cut = _trim_args(probe, opts)
    if cut:
        chain.append(f"atrim={cut}")  # mesmos pontos de corte do vídeo
    if opts.trim_start > 0:
        chain.append(f"asetpts=PTS-{_num(opts.trim_start)}/TB")
    pitch = _pitch_factor(probe, opts)
    if pitch is None:
        if _speed_active(opts):
            chain += atempo_chain(opts.speed)
    else:
        # asetrate muda tom e duração juntos (fator f); o atempo compensa para a velocidade final ser a do preset
        factor, rate = pitch
        chain += [f"asetrate={round(rate * factor)}", f"aresample={rate}"]
        if abs(opts.speed / factor - 1.0) > EPS:
            chain += atempo_chain(opts.speed / factor)
    if opts.audio_sync_start and _speed_active(opts):
        # o atempo mantém o PTS inicial: o início do áudio também é dividido pela velocidade (como o setpts do vídeo)
        chain.append(f"asetpts=PTS-STARTPTS+STARTPTS/{_num(opts.speed)}")
    target = effective_max_duration(probe, opts)
    if target is not None:
        chain.append(f"atrim=end={_num(target)}")  # mesmo ponto do vídeo, depois do atempo (exato por amostra)
    return chain


def image_filters(probe: ProbeResult, opts: ProcessingOptions) -> list[str]:
    return (["hflip"] if opts.mirror else []) + _look_filters(probe, opts)


def abs_path(path: Path) -> str:
    """Caminho absoluto: no Windows começa com letra de drive, no POSIX com "/"; nunca vira opção."""
    text = os.path.abspath(str(path))
    if text.startswith("-"):
        raise ValueError(f"caminho inválido: {text}")
    return text


def _cleanup_args(kind: str) -> list[str]:
    args = ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact"]
    if kind != "copy":
        args += ["-flags:v", "+bitexact"]
    if kind == "encode":
        args += ["-flags:a", "+bitexact"]
    for tag in CLEARED_STREAM_TAGS:
        args += ["-metadata:s:v", f"{tag}="]
        if kind != "image":
            args += ["-metadata:s:a", f"{tag}="]
    return args


def build(ffmpeg: str, src: Path, dst: Path, probe: ProbeResult, opts: ProcessingOptions) -> list[str]:
    p = plan(probe, opts)
    vs = video_stream(probe.raw)
    aus = audio_stream(probe.raw) if p.kind != "image" else None
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats"]

    # ---- opções de entrada ----
    if p.kind == "encode":
        if not opts.auto_adjust:
            cmd += ["-noautorotate"]  # mantém a matriz de rotação como metadata de exibição
        if opts.auto_adjust:
            cmd += ["-fflags", "+genpts"]
    cmd += ["-i", abs_path(src)]

    # ---- mapeamento explícito (ignora capas, legendas, dados) ----
    cmd += ["-map", f"0:{vs['index']}"]
    if aus is not None:
        cmd += ["-map", f"0:{aus['index']}"]

    if p.kind == "image":
        vf = image_filters(probe, opts)
        if vf:
            cmd += ["-filter:v", ",".join(vf)]
        cmd += ["-frames:v", "1", "-update", "1"]
        if opts.metadata_cleanup:
            cmd += _cleanup_args("image")
        if p.suffix == ".png":
            cmd += ["-c:v", "png"]
        else:
            cmd += ["-c:v", "mjpeg", "-q:v", JPEG_QUALITY]
    elif p.kind == "copy":
        cmd += ["-c", "copy"]
        if probe.video_codec == "hevc":
            cmd += ["-tag:v", "hvc1"]  # tag esperada por players da Apple para HEVC em mp4
        if opts.metadata_cleanup:
            cmd += _cleanup_args("copy")
        cmd += ["-movflags", "+faststart"]
    else:
        vf = video_filters(probe, opts)
        if vf:
            cmd += ["-filter:v", ",".join(vf)]
        af = audio_filters(probe, opts)
        if aus is not None and af:
            cmd += ["-filter:a", ",".join(af)]
        cmd += ["-c:v", "libx264", "-preset", X264_PRESET, "-crf", X264_CRF, "-profile:v", "high",
                "-pix_fmt", "yuv420p"]
        if effective_max_duration(probe, opts) is not None:
            # Com corte, o encoder usa a time base dos filtros em vez de 1/fps: sem isso o PTS do vídeo é arredondado
            # para a grade de quadros (vídeo que começa fora da grade desloca até meio quadro e o fim passa do alvo).
            cmd += ["-enc_time_base:v", "filter"]
        if opts.metadata_cleanup:
            # remove NALs SEI (tipo 6): a SEI do x264 traz versão e opções do encoder em texto
            cmd += ["-bsf:v", "filter_units=remove_types=6"]
        if aus is not None:
            cmd += ["-c:a", "aac", "-b:a", AAC_BITRATE]
            channels = aus.get("channels")
            if isinstance(channels, int) and channels > 2:
                cmd += ["-ac", "2"]
        if opts.metadata_cleanup:
            cmd += _cleanup_args("encode")
        # Sem "-avoid_negative_ts make_zero" no mp4: no ffmpeg 9 ele desloca o início das streams
        # (vídeo 0.067s por causa do atraso de B-frames, áudio 0.043s pelo priming do AAC). O padrão do muxer
        # mp4 (edit list) já entrega start_time 0 em ambas; +genpts na entrada regenera PTS ausentes.
        cmd += ["-movflags", "+faststart"]

    cmd += ["-f", p.muxer, abs_path(dst)]
    return cmd
