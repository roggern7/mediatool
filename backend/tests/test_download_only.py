"""Download-only é o fluxo padrão: o lote termina em `completed` com o original salvo, validado (SHA-256) e
registrado, sem job de processamento, sem rendition e sem depender do pool, da fila ou do binário do FFmpeg.
O processamento continua disponível como ação separada (process-by-filter / Biblioteca)."""

from __future__ import annotations

import hashlib
import logging

import pytest
from media_processor import InvalidMediaError, sha256_file

from app.services import disk
from media_fakes import FakeInstagramDownloader, FakeProcessor, remote

LIMITS = {"max_process_backlog": 10, "resume_process_backlog": 4, "max_process_backlog_global": 10,
          "resume_process_backlog_global": 4}


def _preset(h) -> int:
    return h.client.get("/api/presets").json()[0]["id"]


def _download_pool(h) -> int:
    """Executa só o pool de download até não haver claim (o pool de FFmpeg nunca anda)."""
    count = 0
    while (claimed := h.ctx.worker._claim("download")) is not None:
        h.ctx.worker._execute(*claimed)
        count += 1
    return count


def _ffmpeg_backlog(h, batch_id: int) -> int:
    return int(h.ctx.db.scalar(
        "SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND kind = 'ingest' AND params IS NOT NULL"
        " AND status IN ('downloading', 'downloaded', 'processing')", (batch_id,)))


def _record_statuses(h, monkeypatch) -> list[str]:
    """Todo status gravado pelo pipeline (update/finish) durante a execução dos jobs."""
    seen: list[str] = []
    for name in ("update", "finish"):
        original = getattr(h.ctx.jobs, name)

        def wrapper(job_id, _original=original, **fields):
            if fields.get("status"):
                seen.append(fields["status"])
            return _original(job_id, **fields)

        monkeypatch.setattr(h.ctx.jobs, name, wrapper)
    return seen


# ---- contrato da API ---------------------------------------------------------------------------------------------
def test_import_defaults_to_download_only(harness):
    h = harness
    h.downloader.items = [remote("API0")]
    response = h.client.post("/api/imports", json={"profile": "perfil_teste", "content_type": "all", "limit": 10})
    assert response.status_code == 202, response.text
    batch = response.json()
    assert batch["process"] is False and batch["preset_id"] is None and batch["preset_name"] is None
    row = h.ctx.db.fetchone("SELECT process, params, preset_id FROM batches WHERE id = ?", (batch["id"],))
    assert row["process"] == 0 and row["params"] is None and row["preset_id"] is None
    h.run_all()
    assert h.batch(batch["id"])["status"] == "completed"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'process' OR params IS NOT NULL") == 0


def test_import_with_process_true_still_requires_and_snapshots_a_preset(harness):
    h = harness
    base = {"profile": "perfil_teste", "content_type": "all", "limit": 10}
    missing = h.client.post("/api/imports", json={**base, "process": True, "preset_id": 999})
    assert missing.status_code == 404 and missing.json()["code"] == "preset_not_found"
    assert h.client.post("/api/imports", json={**base, "process": "sim"}).status_code == 422
    batch = h.client.post("/api/imports", json={**base, "process": True}).json()
    assert batch["process"] is True and batch["preset_id"] is not None and batch["preset_name"]
    assert h.ctx.db.scalar("SELECT params FROM batches WHERE id = ?", (batch["id"],)) is not None


# ---- caminho do job ----------------------------------------------------------------------------------------------
def test_download_only_completes_right_after_original_is_saved_hashed_and_registered(harness_factory, monkeypatch):
    dl = FakeInstagramDownloader(items=[remote(f"D{i:03d}", media_type="video" if i % 2 else "image")
                                        for i in range(30)])
    proc = FakeProcessor()
    h = harness_factory(downloader=dl, processor=proc)
    batch = h.import_profile(limit=50)["id"]
    h.run_imports()
    seen = _record_statuses(h, monkeypatch)
    assert _download_pool(h) == 30, "o pool de download conclui o lote sozinho"
    assert h.ctx.worker._claim("process") is None
    result = h.batch(batch)
    assert result["status"] == "completed" and result["process"] is False
    assert result["progress"] == {"found": 30, "downloaded": 30, "processed": 0, "skipped": 0, "failed": 0,
                                  "pending": 0, "active": 0}
    # Nenhum estado intermediário de espera do FFmpeg: o job vai de `downloading` direto a `completed`.
    assert set(seen) == {"downloading", "completed"}, sorted(set(seen))
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'process'") == 0
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE params IS NOT NULL OR rendition_id IS NOT NULL") == 0
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == 0
    assert proc.processed == [], "nenhuma chamada ao FFmpeg de processamento"
    for job in result["jobs"]:
        assert job["status"] == "completed" and job["stage"] == "done" and job["rendition_id"] is None
        media = h.ctx.media.get(job["media_id"])
        assert sha256_file(h.ctx.storage.resolve(media["original_path"])) == media["original_sha256"]


def test_interrupted_download_only_job_requeues_as_pending_not_waiting_for_ffmpeg(harness_factory):
    class StoppingProcessor(FakeProcessor):
        """Simula o encerramento do app (worker.stop) logo depois de o original ser registrado."""

        worker = None
        fired = False

        def stop(self):
            if not self.fired:
                self.fired = True
                self.worker._stop.set()
                with self.worker._lock:
                    for event in self.worker._active.values():
                        event.set()

    dl = FakeInstagramDownloader(items=[remote("STOP0")])
    proc = StoppingProcessor()
    h = harness_factory(downloader=dl, processor=proc)
    worker = proc.worker = h.ctx.worker
    # Download puro não chama o processador: o gancho é o registro do original no banco.
    original_insert = h.ctx.media.insert

    def insert_then_stop(row):
        media_id = original_insert(row)
        proc.stop()
        return media_id

    h.ctx.media.insert = insert_then_stop
    batch = h.import_profile(limit=10)["id"]
    h.run_imports()
    worker._execute(*worker._claim("download"))
    job = h.ctx.jobs.for_batch(batch)[0]
    assert job["status"] == "pending" and job["media_id"] is not None and job["attempts"] == 0
    worker._stop.clear()
    assert worker._claim("process") is None, "download-only não entra na fila do FFmpeg"
    assert _download_pool(h) == 1
    assert h.batch(batch)["status"] == "completed" and len(dl.downloads) == 1, "conclui sem baixar de novo"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 1


# ---- backpressure independente do FFmpeg -------------------------------------------------------------------------
def test_full_global_ffmpeg_backlog_does_not_hold_download_only_listing_or_claims(harness_factory):
    dl = FakeInstagramDownloader(items=[remote(f"A{i:03d}", profile="perfil_a") for i in range(30)])
    proc = FakeProcessor()
    h = harness_factory(downloader=dl, processor=proc, **LIMITS)
    h.ctx.worker.disk_check = lambda: (True, 1 << 40, 0)
    with_process = h.import_profile(profile="perfil_a", limit=50, process=True, preset_id=_preset(h))["id"]
    h.run_imports()
    assert _download_pool(h) == 10 and _ffmpeg_backlog(h, with_process) == 10
    assert h.ctx.worker.process_backlog_state() == ({with_process}, True), "teto do lote e teto global atingidos"

    dl.items = [remote(f"B{i:03d}", profile="perfil_b") for i in range(150)]
    plain = h.import_profile(profile="perfil_b", limit=250)["id"]
    max_pending = 0
    for _ in range(100):
        h.run_imports()
        max_pending = max(max_pending, h.ctx.jobs.pending_ingest_count(plain))
        _download_pool(h)
        if h.ctx.batches.get(plain)["status"] == "completed":
            break
    assert h.batch(plain)["status"] == "completed" and h.batch(plain)["found"] == 150
    assert 0 < max_pending <= 100, "o lote download-only só obedece ao backpressure de download"
    assert h.ctx.worker.process_backlog_state() == ({with_process}, True), "a fila do FFmpeg continuou cheia"
    assert _ffmpeg_backlog(h, with_process) == 10 and h.ctx.jobs.pending_ingest_count(with_process) == 20
    assert proc.processed == [] and h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == 0
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND params IS NOT NULL", (plain,)) == 0


def test_disk_reserve_counts_ffmpeg_outputs_only_while_processing_work_is_open(harness_factory):
    dl = FakeInstagramDownloader(items=[remote("K0", media_type="video", profile="perfil_a")])
    h = harness_factory(downloader=dl)
    settings, worker = h.ctx.settings, h.ctx.worker
    plain = disk.required_free_bytes(settings.max_download_bytes, worker.download_concurrency,
                                     settings.disk_reserve_bytes)
    full = disk.required_free_bytes(settings.max_download_bytes, worker.download_concurrency,
                                    settings.disk_reserve_bytes, processing_concurrency=worker.concurrency)
    assert full == plain + 2 * settings.max_download_bytes * worker.concurrency

    def required() -> int:
        return h.client.get("/api/settings").json()["disk"]["required_bytes"]

    assert required() == plain
    h.import_profile(profile="perfil_a", limit=10)
    h.run_imports()
    assert required() == plain, "ingest download-only pendente não reserva saída do FFmpeg"
    h.run_jobs()
    dl.items = [remote("K1", media_type="video", profile="perfil_b")]
    h.import_profile(profile="perfil_b", limit=10, process=True, preset_id=_preset(h))
    h.run_imports()
    assert required() == full, "lote com processamento continua reservando como antes"
    h.run_jobs()
    assert required() == plain
    queued = h.client.post("/api/media/process-by-filter",
                           json={"view": "all", "profile": "perfil_a", "preset_id": _preset(h)})
    assert queued.status_code == 202, queued.text
    assert required() == full, "processamento pedido depois volta a reservar"
    h.run_jobs()
    assert required() == plain


# ---- FFmpeg ausente ou indisponível ------------------------------------------------------------------------------
def test_download_only_batch_completes_without_ffmpeg_and_ffprobe(harness_factory):
    dl = FakeInstagramDownloader(items=[remote(f"N{i:03d}", media_type="video" if i % 3 == 0 else "image")
                                        for i in range(30)])
    proc = FakeProcessor(ffmpeg_found=False)
    h = harness_factory(downloader=dl, processor=proc)
    batch = h.import_profile(limit=50)["id"]
    h.run_imports()
    assert _download_pool(h) == 30
    result = h.batch(batch)
    assert result["status"] == "completed" and result["progress"]["downloaded"] == 30
    assert result["progress"]["failed"] == 0 and proc.thumbnails == [] and proc.processed == []
    for job in result["jobs"]:
        media = h.ctx.media.get(job["media_id"])
        assert job["status"] == "completed" and media["media_type"] == job["media_type"]
        assert media["width"] is None and media["thumbnail_path"] is None
        assert sha256_file(h.ctx.storage.resolve(media["original_path"])) == media["original_sha256"]


class UnrunnableProbe(FakeProcessor):
    """ffprobe localizado mas que não executa (como `_run_json` do processor: InvalidMediaError a partir de OSError)."""

    def probe(self, path):
        try:
            raise PermissionError("acesso negado")
        except OSError as exc:
            raise InvalidMediaError("falha ao executar ffprobe") from exc


def test_unrunnable_ffprobe_does_not_fail_the_download(harness_factory):
    dl = FakeInstagramDownloader(items=[remote("U0"), remote("U1", media_type="video")])
    h = harness_factory(downloader=dl, processor=UnrunnableProbe())
    batch = h.import_profile(limit=10)["id"]
    h.run_imports()
    assert _download_pool(h) == 2
    result = h.batch(batch)
    assert result["status"] == "completed" and result["progress"]["failed"] == 0
    for job in result["jobs"]:
        media = h.ctx.media.get(job["media_id"])
        assert media["width"] is None and media["media_type"] == job["media_type"]


def test_unrunnable_ffprobe_still_rejects_files_without_media_signature(harness_factory):
    dl = FakeInstagramDownloader(items=[remote("H0")], content=lambda item: b"<html>erro</html>")
    h = harness_factory(downloader=dl, processor=UnrunnableProbe())
    batch = h.import_profile(limit=10)["id"]
    h.run_all()
    job = h.batch(batch)["jobs"][0]
    assert job["status"] == "failed" and job["error_code"] == "invalid_media"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 0


# ---- processar depois e compatibilidade --------------------------------------------------------------------------
def test_processing_later_works_on_download_only_media_and_old_batches_read_the_same(harness_factory):
    dl = FakeInstagramDownloader(items=[remote(f"O{i}", media_type="video", profile="perfil_a") for i in range(3)])
    proc = FakeProcessor()
    h = harness_factory(downloader=dl, processor=proc)
    preset = _preset(h)
    # Lote "antigo": processamento junto com a importação continua igual.
    old = h.import_profile(profile="perfil_a", limit=10, process=True, preset_id=preset)["id"]
    h.run_all()
    old_view = h.batch(old)
    assert old_view["status"] == "completed" and old_view["process"] is True
    assert old_view["progress"]["downloaded"] == 3 and old_view["progress"]["processed"] == 3
    assert all(job["rendition_id"] for job in old_view["jobs"])

    dl.items = [remote(f"L{i}", media_type="video", profile="perfil_b") for i in range(4)]
    plain = h.import_profile(profile="perfil_b", limit=10)["id"]
    h.run_all()
    assert h.batch(plain)["status"] == "completed" and len(proc.processed) == 3
    body = {"view": "all", "profile": "perfil_b", "preset_id": preset}
    later = h.client.post("/api/media/process-by-filter", json=body)
    assert later.status_code == 202, later.text
    h.run_jobs()
    later_view = h.batch(later.json()["id"])
    assert later_view["status"] == "completed" and later_view["source"] == "library"
    assert later_view["progress"]["processed"] == 4 and len(proc.processed) == 7
    again = h.client.post("/api/media/process-by-filter", json=body)
    assert again.status_code == 409 and again.json()["code"] == "nothing_to_process"
    media_ids = [job["media_id"] for job in h.batch(plain)["jobs"]]
    single = h.client.post(f"/api/media/{media_ids[0]}/process", json={"preset_id": preset})
    assert single.status_code == 202, single.text
    # O lote download-only e o lote antigo continuam lendo igual depois do processamento separado.
    plain_view = h.batch(plain)
    assert plain_view["process"] is False and plain_view["progress"]["processed"] == 0
    assert all(job["rendition_id"] is None and job["status"] == "completed" for job in plain_view["jobs"])
    assert h.batch(old)["progress"] == old_view["progress"]
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == 7


# ---- separação: download puro (zero ffprobe/FFmpeg) x análise opcional -------------------------------------------
class CountingProcessor(FakeProcessor):
    """FakeProcessor (FFmpeg/ffprobe presentes e funcionando) que conta cada chamada de análise/processamento."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.probes: list = []

    def probe(self, path):
        self.probes.append(path)
        return super().probe(path)


def test_pure_download_with_working_ffmpeg_makes_zero_probe_thumbnail_and_process_calls(harness_factory, caplog):
    caplog.set_level(logging.DEBUG)
    dl = FakeInstagramDownloader(items=[remote("Z0"), remote("Z1", media_type="video"), remote("Z2")])
    proc = CountingProcessor()
    h = harness_factory(downloader=dl, processor=proc)
    batch = h.import_profile(limit=10)["id"]
    h.run_imports()
    assert _download_pool(h) == 3
    result = h.batch(batch)
    assert result["status"] == "completed" and result["progress"]["failed"] == 0
    assert proc.probes == [] and proc.thumbnails == [] and proc.processed == []
    for job in result["jobs"]:
        media = h.ctx.media.get(job["media_id"])
        assert job["status"] == "completed" and media["media_type"] == job["media_type"]
        assert media["width"] is None and media["height"] is None and media["duration"] is None
        assert media["thumbnail_path"] is None
        assert media["original_path"].endswith(".mp4" if job["media_type"] == "video" else ".jpg")
        path = h.ctx.storage.resolve(media["original_path"])
        assert sha256_file(path) == media["original_sha256"]
        # API/UI com campos nulos: serializa sem erro e sem miniatura
        body = h.client.get(f"/api/media/{media['id']}").json()
        assert body["width"] is None and body["thumbnail_url"] is None
        assert h.client.get(f"/api/media/{media['id']}/thumbnail").status_code == 404
        assert h.client.get(f"/api/media/{media['id']}/original").status_code == 200
    assert h.client.get("/api/media").status_code == 200
    noisy = [r for r in caplog.records if r.levelno >= logging.WARNING and "ffprobe" in r.getMessage().lower()]
    assert noisy == []


def test_pure_download_duplicate_by_sha256_makes_no_analysis_calls(harness_factory):
    dl = FakeInstagramDownloader(items=[remote("D0"), remote("D1")], content=lambda item: b"\xff\xd8\xff\xe0SAME-BYTES")
    proc = CountingProcessor()
    h = harness_factory(downloader=dl, processor=proc)
    batch = h.import_profile(limit=10)["id"]
    h.run_imports()
    assert _download_pool(h) == 2
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 1
    assert h.batch(batch)["progress"]["skipped"] == 1
    assert proc.probes == [] and proc.thumbnails == []


def _pure_download(harness_factory, content):
    dl = FakeInstagramDownloader(items=[remote("V0")], content=lambda item: content)
    proc = CountingProcessor()
    h = harness_factory(downloader=dl, processor=proc)
    batch = h.import_profile(limit=10)["id"]
    h.run_all()
    assert proc.probes == [] and proc.thumbnails == []
    return h, h.batch(batch)["jobs"][0]


@pytest.mark.parametrize("content", [
    b"",
    b"<!DOCTYPE html><html><body>erro</body></html>",
    b'{"status": "fail", "message": "login_required"}',
    b"The free tier allows ten downloads per day.",
    b"The skip list is rebuilt after every run.",
    b"\x00\x00\x00\x10free",  # tamanho de caixa maior que o arquivo
], ids=["empty", "html", "json", "text-free", "text-skip", "box-larger-than-file"])
def test_pure_download_rejects_non_media_as_invalid_media(harness_factory, content):
    h, job = _pure_download(harness_factory, content)
    assert job["status"] == "failed" and job["error_code"] == "invalid_media"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 0
    assert not list(h.ctx.storage.originals.rglob("*.*"))


@pytest.mark.parametrize("content", [
    b"\xff\xd8\xff\xe0" + b"JPEGDATA" * 4,
    b"\x89PNG\r\n\x1a\n" + b"PNGDATA" * 4,
    b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16,
    b"\x00\x00\x00\x20ftypmp42" + b"\x00" * 24,
    b"\x1a\x45\xdf\xa3" + b"WEBMDATA" * 4,
], ids=["jpeg", "png", "mp4-ftyp", "mp4-ftyp-32", "webm"])
def test_pure_download_accepts_real_media_signatures(harness_factory, content):
    h, job = _pure_download(harness_factory, content)
    assert job["status"] == "completed", (job["status"], job["error_code"])
    media = h.ctx.media.get(job["media_id"])
    assert media["original_sha256"] == hashlib.sha256(content).hexdigest()


def test_optional_analysis_after_pure_download_fills_metadata_and_thumbnail(harness_factory):
    dl = FakeInstagramDownloader(items=[remote("A0", media_type="video"), remote("A1")])
    proc = CountingProcessor()
    h = harness_factory(downloader=dl, processor=proc)
    h.import_profile(limit=10)
    h.run_all()
    assert proc.probes == [] and proc.thumbnails == []
    rows = h.ctx.db.fetchall("SELECT id FROM media ORDER BY id")
    assert all(h.ctx.media.get(r["id"])["width"] is None for r in rows)
    queued = h.client.post("/api/media/process-by-filter", json={"view": "all", "preset_id": _preset(h)})
    assert queued.status_code == 202, queued.text
    h.run_jobs()
    assert h.batch(queued.json()["id"])["status"] == "completed"
    for r in rows:
        media = h.ctx.media.get(r["id"])
        assert media["width"] == 64 and media["height"] == 48
        assert (media["duration"] == 2.0) == (media["media_type"] == "video")
        assert media["thumbnail_path"] and h.ctx.storage.resolve(media["thumbnail_path"]).is_file()
        assert h.client.get(f"/api/media/{media['id']}/thumbnail").status_code == 200
    assert len(proc.thumbnails) == 2 and len(proc.processed) == 2


def test_processing_after_pure_download_without_ffprobe_fails_as_before(harness_factory):
    dl = FakeInstagramDownloader(items=[remote("B0")])
    proc = CountingProcessor()
    h = harness_factory(downloader=dl, processor=proc)
    h.import_profile(limit=10)
    h.run_all()
    media_id = h.ctx.db.scalar("SELECT id FROM media")
    proc.ffmpeg_found = False  # a ferramenta some antes da análise opcional
    queued = h.client.post(f"/api/media/{media_id}/process", json={"preset_id": _preset(h)})
    assert queued.status_code == 202, queued.text
    h.run_jobs()
    job = h.client.get(f"/api/jobs/{queued.json()['id']}").json()
    assert job["status"] == "failed" and job["error_code"] == "ffmpeg_not_found"
    media = h.ctx.media.get(media_id)
    assert media["width"] is None and media["thumbnail_path"] is None
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == 0


def test_batch_with_processing_still_probes_and_makes_thumbnail_at_ingest(harness_factory):
    dl = FakeInstagramDownloader(items=[remote("W0", media_type="video")])
    proc = CountingProcessor()
    h = harness_factory(downloader=dl, processor=proc)
    batch = h.import_profile(limit=10, process=True, preset_id=_preset(h))["id"]
    h.run_all()
    job = h.batch(batch)["jobs"][0]
    assert job["status"] == "completed" and job["rendition_id"]
    media = h.ctx.media.get(job["media_id"])
    assert media["width"] == 64 and media["duration"] == 2.0 and media["has_audio"] == 1
    assert media["thumbnail_path"] and len(proc.thumbnails) == 1 and len(proc.probes) >= 1


def test_batch_with_processing_without_ffprobe_keeps_warning_and_registers_original(harness_factory, caplog):
    caplog.set_level(logging.WARNING)
    dl = FakeInstagramDownloader(items=[remote("W1")])
    h = harness_factory(downloader=dl, processor=FakeProcessor(ffmpeg_found=False))
    h.import_profile(limit=10, process=True, preset_id=_preset(h))
    h.run_all()
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 1
    assert any("ffprobe ausente" in r.getMessage() for r in caplog.records)
