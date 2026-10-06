from __future__ import annotations

from app.db.connection import utc_now
from media_fakes import FakeInstagramDownloader, remote


def _stop(harness):
    harness.__exit__(None, None, None)


def test_restart_recovers_interrupted_jobs(harness_factory):
    first = harness_factory()
    first.downloader.items = [remote(f"R{i}") for i in range(4)]
    batch = first.import_profile(process=True)
    first.run_imports()
    ctx = first.ctx
    jobs = ctx.jobs.for_batch(batch["id"])

    # Simula estados deixados por um processo que morreu no meio do trabalho
    ctx.jobs.update(jobs[0]["id"], status="downloading", stage="downloading", progress=0.4,
                    temp_path=f"temp/job-{jobs[0]['id']}")
    first.run_jobs()  # executa todos os pendentes (1..3); depois forçamos estados intermediários
    ctx.jobs.update(jobs[1]["id"], status="processing", stage="processing", progress=0.7, rendition_id=None)
    ctx.db.execute("DELETE FROM renditions WHERE job_id = ?", (jobs[1]["id"],))
    ctx.jobs.update(jobs[2]["id"], status="downloading", stage="downloading", attempts=2)
    ctx.batches.update(batch["id"], status="running", finished_at=None)
    ctx.storage.job_temp_dir(jobs[0]["id"])
    (ctx.storage.job_temp_dir(jobs[0]["id"]) / "parcial.mp4").write_bytes(b"parcial")
    (ctx.storage.temp / "lixo.tmp").write_bytes(b"x")
    outside = ctx.storage.root / "fora-do-temp.txt"
    outside.write_text("manter")
    root = first.root
    _stop(first)

    second = harness_factory(root=root, downloader=FakeInstagramDownloader(items=[remote(f"R{i}") for i in range(4)]))
    recovered = {j["id"]: j for j in second.ctx.jobs.for_batch(batch["id"])}
    j0, j1, j2 = recovered[jobs[0]["id"]], recovered[jobs[1]["id"]], recovered[jobs[2]["id"]]
    # Seção 9, M7: reinício abrupto não consome tentativa (nem esgota as tentativas de j2).
    assert j0["status"] == "pending" and j0["attempts"] == 0 and j0["error_code"] == "interrupted"
    assert j0["progress"] is None and j0["temp_path"] is None
    assert j1["status"] == "downloaded" and j1["attempts"] == 0 and j1["error_code"] == "interrupted"
    assert j2["status"] == "pending" and j2["attempts"] == 2 and j2["error_code"] == "interrupted"
    assert all(j["status"] != "completed" or j["id"] == jobs[3]["id"] for j in recovered.values())
    assert not any(second.ctx.storage.temp.iterdir())
    assert outside.read_text() == "manter"

    second.run_jobs()
    final = second.batch(batch["id"])
    by_id = {j["id"]: j for j in final["jobs"]}
    assert by_id[jobs[0]["id"]]["status"] == "completed" and by_id[jobs[0]["id"]]["rendition_id"]
    assert by_id[jobs[1]["id"]]["status"] == "completed" and by_id[jobs[1]["id"]]["rendition_id"]
    assert final["status"] == "completed"
    for job in final["jobs"]:
        if job["status"] == "completed":
            media = second.ctx.media.get(job["media_id"])
            assert second.ctx.storage.resolve(media["original_path"]).is_file()


def test_completed_is_never_reported_without_file(harness):
    harness.downloader.items = [remote("MISS")]
    batch = harness.import_profile()
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    media = harness.ctx.media.get(job["media_id"])
    harness.ctx.storage.resolve(media["original_path"]).unlink()
    # Outro lote encontra a mídia pela origem, mas o arquivo sumiu: falha controlada, não "completed" falso
    harness.ctx.jobs.update(job["id"], status="downloaded")
    harness.run_jobs()
    assert harness.client.get(f"/api/jobs/{job['id']}").json()["error_code"] == "original_missing"


def test_restart_resumes_listing_without_duplicating_jobs(harness_factory):
    first = harness_factory()
    first.downloader.items = [remote("L0"), remote("L1"), remote("L2")]
    batch = first.import_profile()
    # Simula listagem interrompida: só o primeiro item foi gravado e o lote continua em "listing"
    first.ctx.jobs.create_ingest(batch_id=batch["id"], source={
        "source_platform": "instagram", "source_profile": "perfil_teste", "source_post_id": "L0",
        "source_media_index": 0, "media_type": "image", "source_url": "https://www.instagram.com/p/L0/",
        "media_url": "https://scontent.cdninstagram.com/v/L0_0.jpg"}, preset_id=None, preset_name=None, params=None)
    first.ctx.batches.sync_found(batch["id"])
    root = first.root
    _stop(first)

    second = harness_factory(root=root, downloader=FakeInstagramDownloader(
        items=[remote("L0"), remote("L1"), remote("L2")]))
    assert second.batch(batch["id"])["status"] == "listing"
    second.run_all()
    final = second.batch(batch["id"])
    assert final["status"] == "completed" and final["found"] == 3
    assert sorted(j["source_post_id"] for j in final["jobs"]) == ["L0", "L1", "L2"]


def test_pending_cancel_request_is_finalized_on_restart(harness_factory):
    first = harness_factory()
    first.downloader.items = [remote("CX")]
    batch = first.import_profile()
    first.run_imports()
    job = first.ctx.jobs.for_batch(batch["id"])[0]
    first.ctx.jobs.update(job["id"], status="processing", cancel_requested=1, heartbeat_at=utc_now())
    root = first.root
    _stop(first)
    second = harness_factory(root=root)
    assert second.ctx.jobs.get(job["id"])["status"] == "cancelled"


def test_restart_removes_orphan_processed_and_keeps_referenced(harness_factory):
    first = harness_factory()
    first.downloader.items = [remote("OP", media_type="video")]
    batch = first.import_profile(process=True)
    first.run_all()
    job = first.batch(batch["id"])["jobs"][0]
    referenced = first.ctx.storage.resolve(first.ctx.media.get_rendition(job["rendition_id"])["processed_path"])
    orphan = referenced.with_name(f"{job['media_id']}_999_deadbeef.mp4")
    orphan.write_bytes(b"orfao")
    foreign = referenced.with_name("meu-video-editado.mp4")
    foreign.write_bytes(b"do usuario")
    root = first.root
    first.__exit__(None, None, None)
    harness_factory(root=root)
    assert referenced.is_file() and foreign.is_file()
    assert not orphan.exists()


def test_rendition_with_missing_file_is_replaced_not_duplicated(harness):
    harness.downloader.items = [remote("RM")]
    batch = harness.import_profile(process=True)
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    old = harness.ctx.media.get_rendition(job["rendition_id"])
    harness.ctx.storage.resolve(old["processed_path"]).unlink()
    preset_id = harness.batch(batch["id"])["preset_id"]
    reprocess = harness.client.post(f"/api/media/{job['media_id']}/process", json={"preset_id": preset_id}).json()
    harness.run_jobs()
    done = harness.client.get(f"/api/jobs/{reprocess['id']}").json()
    assert done["status"] == "completed" and done["stage"] == "done"
    renditions = harness.ctx.media.renditions_for([job["media_id"]])[job["media_id"]]
    assert [r["id"] for r in renditions] == [done["rendition_id"]]
    assert harness.ctx.storage.resolve(renditions[0]["processed_path"]).is_file()
