"""Teste de sessão e prévia de perfil com prazo duro, resultado estruturado e sem segredos."""

from __future__ import annotations

import threading
from pathlib import Path
import time
from types import SimpleNamespace

from conftest import make_settings, wait_until

from app.integrations.instagram.downloader import InstagramError, SessionTestResult
from media_fakes import FAKE_SESSION, FakeInstagramDownloader

LAST_TEST_FIELDS = {"ok", "result", "code", "message", "username", "http_status", "duration_ms", "detail",
                    "session_recognized", "listing_api", "session_present", "bootstrap", "graphql", "tested_at"}


class BlockingDownloader(FakeInstagramDownloader):
    """test_session/get_profile ficam presos até `release` (simula o gallery-dl dormindo por 429)."""

    def __init__(self, hold: float = 120.0):
        super().__init__()
        self.release = threading.Event()
        self.hold = hold
        self.calls = 0

    def test_session(self):
        self.calls += 1
        self.release.wait(self.hold)
        return SessionTestResult(ok=True, code="ok", message="Sessão válida.", username="conta")

    def get_profile(self, username):
        self.release.wait(self.hold)
        return super().get_profile(username)


def test_default_deadlines():
    settings = make_settings(Path("."))
    assert settings.session_test_timeout == 40.0 and settings.profile_preview_timeout == 50.0


def test_slow_session_test_times_out_without_blocking(harness_factory):
    downloader = BlockingDownloader()
    harness = harness_factory(downloader=downloader, session_test_timeout=1.0)
    started = time.monotonic()
    response = harness.client.post("/api/settings/instagram/session/test")
    elapsed = time.monotonic() - started
    assert response.status_code == 200 and elapsed < 3.0
    last = response.json()["last_test"]
    assert set(last) == LAST_TEST_FIELDS
    assert last["ok"] is False and last["result"] == "timeout" and last["code"] == "timeout"
    assert last["http_status"] is None and 900 <= last["duration_ms"] < 3000
    assert "1 s" in last["message"]

    # Outras rotas continuam respondendo enquanto a chamada presa termina em segundo plano
    assert harness.client.get("/api/health").status_code == 200
    # Segundo teste enquanto o primeiro ainda roda: 409 sem iniciar outra chamada ao adapter
    busy = harness.client.post("/api/settings/instagram/session/test")
    assert busy.status_code == 409 and busy.json()["code"] == "session_test_in_progress"
    assert downloader.calls == 1

    downloader.release.set()
    assert wait_until(lambda: not harness.ctx.session_test_lock.locked(), timeout=5)
    again = harness.client.post("/api/settings/instagram/session/test")
    assert again.status_code == 200 and again.json()["last_test"]["result"] == "valid"


def test_concurrent_session_tests_only_one_runs(harness_factory):
    downloader = BlockingDownloader(hold=0.5)
    harness = harness_factory(downloader=downloader)
    results: list[int] = []

    def call():
        results.append(harness.client.post("/api/settings/instagram/session/test").status_code)

    threads = [threading.Thread(target=call) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(results) == [200, 409, 409]
    assert downloader.calls == 1


def test_structured_rate_limited_result(harness):
    harness.downloader.session_result = SimpleNamespace(
        ok=False, code="rate_limited", message="O Instagram limitou as requisições (HTTP 429).", username=None,
        result="rate_limited", http_status=429, duration_ms=1234,
    )
    response = harness.client.post("/api/settings/instagram/session/test")
    last = response.json()["last_test"]
    assert last["result"] == "rate_limited" and last["http_status"] == 429 and last["duration_ms"] == 1234
    assert harness.ctx.worker.downloads_paused()

    settings = harness.client.get("/api/settings").json()["instagram"]["last_test"]
    assert set(settings) == LAST_TEST_FIELDS and settings["result"] == "rate_limited"


def test_structured_restricted_result_is_exposed_and_pauses(harness):
    harness.downloader.session_result = SimpleNamespace(
        ok=False, code="action_blocked", message="Bloqueio de ação (FAKE).", username=None,
        result="restricted", http_status=400, duration_ms=500,
    )
    last = harness.client.post("/api/settings/instagram/session/test").json()["last_test"]
    assert (last["result"], last["code"], last["http_status"]) == ("restricted", "action_blocked", 400)
    assert harness.ctx.worker.downloads_paused()
    settings = harness.client.get("/api/settings").json()["instagram"]["last_test"]
    assert set(settings) == LAST_TEST_FIELDS and settings["result"] == "restricted"


def test_legacy_result_without_new_fields_is_mapped(harness):
    harness.downloader.session_result = SessionTestResult(
        ok=False, code="session_expired", message="A sessão expirou.", username=None
    )
    last = harness.client.post("/api/settings/instagram/session/test").json()["last_test"]
    assert last["result"] == "expired" and last["http_status"] is None and isinstance(last["duration_ms"], int)

    harness.downloader.session_result = SessionTestResult(ok=True, code="ok", message="ok", username="conta")
    assert harness.client.post("/api/settings/instagram/session/test").json()["last_test"]["result"] == "valid"


def test_adapter_exception_becomes_structured_result(harness, monkeypatch):
    def raising():
        raise InstagramError("network_error")

    monkeypatch.setattr(harness.downloader, "test_session", raising)
    last = harness.client.post("/api/settings/instagram/session/test").json()["last_test"]
    assert last["ok"] is False and last["result"] == "network_error" and last["code"] == "network_error"


def test_old_last_test_record_is_normalized(harness):
    import json

    harness.ctx.app_settings.set_state("instagram_session_last_test", json.dumps(
        {"ok": True, "code": "ok", "message": "Sessão válida.", "username": "conta",
         "tested_at": "2026-10-01T00:00:00.000Z"}))
    last = harness.client.get("/api/settings").json()["instagram"]["last_test"]
    assert last == {"ok": True, "result": "valid", "code": "ok", "message": "Sessão válida.", "username": "conta",
                    "http_status": None, "duration_ms": 0, "detail": None, "session_recognized": None,
                    "listing_api": None, "session_present": None, "bootstrap": None, "graphql": None,
                    "tested_at": "2026-10-01T00:00:00.000Z"}


def test_session_test_responses_have_no_secret(harness):
    harness.client.put("/api/settings/instagram/session", json={"sessionid": FAKE_SESSION})
    harness.downloader.session_result = SimpleNamespace(
        ok=False, code="session_invalid", message=f"cookie sessionid={FAKE_SESSION} recusado", username=None,
        result="invalid", http_status=401, duration_ms=10,
    )
    tested = harness.client.post("/api/settings/instagram/session/test")
    settings = harness.client.get("/api/settings")
    for response in (tested, settings):
        assert FAKE_SESSION not in response.text
    assert tested.json()["last_test"]["result"] == "invalid"


def test_profile_preview_has_hard_deadline(harness_factory):
    downloader = BlockingDownloader()
    harness = harness_factory(downloader=downloader, profile_preview_timeout=1.0)
    started = time.monotonic()
    response = harness.client.post("/api/instagram/profile", json={"profile": "perfil_teste"})
    assert time.monotonic() - started < 3.0
    assert response.status_code == 504 and response.json()["code"] == "timeout"
    downloader.release.set()


def test_profile_preview_rate_limited_is_structured(harness):
    harness.downloader.listing_error = InstagramError("rate_limited")
    response = harness.client.post("/api/instagram/profile", json={"profile": "perfil_teste"})
    assert response.status_code == 429 and response.json()["code"] == "rate_limited"
    assert harness.ctx.worker.downloads_paused()


def test_lock_is_free_as_soon_as_waiter_wakes():
    from app.services.deadline import run_with_deadline

    lock = threading.Lock()
    for _ in range(50):
        run_with_deadline(lambda: 1, 5, name="t", lock=lock)
        assert not lock.locked()  # liberada antes do sinal de fim: nunca 409 falso logo em seguida


def test_lock_released_when_thread_cannot_start(monkeypatch):
    import pytest

    from app.services import deadline

    class Broken:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    lock = threading.Lock()
    monkeypatch.setattr(deadline.threading, "Thread", Broken)
    with pytest.raises(RuntimeError):
        deadline.run_with_deadline(lambda: 1, 1, name="t", lock=lock)
    assert not lock.locked()


def test_concurrent_profile_previews_only_one_runs(harness_factory):
    downloader = BlockingDownloader()
    harness = harness_factory(downloader=downloader, profile_preview_timeout=1.0)
    first = harness.client.post("/api/instagram/profile", json={"profile": "perfil_teste"})
    assert first.status_code == 504
    busy = harness.client.post("/api/instagram/profile", json={"profile": "perfil_teste"})
    assert busy.status_code == 409 and busy.json() == {
        "detail": "Já existe uma consulta de perfil em andamento.", "code": "profile_preview_in_progress"}
    downloader.release.set()
    assert wait_until(lambda: not harness.ctx.profile_preview_lock.locked(), timeout=5)
    assert harness.client.post("/api/instagram/profile", json={"profile": "perfil_teste"}).status_code == 200


def test_detail_is_recorded_and_message_passed_through(harness):
    message = ("O Instagram redirecionou para a página inicial: a sessão não está logada. "
               "Copie um sessionid novo do navegador.")
    harness.downloader.session_result = SimpleNamespace(
        ok=False, code="instagram_error", message=message, username=None, result="instagram_error",
        http_status=302, duration_ms=850, detail="redirect_home",
    )
    last = harness.client.post("/api/settings/instagram/session/test").json()["last_test"]
    assert last["result"] == "instagram_error" and last["detail"] == "redirect_home"
    assert last["message"] == message and last["http_status"] == 302
    assert harness.client.get("/api/settings").json()["instagram"]["last_test"]["detail"] == "redirect_home"


def test_detail_is_null_when_absent_or_unexpected(harness):
    harness.downloader.session_result = SessionTestResult(ok=True, code="ok", message="ok", username="conta")
    assert harness.client.post("/api/settings/instagram/session/test").json()["last_test"]["detail"] is None
    harness.downloader.session_result = SimpleNamespace(
        ok=False, code="session_invalid", message="Sessão recusada.", username=None, result="invalid",
        http_status=401, duration_ms=5, detail=f"sessionid={FAKE_SESSION}",
    )
    response = harness.client.post("/api/settings/instagram/session/test")
    assert response.json()["last_test"]["detail"] is None and FAKE_SESSION not in response.text
