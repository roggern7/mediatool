from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from media_processor import MediaProcessor, ProcessingFailedError, sha256_file

from media_fakes import FakeInstagramDownloader, FakeProcessor, remote

ORIGINAL_RE = re.compile(r"^originals/\d{4}/\d{2}/([0-9a-f]{2})/([0-9a-f]{64})\.(jpg|mp4)$")
PROCESSED_RE = re.compile(r"^processed/\d{4}/\d{2}/(\d+)_(\d+)_([0-9a-f]{8})\.(jpg|mp4)$")


def test_ingest_promotes_original_and_thumbnail(harness):
    # Miniatura e metadados técnicos pertencem ao lote COM processamento (download puro não chama FFmpeg/ffprobe).
    harness.downloader.items = [remote("IMG1"), remote("VID1", media_type="video")]
    batch = harness.import_profile(process=True)
    harness.run_all()
    result = harness.batch(batch["id"])
    assert result["status"] == "completed"
    assert result["progress"] == {"found": 2, "downloaded": 2, "processed": 2, "skipped": 0, "failed": 0,
                                  "pending": 0, "active": 0}
    for job in result["jobs"]:
        assert job["status"] == "completed" and job["stage"] == "done" and job["progress"] == 1.0
        assert job["thumbnail_url"] == f"/api/media/{job['media_id']}/thumbnail"
        media = harness.ctx.media.get(job["media_id"])
        match = ORIGINAL_RE.match(media["original_path"])
        assert match and match.group(2) == media["original_sha256"] and match.group(1) == media["original_sha256"][:2]
        path = harness.ctx.storage.resolve(media["original_path"])
        assert sha256_file(path) == media["original_sha256"]
        assert media["thumbnail_path"] == f"thumbs/{media['id'] % 256}/{media['id']}.jpg"
        assert media["width"] == 64 and media["media_type"] == job["media_type"]
    assert not any(harness.ctx.storage.temp.iterdir())


def test_ingest_with_processing_creates_rendition(harness):
    harness.downloader.items = [remote("P1", media_type="video")]
    batch = harness.import_profile(process=True)
    harness.run_all()
    result = harness.batch(batch["id"])
    job = result["jobs"][0]
    assert job["status"] == "completed" and job["stage"] == "done" and job["rendition_id"]
    assert result["preset_name"] == "Padrão" and result["progress"]["processed"] == 1
    rendition = harness.ctx.media.get_rendition(job["rendition_id"])
    match = PROCESSED_RE.match(rendition["processed_path"])
    assert match and int(match.group(1)) == job["media_id"] and int(match.group(2)) == job["id"]
    assert rendition["processed_sha256"].startswith(match.group(3))
    media = harness.ctx.media.get(job["media_id"])
    assert rendition["processed_sha256"] != media["original_sha256"]
    assert rendition["params"]["speed"] == 1.05


def test_ffprobe_missing_registers_media_without_metadata(harness_factory):
    harness = harness_factory(processor=FakeProcessor(ffmpeg_found=False))
    harness.downloader.items = [remote("NOFF")]
    batch = harness.import_profile()
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    assert job["status"] == "completed"
    media = harness.ctx.media.get(job["media_id"])
    assert media["width"] is None and media["duration"] is None and media["thumbnail_path"] is None


def test_processing_without_ffmpeg_fails_with_clear_code(harness_factory):
    harness = harness_factory(processor=FakeProcessor(ffmpeg_found=False))
    harness.downloader.items = [remote("NOFF2")]
    batch = harness.import_profile(process=True)
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    assert job["status"] == "failed" and job["error_code"] == "ffmpeg_not_found"
    assert job["media_id"] is not None  # o original foi registrado mesmo assim
    assert "FFmpeg" in job["error_message"]


def test_processing_failure_is_not_retried_and_keeps_original(harness_factory):
    harness = harness_factory(processor=FakeProcessor(fail_process=ProcessingFailedError("saída inválida")))
    harness.downloader.items = [remote("PF")]
    batch = harness.import_profile(process=True)
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    assert job["status"] == "failed" and job["error_code"] == "processing_failed" and job["attempts"] == 1
    media = harness.ctx.media.get(job["media_id"])
    assert harness.ctx.storage.resolve(media["original_path"]).is_file()
    assert not list(harness.ctx.storage.processed.rglob("*.*"))


def test_invalid_download_fails_without_promoting(harness):
    harness.downloader.items = [remote("BAD")]
    harness.downloader.content = lambda item: b"<html>not media</html>"
    batch = harness.import_profile()
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    assert job["status"] == "failed" and job["error_code"] == "invalid_media"
    assert not list(harness.ctx.storage.originals.rglob("*.*"))


def test_thumbnail_failure_does_not_fail_job(harness, monkeypatch):
    def broken(*args, **kwargs):
        raise ProcessingFailedError("thumb quebrou")

    monkeypatch.setattr(harness.processor, "make_thumbnail", broken)
    harness.downloader.items = [remote("THUMB")]
    batch = harness.import_profile()
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    assert job["status"] == "completed" and job["thumbnail_url"] is None


def test_library_process_job_and_skip_when_rendition_exists(harness):
    harness.downloader.items = [remote("LIB")]
    batch = harness.import_profile()
    harness.run_all()
    media_id = harness.batch(batch["id"])["jobs"][0]["media_id"]
    preset_id = harness.client.get("/api/presets").json()[0]["id"]

    first = harness.client.post(f"/api/media/{media_id}/process", json={"preset_id": preset_id})
    assert first.status_code == 202 and first.json()["kind"] == "process" and first.json()["status"] == "pending"
    harness.run_jobs()
    done = harness.client.get(f"/api/jobs/{first.json()['id']}").json()
    assert done["status"] == "completed" and done["stage"] == "done" and done["rendition_id"]

    second = harness.client.post(f"/api/media/{media_id}/process", json={"preset_id": preset_id}).json()
    harness.run_jobs()
    skipped = harness.client.get(f"/api/jobs/{second['id']}").json()
    assert skipped["status"] == "completed" and skipped["stage"] == "skipped"
    assert skipped["rendition_id"] == done["rendition_id"]
    assert len(harness.ctx.media.renditions_for([media_id])[media_id]) == 1

    harness.client.put(f"/api/presets/{preset_id}", json={"params": {
        "metadata_cleanup": True, "trim_start": 0.0, "mirror": True, "speed": 1.0, "auto_adjust": True,
        "reencode": True}})
    third = harness.client.post(f"/api/media/{media_id}/process", json={"preset_id": preset_id}).json()
    harness.run_jobs()
    assert harness.client.get(f"/api/jobs/{third['id']}").json()["stage"] == "done"
    assert len(harness.ctx.media.renditions_for([media_id])[media_id]) == 2


def test_bulk_library_process_batch(harness):
    harness.downloader.items = [remote("B1"), remote("B2")]
    batch = harness.import_profile()
    harness.run_all()
    media_ids = [j["media_id"] for j in harness.batch(batch["id"])["jobs"]]
    preset_id = harness.client.get("/api/presets").json()[1]["id"]
    response = harness.client.post("/api/media/process", json={"media_ids": media_ids, "preset_id": preset_id})
    assert response.status_code == 202
    lib = response.json()
    assert lib["source"] == "library" and lib["status"] == "running" and lib["found"] == 2
    harness.run_jobs()
    final = harness.batch(lib["id"])
    assert final["status"] == "completed" and final["progress"]["processed"] == 2
    missing = harness.client.post("/api/media/process", json={"media_ids": [999], "preset_id": preset_id})
    assert missing.status_code == 404
    assert harness.client.post("/api/media/process", json={"media_ids": [], "preset_id": preset_id}).status_code == 422


# ---- FFmpeg real -----------------------------------------------------------------------------------------------

requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg/ffprobe não estão no PATH"
)


def _make_samples(directory: Path) -> tuple[Path, Path]:
    video = directory / "sample.mp4"
    image = directory / "sample.jpg"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         "-metadata", "title=origem", str(video)],
        check=True, timeout=120,
    )
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240",
         "-frames:v", "1", str(image)],
        check=True, timeout=60,
    )
    return video, image


@requires_ffmpeg
def test_real_ffmpeg_end_to_end(harness_factory, tmp_path):
    samples = tmp_path / "samples"
    samples.mkdir()
    video, image = _make_samples(samples)
    downloader = FakeInstagramDownloader(
        items=[remote("REALV", media_type="video"), remote("REALI", media_type="image")],
        source_files={"REALV:0": video, "REALI:0": image},
    )
    processor = MediaProcessor()
    harness = harness_factory(downloader=downloader, processor=processor)
    batch = harness.import_profile(process=True)
    harness.run_all()
    result = harness.batch(batch["id"])
    assert result["status"] == "completed", result
    assert result["progress"]["processed"] == 2 and result["progress"]["failed"] == 0

    sources = {"REALV": video, "REALI": image}
    for job in result["jobs"]:
        assert job["status"] == "completed" and job["stage"] == "done", job
        media = harness.ctx.media.get(job["media_id"])
        original = harness.ctx.storage.resolve(media["original_path"])
        # original intacto: hash igual ao do arquivo de origem antes e depois do processamento
        assert sha256_file(original) == sha256_file(sources[job["source_post_id"]]) == media["original_sha256"]
        rendition = harness.ctx.media.get_rendition(job["rendition_id"])
        processed = harness.ctx.storage.resolve(rendition["processed_path"])
        assert processed.is_file()
        assert sha256_file(processed) == rendition["processed_sha256"] != media["original_sha256"]
        probe = processor.probe(processed)
        assert probe.media_type == media["media_type"]
        assert rendition["width"] == probe.width and rendition["height"] == probe.height
        thumb = harness.ctx.storage.resolve(media["thumbnail_path"])
        assert thumb.read_bytes()[:2] == b"\xff\xd8"
        if media["media_type"] == "video":
            assert media["has_audio"] == 1 and media["duration"] == pytest.approx(2.0, abs=0.2)
            # trim 0.5s + speed 1.05 -> ~1.43s
            assert probe.duration == pytest.approx((2.0 - 0.5) / 1.05, abs=0.25)
            assert "title" not in {k.lower() for k in probe.tags}

    response = harness.client.get(f"/api/renditions/{result['jobs'][0]['rendition_id']}/file")
    assert response.status_code == 200 and len(response.content) > 0


def test_ffprobe_missing_rejects_unknown_signature(harness_factory):
    harness = harness_factory(processor=FakeProcessor(ffmpeg_found=False))
    harness.downloader.items = [remote("SIG")]
    harness.downloader.content = lambda item: b"<html>erro</html>"
    batch = harness.import_profile()
    harness.run_all()
    job = harness.batch(batch["id"])["jobs"][0]
    assert job["status"] == "failed" and job["error_code"] == "invalid_media" and job["attempts"] == 1
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM media") == 0


def test_probe_without_dimensions_is_invalid(harness, monkeypatch):
    original_probe = harness.processor.probe

    def zero_dims(path):
        result = original_probe(path)
        result.width = None
        return result

    monkeypatch.setattr(harness.processor, "probe", zero_dims)
    harness.downloader.items = [remote("ZERO")]
    batch = harness.import_profile(process=True)  # a validação por dimensões é do ingest com processamento
    harness.run_all()
    assert harness.batch(batch["id"])["jobs"][0]["error_code"] == "invalid_media"
    assert not list(harness.ctx.storage.originals.rglob("*.*"))


def test_existing_original_with_different_content_is_quarantined(harness):
    import hashlib
    from datetime import UTC, datetime

    content = b"\xff\xd8\xff\xe0FAKE-image-QUARENTENA"
    sha = hashlib.sha256(content).hexdigest()
    now = datetime.now(UTC)
    target = harness.ctx.storage.originals / f"{now:%Y}" / f"{now:%m}" / sha[:2] / f"{sha}.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"conteudo corrompido")
    harness.downloader.items = [remote("QUA")]
    harness.downloader.content = lambda item: content
    batch = harness.import_profile()
    harness.run_all()
    assert harness.batch(batch["id"])["jobs"][0]["status"] == "completed"
    assert sha256_file(target) == sha
    quarantined = [p for p in target.parent.iterdir() if ".quarantine-" in p.name]
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == b"conteudo corrompido"


def test_thumbnail_is_reused_without_errors(harness, caplog):
    import logging

    harness.downloader.items = [remote("TH1")]
    first = harness.import_profile(process=True)
    harness.run_all()
    media_id = harness.batch(first["id"])["jobs"][0]["media_id"]
    thumb = harness.ctx.storage.thumb_path(media_id)
    created = len(harness.processor.thumbnails)
    assert thumb.is_file() and created == 1

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="media_tool.jobs"):
        # reimportação (duplicado por origem), processamento da biblioteca e thumb sem registro no banco
        harness.ctx.media.update(media_id, thumbnail_path=None)
        second = harness.import_profile(process=True)
        harness.run_all()
        preset_id = harness.client.get("/api/presets").json()[1]["id"]
        harness.client.post(f"/api/media/{media_id}/process", json={"preset_id": preset_id})
        harness.run_jobs()
    assert not [r for r in caplog.records if "thumbnail" in r.getMessage().lower()]
    assert len(harness.processor.thumbnails) == created  # nada foi regerado
    assert harness.ctx.media.get(media_id)["thumbnail_path"] == f"thumbs/{media_id % 256}/{media_id}.jpg"
    assert harness.batch(second["id"])["jobs"][0]["status"] == "completed"


def test_gif_original_keeps_extension(harness):
    harness.downloader.items = [remote("GIF", media_type="video")]

    def gif_download(item, dest_dir, *, cancel=None, progress=None):
        target = dest_dir / "download.gif"
        dest_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"GIF89aFAKE-video-gif")
        return target

    harness.downloader.download = gif_download
    batch = harness.import_profile()
    harness.run_all()
    media = harness.ctx.media.get(harness.batch(batch["id"])["jobs"][0]["media_id"])
    assert media["original_path"].endswith(".gif") and media["mime_type"] == "image/gif"
