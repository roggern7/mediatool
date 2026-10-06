"""ffprobe: execução e interpretação do JSON."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .errors import InvalidMediaError
from .tools import popen_kwargs

IMAGE_CODECS = {"mjpeg", "jpegls", "png", "apng", "webp", "webp_anim", "bmp", "tiff", "heif", "gif", "jpeg2000",
                "jpegxl"}
MIN_VIDEO_DURATION = 0.05  # abaixo disso não há "duração útil"
# Chave que run_probe acrescenta à stream de vídeo do JSON quando conta pacotes: soma de duration_time dos pacotes.
PACKETS_DURATION_KEY = "x_packets_duration"


@dataclass
class ProbeResult:
    media_type: Literal["video", "image"]
    format_name: str
    duration: float | None
    width: int | None  # dimensões de exibição (rotação já considerada)
    height: int | None
    fps: float | None
    has_audio: bool
    has_video: bool
    video_codec: str | None
    audio_codec: str | None
    rotation: int  # graus no sentido horário a aplicar para exibir (0, 90, 180, 270)
    tags: dict = field(default_factory=dict)
    stream_tags: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _float(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_rate(value) -> float | None:
    """'30000/1001' -> 29.97; '0/0' ou inválido -> None."""
    if not isinstance(value, str) or "/" not in value:
        return _float(value) if value else None
    num, _, den = value.partition("/")
    n, d = _float(num), _float(den)
    if not n or not d or n <= 0 or d <= 0:
        return None
    return n / d


def video_stream(raw: dict) -> dict | None:
    """Primeira stream de vídeo que não é capa (attached_pic)."""
    for s in raw.get("streams", []):
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            return s
    return None


def audio_stream(raw: dict) -> dict | None:
    for s in raw.get("streams", []):
        if s.get("codec_type") == "audio":
            return s
    return None


def _rotation_from(stream: dict, frame_side_data: list | None = None) -> int:
    """Rotação horária normalizada. displaymatrix `rotation` é anti-horária; a tag antiga `rotate` é horária."""
    deg = None
    for sd in list(stream.get("side_data_list") or []) + list(frame_side_data or []):
        kind = str(sd.get("side_data_type", "")).lower().replace(" ", "")
        if "displaymatrix" in kind and sd.get("rotation") is not None:
            r = _float(sd.get("rotation"))
            if r is not None:
                deg = -r
                break
    if deg is None:
        r = _float((stream.get("tags") or {}).get("rotate"))
        if r is not None:
            deg = r
    if deg is None:
        return 0
    return int(round(deg / 90.0)) * 90 % 360


def needs_packet_count(raw: dict, vstream: dict) -> bool:
    """Casos em que só a contagem de pacotes decide imagem x vídeo (e dá a duração se faltar)."""
    names = str((raw.get("format") or {}).get("format_name", "")).split(",")
    if "image2" in names or any(n.endswith("_pipe") for n in names):
        return True
    if vstream.get("codec_name") in IMAGE_CODECS:
        return True
    return _duration(raw, vstream) is None


def _duration(raw: dict, vstream: dict) -> float | None:
    d = _float((raw.get("format") or {}).get("duration")) or _float(vstream.get("duration"))
    return d if d is not None and d >= MIN_VIDEO_DURATION else None


def _packet_count(vstream: dict) -> int | None:
    try:
        return int(vstream.get("nb_read_packets"))
    except (TypeError, ValueError):
        return None


def _is_image(raw: dict, vstream: dict) -> bool:
    if not needs_packet_count(raw, vstream):
        return False
    packets = _packet_count(vstream)
    if packets is not None:
        # GIF/WebP animado e vídeo cru (.h264) têm mais de 1 pacote -> vídeo
        return packets <= 1
    # sem contagem (JSON montado à mão): heurística por formato/duração
    names = str((raw.get("format") or {}).get("format_name", "")).split(",")
    if "image2" in names or any(n.endswith("_pipe") for n in names):
        return True
    if audio_stream(raw) is not None:
        return False
    duration = _duration(raw, vstream)
    return duration is None or duration < 1.0


def parse_probe(raw: dict, frame_side_data: list | None = None) -> ProbeResult:
    """Interpreta o JSON do ffprobe. Sem stream de vídeo -> InvalidMediaError."""
    vs = video_stream(raw)
    if vs is None:
        raise InvalidMediaError("arquivo sem stream de vídeo/imagem")
    aus = audio_stream(raw)
    fmt = raw.get("format") or {}
    media_type = "image" if _is_image(raw, vs) else "video"
    rotation = _rotation_from(vs, frame_side_data)
    width, height = vs.get("width"), vs.get("height")
    if rotation in (90, 270) and width and height:
        width, height = height, width
    if media_type == "video":
        fps = parse_rate(vs.get("avg_frame_rate")) or parse_rate(vs.get("r_frame_rate"))
        duration = _duration(raw, vs)
        packets = _packet_count(vs)
        packets_duration = _float(vs.get(PACKETS_DURATION_KEY))
        if duration is None and packets and packets_duration and packets_duration > 0:
            # stream sem duração no container (H.264 cru, WebP animado): soma das durações dos pacotes; o fps
            # declarado nesses casos é o padrão do demuxer (25) e não o real
            duration = packets_duration
            fps = packets / packets_duration
        elif duration is None and packets and fps:
            duration = packets / fps
        if duration is None or duration < MIN_VIDEO_DURATION:
            raise InvalidMediaError("vídeo sem duração útil")
    else:
        duration, fps = None, None
    return ProbeResult(
        media_type=media_type,
        format_name=str(fmt.get("format_name", "")),
        duration=duration,
        width=int(width) if width else None,
        height=int(height) if height else None,
        fps=round(fps, 3) if fps else None,
        has_audio=aus is not None and media_type == "video",
        has_video=True,
        video_codec=vs.get("codec_name"),
        audio_codec=aus.get("codec_name") if aus is not None and media_type == "video" else None,
        rotation=rotation,
        tags=dict(fmt.get("tags") or {}),
        stream_tags=[dict(s.get("tags") or {}) for s in raw.get("streams", [])],
        raw=raw,
    )


def _run_json(cmd: list[str], timeout: float) -> dict:
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            **popen_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise InvalidMediaError(f"ffprobe excedeu {timeout:.0f}s") from exc
    except OSError as exc:
        raise InvalidMediaError(f"falha ao executar ffprobe: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise InvalidMediaError(f"ffprobe não reconheceu o arquivo: {err[-500:] or proc.returncode}")
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise InvalidMediaError("saída do ffprobe não é JSON válido") from exc


def run_probe(ffprobe: str, path: Path, timeout: float = 60.0) -> ProbeResult:
    target = str(Path(path).absolute())
    raw = _run_json(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", target], timeout
    )
    if not raw.get("streams"):
        raise InvalidMediaError("arquivo sem streams")
    vs = video_stream(raw)
    if vs is not None and needs_packet_count(raw, vs):
        # Lê só os cabeçalhos dos pacotes (sem decodificar): >1 pacote = vídeo/animação.
        packets = _run_json(
            [ffprobe, "-v", "error", "-print_format", "json", "-select_streams", str(vs.get("index", 0)),
             "-show_entries", "packet=duration_time", target],
            timeout,
        ).get("packets") or []
        vs["nb_read_packets"] = str(len(packets))
        vs[PACKETS_DURATION_KEY] = str(sum(_float(p.get("duration_time")) or 0.0 for p in packets))
    result = parse_probe(raw)
    if result.media_type == "image":
        # Orientação EXIF só aparece como side data do frame decodificado.
        frames = _run_json(
            [ffprobe, "-v", "error", "-print_format", "json", "-select_streams", "v:0", "-read_intervals", "%+#1",
             "-show_entries", "frame_side_data=side_data_type,rotation", target],
            timeout,
        ).get("frames") or []
        side = frames[0].get("side_data_list") if frames else None
        if side:
            result = parse_probe(raw, side)
    return result
