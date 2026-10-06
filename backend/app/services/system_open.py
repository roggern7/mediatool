"""Abre arquivo (aplicativo padrão) ou pasta (Explorer com o item selecionado) no desktop Windows local."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from app.core.security import UnsafePathError, ensure_within

# Só mídia é aberta pelo aplicativo padrão: evita executar .exe/.bat/.lnk que porventura estejam no storage.
OPENABLE_SUFFIXES = frozenset(
    {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}
)


class SystemOpenError(Exception):
    """Erro controlado. ``code``: invalid_target, not_allowed, not_found, unsupported, failed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


_SENSITIVE_ENV_PARTS = ("SESSION", "COOKIE", "TOKEN", "SECRET", "PASSWORD")


def _child_env() -> dict[str, str]:
    """Ambiente para o Explorer sem variáveis que possam carregar credenciais."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.startswith("INSTAGRAM_") or any(part in upper for part in _SENSITIVE_ENV_PARTS):
            continue
        env[key] = value
    return env


def _explorer_executable() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")


def open_path(path: Path, *, allowed_roots: Sequence[Path], target: Literal["file", "folder"]) -> None:
    if target not in ("file", "folder"):
        raise SystemOpenError("invalid_target", "Destino deve ser 'file' ou 'folder'.")
    try:
        resolved = ensure_within(Path(path), allowed_roots)
    except UnsafePathError:
        raise SystemOpenError("not_allowed", "Caminho fora das pastas permitidas.") from None
    if not resolved.exists():
        raise SystemOpenError("not_found", "Arquivo ou pasta não encontrado.")
    if os.name != "nt" or not hasattr(os, "startfile"):
        raise SystemOpenError("unsupported", "Abrir no sistema só é suportado no Windows com desktop.")

    try:
        if target == "file":
            if not resolved.is_file():
                raise SystemOpenError("not_found", "O caminho não é um arquivo.")
            if resolved.suffix.lower() not in OPENABLE_SUFFIXES:
                raise SystemOpenError("not_allowed", "Tipo de arquivo não permitido para abertura.")
            os.startfile(str(resolved))
        else:
            subprocess.Popen([_explorer_executable(), f"/select,{resolved}"], shell=False, env=_child_env())
    except SystemOpenError:
        raise
    except OSError as exc:
        raise SystemOpenError("failed", f"Não foi possível abrir ({type(exc).__name__}).") from None


def open_folder(path: Path, *, allowed_roots: Sequence[Path]) -> None:
    """Abre a PASTA no Explorer (o conteúdo dela, sem selecionar item). Só pastas dentro das raízes permitidas.

    Usa a associação do shell para diretórios (`os.startfile`): um diretório nunca é executado, e nomes com vírgula
    não são interpretados como opções do `explorer.exe`."""
    try:
        resolved = ensure_within(Path(path), allowed_roots)
    except UnsafePathError:
        raise SystemOpenError("not_allowed", "Caminho fora das pastas permitidas.") from None
    if not resolved.is_dir():
        raise SystemOpenError("not_found", "Pasta não encontrada.")
    if os.name != "nt" or not hasattr(os, "startfile"):
        raise SystemOpenError("unsupported", "Abrir no sistema só é suportado no Windows com desktop.")
    try:
        os.startfile(str(resolved))
    except OSError as exc:
        raise SystemOpenError("failed", f"Não foi possível abrir ({type(exc).__name__}).") from None
