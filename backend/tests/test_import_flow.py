from __future__ import annotations

from conftest import wait_until

from app.integrations.instagram.downloader import InstagramError
from media_fakes import FakeInstagramDownloader, MemorySessionProvider, remote


def test_import_requires_session(harness_factory):
    harness = harness_factory(session_provider=MemorySessionProvider(configured=False))
    response = harness.client.post("/api/imports", json={"profile": "perfil_teste", "content_type": "all",
                                                         "limit": 10, "preset_id": None, "process": False})
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "session_missing" and "sessionid" in body["detail"]
    assert harness.client.get("/api/batches").json()["total"] == 0


def _listing_state(harness, batch_id):
    return harness.ctx.db.scalar("SELECT listing_state FROM batches WHERE id = ?", (batch_id,))


def test_listing_session_error_pauses_listing_with_clear_message(harness):
    # Seção 0: erro de proteção pausa a listagem (retomável do checkpoint depois de salvar a sessão).
    harness.downloader.listing_error = InstagramError("session_invalid")
    batch = harness.import_profile()
    harness.run_imports()
    result = harness.batch(batch["id"])
    assert result["status"] == "listing" and result["error_code"] == "session_invalid"
    assert "sessão" in result["error_message"].lower() and result["finished_at"] is None
    assert result["found"] == 0 and _listing_state(harness, batch["id"]) == "paused"


def test_listing_errors_map_codes(harness):
    for code in ("profile_not_found", "profile_private", "session_missing"):
        harness.downloader.listing_error = InstagramError(code)
        batch = harness.import_profile()
        harness.run_imports()
        result = harness.batch(batch["id"])
        assert result["status"] == "failed" and result["error_code"] == code and result["error_message"]
        assert _listing_state(harness, batch["id"]) == "done"
    harness.downloader.listing_error = InstagramError("checkpoint_required")
    batch = harness.import_profile()
    harness.run_imports()
    result = harness.batch(batch["id"])
    # Erro de proteção: a listagem fica pausada (retomável), não falha.
    assert result["status"] == "listing" and result["error_code"] == "checkpoint_required"
    assert _listing_state(harness, batch["id"]) == "paused"
    # checkpoint_required (o último) pausa o Instagram por 24 h.
    assert harness.ctx.worker.pause_state()[1] == "checkpoint_required"


def test_rate_limited_listing_pauses_downloads(harness):
    harness.downloader.listing_error = InstagramError("rate_limited")
    batch = harness.import_profile()
    harness.run_imports()
    assert harness.batch(batch["id"])["error_code"] == "rate_limited"
    assert harness.ctx.worker.downloads_paused()


def test_action_blocked_listing_pauses_long_without_retry_until_pause_ends(harness):
    harness.downloader.listing_error = InstagramError("action_blocked")
    batch = harness.import_profile()
    harness.run_imports()
    harness.run_imports()  # pausado: nenhuma nova chamada de listagem
    assert harness.downloader.listings == 1
    result = harness.batch(batch["id"])
    assert result["status"] == "listing" and result["error_code"] == "action_blocked"
    assert _listing_state(harness, batch["id"]) == "paused"
    assert "aviso de restrição" in result["error_message"] and "1 hora" in result["error_message"]
    assert harness.ctx.worker.downloads_paused()
    from app.db.connection import parse_utc, utc_now

    until, reason = harness.ctx.worker.pause_state()
    remaining = (parse_utc(until) - parse_utc(utc_now())).total_seconds()
    assert reason == "action_blocked" and remaining > harness.ctx.settings.rate_limit_pause_seconds
    assert harness.ctx.settings.action_blocked_pause_seconds == 3600


def test_network_error_in_listing_resumes_later_from_checkpoint(harness):
    harness.downloader.items = [remote("A"), remote("B"), remote("C")]
    harness.downloader.listing_error = InstagramError("network_error")
    harness.downloader.listing_error_after = 2
    batch = harness.import_profile()
    harness.run_all()
    result = harness.batch(batch["id"])
    assert result["status"] == "listing" and result["found"] == 2 and result["error_code"] == "network_error"
    assert _listing_state(harness, batch["id"]) == "paused" and result["progress"]["downloaded"] == 2
    harness.run_imports()  # dentro do intervalo de espera: não lista de novo
    assert harness.downloader.listings == 1
    harness.downloader.listing_error = None
    harness.ctx.db.execute("UPDATE batches SET listing_retry_at = NULL")  # simula o fim do intervalo
    harness.run_all()
    result = harness.batch(batch["id"])
    assert result["status"] == "completed" and result["found"] == 3 and result["error_code"] is None
    assert harness.downloader.downloads.count("A:0") == 1  # o que já baixou não baixa de novo


def test_persistent_network_error_keeps_found_items(harness):
    harness.downloader.items = [remote("A"), remote("B"), remote("C")]
    harness.downloader.listing_error = InstagramError("network_error")
    harness.downloader.listing_error_after = 2
    batch = harness.import_profile()
    for _ in range(3):
        harness.ctx.db.execute("UPDATE batches SET listing_retry_at = NULL")
        harness.run_all()
    result = harness.batch(batch["id"])
    assert harness.downloader.listings == 3
    # Seção 9 (C1): falha de rede nunca encerra a listagem; o lote segue pausado com backoff, itens mantidos.
    assert result["status"] == "listing" and result["found"] == 2 and _listing_state(harness, batch["id"]) == "paused"
    assert result["error_code"] == "network_error" and result["progress"]["downloaded"] == 2


def test_limit_and_content_type_are_forwarded(harness):
    calls = []

    class Recording(FakeInstagramDownloader):
        def iter_profile_media(self, username, content, limit, *, cancel=None, **kwargs):
            calls.append((username, content, limit))
            yield from super().iter_profile_media(username, content, limit, cancel=cancel, **kwargs)

    harness.downloader = Recording(items=[remote(f"I{i}") for i in range(15)])
    batch = harness.import_profile(profile="https://www.instagram.com/Perfil.Teste/", content_type="reels", limit=10)
    harness.run_all()
    assert len(calls) == 1
    assert calls[0][0].lower() == "perfil.teste" and calls[0][1:] == ("reels", 10)
    result = harness.batch(batch["id"])
    assert result["found"] == 10 and result["requested_limit"] == 10 and result["content_type"] == "reels"
    assert result["source_profile"] == calls[0][0]


def test_import_validation(harness):
    base = {"profile": "perfil_teste", "content_type": "all", "limit": 10, "preset_id": None, "process": False}
    bad = [
        {**base, "limit": 0},            # N livre: 15 passou a ser aceito; só lixo é recusado
        {**base, "limit": 1_000_001},
        {**base, "limit": "15"},
        {**base, "content_type": "stories"},
        {**base, "process": "sim"},
        {**base, "preset_id": 0},
        {**base, "extra": True},
        {k: v for k, v in base.items() if k != "limit"},
    ]
    for body in bad:
        assert harness.client.post("/api/imports", json=body).status_code == 422, body
    invalid_profile = harness.client.post("/api/imports", json={**base, "profile": "../etc"})
    assert invalid_profile.status_code == 422 and invalid_profile.json()["code"] == "invalid_input"
    missing_preset = harness.client.post("/api/imports", json={**base, "process": True, "preset_id": 999})
    assert missing_preset.status_code == 404


def test_import_snapshot_uses_default_preset(harness):
    presets = harness.client.get("/api/presets").json()
    harness.client.put("/api/settings", json={"default_preset_id": presets[1]["id"]})
    harness.downloader.items = [remote("SNAP")]
    batch = harness.import_profile(process=True)
    assert batch["preset_name"] == "Anti-duplicidade" and batch["process"] is True
    harness.client.put(f"/api/presets/{presets[1]['id']}", json={"params": {**presets[1]["params"], "speed": 1.5}})
    harness.run_imports()
    job = harness.ctx.jobs.for_batch(batch["id"])[0]
    assert job["params"]["speed"] == 1.05  # snapshot do momento da importação


def test_profile_preview(harness):
    response = harness.client.post("/api/instagram/profile", json={"profile": "@perfil_teste"})
    assert response.status_code == 200
    assert response.json()["username"] == "perfil_teste" and set(response.json()) == {
        "username", "user_id", "full_name", "avatar_url", "post_count", "is_private", "is_verified"}
    harness.downloader.listing_error = InstagramError("profile_not_found")
    missing = harness.client.post("/api/instagram/profile", json={"profile": "nao_existe"})
    assert missing.status_code == 404 and missing.json()["code"] == "profile_not_found"
    assert harness.client.post("/api/instagram/profile", json={"profile": "a b"}).status_code == 422


def test_full_import_with_real_threads(harness_factory):
    downloader = FakeInstagramDownloader(items=[remote(f"T{i}", media_type="video" if i % 2 else "image")
                                                for i in range(6)])
    harness = harness_factory(downloader=downloader, start_worker=True)
    batch = harness.import_profile(process=True)
    assert wait_until(lambda: harness.batch(batch["id"])["status"] == "completed", timeout=20)
    result = harness.batch(batch["id"])
    assert result["progress"] == {"found": 6, "downloaded": 6, "processed": 6, "skipped": 0, "failed": 0,
                                  "pending": 0, "active": 0}
