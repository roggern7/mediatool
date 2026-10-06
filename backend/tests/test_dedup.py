from __future__ import annotations

from media_fakes import remote


def test_same_item_listed_twice_creates_one_job(harness):
    harness.downloader.items = [remote("DUP"), remote("DUP"), remote("DUP", index=1)]
    batch = harness.import_profile()
    harness.run_all()
    result = harness.batch(batch["id"])
    assert result["found"] == 2
    assert sorted((j["source_post_id"], j["source_media_index"]) for j in result["jobs"]) == [("DUP", 0), ("DUP", 1)]


def test_source_id_dedup_across_imports_skips_download(harness):
    harness.downloader.items = [remote("A1"), remote("A2")]
    first = harness.import_profile()
    harness.run_all()
    downloads_before = list(harness.downloader.downloads)

    second = harness.import_profile()
    harness.run_imports()
    result = harness.batch(second["id"])
    # Duplicatas por origem sem processamento já nascem concluídas como "skipped", sem download
    assert result["status"] == "completed"
    assert result["progress"]["skipped"] == 2 and result["progress"]["downloaded"] == 0
    for job in result["jobs"]:
        assert job["is_duplicate"] is True and job["duplicate_reason"] == "source_id"
        assert job["status"] == "completed" and job["stage"] == "skipped"
    first_media = {j["source_post_id"]: j["media_id"] for j in harness.batch(first["id"])["jobs"]}
    assert {j["source_post_id"]: j["media_id"] for j in result["jobs"]} == first_media
    assert harness.run_jobs() == 0
    assert harness.downloader.downloads == downloads_before
    assert harness.client.get("/api/media").json()["total"] == 2


def test_source_duplicate_with_processing_processes_existing_original(harness):
    harness.downloader.items = [remote("PR")]
    harness.import_profile()
    harness.run_all()
    downloads_before = len(harness.downloader.downloads)

    second = harness.import_profile(process=True)
    harness.run_all()
    job = harness.batch(second["id"])["jobs"][0]
    assert job["is_duplicate"] is True and job["duplicate_reason"] == "source_id"
    assert job["status"] == "completed" and job["stage"] == "done" and job["rendition_id"]
    assert len(harness.downloader.downloads) == downloads_before

    third = harness.import_profile(process=True)
    harness.run_all()
    job3 = harness.batch(third["id"])["jobs"][0]
    assert job3["status"] == "completed" and job3["stage"] == "skipped"
    assert harness.batch(third["id"])["progress"]["skipped"] == 1


def test_sha256_dedup_links_existing_media_and_discards_temp(harness):
    harness.downloader.items = [remote("S1"), remote("S2")]
    # Download puro valida pela assinatura do arquivo (sem ffprobe): o conteúdo FAKE começa como um JPEG.
    harness.downloader.content = lambda item: b"\xff\xd8\xff\xe0FAKE-image-mesmo-conteudo"
    batch = harness.import_profile()
    harness.run_all()
    jobs = sorted(harness.batch(batch["id"])["jobs"], key=lambda j: j["id"])
    assert jobs[0]["is_duplicate"] is False
    assert jobs[1]["is_duplicate"] is True and jobs[1]["duplicate_reason"] == "sha256"
    assert jobs[1]["media_id"] == jobs[0]["media_id"]
    assert jobs[1]["status"] == "completed" and jobs[1]["stage"] == "skipped"
    assert harness.client.get("/api/media").json()["total"] == 1
    assert len(list(harness.ctx.storage.originals.rglob("*.jpg"))) == 1
    assert not any(harness.ctx.storage.temp.iterdir())
    progress = harness.batch(batch["id"])["progress"]
    assert progress["downloaded"] == 1 and progress["skipped"] == 1
