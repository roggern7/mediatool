"""Backpressure de processamento (docs/BATCH_ROBUSTNESS.md, seção 9): com processamento ligado, o lote para de
reivindicar NOVOS downloads (e de listar) com `max_process_backlog` ingests baixando/baixados/processando e retoma
abaixo de `resume_process_backlog`; o teto global soma todos os lotes com processamento. Lotes sem processamento não
são afetados."""

from __future__ import annotations

import threading
import time

from conftest import wait_until
from media_fakes import FakeInstagramDownloader, FakeProcessor, remote

OK_DISK = (True, 1 << 40, 0)
LIMITS = {"max_process_backlog": 10, "resume_process_backlog": 4, "max_process_backlog_global": 15,
          "resume_process_backlog_global": 6}


def _backlog(h, batch_id: int | None = None) -> int:
    where = "" if batch_id is None else f" AND batch_id = {int(batch_id)}"
    return int(h.ctx.db.scalar(
        "SELECT COUNT(*) FROM jobs WHERE kind = 'ingest' AND status IN ('downloading', 'downloaded', 'processing')"
        " AND params IS NOT NULL" + where))


class RecordingProcessor(FakeProcessor):
    """Anota o backlog do app a cada processamento (opcionalmente lento)."""

    def __init__(self, *, delay: float = 0.0) -> None:
        super().__init__()
        self.delay = delay
        self.h = None
        self.samples: list[int] = []

    def process(self, src, dst, opts, **kw):
        if self.h is not None:
            self.samples.append(_backlog(self.h))
        if self.delay:
            time.sleep(self.delay)
        return super().process(src, dst, opts, **kw)


def _harness(harness_factory, items, *, processor=None, **kw):
    dl = FakeInstagramDownloader(items=items)
    h = harness_factory(downloader=dl, processor=processor, **{**LIMITS, **kw})
    h.ctx.worker.disk_check = lambda: OK_DISK
    if processor is not None:
        processor.h = h
    return h, dl


def _preset(h) -> int:
    return h.client.get("/api/presets").json()[0]["id"]


def _download_only(h) -> int:
    """Executa só o pool de download até não haver claim (o FFmpeg não anda)."""
    count = 0
    while (claimed := h.ctx.worker._claim("download")) is not None:
        h.ctx.worker._execute(*claimed)
        count += 1
    return count


def test_backlog_caps_new_downloads_with_hysteresis_and_batch_completes(harness_factory):
    proc = RecordingProcessor()
    h, dl = _harness(harness_factory, [remote(f"P{i:03d}") for i in range(60)], processor=proc)
    batch = h.import_profile(limit=100, process=True, preset_id=_preset(h))["id"]
    h.run_imports()
    assert _download_only(h) == 10 and _backlog(h, batch) == 10, "para no teto do lote"
    worker = h.ctx.worker
    for _ in range(6):  # 10 -> 4: ainda não retoma (histerese: só abaixo de 4)
        worker._execute(*worker._claim("process"))
        assert worker._claim("download") is None
    worker._execute(*worker._claim("process"))  # 3 < 4: retoma
    assert _download_only(h) == 7 and _backlog(h, batch) == 10
    h.ctx.worker.run_pending()
    assert h.batch(batch)["status"] == "completed"
    assert max(proc.samples) <= 10 and len(proc.samples) == 60
    assert len(dl.downloads) == len(set(dl.downloads)) == 60


def test_batches_without_processing_are_not_affected(harness_factory):
    h, dl = _harness(harness_factory, [remote(f"N{i:03d}") for i in range(30)], processor=RecordingProcessor())
    with_process = h.import_profile(profile="perfil_a", limit=50, process=True, preset_id=_preset(h))["id"]
    h.run_imports()
    dl.items = [remote(f"M{i:03d}") for i in range(30)]
    plain = h.import_profile(profile="perfil_b", limit=50)["id"]
    h.run_imports()
    _download_only(h)
    assert _backlog(h, with_process) == 10
    assert h.batch(plain)["status"] == "completed", "lote sem processamento baixa tudo"


def test_global_backlog_cap_across_batches(harness_factory):
    proc = RecordingProcessor()
    h, dl = _harness(harness_factory, [remote(f"G{i:03d}") for i in range(30)], processor=proc)
    preset = _preset(h)
    first = h.import_profile(profile="perfil_a", limit=50, process=True, preset_id=preset)["id"]
    h.run_imports()
    dl.items = [remote(f"H{i:03d}") for i in range(30)]
    second = h.import_profile(profile="perfil_b", limit=50, process=True, preset_id=preset)["id"]
    h.run_imports()
    _download_only(h)
    assert _backlog(h) == 15 and _backlog(h, first) <= 10 and _backlog(h, second) <= 10
    h.ctx.worker.run_pending()
    assert all(h.batch(i)["status"] == "completed" for i in (first, second))
    assert max(proc.samples) <= 15


def test_listing_waits_while_processing_backlog_is_full(harness_factory):
    h, dl = _harness(harness_factory, [remote(f"L{i:03d}") for i in range(300)], processor=RecordingProcessor())
    batch = h.import_profile(limit=250, process=True, preset_id=_preset(h))["id"]
    h.run_imports()
    _download_only(h)
    listed = h.ctx.jobs.count_for_batch(batch)
    # Sem ingests pendentes (o backpressure de listagem liberaria), só o backlog de processamento segura a listagem.
    h.ctx.db.execute("UPDATE jobs SET status = 'failed', finished_at = '2026-01-01T00:00:00.000Z'"
                     " WHERE batch_id = ? AND status = 'pending'", (batch,))
    h.run_imports()
    assert h.ctx.jobs.count_for_batch(batch) == listed
    assert h.ctx.batches.get(batch)["listing_state"] == "waiting_backpressure"
    worker = h.ctx.worker
    while (claimed := worker._claim("process")) is not None:
        worker._execute(*claimed)
    h.run_imports()
    assert h.ctx.jobs.count_for_batch(batch) > listed, "backlog drenado: a listagem retoma"


def test_slow_ffmpeg_with_real_threads_never_exceeds_backlog_cap(harness_factory):
    proc = RecordingProcessor(delay=0.01)
    h, dl = _harness(harness_factory, [remote(f"T{i:03d}", media_type="video") for i in range(80)],
                     processor=proc, poll_interval=0.01)
    batch = h.import_profile(limit=100, process=True, preset_id=_preset(h))["id"]
    peak = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, _backlog(h, batch))
            time.sleep(0.005)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        h.ctx.import_runner.start()
        h.ctx.worker.start()
        assert wait_until(lambda: h.ctx.batches.get(batch)["status"] == "completed", timeout=60)
    finally:
        stop.set()
        sampler.join(5)
    assert peak <= 10 and max(proc.samples) <= 10
    assert len(dl.downloads) == len(set(dl.downloads)) == 80
