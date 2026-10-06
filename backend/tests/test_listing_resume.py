"""Listagem incremental e retomável com backpressure (docs/BATCH_ROBUSTNESS.md, seção 0). Offline, valores FAKE."""

from __future__ import annotations

from app.integrations.instagram.downloader import InstagramError
from app.jobs.import_runner import MAX_PENDING_INGEST, RESUME_PENDING_INGEST
from media_fakes import FakeInstagramDownloader, remote


def _items(n: int):
    return [remote(f"L{i:04d}") for i in range(n)]


def _internal_batch(h, limit: int) -> int:
    """Lote com limite arbitrário pelo caminho interno (a API só aceita 10/20/50/100)."""
    batch_id = h.ctx.batches.create(source="instagram_import", status="listing", source_profile="perfil_teste",
                                    content_type="all", requested_limit=limit)
    return batch_id


def _state(h, batch_id: int) -> str | None:
    return h.ctx.db.scalar("SELECT listing_state FROM batches WHERE id = ?", (batch_id,))


def _pending(h, batch_id: int) -> int:
    return h.ctx.jobs.pending_ingest_count(batch_id)


def _keys(h, batch_id: int) -> list[str]:
    return [j["source_post_id"] for j in h.ctx.jobs.for_batch(batch_id)]


def test_backpressure_never_exceeds_max_pending_and_resumes_below_threshold(harness_factory):
    dl = FakeInstagramDownloader(items=_items(300))
    h = harness_factory(downloader=dl)
    batch_id = _internal_batch(h, 300)
    h.run_imports()
    assert _state(h, batch_id) == "waiting_backpressure"
    assert 0 < _pending(h, batch_id) <= MAX_PENDING_INGEST
    stopped_at = list(dl.page_requests)
    h.ctx.worker.run_pending(max_jobs=_pending(h, batch_id) - RESUME_PENDING_INGEST)  # ainda == 40
    h.run_imports()
    assert dl.page_requests == stopped_at, "só retoma abaixo de RESUME_PENDING_INGEST"
    peak = 0
    for _ in range(100):
        h.run_imports()
        peak = max(peak, _pending(h, batch_id))
        h.ctx.worker.run_pending(max_jobs=25)
        if h.batch(batch_id)["status"] == "completed":
            break
    batch = h.batch(batch_id)
    assert peak <= MAX_PENDING_INGEST
    assert batch["status"] == "completed" and batch["found"] == 300 and _state(h, batch_id) == "done"
    assert dl.page_requests == list(range(1, 26)), "cada página pedida uma vez, em ordem"
    assert sorted(dl.downloads) == sorted(f"L{i:04d}:0" for i in range(300))


def test_restart_resumes_from_saved_checkpoint_without_repeating(harness_factory):
    first = FakeInstagramDownloader(items=_items(150))
    h = harness_factory(downloader=first)
    batch_id = h.import_profile(limit=100)["id"]
    h.run_imports()  # 8 páginas (96 pendentes): a 9a passaria de 100
    assert _state(h, batch_id) == "waiting_backpressure" and first.page_requests == list(range(1, 9))
    h.ctx.worker.run_pending(max_jobs=60)
    saved = h.ctx.db.fetchone("SELECT listing_cursor, listing_next_page FROM batches WHERE id = ?", (batch_id,))
    assert saved["listing_cursor"] == "FAKE-CURSOR-9" and saved["listing_next_page"] == 9

    second = FakeInstagramDownloader(items=_items(150))
    h2 = harness_factory(root=h.root, downloader=second)  # "reinício": novo app sobre o mesmo banco
    h2.run_all()
    for _ in range(5):
        h2.run_all()
    batch = h2.batch(batch_id)
    assert batch["status"] == "completed" and batch["found"] == 100
    assert second.page_requests == [9], "retoma da página 9, sem repetir páginas antigas"
    assert len(_keys(h2, batch_id)) == len(set(_keys(h2, batch_id))) == 100
    assert not set(first.downloads) & set(second.downloads), "o que terminou não é baixado de novo"
    assert len(first.downloads) + len(second.downloads) == 100
    assert h2.ctx.db.scalar("SELECT listing_cursor FROM batches WHERE id = ?", (batch_id,)) is None


def test_cancel_while_waiting_backpressure_stops_listing_and_jobs(harness_factory):
    dl = FakeInstagramDownloader(items=_items(300))
    h = harness_factory(downloader=dl)
    batch_id = _internal_batch(h, 300)
    h.run_imports()
    assert _state(h, batch_id) == "waiting_backpressure"
    assert h.client.post(f"/api/batches/{batch_id}/cancel").json()["status"] == "cancelled"
    listings = dl.listings
    h.ctx.worker.run_pending(max_jobs=200)
    h.run_imports()
    assert dl.listings == listings and dl.downloads == []
    statuses = {j["status"] for j in h.ctx.jobs.for_batch(batch_id)}
    assert statuses == {"cancelled"}


def test_rate_limit_mid_listing_pauses_and_resumes_from_checkpoint(harness_factory):
    dl = FakeInstagramDownloader(items=_items(80))
    dl.listing_error = InstagramError("rate_limited")
    dl.listing_error_after = 24  # depois de 2 páginas completas (checkpoint da página 3 salvo)
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile(limit=50)["id"]
    h.run_imports()
    batch = h.batch(batch_id)
    assert batch["status"] == "listing" and batch["error_code"] == "rate_limited" and batch["found"] == 24
    assert _state(h, batch_id) == "paused" and h.ctx.worker.downloads_paused()
    h.run_imports()
    assert dl.listings == 1, "pausado: não lista"
    dl.listing_error = None
    h.ctx.worker.clear_pause()
    requests_before = len(dl.page_requests)
    h.run_all()
    batch = h.batch(batch_id)
    assert dl.page_requests[requests_before] == 3, "retoma do checkpoint"
    assert batch["status"] == "completed" and batch["found"] == 50 and batch["error_code"] is None


def test_dedupe_between_chunks_after_partial_page(harness_factory):
    dl = FakeInstagramDownloader(items=_items(80))
    dl.listing_error = InstagramError("network_error")
    dl.listing_error_after = 30  # páginas 1-2 com checkpoint + 6 itens da página 3
    h = harness_factory(downloader=dl)
    batch_id = h.import_profile(limit=50)["id"]
    h.run_all()
    assert h.batch(batch_id)["found"] == 30 and _state(h, batch_id) == "paused"
    dl.listing_error = None
    h.ctx.db.execute("UPDATE batches SET listing_retry_at = NULL")  # fim do intervalo de espera
    h.run_all()
    batch = h.batch(batch_id)
    keys = _keys(h, batch_id)
    assert batch["status"] == "completed" and batch["found"] == 50
    assert len(keys) == len(set(keys)) == 50 and keys == [f"L{i:04d}" for i in range(50)]
    assert dl.page_requests == [1, 2, 3, 3, 4, 5], "só a página parcial é pedida de novo"
    assert len(dl.downloads) == len(set(dl.downloads)) == 50
