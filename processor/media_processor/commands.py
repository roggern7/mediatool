"""Construção pura (sem executar nada) dos argumentos do ffmpeg."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .options import ProcessingOptions
from .probe import PACKETS_DURATION_KEY, ProbeResult, audio_stream, parse_rate, video_stream

X264_PRESET = "medium"
X264_CRF = "20"
AAC_BITRATE = "160k"
JPEG_QUALITY = "2"
MAX_FPS = 60.0
EPS = 1e-6

# Stream copy (só cleanup) sempre remuxa para mp4: é o único container em que o cleanup fica completo
# (matroska/webm gravam MuxingApp "Lavf", exposto como tag encoder; mov grava vendor_id "FFMP").
# Codecs fora destas listas -> reencode forçado.
MP4_COPY_VIDEO = {"h264", "hevc", "av1", "vp9", "mpeg4"}
MP4_COPY_AUDIO = {"aac", "mp3", "opus", "flac", "alac", "ac3", "eac3"}

ALPHA_HINTS = ("yuva", "rgba", "argb", "bgra", "abgr", "gbrap", "ya8", "ya16")

# Tags de stream zeradas no cleanup (o muxer mp4 deixa de gravar a tag quando vazia)
CLEARED_STREAM_TAGS = ("encoder",)


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
    filters_active = (
        opts.trim_start > 0
        or opts.mirror
        or _speed_active(opts)
        or (opts.auto_adjust and _needs_adjust(probe))
    )
    encoded_duration = max(duration - opts.trim_start, 0.0) / opts.speed
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
    if opts.trim_start > 0:
        # trim por filtro na linha do tempo da entrada (já relativa ao start_time do container)
        chain.append(f"trim=start={_num(opts.trim_start)}")
    if opts.auto_adjust and not _sar_is_square(vs):
        # pixels quadrados: corrige a largura pelo SAR mantendo a altura (sem deformar a imagem exibida)
        chain.append("scale=w=trunc(iw*sar/2)*2:h=trunc(ih/2)*2")
    elif _odd_dims(vs):
        # yuv420p exige dimensões pares: recorta 1 px na borda (não reescala, não deforma)
        chain.append("crop=w=trunc(iw/2)*2:h=trunc(ih/2)*2:x=0:y=0")
    if opts.mirror:
        chain.append("hflip")
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
    if opts.auto_adjust:
        chain.append("setsar=1")
    return chain


def audio_filters(opts: ProcessingOptions) -> list[str]:
    chain: list[str] = []
    if opts.trim_start > 0:
        t = _num(opts.trim_start)
        chain += [f"atrim=start={t}", f"asetpts=PTS-{t}/TB"]
    if _speed_active(opts):
        chain += atempo_chain(opts.speed)
    return chain


def image_filters(probe: ProbeResult, opts: ProcessingOptions) -> list[str]:
    return ["hflip"] if opts.mirror else []


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
        af = audio_filters(opts)
        if aus is not None and af:
            cmd += ["-filter:a", ",".join(af)]
        cmd += ["-c:v", "libx264", "-preset", X264_PRESET, "-crf", X264_CRF, "-profile:v", "high",
                "-pix_fmt", "yuv420p"]
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
