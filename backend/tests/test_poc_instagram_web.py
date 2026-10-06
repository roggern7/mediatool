"""POC do provider web (tools/poc_instagram_web.py) com fakes: sem rede, banco e storage temporários."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from media_processor import sha256_file

from app.integrations.instagram.web.errors import InstagramWebError
from conftest import make_settings
from media_fakes import FAKE_SESSION, FakeInstagramDownloader, FakeProcessor, MemorySessionProvider, remote

_SPEC = importlib.util.spec_from_file_location(
    "poc_instagram_web", Path(__file__).resolve().parents[1] / "tools" / "poc_instagram_web.py"
)
poc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(poc)


def run_poc(tmp_path: Path, downloader: FakeInstagramDownloader, *argv: str, running: bool = False, prepare=None):
    settings = make_settings(tmp_path)
    if prepare is not None:
        prepare(settings)
    code, report = poc.run(
        poc.parse_args(["--profile", "@perfil_teste", *argv]), settings=settings,
        session_provider=MemorySessionProvider(), web_factory=lambda ctx: downloader, processor=FakeProcessor(),
        is_app_running=lambda s: running,
    )
    text = json.dumps(report, ensure_ascii=True)
    assert FAKE_SESSION not in text
    return code, report, settings


def state(settings, key: str):
    with sqlite3.connect(settings.database_path) as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def test_ingest_one_media_into_real_pipeline(tmp_path):
    downloader = FakeInstagramDownloader(items=[remote("ABCDE1"), remote("ABCDE2"), remote("ABCDE3")])
    code, report, settings = run_poc(tmp_path, downloader)
    assert code == 0 and report["outcome"] == "ok" and report["limit"] == 1 and report["provider"] == "web"
    assert report["steps"]["bootstrap"] == "ok" and report["steps"]["listing"] == "ok"
    assert report["steps"]["found"] == 1 and downloader.downloads == ["ABCDE1:0"]
    media = report["media"]
    original = Path(media["original_path"])
    assert original.is_file() and original.is_relative_to(settings.storage_dir)
    assert sha256_file(original) == media["original_sha256"] and media["shortcode"] == "ABCDE1"
    assert "processed" not in report
    with sqlite3.connect(settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1
        assert conn.execute("SELECT requested_limit, process FROM batches").fetchone() == (1, 0)


def test_process_flag_uses_anti_duplicate_preset_and_keeps_original(tmp_path):
    code, report, _ = run_poc(tmp_path, FakeInstagramDownloader(items=[remote("ABCDE1", media_type="video")]),
                              "--process")
    assert code == 0 and report["outcome"] == "ok", report
    assert report["steps"]["process"]["preset"] == "Anti-duplicidade"
    processed = report["processed"]
    assert Path(processed["processed_path"]).is_file() and processed["processed_sha256_matches_file"]
    assert processed["ffprobe"]["media_type"] == "video"
    assert report["media"]["original_unchanged"] is True


@pytest.mark.parametrize("code, stage, paused", [
    ("contract_changed", "graphql", None),
    ("rate_limited", "bootstrap", "rate_limited"),
    ("checkpoint_required", "bootstrap", "checkpoint_required"),
    ("action_blocked", "graphql", "action_blocked"),
    ("session_invalid", "bootstrap", "session_invalid"),
])
def test_listing_errors_stop_immediately_with_safe_fields(tmp_path, code, stage, paused):
    error = InstagramWebError(code, f"Mensagem {code}.", stage=stage, diagnostic={"detail": "repeated_cursor"})
    downloader = FakeInstagramDownloader(items=[], listing_error=error)
    exit_code, report, settings = run_poc(tmp_path, downloader)
    assert exit_code == 1 and report["outcome"] == code
    assert report["error"] == {"operation": "listing", "code": code, "message": f"Mensagem {code}.",
                               "stage": stage, "detail": "repeated_cursor"}
    assert report["steps"]["listing"] == "failed"
    assert report["steps"]["bootstrap"] == ("failed" if stage == "bootstrap" else "unknown")
    assert downloader.listings == 1 and downloader.downloads == [] and "media" not in report
    assert state(settings, "instagram_pause_reason") == paused
    assert_nothing_pending(settings)


def test_download_error_stops_without_retry(tmp_path):
    from app.integrations.instagram.downloader import InstagramError

    downloader = FakeInstagramDownloader(
        items=[remote("ABCDE1")],
        download_errors={"ABCDE1:0": [InstagramError("network_error", "Falha FAKE.", retryable=True)] * 3},
    )
    code, report, settings = run_poc(tmp_path, downloader, "--process")
    assert code == 1 and report["outcome"] == "network_error" and downloader.downloads == ["ABCDE1:0"]
    assert report["error"]["operation"] == "download" and "processed" not in report
    assert_nothing_pending(settings)


def assert_nothing_pending(settings):
    """Nada fica para o worker do app refazer: jobs e lote terminais, sem nova tentativa agendada."""
    with sqlite3.connect(settings.database_path) as conn:
        jobs = conn.execute("SELECT status, next_attempt_at, claimed_by FROM jobs").fetchall()
        batches = conn.execute("SELECT status FROM batches").fetchall()
    assert all(status in ("completed", "failed", "cancelled") and nxt is None and owner is None
               for status, nxt, owner in jobs), jobs
    assert all(status in ("completed", "failed", "cancelled") for (status,) in batches), batches


@pytest.mark.parametrize("code", ["rate_limited", "network_error", "download_failed"])
def test_any_download_error_leaves_no_retry_behind(tmp_path, code):
    from app.integrations.instagram.downloader import InstagramError

    error = InstagramError(code, "Falha FAKE.", retryable=True)
    downloader = FakeInstagramDownloader(items=[remote("ABCDE1")], download_errors={"ABCDE1:0": [error]})
    exit_code, report, settings = run_poc(tmp_path, downloader)
    assert exit_code == 1 and report["outcome"] == code
    assert_nothing_pending(settings)


def test_processing_failure_leaves_no_retry_behind(tmp_path):
    downloader = FakeInstagramDownloader(items=[remote("ABCDE1", media_type="video")])
    settings = make_settings(tmp_path)
    code, report = poc.run(
        poc.parse_args(["--profile", "perfil_teste", "--process"]), settings=settings,
        session_provider=MemorySessionProvider(), web_factory=lambda ctx: downloader,
        processor=FakeProcessor(fail_process=RuntimeError("FAKE")), is_app_running=lambda s: False,
    )
    assert code == 1 and report["steps"]["ingest"]["status"] == "completed" and "processed" not in report
    assert_nothing_pending(settings)


def test_build_context_failure_returns_safe_json(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disco FAKE")

    monkeypatch.setattr(poc, "build_context", boom)
    code, report, _ = run_poc(tmp_path, FakeInstagramDownloader(items=[remote("ABCDE1")]))
    assert code == 1 and report["outcome"] == "error" and report["error"]["code"] == "internal_error"
    assert "disco FAKE" not in json.dumps(report)


def test_empty_listing_is_reported(tmp_path):
    code, report, _ = run_poc(tmp_path, FakeInstagramDownloader(items=[]))
    assert code == 1 and report["outcome"] == "no_items"


def test_refuses_when_app_is_running(tmp_path):
    downloader = FakeInstagramDownloader(items=[remote("ABCDE1")])
    code, report, settings = run_poc(tmp_path, downloader, running=True)
    assert code == 2 and report["outcome"] == "app_running" and downloader.listings == 0
    assert not settings.database_path.exists()


def test_refuses_when_queue_has_open_work(tmp_path):
    from app.db.connection import Database
    from app.db.migrate import apply_migrations
    from app.db.repositories import BatchRepository

    def prepare(settings):
        settings.data_dir.mkdir(parents=True)
        db = Database(settings.database_path)
        apply_migrations(db)
        BatchRepository(db).create(source="instagram_import", status="listing", source_profile="outro",
                                   content_type="all", requested_limit=None, preset_id=None, preset_name=None,
                                   params=None, process=False)
        db.close_all()

    downloader = FakeInstagramDownloader(items=[remote("ABCDE1")])
    code, report, _ = run_poc(tmp_path, downloader, prepare=prepare)
    assert code == 2 and report["outcome"] == "queue_busy" and report["error"]["listing_batches"] == 1
    assert downloader.listings == 0
