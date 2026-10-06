"""Renovação de URL expirada por página da listagem (docs/BATCH_ROBUSTNESS.md, seção 2). Offline, valores FAKE."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace

import pytest

from app.integrations.instagram.downloader import InstagramError
from app.jobs.pipeline import JobExecutor
from media_fakes import FakeInstagramDownloader, remote

SECRET_NEEDLES = ("FAKE-CURSOR", "SIGFAKE", "scontent", "listing_cursor", "media_url", "listing_page")


def _key(item) -> str:
    return f"{item.source_post_id}:{item.source_media_index}"


def _fresh_url(key: str) -> str:
    return f"https://scontent.cdninstagram.com/v/FRESH-{key}.jpg?oh=SIGFAKE"


@dataclass
class RefreshDownloader(FakeInstagramDownloader):
    expired: set[str] = field(default_factory=set)  # media_url recusadas pelo "CDN"
    missing: set[str] = field(default_factory=set)  # chaves ausentes na página renovada
    refresh_calls: list = field(default_factory=list)
    refresh_error: Exception | None = None
    refresh_delay: float = 0.0
    barrier: threading.Barrier | None = None  # sincroniza a 1a recusa de downloads concorrentes
    download_urls: dict[str, list[str]] = field(default_factory=dict)

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        self.download_urls.setdefault(_key(item), []).append(item.media_url)
        if item.media_url in self.expired:
            if self.barrier is not None and "FRESH-" not in item.media_url:
                self.barrier.wait(5)
            raise InstagramError("media_url_expired")
        return super().download(item, dest_dir, cancel=cancel, progress=progress)

    def refresh_page(self, profile, ref, *, cancel=None):
        self.refresh_calls.append(ref)
        if self.refresh_delay:
            time.sleep(self.refresh_delay)
        if self.refresh_error is not None:
            raise self.refresh_error
        size = ref.page_size
        page = self.items[(ref.page_index - 1) * size: ref.page_index * size]
        return [replace(item, media_url=_fresh_url(_key(item)), listing_ref=ref)
                for item in page if _key(item) not in self.missing]


def _setup(harness_factory, n: int = 3, **kwargs):
    downloader = RefreshDownloader(items=[remote(f"P{i}") for i in range(n)], **kwargs)
    h = harness_factory(downloader=downloader)
    batch = h.import_profile()
    h.run_imports()
    return h, downloader, batch["id"]


def _job(h, batch_id: int, code: str) -> dict:
    return next(j for j in h.ctx.jobs.for_batch(batch_id) if j["source_post_id"] == code)


def test_expired_url_refreshes_page_and_download_completes(harness_factory):
    h, dl, batch_id = _setup(harness_factory)
    dl.expired.add(remote("P1").media_url)
    h.run_jobs()
    job = _job(h, batch_id, "P1")
    assert job["status"] == "completed" and job["attempts"] == 0
    assert len(dl.refresh_calls) == 1
    ref = dl.refresh_calls[0]
    assert ref.page_index == 1 and ref.cursor is None and ref.page_size == 12
    assert dl.download_urls["P1:0"] == [remote("P1").media_url, _fresh_url("P1:0")]
    assert h.batch(batch_id)["status"] == "completed"


def test_concurrent_expiry_on_same_page_does_exactly_one_refresh(harness_factory):
    h, dl, batch_id = _setup(harness_factory, refresh_delay=0.3)
    dl.expired.update({remote("P0").media_url, remote("P1").media_url})
    dl.barrier = threading.Barrier(2)
    claimed = [h.ctx.jobs.claim(allow_downloads=True, owner="t1"), h.ctx.jobs.claim(allow_downloads=True, owner="t2")]
    threads = [threading.Thread(target=JobExecutor(h.ctx, h.ctx.worker, job, threading.Event()).run)
               for job in claimed]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(dl.refresh_calls) == 1
    for code in ("P0", "P1"):
        assert _job(h, batch_id, code)["status"] == "completed"
        assert dl.download_urls[f"{code}:0"] == [remote(code).media_url, _fresh_url(f"{code}:0")]


@pytest.mark.parametrize("code, paused, status", [
    ("rate_limited", True, "pending"),
    ("contract_changed", False, "failed"),
    ("content_unavailable", False, "failed"),
])
def test_refresh_failure_follows_normal_classification(harness_factory, code, paused, status):
    h, dl, batch_id = _setup(harness_factory, n=1)
    dl.expired.add(remote("P0").media_url)
    dl.refresh_error = InstagramError(code)
    h.run_jobs()
    h.run_jobs()
    job = _job(h, batch_id, "P0")
    assert job["error_code"] == code and job["status"] == status
    assert len(dl.refresh_calls) == 1  # sem nova renovação nem retry extra
    assert dl.download_urls["P0:0"] == [remote("P0").media_url]
    assert h.ctx.worker.downloads_paused() is paused
    if status == "pending":
        # Seção 9, M7: aguarda o fim da pausa (que bloqueia o claim) sem consumir tentativa.
        assert job["stage"] == "waiting_retry" and job["attempts"] == 0


def test_item_missing_on_refreshed_page_is_content_unavailable(harness_factory):
    h, dl, batch_id = _setup(harness_factory, missing={"P1:0"})
    dl.expired.add(remote("P1").media_url)
    h.run_jobs()
    job = _job(h, batch_id, "P1")
    assert job["status"] == "failed" and job["error_code"] == "content_unavailable" and job["next_attempt_at"] is None
    assert len(dl.refresh_calls) == 1 and dl.download_urls["P1:0"] == [remote("P1").media_url]
    assert _job(h, batch_id, "P0")["status"] == "completed"


def test_old_job_waiting_in_queue_uses_stored_cursor(harness_factory):
    h, dl, batch_id = _setup(harness_factory, n=15)  # P12..P14 na página 2
    stored = _job(h, batch_id, "P13")
    assert stored["listing_page_index"] == 2
    assert h.ctx.jobs.get_remote_ref(stored["id"])["listing_cursor"] == "FAKE-CURSOR-2"
    dl.expired.add(remote("P13").media_url)
    h.run_jobs()
    assert _job(h, batch_id, "P13")["status"] == "completed"
    assert len(dl.refresh_calls) == 1
    ref = dl.refresh_calls[0]
    assert ref.page_index == 2 and ref.cursor == "FAKE-CURSOR-2" and ref.page_size == 12


def test_refresh_updates_siblings_of_same_page(harness_factory):
    h, dl, batch_id = _setup(harness_factory)
    dl.expired.update({remote("P0").media_url, remote("P1").media_url})
    h.run_jobs()  # sequencial: P0 renova a página; P1 já começa com a URL nova
    assert len(dl.refresh_calls) == 1
    assert dl.download_urls["P1:0"] == [_fresh_url("P1:0")]
    assert all(j["status"] == "completed" for j in h.ctx.jobs.for_batch(batch_id))


def test_page_refreshed_recently_and_url_refused_fails_without_new_call(harness_factory):
    h, dl, batch_id = _setup(harness_factory)
    dl.expired.update({remote("P0").media_url, remote("P1").media_url, _fresh_url("P1:0")})
    h.run_jobs()
    assert len(dl.refresh_calls) == 1
    job = _job(h, batch_id, "P1")
    assert job["status"] == "failed" and job["error_code"] == "content_unavailable"
    assert job["attempts"] == 1 and job["next_attempt_at"] is None
    assert dl.download_urls["P1:0"] == [_fresh_url("P1:0")]


def test_still_refused_after_refresh_is_content_unavailable(harness_factory):
    h, dl, batch_id = _setup(harness_factory, n=1)
    dl.expired.update({remote("P0").media_url, _fresh_url("P0:0")})
    h.run_jobs()
    job = _job(h, batch_id, "P0")
    assert job["status"] == "failed" and job["error_code"] == "content_unavailable" and job["attempts"] == 1
    assert len(dl.refresh_calls) == 1 and len(dl.download_urls["P0:0"]) == 2


def test_job_without_listing_ref_fails_with_clear_message(harness_factory):
    h, dl, batch_id = _setup(harness_factory, n=1)
    h.ctx.db.execute("UPDATE jobs SET listing_page_index = NULL, listing_page_size = NULL WHERE batch_id = ?",
                     (batch_id,))
    h.ctx.db.execute("UPDATE job_remote_refs SET listing_page_index = NULL, listing_page_size = NULL")
    dl.expired.add(remote("P0").media_url)
    h.run_jobs()
    job = _job(h, batch_id, "P0")
    assert job["status"] == "failed" and job["error_code"] == "content_unavailable"
    assert "referência de listagem" in job["error_message"] and dl.refresh_calls == []


def test_urls_and_cursor_cleared_when_jobs_finish(harness_factory):
    h, dl, batch_id = _setup(harness_factory, n=15)
    dl.expired.add(remote("P13").media_url)
    dl.download_errors = {"P14:0": [InstagramError("content_unavailable")]}
    cancel_id = _job(h, batch_id, "P12")["id"]
    assert h.client.post(f"/api/jobs/{cancel_id}/cancel").status_code == 200
    h.run_jobs()
    rows = h.ctx.db.fetchall("SELECT status, listing_page_index FROM jobs WHERE batch_id = ?", (batch_id,))
    assert {r["status"] for r in rows} == {"completed", "failed", "cancelled"}
    # Lote concluído: URLs e cursores (job_remote_refs) apagados; jobs não têm mais colunas de URL/cursor.
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM job_remote_refs") == 0
    assert any(r["listing_page_index"] == 2 for r in rows)  # a referência de página (não sensível) permanece


def test_api_and_logs_never_expose_cursor_or_urls(harness_factory, caplog):
    with caplog.at_level("DEBUG"):
        h, dl, batch_id = _setup(harness_factory, n=15)
        dl.expired.update({remote("P13").media_url, remote("P1").media_url})
        for path in ("/api/jobs", f"/api/batches/{batch_id}", "/api/batches"):
            text = h.client.get(path).text
            assert not [n for n in SECRET_NEEDLES if n in text], path
        h.run_jobs()
        assert len(dl.refresh_calls) == 2
        for path in ("/api/jobs", f"/api/batches/{batch_id}", "/api/batches"):
            text = h.client.get(path).text
            assert not [n for n in SECRET_NEEDLES if n in text], path
    blob = "\n".join(f"{r.getMessage()} {r.args} {r.exc_text}" for r in caplog.records)
    assert "renovada" in blob
    for needle in ("FAKE-CURSOR", "SIGFAKE", "scontent", "FRESH-"):
        assert needle not in blob, needle
