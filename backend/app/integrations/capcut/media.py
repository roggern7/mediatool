"""FFmpeg/ffprobe e utilidades de arquivo do núcleo CapCut (sem dependências externas).

Os caminhos dos executáveis seguem as mesmas variáveis do app (`MEDIA_TOOL_FFMPEG`, `MEDIA_TOOL_FFPROBE`) e, sem
elas, o PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from app.integrations.capcut.errors import CapCutError

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def new_id() -> str:
    """Id no formato que o CapCut usa (UUID maiúsculo)."""
    return str(uuid.uuid4()).upper()


def tool_path(name: str) -> str:
    """Executável do FFmpeg/ffprobe (variável do app ou PATH); CapCutError('ffmpeg_not_found') se ausente."""
    env_name = "MEDIA_TOOL_FFMPEG" if name == "ffmpeg" else "MEDIA_TOOL_FFPROBE"
    configured = (os.environ.get(env_name) or "").strip()
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which(name)
    if not found:
        raise CapCutError("ffmpeg_not_found", "FFmpeg/ffprobe não encontrado.")
    return found


def run_tool(args: list[str], *, timeout: float = 600, binary: bool = False) -> subprocess.CompletedProcess:
    """Roda FFmpeg/ffprobe. Demora além do limite ou falha ao iniciar o executável viram CapCutError transitório."""
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, text=not binary,
                              encoding=None if binary else "utf-8", errors=None if binary else "replace",
                              creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise CapCutError("ffmpeg_timeout", "O FFmpeg demorou demais para processar o vídeo. Nova tentativa em breve.",
                          transient=True) from exc
    except OSError as exc:
        raise CapCutError("ffmpeg_failed", "Não foi possível executar o FFmpeg. Nova tentativa em breve.",
                          transient=True) from exc


@dataclass
class MediaProbe:
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str | None
    has_audio: bool
    audio_codec: str | None
    sample_rate: int | None
    channels: int | None
    size: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fps(stream: dict[str, Any]) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or "0/0"
        try:
            num, _, den = raw.partition("/")
            value = float(Fraction(int(num), int(den or 1))) if int(den or 1) else 0.0
        except (ValueError, ZeroDivisionError):
            value = 0.0
        if 0 < value < 1000:
            return round(value, 3)
    return 0.0


def _rotation(stream: dict[str, Any]) -> int:
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        try:
            return int(float(tags["rotate"])) % 360
        except ValueError:
            pass
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return int(float(side["rotation"])) % 360
            except (TypeError, ValueError):
                pass
    return 0


def probe(path: Path) -> MediaProbe:
    """ffprobe do 1º vídeo e 1º áudio; CapCutError('no_video_stream') se não houver vídeo."""
    result = run_tool([tool_path("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                      timeout=120)
    if result.returncode != 0:
        raise CapCutError("no_video_stream", "Não foi possível ler o vídeo (ffprobe).")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise CapCutError("no_video_stream", "O arquivo não tem faixa de vídeo.")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if _rotation(video) in (90, 270):
        width, height = height, width
    duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0.0)
    return MediaProbe(
        duration=duration, width=width, height=height, fps=_fps(video), video_codec=video.get("codec_name"),
        has_audio=audio is not None, audio_codec=(audio or {}).get("codec_name"),
        sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        channels=int(audio["channels"]) if audio and audio.get("channels") else None,
        size=Path(path).stat().st_size,
    )


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Grava em arquivo temporário na mesma pasta e troca com os.replace (sem arquivo pela metade)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mt", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Texto sem tradução de fim de linha, gravado de forma atômica."""
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path, obj: Any) -> str:
    """JSON compacto (como o CapCut grava), atômico. Devolve o texto gravado."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    atomic_write_text(path, text)
    return text


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))
