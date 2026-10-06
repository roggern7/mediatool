"""Espaço livre em disco antes de downloads e trechos de listagem (docs/BATCH_ROBUSTNESS.md, seção 9, C2).

Mínimo exigido = reserva fixa (5 GB, configurável) + 2 x max_download_bytes x download_concurrency + saídas do
FFmpeg (2 x tamanho máximo de um original, que é max_download_bytes, x processing_concurrency). O worker configura o
caminho do storage e o mínimo no início e quando uma das concorrências muda; vale antes de cada claim dos dois pools.
A parcela das saídas do FFmpeg só entra enquanto houver processamento em aberto: com apenas lotes download-only o
mínimo é reserva + 2 x max_download_bytes x download_concurrency.
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

from app.core.config import DEFAULT_DISK_RESERVE_BYTES, DEFAULT_DOWNLOAD_CONCURRENCY, DEFAULT_MAX_DOWNLOAD_BYTES

logger = logging.getLogger("media_tool.disk")


def required_free_bytes(max_download_bytes: int, download_concurrency: int,
                        reserve: int = DEFAULT_DISK_RESERVE_BYTES, *, processing_concurrency: int = 0) -> int:
    return int(reserve) + 2 * int(max_download_bytes) * (
        max(1, int(download_concurrency)) + max(0, int(processing_concurrency)))


_lock = threading.Lock()
_path: Path | None = None
_required = required_free_bytes(DEFAULT_MAX_DOWNLOAD_BYTES, DEFAULT_DOWNLOAD_CONCURRENCY)
_required_download_only = _required


def configure(path: Path, required: int, download_only_required: int | None = None) -> None:
    """Pasta verificada (a do storage) e mínimo exigido por padrão. `download_only_required`: mínimo quando não há
    processamento em aberto (sem a parcela das saídas do FFmpeg); omitido, vale o mesmo `required`."""
    global _path, _required, _required_download_only
    with _lock:
        _path, _required = Path(path), int(required)
        _required_download_only = int(required if download_only_required is None else download_only_required)


def disk_ok(min_free: int | None = None, *, download_only: bool = False) -> tuple[bool, int, int]:
    """(ok, free_bytes, required_bytes). Sem como medir (pasta inexistente/erro do sistema): não bloqueia e
    informa free_bytes = -1. `download_only`: usa o mínimo sem a parcela das saídas do FFmpeg."""
    with _lock:
        path, default_required = _path, (_required_download_only if download_only else _required)
    required = default_required if min_free is None else int(min_free)
    target = path if path is not None else Path.cwd()
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        free = int(shutil.disk_usage(target).free)
    except OSError as exc:
        logger.warning("Espaço livre em disco não pôde ser medido: %s", exc.strerror or exc.__class__.__name__)
        return True, -1, required
    return free >= required, free, required
