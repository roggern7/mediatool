"""Junção de um fluxo de vídeo e um de áudio (arquivos separados) em um MP4, SEM recodificar (`-c copy`).

Nenhuma transformação: sem filtro, corte, espelhamento ou preset. `+bitexact` omite a tag `encoder` e as datas
variáveis, então a mesma entrada com a mesma versão do ffmpeg gera os mesmos bytes.

VERIFICADO só com mídias sintéticas geradas pelo ffmpeg local (par fMP4 VP9/H.264 + AAC). NUNCA rodou sobre arquivos
baixados do CDN real do Instagram.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from . import commands
from .errors import ProcessingFailedError

MUX_TIMEOUT = 300.0


def mux_streams(ffmpeg: str, video: Path, audio: Path, dst: Path, *, cancel: threading.Event | None = None,
                timeout: float | None = MUX_TIMEOUT) -> None:
    """Grava em `<dst>.part` e renomeia para `dst`. Em qualquer falha (inclusive cancelamento) o `.part` é apagado."""
    from .processor import _remove, run_ffmpeg

    video, audio, dst = Path(os.path.abspath(video)), Path(os.path.abspath(audio)), Path(os.path.abspath(dst))
    part = dst.with_suffix(dst.suffix + ".part")
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", commands.abs_path(video), "-i", commands.abs_path(audio),
        "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-movflags", "+faststart", "-f", "mp4", commands.abs_path(part),
    ]
    try:
        run_ffmpeg(cmd, cancel=cancel, timeout=timeout)
        if not part.is_file() or part.stat().st_size == 0:
            raise ProcessingFailedError("ffmpeg terminou sem gerar o arquivo de saída")
        os.replace(part, dst)
    except BaseException:
        _remove(part)
        raise
