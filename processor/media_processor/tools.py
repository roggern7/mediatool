"""Localização e versão de ffmpeg/ffprobe e execução de subprocessos sem janela."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0

ENV_VARS = {"ffmpeg": "MEDIA_TOOL_FFMPEG", "ffprobe": "MEDIA_TOOL_FFPROBE"}


@dataclass
class ToolInfo:
    found: bool
    path: str | None
    version: str | None
    error: str | None


_SENSITIVE_PREFIXES = ("INSTAGRAM_",)
_SENSITIVE_PARTS = ("SESSION", "COOKIE", "TOKEN", "SECRET", "PASSWORD")


def is_sensitive_env(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(_SENSITIVE_PREFIXES) or any(part in upper for part in _SENSITIVE_PARTS)


def filtered_env() -> dict[str, str]:
    """Cópia de os.environ sem variáveis que possam conter segredos (defesa em profundidade).

    PATH, SYSTEMROOT, TEMP etc. continuam (o ffmpeg precisa deles no Windows).
    """
    return {k: v for k, v in os.environ.items() if not is_sensitive_env(k)}


def popen_kwargs() -> dict:
    """Argumentos comuns: shell=False, env filtrado, sem janela de console no Windows, grupo próprio no POSIX."""
    if sys.platform == "win32":
        return {"shell": False, "env": filtered_env(), "creationflags": CREATE_NO_WINDOW}
    return {"shell": False, "env": filtered_env(), "start_new_session": True}


def _resolve_candidate(candidate: str) -> str | None:
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    found = shutil.which(candidate)
    return os.path.abspath(found) if found else None


def locate(tool: str, explicit: str | None = None) -> tuple[str | None, str | None]:
    """Retorna (caminho, erro). Ordem: argumento explícito > variável de ambiente > PATH.

    A primeira fonte definida decide: um caminho explícito inválido não cai para o PATH.
    """
    if explicit:
        path = _resolve_candidate(explicit)
        return (path, None) if path else (None, f"{tool} não encontrado no caminho configurado: {explicit}")
    env_name = ENV_VARS[tool]
    env_value = os.environ.get(env_name, "").strip()
    if env_value:
        path = _resolve_candidate(env_value)
        return (path, None) if path else (None, f"{tool} não encontrado em {env_name}={env_value}")
    path = shutil.which(tool)
    if path:
        return os.path.abspath(path), None
    return None, f"{tool} não encontrado no PATH"


def read_version(path: str, timeout: float = 15.0) -> tuple[str | None, str | None]:
    """Primeira linha de `<tool> -version`. Retorna (versão, erro); nunca levanta."""
    try:
        proc = subprocess.run(
            [path, "-version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            **popen_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"falha ao executar {path}: {exc}"
    if proc.returncode != 0:
        return None, f"{path} -version saiu com código {proc.returncode}"
    first = proc.stdout.decode("utf-8", "replace").strip().splitlines()
    return (first[0].strip() if first else None), None


def tool_info(tool: str, explicit: str | None = None) -> ToolInfo:
    try:
        path, error = locate(tool, explicit)
        if path is None:
            return ToolInfo(found=False, path=None, version=None, error=error)
        version, error = read_version(path)
        if error:
            return ToolInfo(found=False, path=path, version=None, error=error)
        return ToolInfo(found=True, path=path, version=version, error=None)
    except Exception as exc:  # ffmpeg_info()/ffprobe_info() nunca levantam
        return ToolInfo(found=False, path=None, version=None, error=f"{type(exc).__name__}: {exc}")
