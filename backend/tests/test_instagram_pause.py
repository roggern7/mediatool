"""Pausa do tráfego automático ao Instagram: duração por motivo, persistência, bloqueios e liberação manual."""

from __future__ import annotations

from types import SimpleNamespace

from app.db.connection import parse_utc, utc_now
from app.integrations.instagram.downloader import InstagramError
from media_fakes import FAKE_SESSION, remote

HOUR = 3600


def _remaining(harness) -> float:
    until, _ = harness.ctx.worker.pause_state()
    assert until is not None
    return (parse_utc(until) - parse_utc(utc_now())).total_seconds()


def _access_refused() -> InstagramError:
    # O código novo do adapter; construído assim para não depender da tabela ERROR_CODES do adapter.
    exc = InstagramError("session_invalid", "O Instagram recusou a chamada de listagem.")
    exc.code, exc.retryable = "access_refused", False
    return exc


def test_shorter_pause_never_overrides_longer_one_even_after_restart(harness_factory):
    first = harness_factory()
    first.ctx.worker.pause_downloads(reason="action_blocked")
    first.ctx.worker.pause_downloads(reason="rate_limited")
    assert first.ctx.worker.pause_state()[1] == "action_blocked"
    assert _remaining(first) > 0.9 * HOUR
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    assert second.ctx.worker.pause_state()[1] == "action_blocked"
    assert 0.9 * HOUR < _remaining(second) <= HOUR
    # Novo rate_limited depois do reinício também não encurta (lê a pausa persistida)
    second.ctx.worker.pause_downloads(reason="rate_limited")
    assert 0.9 * HOUR < _remaining(second) and second.ctx.app_settings.get_state(
        "instagram_pause_reason") == "action_blocked"


def test_pause_durations_by_reason(harness):
    worker = harness.ctx.worker
    assert worker.pause_for("access_refused") is False and worker.pause_state() == (None, None)
    assert worker.pause_for("rate_limited") is True
    # Seção 8: 1o degrau de rate_limited é 15 min; action_blocked (mais grave) troca o motivo, 1o degrau 1 h.
    assert worker.pause_state()[1] == "rate_limited" and 890 < _remaining(harness) <= 900
    assert worker.pause_for("action_blocked") is True
    assert worker.pause_state()[1] == "action_blocked" and 0.99 * HOUR < _remaining(harness) <= HOUR


def test_access_refused_never_pauses(harness):
    # listagem
    harness.downloader.listing_error = _access_refused()
    batch = harness.import_profile()
    harness.run_imports()
    result = harness.batch(batch["id"])
    assert result["status"] == "failed" and result["error_code"] == "access_refused"
    assert harness.ctx.worker.pause_state() == (None, None)

    # download de um job (não retryable)
    harness.downloader.listing_error = None
    harness.downloader.items = [remote("AR")]
    harness.downloader.download_errors = {"AR:0": [_access_refused()]}
    batch = harness.import_profile()
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    assert job["status"] == "failed" and job["error_code"] == "access_refused" and job["attempts"] == 1
    assert harness.ctx.worker.pause_state() == (None, None)

    # prévia de perfil
    harness.downloader.listing_error = _access_refused()
    preview = harness.client.post("/api/instagram/profile", json={"profile": "perfil_teste"})
    assert preview.status_code == 403 and preview.json()["code"] == "access_refused"
    assert harness.ctx.worker.pause_state() == (None, None)
    assert harness.ctx.app_settings.get_state("instagram_pause_reason") is None


def test_session_test_api_refused_does_not_pause(harness):
    from app.integrations.instagram.downloader import SessionTestResult

    harness.downloader.session_result = SessionTestResult(
        ok=False, code="access_refused", message="Listagem recusada.", username=None, result="api_refused",
        http_status=302, duration_ms=10, detail="redirect_home", session_recognized=True, listing_api="refused",
    )
    status = harness.client.post("/api/settings/instagram/session/test").json()
    assert status["paused_until"] is None and status["pause_reason"] is None
    last = status["last_test"]
    assert (last["result"], last["code"], last["detail"]) == ("api_refused", "access_refused", "redirect_home")
    assert (last["session_recognized"], last["listing_api"]) == (True, "refused")
    # Resultado no formato antigo (instagram_error + redirect_home) também não pausa mais.
    harness.downloader.session_result = SimpleNamespace(
        ok=False, code="instagram_error", message="Redirecionado.", username=None,
        result="instagram_error", http_status=302, duration_ms=10, detail="redirect_home",
    )
    assert harness.client.post("/api/settings/instagram/session/test").json()["pause_reason"] is None


def test_restricted_session_test_pauses_action_blocked_one_hour(harness):
    harness.downloader.session_result = SimpleNamespace(
        ok=False, code="action_blocked", message="Aviso de restrição.", username=None, result="restricted",
        http_status=400, duration_ms=10, detail=None,
    )
    assert harness.client.post("/api/settings/instagram/session/test").json()["pause_reason"] == "action_blocked"
    assert 0.99 * HOUR < _remaining(harness) <= HOUR


def test_rate_limited_session_test_pauses_fifteen_minutes(harness):
    harness.downloader.session_result = SimpleNamespace(
        ok=False, code="rate_limited", message="Limite.", username=None, result="rate_limited",
        http_status=429, duration_ms=10, detail=None,
    )
    assert harness.client.post("/api/settings/instagram/session/test").json()["pause_reason"] == "rate_limited"
    assert 890 < _remaining(harness) <= 900  # seção 8: 1o degrau de rate_limited


def test_old_access_refused_pause_is_cleared_on_startup(harness_factory):
    first = harness_factory()
    first.ctx.app_settings.set_state("instagram_downloads_paused_until", utc_now(5 * HOUR))
    first.ctx.app_settings.set_state("instagram_pause_reason", "access_refused")
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    assert second.ctx.worker.pause_state() == (None, None)
    assert second.ctx.app_settings.get_state("instagram_downloads_paused_until") is None
    assert second.ctx.app_settings.get_state("instagram_pause_reason") is None
    assert second.client.get("/api/settings").json()["instagram"]["pause_reason"] is None


def test_pause_blocks_imports_and_profile_preview(harness):
    harness.ctx.worker.pause_downloads(reason="action_blocked")
    body = {"profile": "perfil_teste", "content_type": "all", "limit": 10, "preset_id": None, "process": False}
    blocked = harness.client.post("/api/imports", json=body)
    assert blocked.status_code == 409 and blocked.json()["code"] == "instagram_paused"
    assert "proteção do Media Tool" in blocked.json()["detail"] and " às " in blocked.json()["detail"]
    assert "Configurações" in blocked.json()["detail"]
    preview = harness.client.post("/api/instagram/profile", json={"profile": "perfil_teste"})
    assert preview.status_code == 409 and preview.json()["code"] == "instagram_paused"
    assert harness.client.get("/api/batches").json()["total"] == 0


def test_existing_listing_waits_for_pause_and_resumes_after_release(harness):
    harness.downloader.items = [remote("W1")]
    batch = harness.import_profile()
    harness.ctx.worker.pause_downloads(reason="rate_limited")
    assert harness.ctx.import_runner.run_next() is False
    assert harness.batch(batch["id"])["status"] == "listing" and harness.downloader.listings == 0

    released = harness.client.delete("/api/settings/instagram/pause")
    assert released.status_code == 200
    assert released.json()["paused_until"] is None and released.json()["pause_reason"] is None
    harness.run_all()
    assert harness.batch(batch["id"])["status"] == "completed"


def test_settings_expose_pause_and_release_requires_client_header(harness):
    from fastapi.testclient import TestClient

    harness.ctx.worker.pause_downloads(reason="rate_limited")
    instagram = harness.client.get("/api/settings").json()["instagram"]
    assert instagram["pause_reason"] == "rate_limited" and instagram["paused_until"].endswith("Z")
    bare = TestClient(harness.app, base_url="http://127.0.0.1:8765")
    assert bare.delete("/api/settings/instagram/pause").status_code == 403
    assert harness.ctx.worker.downloads_paused()
    assert harness.client.delete("/api/settings/instagram/pause").json()["paused_until"] is None
    assert harness.ctx.app_settings.get_state("instagram_pause_reason") is None


def test_expired_pause_is_not_reported(harness):
    harness.ctx.app_settings.set_state("instagram_downloads_paused_until", utc_now(-5))
    harness.ctx.app_settings.set_state("instagram_pause_reason", "action_blocked")
    harness.ctx.worker.load_pause_state()
    assert harness.ctx.worker.pause_state() == (None, None)
    assert harness.client.get("/api/settings").json()["instagram"]["pause_reason"] is None


def test_checkpoint_and_session_invalid_pause_6h(harness):
    # Seção 8: checkpoint e sessão inválida forte verificam a cada 6 h (fixo) e pedem ação do usuário.
    worker = harness.ctx.worker
    # session_expired (documento redireciona para login) pausa como session_invalid (limpa ao salvar sessão/cookies).
    assert worker.pause_for("session_expired") is True
    assert worker.pause_state()[1] == "session_invalid" and 5.9 * HOUR < _remaining(harness) <= 6 * HOUR
    assert worker.pause_for("session_invalid") is True
    assert worker.pause_state()[1] == "session_invalid" and 5.9 * HOUR < _remaining(harness) <= 6 * HOUR
    assert worker.pause_for("checkpoint_required") is True
    assert worker.pause_state()[1] == "checkpoint_required" and 5.9 * HOUR < _remaining(harness) <= 6 * HOUR
    assert harness.client.get("/api/settings").json()["instagram"]["action_required"] is True
    # Pausas mais curtas não encurtam a de 6 h.
    assert worker.pause_for("action_blocked") is True and worker.pause_state()[1] == "checkpoint_required"


def test_long_pauses_persist_across_restart_and_release_manually(harness_factory):
    for reason in ("checkpoint_required", "session_invalid"):
        first = harness_factory()
        first.ctx.worker.pause_for(reason)
        root = first.root
        first.__exit__(None, None, None)
        second = harness_factory(root=root)
        assert second.ctx.worker.pause_state()[1] == reason and 5.9 * HOUR < _remaining(second)
        assert second.client.get("/api/settings").json()["instagram"]["pause_reason"] == reason
        released = second.client.delete("/api/settings/instagram/pause").json()
        assert released["paused_until"] is None and released["pause_reason"] is None
        assert second.ctx.app_settings.get_state("instagram_pause_reason") is None
        second.__exit__(None, None, None)


def test_session_test_checkpoint_and_session_invalid_pause(harness):
    harness.downloader.session_result = SimpleNamespace(
        ok=False, code="checkpoint_required", message="Verificação.", username=None,
        result="checkpoint_required", http_status=302, duration_ms=10, detail="redirect_checkpoint",
    )
    assert harness.client.post("/api/settings/instagram/session/test").json()["pause_reason"] == "checkpoint_required"
    harness.ctx.worker.clear_pause()
    harness.downloader.session_result = SimpleNamespace(
        ok=False, code="session_invalid", message="Sessão recusada.", username=None, result="invalid",
        http_status=401, duration_ms=10, detail="http_401",
    )
    assert harness.client.post("/api/settings/instagram/session/test").json()["pause_reason"] == "session_invalid"
    assert 5.9 * HOUR < _remaining(harness)
    # Expirada (documento redireciona para login): evidência forte, pausa liberável como session_invalid.
    harness.ctx.worker.clear_pause()
    harness.downloader.session_result = SimpleNamespace(
        ok=False, code="session_expired", message="Expirada.", username=None, result="expired",
        http_status=302, duration_ms=10, detail="redirect_login",
    )
    assert harness.client.post("/api/settings/instagram/session/test").json()["pause_reason"] == "session_invalid"


def test_saving_session_or_cookies_clears_only_session_invalid_pause(harness_factory, tmp_path):
    from app.integrations.instagram.session import LocalInstagramSessionProvider

    harness = harness_factory(session_provider=LocalInstagramSessionProvider(tmp_path / "secrets", env={}))
    worker = harness.ctx.worker
    worker.pause_for("session_invalid")
    saved = harness.client.put("/api/settings/instagram/session", json={"sessionid": FAKE_SESSION})
    assert saved.status_code == 200 and saved.json()["pause_reason"] is None
    assert harness.ctx.app_settings.get_state("instagram_pause_reason") is None

    worker.pause_for("session_invalid")
    patched = harness.client.patch("/api/settings/instagram/session/cookies", json={"csrftoken": "FAKECSRF000000"})
    assert patched.status_code == 200 and patched.json()["pause_reason"] is None

    # Seção 8: salvar a sessão limpa as pausas de escopo "session" (inclui checkpoint); as de escopo
    # "instagram" continuam.
    worker.pause_for("checkpoint_required")
    again = harness.client.put("/api/settings/instagram/session", json={"sessionid": FAKE_SESSION})
    assert again.json()["pause_reason"] is None
    worker.pause_for("rate_limited")
    assert harness.client.put("/api/settings/instagram/session",
                              json={"sessionid": FAKE_SESSION}).json()["pause_reason"] == "rate_limited"

