import builtins
import sqlite3
from types import SimpleNamespace

import pytest

from app.services import diagnostics as diag_mod
from app.services.diagnostics import build_diagnostics


def _tool(found=True, path="C:/tools/ffmpeg.exe", version="9.0", error=None):
    return SimpleNamespace(found=found, path=path, version=version, error=error)


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "media-tool.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()
    storage = {name: tmp_path / "storage" / name for name in ("originals", "processed", "temp", "thumbs")}
    for p in storage.values():
        p.mkdir(parents=True)
    return {
        "settings_like": SimpleNamespace(),
        "db_path": db,
        "schema_version": 3,
        "ffmpeg": _tool(),
        "ffprobe": _tool(path="C:/tools/ffprobe.exe"),
        "worker": {"running": True, "concurrency": 1, "active_jobs": 0},
        "session_configured": True,
        "storage_paths": storage,
        "host": "127.0.0.1",
        "port": 8765,
        "version": "0.1.0",
    }


def _check(result, check_id):
    return next(c for c in result["checks"] if c["id"] == check_id)


def test_all_ok_shape(env, tmp_path):
    result = build_diagnostics(**env)
    assert set(result) == {"generated_at", "overall", "checks", "python", "sqlite", "ffmpeg", "ffprobe",
                           "gallery_dl", "storage", "worker", "instagram", "backend"}
    assert result["generated_at"].endswith("Z")
    assert result["overall"] in ("ok", "warn")  # warn só fora do Windows (sessão sem DPAPI)
    for c in result["checks"]:
        assert set(c) == {"id", "label", "status", "detail"}
        assert c["status"] in ("ok", "warn", "error")
    assert result["sqlite"]["schema_version"] == 3
    assert result["sqlite"]["version"] == sqlite3.sqlite_version
    assert result["ffmpeg"] == {"found": True, "path": "C:/tools/ffmpeg.exe", "version": "9.0"}
    assert result["gallery_dl"]["found"] is True
    assert result["gallery_dl"]["version"]
    assert set(result["storage"]) == {"originals", "processed", "temp", "thumbs"}
    for info in result["storage"].values():
        assert info["exists"] is True and info["writable"] is True
    assert result["worker"] == {"running": True, "concurrency": 1, "active_jobs": 0}
    assert result["instagram"] == {"session_configured": True}
    assert result["backend"] == {"host": "127.0.0.1", "port": 8765, "version": "0.1.0"}
    assert _check(result, "database")["status"] == "ok"
    # teste de escrita não deixa arquivos
    for p in env["storage_paths"].values():
        assert list(p.iterdir()) == []


def test_session_missing_is_warn(env):
    env["session_configured"] = False
    result = build_diagnostics(**env)
    assert _check(result, "instagram_session")["status"] == "warn"
    assert result["overall"] == "warn"


def test_missing_tool_is_error(env):
    env["ffmpeg"] = _tool(found=False, path=None, version=None, error="ffmpeg não encontrado no PATH")
    result = build_diagnostics(**env)
    assert _check(result, "ffmpeg")["status"] == "error"
    assert "PATH" in _check(result, "ffmpeg")["detail"]
    assert result["ffmpeg"]["found"] is False
    assert result["overall"] == "error"


def test_storage_missing_and_db_missing(env, tmp_path):
    env["storage_paths"] = dict(env["storage_paths"], extra=tmp_path / "nao-existe")
    env["db_path"] = tmp_path / "sem-banco.db"
    result = build_diagnostics(**env)
    assert result["storage"]["extra"] == {"path": str(tmp_path / "nao-existe"), "exists": False, "writable": False}
    assert _check(result, "storage.extra")["status"] == "error"
    assert _check(result, "database")["status"] == "error"
    assert not (tmp_path / "sem-banco.db").exists()
    assert result["overall"] == "error"


def test_corrupt_db_is_error(env, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"isto nao e sqlite" * 100)
    env["db_path"] = bad
    result = build_diagnostics(**env)
    assert _check(result, "database")["status"] == "error"


def test_schema_none_and_worker_stopped_are_warn(env):
    env["schema_version"] = None
    env["worker"] = {"running": False, "concurrency": 1, "active_jobs": 0}
    result = build_diagnostics(**env)
    assert _check(result, "database")["status"] == "warn"
    assert _check(result, "worker")["status"] == "warn"


def test_gallery_dl_missing(env, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "gallery_dl" or name.startswith("gallery_dl."):
            raise ImportError("sem gallery_dl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = build_diagnostics(**env)
    assert result["gallery_dl"] == {"found": False, "version": None, "path": None}
    assert _check(result, "gallery_dl")["status"] == "error"


def test_storage_not_writable(env, monkeypatch):
    real_open = diag_mod.os.open

    def deny(path, *a, **k):
        if ".diag-write-" in str(path):
            raise PermissionError("negado")
        return real_open(path, *a, **k)

    monkeypatch.setattr(diag_mod.os, "open", deny)
    result = build_diagnostics(**env)
    assert result["storage"]["temp"]["writable"] is False
    assert _check(result, "storage.temp")["status"] == "error"


def test_details_are_redacted(env):
    env["ffprobe"] = _tool(found=False, path=None, version=None, error="falhou com sessionid=FAKE-SESSION-0000000000")
    result = build_diagnostics(**env)
    assert "FAKE-SESSION-0000000000" not in str(result)


def test_labels_in_portuguese(env, tmp_path):
    root = tmp_path / "storage"
    env["storage_paths"] = dict(env["storage_paths"], root=root)
    result = build_diagnostics(**env)
    labels = {c["id"]: c["label"] for c in result["checks"]}
    assert labels["storage.root"] == "Pasta raiz do storage"
    assert labels["storage.originals"] == "Originais"
    assert labels["storage.processed"] == "Processados"
    assert labels["storage.temp"] == "Temporários"
    assert labels["storage.thumbs"] == "Miniaturas"
    assert labels["worker"] == "Processamento da fila"
    assert labels["database"] == "Banco de dados"
    for c in result["checks"]:
        assert "Pasta root" not in c["label"] and "Worker" not in c["label"]
