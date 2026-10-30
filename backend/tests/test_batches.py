from __future__ import annotations

import threading

from conftest import wait_until

from app.integrations.instagram.downloader import InstagramError
from media_fakes import FakeInstagramDownloader, remote


def test_partially_failed_batch_completes_with_correct_progress(harness):
    harness.downloader.items = [remote(f"P{i}") for i in range(5)]
    harness.downloader.download_errors = {"P2:0": [InstagramError("content_unavailable")]}
    batch = harness.import_profile()
    assert batch["status"] == "listing" and batch["found"] == 0
    harness.run_all()
    result = harness.batch(batch["id"])
    assert result["status"] == "completed" and result["finished_at"]
    assert result["found"] == 5
    assert result["progress"] == {"found": 5, "downloaded": 4, "processed": 0, "skipped": 0, "failed": 1,
                                  "pending": 0, "active": 0, "duplicates": 0, "filtered": 0}
    assert result["counts"]["completed"] == 4 and result["counts"]["failed"] == 1
    statuses = sorted(j["status"] for j in result["jobs"])
    assert statuses == ["completed"] * 4 + ["failed"]


def test_retry_failed_jobs_of_batch(harness):
    harness.downloader.items = [remote("R1"), remote("R2")]
    harness.downloader.download_errors = {"R1:0": [InstagramError("content_unavailable")]}
    batch = harness.import_profile()
    harness.run_all()
    assert harness.batch(batch["id"])["progress"]["failed"] == 1

    response = harness.client.post(f"/api/batches/{batch['id']}/retry-failed")
    assert response.status_code == 200
    assert response.json()["status"] == "running" and response.json()["progress"]["pending"] == 1
    harness.run_jobs()
    final = harness.batch(batch["id"])
    assert final["status"] == "completed" and final["progress"]["failed"] == 0 and final["progress"]["downloaded"] == 2
    again = harness.client.post(f"/api/batches/{batch['id']}/retry-failed")
    assert again.status_code == 409 and again.json()["code"] == "nothing_to_retry"


def test_cancel_batch_cancels_pending_and_running(harness_factory):
    gate = threading.Event()
    downloader = FakeInstagramDownloader(items=[remote(f"C{i}") for i in range(3)], block=gate)
    harness = harness_factory(downloader=downloader, start_worker=True, download_concurrency=1)
    batch = harness.import_profile()
    assert wait_until(lambda: harness.batch(batch["id"])["status"] == "running"
                      and harness.ctx.worker.active_count == 1)
    response = harness.client.post(f"/api/batches/{batch['id']}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert wait_until(lambda: harness.batch(batch["id"])["counts"]["cancelled"] == 3)
    final = harness.batch(batch["id"])
    assert final["status"] == "cancelled" and final["progress"]["active"] == 0
    assert harness.client.post(f"/api/batches/{batch['id']}/cancel").status_code == 409


def test_cancel_during_listing(harness_factory):
    listing_started = threading.Event()
    release = threading.Event()

    class SlowListing(FakeInstagramDownloader):
        def iter_profile_media(self, username, content, limit, *, cancel=None, **kwargs):
            yield remote("L0")
            listing_started.set()
            release.wait(5)
            if cancel is not None and cancel.is_set():
                return
            yield remote("L1")

    harness = harness_factory(downloader=SlowListing())
    batch = harness.import_profile()
    runner = threading.Thread(target=harness.run_imports)
    runner.start()
    assert listing_started.wait(5)
    assert harness.client.post(f"/api/batches/{batch['id']}/cancel").json()["status"] == "cancelled"
    release.set()
    runner.join(5)
    final = harness.batch(batch["id"])
    assert final["status"] == "cancelled"
    assert all(j["status"] == "cancelled" for j in final["jobs"])
    assert harness.run_jobs() == 0


def test_list_batches_pagination_and_validation(harness):
    harness.downloader.items = [remote("X")]
    for i in range(3):
        harness.import_profile(profile=f"perfil_{i}")  # um perfil com listagem aberta por vez (409)
    page = harness.client.get("/api/batches", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    assert page["items"][0]["id"] > page["items"][1]["id"]
    assert "jobs" not in page["items"][0] or page["items"][0]["jobs"] is None
    assert harness.client.get("/api/batches", params={"limit": 0}).status_code == 422
    assert harness.client.get("/api/batches", params={"limit": 201}).status_code == 422
    assert harness.client.get("/api/batches", params={"offset": -1}).status_code == 422
    assert harness.client.get("/api/batches/999").status_code == 404


def test_list_batches_filtered_by_source(harness):
    harness.downloader.items = [remote("SRC")]
    imported = harness.import_profile()
    harness.run_all()
    media_id = harness.client.get("/api/media").json()["items"][0]["id"]
    preset_id = harness.client.get("/api/presets").json()[0]["id"]
    library = harness.client.post("/api/media/process", json={"media_ids": [media_id], "preset_id": preset_id}).json()

    only_ig = harness.client.get("/api/batches", params={"source": "instagram_import"}).json()
    assert only_ig["total"] == 1 and [b["id"] for b in only_ig["items"]] == [imported["id"]]
    only_lib = harness.client.get("/api/batches", params={"source": "library"}).json()
    assert only_lib["total"] == 1 and [b["id"] for b in only_lib["items"]] == [library["id"]]
    assert harness.client.get("/api/batches").json()["total"] == 2
    invalid = harness.client.get("/api/batches", params={"source": "outro"})
    assert invalid.status_code == 422 and invalid.json()["code"] == "validation_error"


def test_process_jobs_are_never_marked_duplicate(harness):
    harness.downloader.items = [remote("PD")]
    harness.import_profile()
    harness.run_all()
    media_id = harness.client.get("/api/media").json()["items"][0]["id"]
    preset_id = harness.client.get("/api/presets").json()[0]["id"]
    single = harness.client.post(f"/api/media/{media_id}/process", json={"preset_id": preset_id}).json()
    again = harness.client.post(f"/api/media/{media_id}/process", json={"preset_id": preset_id}).json()
    bulk = harness.client.post("/api/media/process", json={"media_ids": [media_id], "preset_id": preset_id}).json()
    harness.run_jobs()
    jobs = harness.ctx.db.fetchall("SELECT * FROM jobs WHERE kind = 'process'")
    assert len(jobs) == 3 and {j["status"] for j in jobs} == {"completed"}
    assert all(j["is_duplicate"] == 0 and j["duplicate_reason"] is None for j in jobs)
    assert harness.client.get(f"/api/jobs/{again['id']}").json()["stage"] == "skipped"
    assert harness.client.get(f"/api/jobs/{single['id']}").json()["is_duplicate"] is False
    assert harness.batch(bulk["id"])["jobs"][0]["is_duplicate"] is False


def test_found_is_never_below_job_count_during_listing(harness):
    harness.downloader.items = [remote("FD1"), remote("FD2")]
    first = harness.import_profile()
    harness.run_all()
    batch = harness.import_profile()
    # Listagem em andamento: jobs (duplicados -> skipped) já criados, mas batches.found ainda não sincronizado
    for code in ("FD1", "FD2"):
        harness.ctx.jobs.create_ingest(
            batch_id=batch["id"], source={"source_platform": "instagram", "source_post_id": code,
                                          "source_media_index": 0, "media_type": "image"},
            preset_id=None, preset_name=None, params=None, is_duplicate=True, duplicate_reason="source_id",
            completed_skipped=True)
    harness.ctx.batches.update(batch["id"], found=1)
    result = harness.batch(batch["id"])
    assert result["status"] == "listing"
    assert result["found"] == 2 and result["progress"]["found"] == 2 and result["progress"]["skipped"] == 2
    listed = harness.client.get("/api/batches").json()["items"]
    assert next(b for b in listed if b["id"] == batch["id"])["progress"]["found"] == 2
    assert harness.batch(first["id"])["progress"]["found"] == 2
