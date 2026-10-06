"""Diagnóstico do ambiente local no formato ``Diagnostics`` do contrato (seção 8)."""

from __future__ import annotations

import os
import platform
import sqlite3
import sys
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.core.logging import redact

Status = Literal["ok", "warn", "error"]
_RANK: dict[str, int] = {"ok": 0, "warn": 1, "error": 2}
_MIN_PYTHON = (3, 11)
STORAGE_LABELS: dict[str, str] = {
    "root": "Pasta raiz do storage",
    "originals": "Originais",
    "processed": "Processados",
    "temp": "Temporários",
    "thumbs": "Miniaturas",
}


def _check(check_id: str, label: str, status: Status, detail: str) -> dict[str, str]:
    return {"id": check_id, "label": label, "status": status, "detail": redact(detail)}


def _tool_dict(tool: Any) -> dict[str, Any]:
    return {
        "found": bool(getattr(tool, "found", False)),
        "path": getattr(tool, "path", None),
        "version": getattr(tool, "version", None),
    }


def _tool_check(check_id: str, label: str, tool: Any) -> dict[str, str]:
    if getattr(tool, "found", False):
        version = getattr(tool, "version", None) or "desconhecida"
        path = getattr(tool, "path", None) or "caminho desconhecido"
        return _check(check_id, label, "ok", f"Versão {version} em {path}.")
    error = getattr(tool, "error", None)
    detail = "Não encontrado ou não executável."
    if error:
        detail += f" Detalhe: {error}"
    return _check(check_id, label, "error", detail)


def _gallery_dl_info() -> dict[str, Any]:
    try:
        import gallery_dl
        from gallery_dl import version as gallery_dl_version
    except Exception as exc:
        return {"found": False, "version": None, "path": None, "_error": f"{type(exc).__name__}: {exc}"}
    return {
        "found": True,
        "version": getattr(gallery_dl_version, "__version__", None),
        "path": str(Path(gallery_dl.__file__).resolve().parent),
    }


def _storage_info(path: Path) -> dict[str, Any]:
    path = Path(path)
    exists = path.is_dir()
    writable = False
    if exists:
        probe = path / f".diag-write-{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, b"ok")
            finally:
                os.close(fd)
            writable = True
        except OSError:
            writable = False
        finally:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
    return {"path": str(path), "exists": exists, "writable": writable}


def _database_check(db_path: Path, schema_version: int | None) -> dict[str, str]:
    label = "Banco de dados"
    if not db_path.is_file():
        return _check("database", label, "error", f"Arquivo do banco não encontrado: {db_path}")
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=rw", uri=True, timeout=2)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _check("database", label, "error", f"Não foi possível abrir o banco ({exc}).")
    if schema_version is None:
        return _check("database", label, "warn", "Banco aberto, mas nenhuma migração foi aplicada.")
    return _check("database", label, "ok", f"Banco aberto; versão do esquema {schema_version}.")


def build_diagnostics(
    *,
    settings_like: Any,
    db_path: Path,
    schema_version: int | None,
    ffmpeg: Any,
    ffprobe: Any,
    worker: Mapping[str, Any],
    session_configured: bool,
    storage_paths: Mapping[str, Path],
    host: str,
    port: int,
    version: str,
) -> dict[str, Any]:
    """Monta o dicionário ``Diagnostics``.

    ``ffmpeg``/``ffprobe`` podem ser qualquer objeto com ``found``, ``path``, ``version`` e ``error``.
    ``worker`` deve ter ``running``, ``concurrency`` e ``active_jobs``. ``settings_like`` é aceito para manter a
    assinatura estável; nenhum campo dele é lido hoje.
    """
    db_path = Path(db_path)
    checks: list[dict[str, str]] = []

    py_version = platform.python_version()
    py_ok = sys.version_info[:2] >= _MIN_PYTHON
    checks.append(
        _check(
            "python", "Python", "ok" if py_ok else "warn",
            f"Versão {py_version} em {sys.executable}."
            + ("" if py_ok else f" Recomendado {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]} ou mais recente."),
        )
    )

    checks.append(_check("sqlite", "SQLite", "ok", f"Biblioteca SQLite versão {sqlite3.sqlite_version}."))
    try:
        checks.append(_database_check(db_path, schema_version))
    except Exception as exc:
        checks.append(_check("database", "Banco de dados", "error",
                             f"Erro inesperado ao verificar o banco ({type(exc).__name__}: {exc})."))

    checks.append(_tool_check("ffmpeg", "FFmpeg", ffmpeg))
    checks.append(_tool_check("ffprobe", "FFprobe", ffprobe))

    gdl = _gallery_dl_info()
    gdl_error = gdl.pop("_error", None)
    if gdl["found"]:
        checks.append(_check("gallery_dl", "gallery-dl", "ok", f"Versão {gdl['version']} em {gdl['path']}."))
    else:
        checks.append(_check("gallery_dl", "gallery-dl", "error", f"Não foi possível importar o gallery-dl ({gdl_error})."))

    storage: dict[str, dict[str, Any]] = {}
    for name, path in storage_paths.items():
        label = STORAGE_LABELS.get(name, f"Pasta {name}")
        try:
            info = _storage_info(Path(path))
        except Exception as exc:
            info = {"path": str(path), "exists": False, "writable": False}
            checks.append(_check(f"storage.{name}", label, "error",
                                 f"Erro ao verificar a pasta ({type(exc).__name__}: {exc})."))
            storage[name] = info
            continue
        storage[name] = info
        if not info["exists"]:
            checks.append(_check(f"storage.{name}", label, "error", f"Pasta não existe: {info['path']}"))
        elif not info["writable"]:
            checks.append(_check(f"storage.{name}", label, "error", f"Sem permissão de escrita: {info['path']}"))
        else:
            checks.append(_check(f"storage.{name}", label, "ok", f"Existe e aceita gravação: {info['path']}"))

    worker_info = {
        "running": bool(worker.get("running", False)),
        "concurrency": worker.get("concurrency"),
        "active_jobs": worker.get("active_jobs"),
    }
    if worker_info["running"]:
        checks.append(
            _check("worker", "Processamento da fila", "ok",
                   f"Em execução; {worker_info['concurrency']} tarefa(s) em paralelo, "
                   f"{worker_info['active_jobs']} ativa(s) agora.")
        )
    else:
        checks.append(_check("worker", "Processamento da fila", "warn", "Parado; a fila não será processada."))

    if not session_configured:
        checks.append(_check("instagram_session", "Sessão do Instagram", "warn",
                             "Não configurada. Use scripts\\set-instagram-session.ps1 ou a tela de Configurações."))
    elif os.name != "nt":
        checks.append(_check("instagram_session", "Sessão do Instagram", "warn",
                             "Configurada, mas protegida apenas por permissão de arquivo (sem DPAPI)."))
    else:
        checks.append(_check("instagram_session", "Sessão do Instagram", "ok", "Configurada e protegida com DPAPI."))

    overall = max((c["status"] for c in checks), key=lambda s: _RANK[s], default="ok")

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "overall": overall,
        "checks": checks,
        "python": {"version": py_version, "executable": sys.executable},
        "sqlite": {"version": sqlite3.sqlite_version, "path": str(db_path), "schema_version": schema_version},
        "ffmpeg": _tool_dict(ffmpeg),
        "ffprobe": _tool_dict(ffprobe),
        "gallery_dl": gdl,
        "storage": storage,
        "worker": worker_info,
        "instagram": {"session_configured": bool(session_configured)},
        "backend": {"host": host, "port": port, "version": version},
    }
