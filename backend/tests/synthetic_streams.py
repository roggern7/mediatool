"""Par de fluxos separados SINTÉTICO (fMP4 só-vídeo + fMP4 só-áudio), gerado com o ffmpeg local. Nenhuma rede.

Mesma forma dos arquivos do perfil DASH on-demand (um arquivo MP4 fragmentado por fluxo); o conteúdo é sintético
(``testsrc``/``sine``). Não prova nada sobre os arquivos do CDN real.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

FRAGMENTED = ("-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4")
SIZE = (108, 192)

requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg/ffprobe não estão no PATH"
)


@lru_cache(maxsize=None)
def has_encoder(name: str) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=60)
    return any(line.split()[1:2] == [name] for line in result.stdout.splitlines() if line.strip())


requires_vp9 = pytest.mark.skipif(not has_encoder("libvpx-vp9"), reason="ffmpeg sem libvpx-vp9")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args], check=True, timeout=180)


def make_video(path: Path, *, codec: str = "libvpx-vp9", size: str = "108x192", fps: int = 30,
               seconds: float = 2) -> bytes:
    """fMP4 só com vídeo."""
    options = ["-b:v", "200k", "-deadline", "realtime", "-cpu-used", "8"] if codec == "libvpx-vp9" else [
        "-preset", "ultrafast"]
    _ffmpeg("-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}:duration={seconds}", "-c:v", codec, *options,
            "-pix_fmt", "yuv420p", "-an", *FRAGMENTED, str(path))
    return path.read_bytes()


def make_audio(path: Path, *, seconds: float = 2) -> bytes:
    """fMP4 só com áudio AAC."""
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}", "-c:a", "aac", "-vn",
            *FRAGMENTED, str(path))
    return path.read_bytes()


def make_fmp4_pair(tmp: Path, *, codec: str = "libvpx-vp9", size: str = "108x192", fps: int = 30,
                   seconds: float = 2) -> tuple[bytes, bytes]:
    tmp.mkdir(parents=True, exist_ok=True)
    tag = codec.replace("-", "_")
    return (make_video(tmp / f"v_{tag}.mp4", codec=codec, size=size, fps=fps, seconds=seconds),
            make_audio(tmp / f"a_{tag}.mp4", seconds=seconds))
