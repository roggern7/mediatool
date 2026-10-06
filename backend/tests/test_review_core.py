"""Revisão cruzada do núcleo (banco, fila, worker, recovery, dedup, batch, retry).

Bugs confirmados na leitura/execução viram `xfail(strict=True)`; os demais testes cobrem lacunas da suíte original.
"""

from __future__ import annotations

import shutil
import threading
import time
from datetime import UTC, datetime

import pytest
from media_processor import MediaProcessor, sha256_file

from conftest import wait_until
from media_fakes import FakeInstagramDownloader, FakeProcessor, remote


# ---- Bugs corrigidos (antes xfail) --------------------------------------------------------------------------------------------
def test_running_job_is_never_claimable_by_another_executor(harness):
    harness.downloader.items = [remote("RACE1")]
    harness.import_profile(process=True)
    harness.run_imports()
    ctx = harness.ctx
    stolen: list[dict | None] = []
    original = ctx.media.find_rendition

    def spy(*args, **kwargs):
        stolen.append(ctx.jobs.claim(allow_downloads=True))  # o que o despachante faria nesse instante
        return original(*args, **kwargs)

    ctx.media.find_rendition = spy
    harness.run_jobs()
    assert stolen == [None], "job em execução foi reivindicado por outro executor"


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe ausente")
def test_html_error_page_as_jpg_is_rejected_as_invalid_media(harness_factory):
    harness = harness_factory(processor=MediaProcessor())
    harness.downloader.items = [remote("HTML1")]
    harness.downloader.content = lambda item: b"<html><body>link expirado</body></html>"
    batch = harness.import_profile()
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    assert job["status"] == "failed" and job["error_code"] == "invalid_media"
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM media") == 0
    assert not list((harness.ctx.storage.originals).rglob("*.jpg"))


def test_orphan_original_from_crash_does_not_block_reingest(harness):
    content = b"\xff\xd8\xff\xe0FAKE-image-ORPHAN-CONTENT"  # assinatura JPEG: download puro valida por magic bytes
    import hashlib

    sha = hashlib.sha256(content).hexdigest()
    now = datetime.now(UTC)
    orphan = harness.ctx.storage.originals / f"{now:%Y}" / f"{now:%m}" / sha[:2] / f"{sha}.jpg"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(content)
    harness.downloader.items = [remote("ORPH")]
    harness.downloader.content = lambda item: content
    batch = harness.import_profile()
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    assert job["status"] == "completed", (job["status"], job["error_code"])
    assert sha256_file(orphan) == sha


def test_concurrent_process_jobs_same_preset_create_single_rendition(harness_factory):
    class SlowProcessor(FakeProcessor):
        def process(self, *args, **kwargs):
            time.sleep(0.4)
            return super().process(*args, **kwargs)

    harness = harness_factory(processor=SlowProcessor(), start_worker=True, concurrency=2)
    harness.downloader.items = [remote("CONC1")]
    batch = harness.import_profile()
    assert wait_until(lambda: harness.batch(batch["id"])["status"] == "completed")
    media_id = harness.ctx.db.scalar("SELECT id FROM media")
    preset_id = harness.ctx.presets.first_builtin_id()
    harness.client.post(f"/api/media/{media_id}/process", json={"preset_id": preset_id})
    harness.client.post(f"/api/media/{media_id}/process", json={"preset_id": preset_id})
    assert wait_until(lambda: harness.ctx.db.scalar(
        "SELECT COUNT(*) FROM jobs WHERE kind = 'process' AND status IN ('completed','failed')") == 2)
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM renditions WHERE media_id = ?", (media_id,)) == 1


# ---- Lacunas cobertas (passam) -------------------------------------------------------------------------------------
def test_cancel_job_in_downloaded_state_and_retry_completed_is_refused(harness):
    harness.downloader.items = [remote("C1"), remote("C2")]
    batch = harness.import_profile(process=True)
    harness.run_imports()
    first, second = harness.ctx.jobs.for_batch(batch["id"])
    harness.run_jobs()
    assert harness.ctx.jobs.get(first["id"])["status"] == "completed"
    refused = harness.client.post(f"/api/jobs/{first['id']}/retry")
    assert refused.status_code == 409 and refused.json()["code"] == "job_not_retryable"
    assert harness.ctx.jobs.get(first["id"])["status"] == "completed"

    # job 'downloaded' (aguardando processamento) é cancelável direto
    harness.ctx.jobs.update(second["id"], status="downloaded", stage="queued", rendition_id=None)
    assert harness.client.post(f"/api/jobs/{second['id']}/cancel").status_code == 200
    job = harness.ctx.jobs.get(second["id"])
    assert job["status"] == "cancelled" and job["finished_at"]
    assert harness.batch(batch["id"])["status"] in ("completed", "running")
    harness.ctx.batches.refresh_status(batch["id"])
    assert harness.batch(batch["id"])["status"] == "completed"


def test_empty_listing_completes_batch(harness):
    batch = harness.import_profile()
    harness.run_imports()
    result = harness.batch(batch["id"])
    assert result["status"] == "completed" and result["found"] == 0
    assert result["progress"] == {"found": 0, "downloaded": 0, "processed": 0, "skipped": 0, "failed": 0,
                                  "pending": 0, "active": 0}


def test_batch_with_only_source_duplicates_completes(harness):
    harness.downloader.items = [remote("D1"), remote("D2")]
    first = harness.import_profile(process=True)
    harness.run_all()
    second = harness.import_profile(process=True)
    harness.run_imports()
    result = harness.batch(second["id"])
    assert result["status"] == "completed"  # jobs já nascem completed/skipped
    assert result["progress"]["skipped"] == 2 and result["progress"]["downloaded"] == 0
    assert harness.downloader.downloads == ["D1:0", "D2:0"]
    assert harness.batch(first["id"])["progress"]["processed"] == 2


def test_cancelled_batch_with_all_jobs_cancelled_is_terminal_and_retry_reopens(harness):
    harness.downloader.items = [remote("X1"), remote("X2")]
    batch = harness.import_profile()
    harness.run_imports()
    assert harness.client.post(f"/api/batches/{batch['id']}/cancel").status_code == 200
    jobs = harness.ctx.jobs.for_batch(batch["id"])
    assert [j["status"] for j in jobs] == ["cancelled", "cancelled"]
    assert harness.batch(batch["id"])["status"] == "cancelled"
    assert harness.run_jobs() == 0
    # retry-failed não pega cancelados
    assert harness.client.post(f"/api/batches/{batch['id']}/retry-failed").status_code == 409


def test_concurrent_same_item_in_two_batches_dedups_by_sha(harness_factory):
    gate = threading.Event()
    harness = harness_factory(start_worker=True, concurrency=2)
    harness.downloader.block = gate
    harness.downloader.items = [remote("SAME")]
    first = harness.import_profile()
    assert wait_until(lambda: harness.ctx.worker.active_count == 1, timeout=10)
    second = harness.import_profile(profile="outro_perfil")  # mesmo perfil com listagem aberta daria 409
    # Seção 9 (re-revisão): o item já em download pelo outro lote não é baixado de novo; o job do 2o lote aguarda o
    # primário (pending, sem ser reivindicado) e liga à mídia quando ele conclui.
    assert wait_until(lambda: harness.ctx.jobs.count_for_batch(second["id"]) == 1, timeout=10)
    time.sleep(0.3)  # vários ciclos do worker: o dependente não é reivindicado enquanto o primário baixa
    assert harness.ctx.worker.active_count == 1
    assert harness.batch(second["id"])["status"] == "running"
    gate.set()
    assert wait_until(lambda: harness.batch(first["id"])["status"] == "completed")
    assert wait_until(lambda: harness.batch(second["id"])["status"] == "completed")
    jobs = harness.ctx.db.fetchall("SELECT * FROM jobs ORDER BY id")
    assert [j["status"] for j in jobs] == ["completed", "completed"]
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM media") == 1
    assert [j["duplicate_reason"] for j in jobs] == [None, "duplicate_in_flight"]
    assert len(harness.downloader.downloads) == 1
    originals = [p for p in harness.ctx.storage.originals.rglob("*") if p.is_file()]
    assert len(originals) == 1
    assert not any(harness.ctx.storage.temp.iterdir())


def test_recovery_keeps_listing_batch_and_does_not_duplicate_jobs(harness_factory):
    first = harness_factory()
    first.downloader.items = [remote(f"LS{i}") for i in range(3)]
    batch = first.import_profile()
    # listagem parcial: 1 job criado, lote ainda em listing (processo morreu no meio)
    first.ctx.jobs.create_ingest(batch_id=batch["id"], source={"source_platform": "instagram",
                                 "source_post_id": "LS0", "source_media_index": 0, "media_type": "image"},
                                 preset_id=None, preset_name=None, params=None)
    root = first.root
    first.__exit__(None, None, None)
    second = harness_factory(root=root, downloader=FakeInstagramDownloader(
        items=[remote(f"LS{i}") for i in range(3)]))
    assert second.batch(batch["id"])["status"] == "listing"
    second.run_all()
    result = second.batch(batch["id"])
    assert result["status"] == "completed" and result["found"] == 3 and len(result["jobs"]) == 3


def test_retry_failed_resets_only_failed_jobs_and_clears_attempts(harness):
    from app.integrations.instagram.downloader import InstagramError

    harness.downloader.items = [remote("OK1"), remote("BAD")]
    harness.downloader.download_errors = {"BAD:0": [InstagramError("content_unavailable", retryable=False)]}
    batch = harness.import_profile()
    harness.run_all()
    ok_before = harness.ctx.jobs.get(1)
    assert harness.batch(batch["id"])["progress"]["failed"] == 1
    assert harness.client.post(f"/api/batches/{batch['id']}/retry-failed").status_code == 200
    bad = harness.ctx.jobs.get(2)
    assert bad["status"] == "pending" and bad["attempts"] == 0 and bad["error_code"] is None
    assert harness.ctx.jobs.get(1) == ok_before
    harness.run_jobs()
    assert harness.batch(batch["id"])["status"] == "completed"
    assert harness.batch(batch["id"])["progress"]["failed"] == 0
