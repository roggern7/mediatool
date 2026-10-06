from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from conftest import wait_until

from app.db.connection import parse_utc, utc_now
from app.integrations.instagram.downloader import InstagramError
from media_fakes import FakeInstagramDownloader, remote


def _import(harness, count: int, **body) -> dict:
    harness.downloader.items = [remote(f"SC{i}") for i in range(count)]
    batch = harness.import_profile(**body)
    harness.run_imports()
    return batch


def test_claim_is_atomic_across_threads(harness):
    _import(harness, 30)
    claimed: list[int] = []
    lock = threading.Lock()

    def claimer():
        while True:
            job = harness.ctx.jobs.claim(allow_downloads=True)
            if job is None:
                return
            with lock:
                claimed.append(job["id"])

    with ThreadPoolExecutor(max_workers=6) as pool:
        for _ in range(6):
            pool.submit(claimer)
    assert len(claimed) == 30
    assert len(set(claimed)) == 30
    statuses = {row["status"] for row in harness.ctx.db.fetchall("SELECT status FROM jobs")}
    assert statuses == {"downloading"}


def test_claim_respects_next_attempt_and_download_pause(harness):
    batch = _import(harness, 2)
    jobs = harness.ctx.jobs.for_batch(batch["id"])
    harness.ctx.jobs.update(jobs[0]["id"], next_attempt_at=utc_now(60))
    claimed = harness.ctx.jobs.claim(allow_downloads=True)
    assert claimed["id"] == jobs[1]["id"]
    assert harness.ctx.jobs.claim(allow_downloads=True) is None
    # Downloads pausados: ingest pendente sem mídia não é reivindicado
    harness.ctx.jobs.update(jobs[0]["id"], next_attempt_at=None)
    assert harness.ctx.jobs.claim(allow_downloads=False) is None
    assert harness.ctx.jobs.claim(allow_downloads=True)["id"] == jobs[0]["id"]


def test_concurrency_limit_and_runtime_change(harness_factory):
    gate = threading.Event()
    downloader = FakeInstagramDownloader(items=[remote(f"C{i}") for i in range(6)], block=gate)
    harness = harness_factory(downloader=downloader, start_worker=True)
    harness.client.put("/api/settings", json={"download_concurrency": 2})
    batch = harness.import_profile()
    worker = harness.ctx.worker
    peak = 0

    def active_is(n):
        nonlocal peak
        peak = max(peak, worker.active_count)
        return worker.active_count == n

    assert wait_until(lambda: active_is(2))
    for _ in range(10):
        peak = max(peak, worker.active_count)
        threading.Event().wait(0.02)
    assert peak == 2
    harness.client.put("/api/settings", json={"download_concurrency": 4})
    assert wait_until(lambda: worker.active_count == 4)
    gate.set()
    assert wait_until(lambda: harness.batch(batch["id"])["status"] == "completed")
    assert harness.batch(batch["id"])["progress"]["downloaded"] == 6


def test_cancel_running_and_pending_jobs(harness_factory):
    gate = threading.Event()
    downloader = FakeInstagramDownloader(items=[remote("RUN"), remote("WAIT")], block=gate)
    harness = harness_factory(downloader=downloader, start_worker=True, download_concurrency=1)
    batch = harness.import_profile()
    assert wait_until(lambda: harness.batch(batch["id"])["status"] == "running"
                      and harness.ctx.worker.active_count == 1)
    jobs = harness.batch(batch["id"])["jobs"]
    running = next(j for j in jobs if j["status"] == "downloading")
    pending = next(j for j in jobs if j["status"] == "pending")

    response = harness.client.post(f"/api/jobs/{pending['id']}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"

    harness.client.post(f"/api/jobs/{running['id']}/cancel")
    assert wait_until(lambda: harness.client.get(f"/api/jobs/{running['id']}").json()["status"] == "cancelled")
    assert harness.ctx.worker.running
    assert wait_until(lambda: harness.batch(batch["id"])["status"] == "completed")
    assert harness.client.post(f"/api/jobs/{running['id']}/cancel").status_code == 409
    assert not any(harness.ctx.storage.temp.iterdir())


def test_retry_with_backoff_until_exhausted(harness):
    harness.downloader.items = [remote("NET")]
    harness.downloader.download_errors = {"NET:0": [InstagramError("network_error") for _ in range(3)]}
    batch = harness.import_profile()
    harness.run_imports()

    expected_delays = [30, 120]
    for attempt, delay in enumerate(expected_delays, start=1):
        assert harness.run_jobs() == 1
        job = harness.batch(batch["id"])["jobs"][0]
        assert job["status"] == "pending" and job["stage"] == "waiting_retry"
        assert job["attempts"] == attempt and job["error_code"] == "network_error"
        wait = (parse_utc(job["next_attempt_at"]) - parse_utc(utc_now())).total_seconds()
        assert delay - 5 < wait <= delay
        assert harness.run_jobs() == 0  # ainda não chegou a hora
        harness.ctx.jobs.update(job["id"], next_attempt_at=utc_now(-1))

    assert harness.run_jobs() == 1
    job = harness.batch(batch["id"])["jobs"][0]
    assert job["status"] == "failed" and job["attempts"] == 3 and job["next_attempt_at"] is None
    assert harness.batch(batch["id"])["status"] == "completed"
    assert harness.run_jobs() == 0


def test_non_retryable_error_fails_immediately(harness):
    harness.downloader.items = [remote("GONE")]
    harness.downloader.download_errors = {"GONE:0": [InstagramError("content_unavailable")]}
    batch = harness.import_profile()
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    assert job["status"] == "failed" and job["attempts"] == 1 and job["error_code"] == "content_unavailable"
    assert job["error_message"]


def test_unexpected_error_does_not_kill_worker(harness_factory):
    downloader = FakeInstagramDownloader(items=[remote("BOOM"), remote("OK")])
    downloader.download_errors = {"BOOM:0": [RuntimeError("falha inesperada")]}
    harness = harness_factory(downloader=downloader, start_worker=True)
    batch = harness.import_profile()
    assert wait_until(lambda: harness.batch(batch["id"])["status"] == "completed")
    jobs = {j["source_post_id"]: j for j in harness.batch(batch["id"])["jobs"]}
    assert jobs["BOOM"]["status"] == "failed" and jobs["BOOM"]["error_code"] == "internal_error"
    assert "falha inesperada" not in jobs["BOOM"]["error_message"]
    assert jobs["OK"]["status"] == "completed"
    assert harness.ctx.worker.running


def test_rate_limited_pauses_downloads(harness):
    harness.downloader.items = [remote("RL1"), remote("RL2")]
    harness.downloader.download_errors = {"RL1:0": [InstagramError("rate_limited")]}
    harness.import_profile()
    harness.run_imports()
    assert harness.run_jobs() == 1  # RL1 falha com rate_limited e pausa os downloads
    assert harness.ctx.worker.downloads_paused()
    assert harness.run_jobs() == 0  # RL2 não é baixado durante a pausa
    harness.ctx.worker.clear_pause()
    assert harness.run_jobs() == 2  # seção 9, M7: RL1 esperou a pausa sem prazo de retry nem tentativa consumida


def test_job_retry_endpoint(harness):
    harness.downloader.items = [remote("FAIL")]
    harness.downloader.download_errors = {"FAIL:0": [InstagramError("content_unavailable")]}
    batch = harness.import_profile()
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    assert harness.batch(batch["id"])["status"] == "completed"
    retried = harness.client.post(f"/api/jobs/{job['id']}/retry")
    assert retried.status_code == 200 and retried.json()["status"] == "pending" and retried.json()["attempts"] == 0
    assert harness.batch(batch["id"])["status"] == "running"
    harness.run_jobs()
    assert harness.client.get(f"/api/jobs/{job['id']}").json()["status"] == "completed"
    assert harness.batch(batch["id"])["status"] == "completed"
    assert harness.client.post(f"/api/jobs/{job['id']}/retry").status_code == 409


# ---- lease, cancelamento pendente, jobs travados, pausa persistida, retry --------------------------------------


def test_cancel_requested_after_reschedule_is_finalized(harness):
    harness.downloader.items = [remote("RS")]
    harness.downloader.download_errors = {"RS:0": [InstagramError("network_error")]}
    batch = harness.import_profile()
    harness.run_imports()
    harness.run_jobs()
    job = harness.batch(batch["id"])["jobs"][0]
    assert job["status"] == "pending" and job["stage"] == "waiting_retry"
    # Cancelamento chegou quando o executor já tinha reagendado (sinal perdido): fica pending + cancel_requested
    harness.ctx.jobs.request_cancel(job["id"])
    harness.ctx.jobs.update(job["id"], next_attempt_at=utc_now(-1))
    harness.run_jobs()
    assert harness.ctx.jobs.get(job["id"])["status"] == "cancelled"
    assert harness.batch(batch["id"])["status"] == "completed"


def test_failure_with_pending_cancel_becomes_cancelled_not_retry(harness):
    harness.downloader.items = [remote("FC")]
    batch = harness.import_profile()
    harness.run_imports()
    job_id = harness.ctx.jobs.for_batch(batch["id"])[0]["id"]

    def failing_download(item, dest_dir, *, cancel=None, progress=None):
        harness.ctx.jobs.request_cancel(job_id)  # pedido chega durante a execução, antes da falha
        raise InstagramError("network_error")

    harness.downloader.download = failing_download
    worker = harness.ctx.worker
    claimed = harness.ctx.jobs.claim(owner=worker.owner, allow_downloads=True)
    from app.jobs.pipeline import JobExecutor

    JobExecutor(harness.ctx, worker, claimed, threading.Event()).run()
    assert harness.ctx.jobs.get(job_id)["status"] == "cancelled"


def test_stale_claimed_job_is_recovered_at_runtime(harness):
    harness.downloader.items = [remote("ST1"), remote("ST2")]
    batch = harness.import_profile()
    harness.run_imports()
    jobs = harness.ctx.jobs.for_batch(batch["id"])
    # ST1: lease de um executor que morreu há 20 min; ST2: lease recente (ainda vivo)
    harness.ctx.jobs.update(jobs[0]["id"], status="downloading", heartbeat_at=utc_now(-1200))
    harness.ctx.db.execute("UPDATE jobs SET claimed_by = 'morto' WHERE id = ?", (jobs[0]["id"],))
    harness.ctx.jobs.update(jobs[1]["id"], status="downloading", heartbeat_at=utc_now())
    harness.ctx.db.execute("UPDATE jobs SET claimed_by = 'vivo' WHERE id = ?", (jobs[1]["id"],))
    harness.ctx.worker.maintenance()
    stale, alive = harness.ctx.jobs.get(jobs[0]["id"]), harness.ctx.jobs.get(jobs[1]["id"])
    assert stale["status"] == "pending" and stale["error_code"] == "interrupted" and stale["attempts"] == 1
    assert harness.ctx.db.scalar("SELECT claimed_by FROM jobs WHERE id = ?", (jobs[0]["id"],)) is None
    assert alive["status"] == "downloading"
    assert harness.ctx.jobs.claim(allow_downloads=True)["id"] == jobs[0]["id"]


def test_rate_limit_pause_survives_restart(harness_factory):
    first = harness_factory()
    first.ctx.worker.pause_downloads(300)
    root = first.root
    first.__exit__(None, None, None)
    second = harness_factory(root=root)
    assert second.ctx.worker.downloads_paused()
    third_root = harness_factory()
    assert not third_root.ctx.worker.downloads_paused()


def test_retry_endpoints_report_conflict_when_nothing_requeued(harness, monkeypatch):
    harness.downloader.items = [remote("RQ")]
    harness.downloader.download_errors = {"RQ:0": [InstagramError("content_unavailable")]}
    batch = harness.import_profile()
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    monkeypatch.setattr(harness.ctx.jobs, "reset_for_retry", lambda ids: 0)
    response = harness.client.post(f"/api/jobs/{job['id']}/retry")
    assert response.status_code == 409 and response.json()["code"] == "job_not_retryable"
    response = harness.client.post(f"/api/batches/{batch['id']}/retry-failed")
    assert response.status_code == 409 and response.json()["code"] == "nothing_to_retry"
    assert harness.batch(batch["id"])["status"] == "completed"
