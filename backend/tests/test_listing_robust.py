"""Listagem robusta de lotes grandes (docs/BATCH_ROBUSTNESS.md, seção 9: C1, M1, M4, M3). Offline, valores FAKE."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from app.db.connection import Database, parse_utc, utc_now
from app.db.migrate import MIGRATIONS_DIR, apply_migrations
from app.db.repositories.batches import BatchRepository
from app.db.repositories.media import MediaRepository
from app.integrations.instagram.downloader import InstagramError
from app.jobs.import_runner import MAX_PENDING_INGEST_GLOBAL
from conftest import wait_until
from media_fakes import FakeInstagramDownloader, remote

N = 1000


def _items(n: int):
    return [remote(f"R{i:04d}") for i in range(n)]


def _row(h, batch_id: int) -> dict:
    return dict(h.ctx.db.fetchone(
        "SELECT status, listing_state, listing_cursor, listing_next_page, listing_retry_level, listing_retry_at,"
        " listing_cursor_refusals, listing_attention_tries, error_code, error_message FROM batches WHERE id = ?",
        (batch_id,)))


def _expire_wait(h) -> None:
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = NULL")  # fim do intervalo de backoff


def _drive(h, batch_id: int, rounds: int = 80) -> dict:
    for _ in range(rounds):
        h.run_imports()
        h.run_jobs()
        if h.ctx.batches.get(batch_id)["status"] == "completed":
            break
    return h.batch(batch_id)


def _keys(h, batch_id: int) -> list[str]:
    return [j["source_post_id"] for j in h.ctx.jobs.for_batch(batch_id)]


def _assert_complete_without_duplicates(h, dl, batch_id: int) -> None:
    batch = _drive(h, batch_id)
    keys = _keys(h, batch_id)
    assert batch["status"] == "completed" and batch["found"] == N and batch["error_code"] is None
    assert len(keys) == len(set(keys)) == N
    assert len(dl.downloads) == len(set(dl.downloads)) == N, "nada baixado duas vezes"
    assert h.ctx.db.scalar("SELECT listing_cursor FROM batches WHERE id = ?", (batch_id,)) is None



@pytest.mark.parametrize("code", ["access_refused", "contract_changed"])
def test_attention_error_on_page_3_keeps_checkpoint_and_resumes_to_1000(harness_factory, code):
    dl = FakeInstagramDownloader(items=_items(N))
    dl.listing_error = InstagramError(code)
    dl.listing_error_after = 24  # páginas 1-2 entregues; erro ao pedir a página 3
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile(limit=N)["id"]
    h.run_all()
    row = _row(h, batch_id)
    assert row["status"] == "listing" and row["listing_state"] == "needs_attention", row
    assert row["listing_next_page"] == 3 and row["listing_cursor"] == "FAKE-CURSOR-3"
    assert row["error_code"] == code and row["error_message"]
    api = h.batch(batch_id)
    assert api["listing_state"] == "needs_attention" and api["found"] == 24 and "FAKE-CURSOR" not in str(api)
    listings = dl.listings
    h.run_all()
    assert dl.listings == listings, "needs_attention só retoma por ação do usuário"

    dl.listing_error = None
    resumed = h.client.post(f"/api/batches/{batch_id}/resume-listing")
    assert resumed.status_code == 200 and resumed.json()["listing_state"] == "pending"
    assert resumed.json()["error_code"] is None
    _assert_complete_without_duplicates(h, dl, batch_id)
    assert dl.page_requests[:4] == [1, 2, 3, 3], "retoma do checkpoint da página 3"


def test_repeated_timeouts_pause_with_backoff_then_resume_and_finish_1000(harness_factory):
    dl = FakeInstagramDownloader(items=_items(N))
    dl.listing_error = InstagramError("timeout")
    dl.listing_error_after = 24
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile(limit=N)["id"]
    h.run_all()
    delays = []
    row = _row(h, batch_id)
    delays.append(round((parse_utc(row["listing_retry_at"]) - parse_utc(utc_now())).total_seconds()))
    assert row["listing_state"] == "paused" and row["listing_retry_level"] == 1
    h.run_imports()
    assert dl.listings == 1, "dentro do backoff: não lista"
    dl.listing_error_after = 0  # falhas seguidas sem progresso
    for level in (2, 3, 4, 5):
        _expire_wait(h)
        h.run_imports()
        row = _row(h, batch_id)
        assert row["status"] == "listing" and row["listing_state"] == "paused" and row["listing_retry_level"] == level
        assert row["error_code"] == "timeout" and row["listing_next_page"] == 3
        delays.append(round((parse_utc(row["listing_retry_at"]) - parse_utc(utc_now())).total_seconds()))
    assert all(abs(d - e) <= 2 for d, e in zip(delays, [60, 300, 900, 3600, 3600], strict=True)), delays
    assert h.batch(batch_id)["found"] == 24

    dl.listing_error = None
    _expire_wait(h)
    _assert_complete_without_duplicates(h, dl, batch_id)


def test_resume_listing_endpoint(harness_factory):
    dl = FakeInstagramDownloader(items=_items(30))
    dl.listing_error = InstagramError("network_error")
    dl.listing_error_after = 12
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile(limit=20)["id"]
    h.run_imports()
    assert _row(h, batch_id)["listing_state"] == "paused" and _row(h, batch_id)["listing_retry_at"] is not None
    dl.listing_error = None
    r = h.client.post(f"/api/batches/{batch_id}/resume-listing")
    assert r.status_code == 200 and r.json()["listing_state"] == "pending"
    row = _row(h, batch_id)
    assert row["listing_retry_at"] is None and row["listing_retry_level"] == 0
    again = h.client.post(f"/api/batches/{batch_id}/resume-listing")
    assert again.status_code == 409 and again.json()["code"] == "listing_not_resumable"
    h.run_all()
    assert dl.listings == 2 and h.batch(batch_id)["status"] == "completed"
    done = h.client.post(f"/api/batches/{batch_id}/resume-listing")
    assert done.status_code == 409 and done.json()["code"] == "listing_not_resumable"
    assert h.client.post("/api/batches/999999/resume-listing").status_code == 404


def test_refused_cursor_never_drops_checkpoint_retries_same_cursor_then_needs_attention(harness_factory):
    # Experimento da re-revisão: recusa persistente depois de retomar não pode terminar o lote com 96 itens nem
    # perder o checkpoint da página 9.
    dl = FakeInstagramDownloader(items=_items(N))
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile(limit=N)["id"]
    h.run_imports()
    saved = _row(h, batch_id)
    assert (saved["listing_cursor"], saved["listing_next_page"]) == ("FAKE-CURSOR-9", 9)
    h.run_jobs()
    dl.listing_error = InstagramError("access_refused")
    dl.listing_error_after = 0  # toda requisição recusada, inclusive a página 1
    h.run_imports()  # 1a recusa: nova tentativa com o mesmo cursor depois do backoff
    row = _row(h, batch_id)
    assert row["listing_state"] == "paused" and row["listing_cursor_refusals"] == 1, row
    assert (row["listing_cursor"], row["listing_next_page"]) == ("FAKE-CURSOR-9", 9)
    _expire_wait(h)
    h.run_all()  # 2a recusa: needs_attention com o checkpoint
    row = _row(h, batch_id)
    assert row["status"] == "listing" and row["listing_state"] == "needs_attention", row
    assert (row["listing_cursor"], row["listing_next_page"]) == ("FAKE-CURSOR-9", 9), "checkpoint mantido"
    assert dl.page_requests.count(1) == 1, "nunca recomeça da página 1 sozinho"
    assert h.batch(batch_id)["status"] == "listing" and h.batch(batch_id)["found"] == 96

    dl.listing_error = None
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    _assert_complete_without_duplicates(h, dl, batch_id)
    assert dl.page_requests.count(1) == 1 and dl.page_requests.count(9) == 3


def test_restart_listing_requires_confirmation_and_restarts_from_page_1(harness_factory):
    dl = FakeInstagramDownloader(items=_items(300))
    dl.listing_error = InstagramError("contract_changed")
    dl.listing_error_after = 36  # páginas 1-3 entregues
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile(limit=250)["id"]
    h.run_all()
    assert _row(h, batch_id)["listing_state"] == "needs_attention"
    warn = h.client.post(f"/api/batches/{batch_id}/restart-listing")
    assert warn.status_code == 409 and warn.json()["code"] == "restart_confirmation_required"
    assert "página 1" in warn.json()["detail"] and "3" in warn.json()["detail"]
    assert _row(h, batch_id)["listing_next_page"] == 4, "sem confirmação nada muda"
    dl.listing_error = None
    ok = h.client.post(f"/api/batches/{batch_id}/restart-listing", json={"confirm": True})
    assert ok.status_code == 200 and ok.json()["listing_state"] == "pending"
    row = _row(h, batch_id)
    assert row["listing_cursor"] is None and row["listing_next_page"] is None
    batch = _drive(h, batch_id)
    keys = _keys(h, batch_id)
    assert batch["status"] == "completed" and batch["found"] == 250
    assert len(keys) == len(set(keys)) == 250 and len(dl.downloads) == len(set(dl.downloads)) == 250
    assert dl.page_requests[:5] == [1, 2, 3, 4, 1], "recomeço só pela ação do usuário"
    again = h.client.post(f"/api/batches/{batch_id}/restart-listing", json={"confirm": True})
    assert again.status_code == 409 and again.json()["code"] == "listing_not_resumable"


def test_needs_attention_retries_every_6h_at_most_4_times_and_does_not_count_as_active(harness_factory):
    dl = FakeInstagramDownloader(items=_items(100))
    dl.listing_error = InstagramError("access_refused")
    dl.listing_error_after = 24
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile(limit=100)["id"]
    h.run_all()
    row = _row(h, batch_id)
    assert row["listing_state"] == "needs_attention" and row["listing_attention_tries"] == 1
    wait = (parse_utc(row["listing_retry_at"]) - parse_utc(utc_now())).total_seconds()
    assert abs(wait - 6 * 3600) <= 5
    assert not h.ctx.batches.has_active_work(), "needs_attention não impede a suspensão"
    h.run_imports()
    assert dl.listings == 1, "antes de 6 h: nada"
    dl.listing_error_after = 0  # as novas tentativas continuam recusadas logo no primeiro pedido
    for attempt in range(1, 5):
        h.ctx.db.execute("UPDATE batches SET listing_retry_at = '2000-01-01T00:00:00.000Z'"
                         " WHERE listing_retry_at IS NOT NULL")
        h.run_imports()
        assert dl.listings == 1 + attempt
        row = _row(h, batch_id)
        assert row["listing_state"] == "needs_attention" and row["listing_next_page"] == 3
    assert row["listing_retry_at"] is None and row["listing_attention_tries"] == 4, row
    h.run_imports()
    assert dl.listings == 5, "depois de 4 tentativas automáticas, só a ação do usuário"
    assert not h.ctx.batches.has_active_work()

    dl.listing_error = None
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    assert h.ctx.batches.has_active_work()
    h.ctx.batches.update_listing(batch_id, listing_state="paused", listing_retry_at=utc_now(3600))
    assert not h.ctx.batches.has_active_work(), "pausa longa da listagem não conta"
    h.ctx.batches.update_listing(batch_id, listing_retry_at=utc_now(60))
    assert h.ctx.batches.has_active_work(), "pausa curta conta"
    h.ctx.batches.update_listing(batch_id, listing_retry_at=None)
    batch = _drive(h, batch_id)
    assert batch["status"] == "completed" and batch["found"] == 100


def test_item_in_flight_in_another_batch_is_not_downloaded_twice(harness_factory):
    dl = FakeInstagramDownloader(items=_items(300))
    h = harness_factory(downloader=dl)
    first = h.import_profile(profile="perfil_a", limit=250)["id"]
    second = h.import_profile(profile="perfil_b", limit=250)["id"]
    h.run_imports()  # os dois listam antes de qualquer download: o 2o vê os itens do 1o em andamento
    in_flight = [j for j in h.ctx.jobs.for_batch(second) if j["duplicate_reason"] == "duplicate_in_flight"]
    # O dependente aguarda o primário (pending, fora do backpressure) e depois liga à mídia sem baixar.
    assert in_flight and all(j["status"] == "pending" and j["primary_job_id"] for j in in_flight)
    jobs_api = h.client.get("/api/jobs", params={"batch_id": second})
    assert jobs_api.status_code == 200
    for _ in range(100):
        h.run_all()
        if all(h.ctx.batches.get(i)["status"] == "completed" for i in (first, second)):
            break
    assert all(h.batch(i)["status"] == "completed" and h.batch(i)["found"] == 250 for i in (first, second))
    assert len(dl.downloads) == len(set(dl.downloads)) == 250
    done = {j["id"]: j for j in h.ctx.jobs.for_batch(second)}
    assert all(done[j["id"]]["status"] == "completed" and done[j["id"]]["stage"] == "skipped"
               and done[j["id"]]["duplicate_reason"] == "duplicate_in_flight" and done[j["id"]]["media_id"]
               for j in in_flight)


def _two_batches_same_items(h, *, second_process: bool = False) -> tuple[int, int]:
    first = h.import_profile(profile="perfil_a", limit=10)["id"]
    body = {"process": True, "preset_id": h.client.get("/api/presets").json()[0]["id"]} if second_process else {}
    second = h.import_profile(profile="perfil_b", limit=10, **body)["id"]
    h.run_imports()
    return first, second


def test_in_flight_dependent_waits_without_backpressure_then_links_and_processes(harness_factory):
    dl = FakeInstagramDownloader(items=_items(5))
    h = harness_factory(downloader=dl)
    first, second = _two_batches_same_items(h, second_process=True)
    deps = h.ctx.jobs.for_batch(second)
    assert [j["status"] for j in deps] == ["pending"] * 5
    assert all(j["duplicate_reason"] == "duplicate_in_flight" for j in deps)
    assert h.ctx.jobs.pending_ingest_count(second) == 0, "o dependente não conta no backpressure do lote"
    assert h.ctx.batches.pending_ingest_global() == 5, "nem no global (só os 5 primários)"
    assert h.ctx.jobs.claim(allow_downloads=True, pool="download", batch_id=second) is None, "não é reivindicado"
    h.ctx.worker.run_pending()
    assert all(h.batch(i)["status"] == "completed" for i in (first, second))
    assert len(dl.downloads) == len(set(dl.downloads)) == 5, "sem download duplicado no caso feliz"
    media = {j["source_post_id"]: j["media_id"] for j in h.ctx.jobs.for_batch(first)}
    for job in h.ctx.jobs.for_batch(second):
        assert job["status"] == "completed" and job["media_id"] == media[job["source_post_id"]]
        assert job["rendition_id"] and job["duplicate_reason"] == "duplicate_in_flight"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == 5


def test_in_flight_dependent_downloads_itself_when_primary_fails_or_is_cancelled(harness_factory):
    dl = FakeInstagramDownloader(items=_items(4))
    dl.download_errors = {"R0000:0": [InstagramError("content_unavailable", "x", retryable=False)]}
    h = harness_factory(downloader=dl)
    first, second = _two_batches_same_items(h)
    primary = next(j for j in h.ctx.jobs.for_batch(first) if j["source_post_id"] == "R0001")
    h.ctx.jobs.update(primary["id"], status="cancelled", stage=None, finished_at=utc_now())  # primário cancelado
    h.ctx.worker.run_pending()
    jobs = {j["source_post_id"]: j for j in h.ctx.jobs.for_batch(second)}
    assert h.batch(second)["status"] == "completed"
    assert all(j["status"] == "completed" for j in jobs.values())
    for key in ("R0000", "R0001"):  # primário falho / cancelado: baixou sozinho, como job normal
        assert jobs[key]["duplicate_reason"] is None and not jobs[key]["is_duplicate"] and jobs[key]["media_id"]
    assert all(jobs[k]["duplicate_reason"] == "duplicate_in_flight" for k in ("R0002", "R0003"))
    assert sorted(dl.downloads) == ["R0000:0", "R0000:0", "R0001:0", "R0002:0", "R0003:0"]


def test_item_in_flight_with_real_download_threads(harness_factory):
    dl = FakeInstagramDownloader(items=_items(300))
    h = harness_factory(downloader=dl, start_worker=True, download_concurrency=2, poll_interval=0.01)
    h.ctx.worker.disk_check = lambda: (True, 1 << 40, 0)
    ids = [h.import_profile(profile=f"perfil_{c}", limit=250)["id"] for c in "ab"]
    assert wait_until(lambda: all(h.ctx.batches.get(i)["status"] == "completed" for i in ids), timeout=120)
    assert len(dl.downloads) == len(set(dl.downloads)), "nenhum item baixado duas vezes"


def test_cancel_sets_listing_state_cancelled(harness_factory):
    dl = FakeInstagramDownloader(items=_items(300))
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile(limit=250)["id"]
    h.run_imports()
    assert _row(h, batch_id)["listing_state"] == "waiting_backpressure"
    body = h.client.post(f"/api/batches/{batch_id}/cancel").json()
    assert body["status"] == "cancelled" and body["listing_state"] == "cancelled"
    h.run_all()
    assert _row(h, batch_id)["listing_state"] == "cancelled"
    assert not h.ctx.batches.has_active_work()


def test_global_backpressure_across_three_batches(harness_factory):
    dl = FakeInstagramDownloader(items=_items(300))
    h = harness_factory(downloader=dl)
    ids = [h.import_profile(profile=f"perfil_{c}", limit=250)["id"] for c in "abc"]
    h.run_imports()
    assert 0 < h.ctx.batches.pending_ingest_global() <= MAX_PENDING_INGEST_GLOBAL
    assert h.ctx.jobs.count_for_batch(ids[2]) == 0, "terceiro lote aguarda o teto global"
    assert _row(h, ids[2])["listing_state"] == "waiting_backpressure"
    peak = 0
    for _ in range(200):
        h.run_imports()
        peak = max(peak, h.ctx.batches.pending_ingest_global())
        h.ctx.worker.run_pending(max_jobs=40)
        if all(h.ctx.batches.get(i)["status"] == "completed" for i in ids):
            break
    assert peak <= MAX_PENDING_INGEST_GLOBAL
    assert [h.batch(i)["found"] for i in ids] == [250, 250, 250]
    assert all(h.batch(i)["status"] == "completed" for i in ids)


def test_profile_already_importing_returns_409(harness):
    h = harness
    payload = {"profile": "perfil_teste", "content_type": "all", "limit": 100, "preset_id": None, "process": False}
    first = h.import_profile()
    second = h.client.post("/api/imports", json=payload)
    assert second.status_code == 409 and second.json()["code"] == "profile_already_importing"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 1
    assert h.client.post("/api/imports", json={**payload, "profile": "outro_perfil"}).status_code == 202
    h.client.post(f"/api/batches/{first['id']}/cancel")
    assert h.client.post("/api/imports", json=payload).status_code == 202, "listagem encerrada libera o perfil"


def _media(h, n: int) -> list[int]:
    return [h.ctx.media.insert({
        "source_platform": "instagram", "source_profile": "alvo", "source_post_id": f"P{i}", "source_media_index": 0,
        "media_type": "image", "original_path": f"originals/fake{i}.jpg", "original_sha256": f"{i:064d}",
        "file_size": 10,
    }) for i in range(n)]


def _rendition(h, media_id: int, preset: dict) -> int:
    return h.ctx.media.insert_rendition({
        "media_id": media_id, "preset_id": preset["id"], "preset_name": preset["name"], "params": preset["params"],
        "processed_path": f"processed/fake{media_id}.mp4", "processed_sha256": f"{media_id:064d}", "file_size": 5,
    })


def test_process_by_filter_is_idempotent(harness):
    h = harness
    ids = _media(h, 4)
    preset = h.ctx.presets.get(1)
    _rendition(h, ids[0], preset)
    body = {"view": "all", "profile": "alvo", "preset_id": 1}
    first = h.client.post("/api/media/process-by-filter", json=body)
    assert first.status_code == 202, first.text
    created = h.ctx.jobs.for_batch(first.json()["id"])
    assert sorted(j["media_id"] for j in created) == ids[1:], "mídia já processada com o preset não entra"
    jobs = h.ctx.db.scalar("SELECT COUNT(*) FROM jobs")
    batches = h.ctx.db.scalar("SELECT COUNT(*) FROM batches")
    second = h.client.post("/api/media/process-by-filter", json=body)
    assert second.status_code == 409 and second.json()["code"] == "nothing_to_process"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs") == jobs and h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == batches
    with pytest.raises(sqlite3.IntegrityError):
        _rendition(h, ids[0], preset)


def test_migration_dedupes_renditions_and_keeps_listing_state(tmp_path):
    old = tmp_path / "old_migrations"
    old.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0008":
            shutil.copy(path, old)
    db = Database(tmp_path / "data" / "media-tool.db")
    try:
        apply_migrations(db, old)
        media_id = MediaRepository(db).insert({
            "source_platform": "instagram", "source_profile": "alvo", "source_post_id": "X", "source_media_index": 0,
            "media_type": "image", "original_path": "originals/x.jpg", "original_sha256": "a" * 64, "file_size": 1,
        })
        now = utc_now()
        rendition_ids = []
        for params in ('{"a":1}', '{"a":1}', '{"a":1}', '{"a":2}'):
            rendition_ids.append(db.execute(
                "INSERT INTO renditions (media_id, preset_id, params, processed_path, processed_sha256, file_size,"
                " created_at) VALUES (?, 1, ?, 'processed/x.mp4', ?, 1, ?)", (media_id, params, "b" * 64, now),
            ).lastrowid)
        job_id = db.execute(
            "INSERT INTO jobs (media_id, rendition_id, kind, status, created_at, updated_at)"
            " VALUES (?, ?, 'process', 'completed', ?, ?)", (media_id, rendition_ids[2], now, now),
        ).lastrowid
        db.execute("UPDATE jobs SET is_duplicate = 1, duplicate_reason = 'sha256' WHERE id = ?", (job_id,))
        batches = BatchRepository(db)
        batch_id = batches.create(source="instagram_import", status="listing", source_profile="alvo",
                                  content_type="all", requested_limit=10)
        db.execute("UPDATE batches SET listing_state = 'paused', listing_cursor = 'FAKE-C' WHERE id = ?", (batch_id,))
        cancelled = batches.create(source="instagram_import", status="cancelled", source_profile="beta",
                                   content_type="all", requested_limit=10)
        db.execute("UPDATE batches SET listing_state = 'waiting_backpressure' WHERE id = ?", (cancelled,))

        apply_migrations(db)
        remaining = [r["id"] for r in db.fetchall("SELECT id FROM renditions ORDER BY id")]
        assert remaining == [rendition_ids[0], rendition_ids[3]]
        assert db.scalar("SELECT rendition_id FROM jobs WHERE id = ?", (job_id,)) == rendition_ids[0]
        assert db.scalar("SELECT duplicate_reason FROM jobs WHERE id = ?", (job_id,)) == "sha256"
        db.execute("UPDATE jobs SET duplicate_reason = 'duplicate_in_flight' WHERE id = ?", (job_id,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE jobs SET duplicate_reason = 'bogus' WHERE id = ?", (job_id,))
        row = db.fetchone("SELECT listing_state, listing_cursor, listing_retry_level, listing_cursor_refusals,"
                          " listing_attention_tries FROM batches WHERE id = ?", (batch_id,))
        assert dict(row) == {"listing_state": "paused", "listing_cursor": "FAKE-C", "listing_retry_level": 0,
                             "listing_cursor_refusals": 0, "listing_attention_tries": 0}
        assert db.scalar("SELECT listing_state FROM batches WHERE id = ?", (cancelled,)) == "cancelled"
        db.execute("UPDATE batches SET listing_state = 'needs_attention' WHERE id = ?", (batch_id,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE batches SET listing_state = 'bogus' WHERE id = ?", (batch_id,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO renditions (media_id, preset_id, params, processed_path, processed_sha256, file_size,"
                " created_at) VALUES (?, 1, '{\"a\":1}', 'processed/y.mp4', ?, 1, ?)", (media_id, "c" * 64, now),
            )
    finally:
        db.close_all()


@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("FAKE database is locked"),
    InstagramError("session_missing"),
    InstagramError("profile_not_found"),
    InstagramError("content_unavailable"),
])
def test_mid_batch_error_never_completes_partial_nor_drops_cursor(harness_factory, error):
    dl = FakeInstagramDownloader(items=_items(300))
    dl.listing_error = error
    dl.listing_error_after = 24  # páginas 1-2 entregues (checkpoint da página 3 salvo)
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile(limit=250)["id"]
    h.run_all()
    row = _row(h, batch_id)
    assert row["status"] == "listing" and row["listing_state"] == "needs_attention", row
    assert (row["listing_cursor"], row["listing_next_page"]) == ("FAKE-CURSOR-3", 3)
    assert row["error_code"] and row["error_message"] and "FAKE database" not in row["error_message"]
    assert h.batch(batch_id)["found"] == 24
    dl.listing_error = None
    assert h.client.post(f"/api/batches/{batch_id}/resume-listing").status_code == 200
    batch = _drive(h, batch_id)
    keys = _keys(h, batch_id)
    assert batch["status"] == "completed" and batch["found"] == 250 and len(keys) == len(set(keys)) == 250


def test_unsupported_media_mid_batch_does_not_stop_listing(harness_factory):
    dl = FakeInstagramDownloader(items=_items(300))
    dl.listing_error = InstagramError("unsupported_media")
    dl.listing_error_after = 24
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile(limit=250)["id"]
    h.run_imports()
    row = _row(h, batch_id)
    assert row["status"] == "listing" and row["listing_state"] != "needs_attention", row
    assert row["listing_next_page"] >= 3 and dl.listings >= 2, "segue do checkpoint, sem concluir parcial"
    dl.listing_error = None
    batch = _drive(h, batch_id)
    assert batch["status"] == "completed" and batch["found"] == 250 and batch["error_code"] is None


def test_process_by_filter_excludes_only_open_jobs_of_the_same_preset(harness):
    h = harness
    ids = _media(h, 3)
    other = h.ctx.presets.get(2)
    h.ctx.jobs.create_process(media=h.ctx.media.get(ids[0]), batch_id=None, preset_id=other["id"],
                              preset_name=other["name"], params={**other["params"], "speed": 1.2})
    first = h.client.post("/api/media/process-by-filter", json={"view": "all", "profile": "alvo", "preset_id": 1})
    assert first.status_code == 202, first.text
    created = sorted(j["media_id"] for j in h.ctx.jobs.for_batch(first.json()["id"]))
    assert created == ids, "job aberto de OUTRO preset/params não exclui a mídia"
    again = h.client.post("/api/media/process-by-filter", json={"view": "all", "profile": "alvo", "preset_id": 1})
    assert again.status_code == 409 and again.json()["code"] == "nothing_to_process"
