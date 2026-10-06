"""Execução robusta (docs/BATCH_ROBUSTNESS.md, seção 9, parte "Execução"). Offline, valores FAKE."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace

import pytest

import app.jobs.worker as worker_module
from app.integrations.instagram.downloader import InstagramError
from app.jobs.recovery import migrate_thumbnails
from conftest import wait_until
from media_fakes import FakeInstagramDownloader, FakeProcessor, remote

GB = 1024 ** 3


@dataclass
class RefusingDownloader(FakeInstagramDownloader):
    """Toda URL de mídia é recusada pelo "CDN"; a renovação da página falha com `refresh_error` (ou devolve URLs
    novas que funcionam)."""

    refresh_error: Exception | None = None
    refresh_calls: list = field(default_factory=list)

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        if "FRESH" not in (item.media_url or ""):
            raise InstagramError("media_url_expired")
        return super().download(item, dest_dir, cancel=cancel, progress=progress)

    def refresh_page(self, profile, ref, *, cancel=None):
        self.refresh_calls.append(ref.page_index)
        if self.refresh_error is not None:
            raise self.refresh_error
        size = ref.page_size
        return [replace(item, media_url=f"https://cdn.example.invalid/FRESH-{item.source_post_id}.jpg",
                        listing_ref=ref) for item in self.items[(ref.page_index - 1) * size: ref.page_index * size]]


def _import(h, **body):
    batch = h.import_profile(**body)
    h.run_imports()
    return batch["id"]


def _jobs(h, batch_id):
    return h.ctx.jobs.for_batch(batch_id)


# ---- A1: listing_pages ----------------------------------------------------------------------------------------------
def test_refused_refresh_20_jobs_2_pages_at_most_one_call_per_page(harness_factory):
    dl = RefusingDownloader(items=[remote(f"P{i}") for i in range(20)], page_size=10,
                            refresh_error=InstagramError("access_refused"))
    h = harness_factory(downloader=dl)
    batch_id = _import(h, limit=20)
    h.run_jobs()
    assert sorted(dl.refresh_calls) == [1, 2]  # no máximo 1 chamada por página
    jobs = _jobs(h, batch_id)
    assert len(jobs) == 20 and all(j["status"] == "failed" and j["error_code"] == "access_refused" for j in jobs)
    rows = h.ctx.db.fetchall("SELECT * FROM listing_pages WHERE batch_id = ? ORDER BY page_index", (batch_id,))
    assert [(r["page_index"], r["refresh_attempts"], r["last_refresh_ok"], r["last_refresh_code"]) for r in rows] == [
        (1, 1, 0, "access_refused"), (2, 1, 0, "access_refused")]


def test_successful_refresh_is_recorded(harness_factory):
    dl = RefusingDownloader(items=[remote(f"S{i}") for i in range(3)])
    h = harness_factory(downloader=dl)
    batch_id = _import(h, limit=10)
    h.run_jobs()
    assert dl.refresh_calls == [1]
    assert all(j["status"] == "completed" for j in _jobs(h, batch_id))
    page = h.ctx.jobs.listing_page(batch_id, 1)
    assert page["last_refresh_ok"] == 1 and page["refresh_attempts"] == 1 and page["last_refresh_code"] is None


# ---- A4: disjuntor por lote -----------------------------------------------------------------------------------------
def test_breaker_three_refresh_failures_pause_batch_with_backoff(harness_factory):
    dl = RefusingDownloader(items=[remote(f"B{i}") for i in range(4)], page_size=1,
                            refresh_error=InstagramError("contract_changed"))
    h = harness_factory(downloader=dl)
    batch_id = _import(h, limit=10)
    assert h.run_jobs() == 3  # 3 renovações seguidas com o mesmo código pausam o lote: o 4o job não é reivindicado
    assert dl.refresh_calls == [1, 2, 3]
    assert h.ctx.jobs.is_batch_paused(batch_id)
    pause = h.ctx.jobs.batch_pause(batch_id)
    assert pause["paused_reason"] == "repeated_failures" and pause["paused_code"] == "contract_changed"
    assert [j["status"] for j in _jobs(h, batch_id)][-1] == "pending"
    assert h.run_jobs() == 0
    diag = h.client.get("/api/settings/diagnostics").json()
    assert diag["paused_batches"][0]["batch_id"] == batch_id
    out = h.client.get(f"/api/batches/{batch_id}").json()
    assert out["paused_reason"] == "contract_changed" and out["paused_until"] == pause["paused_until"]
    # Retomada automática quando a pausa vence (sem ação do usuário).
    h.ctx.db.execute("UPDATE batches SET paused_until = '2000-01-01T00:00:00.000Z' WHERE id = ?", (batch_id,))
    assert not h.ctx.jobs.is_batch_paused(batch_id)
    out = h.client.get(f"/api/batches/{batch_id}").json()
    assert out["paused_reason"] is None and out["paused_until"] is None
    assert h.run_jobs() == 1 and dl.refresh_calls == [1, 2, 3, 4]
    # Backoff: a próxima pausa usa o degrau seguinte.
    level = h.ctx.db.scalar("SELECT pause_level FROM batches WHERE id = ?", (batch_id,))
    assert level == 1
    h.ctx.jobs.record_refresh(batch_id, 9, ok=False, code="x", at="2026-01-01T00:00:00.000Z")
    h.ctx.jobs.record_refresh(batch_id, 9, ok=False, code="x", at="2026-01-01T00:00:00.000Z")
    from app.jobs.pipeline import BATCH_PAUSE_STEPS
    until = h.ctx.jobs.pause_batch(batch_id, reason="repeated_failures", code="x", steps=BATCH_PAUSE_STEPS)
    from app.db.connection import parse_utc, utc_now
    assert (parse_utc(until) - parse_utc(utc_now())).total_seconds() > BATCH_PAUSE_STEPS[0] + 60


def test_breaker_twenty_job_failures_same_code_pause_batch(harness_factory):
    dl = FakeInstagramDownloader(items=[remote(f"F{i}") for i in range(25)])
    dl.download_errors = {f"F{i}:0": [InstagramError("content_unavailable")] for i in range(25)}
    h = harness_factory(downloader=dl)
    batch_id = _import(h, limit=50)
    assert h.run_jobs() == 20
    statuses = [j["status"] for j in _jobs(h, batch_id)]
    assert statuses.count("failed") == 20 and statuses.count("pending") == 5
    assert h.ctx.jobs.is_batch_paused(batch_id)
    assert h.ctx.jobs.batch_pause(batch_id)["paused_code"] == "content_unavailable"


def test_success_resets_breaker_streak(harness_factory):
    dl = FakeInstagramDownloader(items=[remote(f"G{i}") for i in range(30)])
    dl.download_errors = {f"G{i}:0": [InstagramError("content_unavailable")] for i in range(30) if i != 15}
    h = harness_factory(downloader=dl)
    batch_id = _import(h, limit=50)
    h.run_jobs()
    assert not h.ctx.jobs.is_batch_paused(batch_id)  # 15 falhas, 1 sucesso, 14 falhas: nunca 20 seguidas


def test_settings_processing_concurrency_is_concurrency(harness_factory):
    h = harness_factory()
    assert h.client.put("/api/settings", json={"processing_concurrency": 2}).status_code == 200
    out = h.client.get("/api/settings").json()
    assert out["processing_concurrency"] == out["concurrency"] == 2 and h.ctx.worker.concurrency == 2
    assert {"free_bytes", "required_bytes", "ok", "disk_full"} <= set(out["disk"])
    bad = h.client.put("/api/settings", json={"concurrency": 1, "processing_concurrency": 2})
    assert bad.status_code == 422


# ---- C2: disco -------------------------------------------------------------------------------------------------------
def test_disk_full_batch_of_500_waits_without_attempts_and_resumes(harness_factory):
    dl = FakeInstagramDownloader(items=[remote(f"D{i}") for i in range(500)])
    h = harness_factory(downloader=dl)
    worker = h.ctx.worker
    disk = {"free": 1 * GB}
    calls = []

    def fake_disk_ok():
        calls.append(1)
        return disk["free"] >= 13 * GB, disk["free"], 13 * GB

    batch_id = _import(h, limit=500)  # 1o trecho da listagem (até o backpressure) com disco ok
    worker.disk_check = fake_disk_ok
    assert h.run_jobs() == 0
    jobs = _jobs(h, batch_id)
    assert jobs and all(j["status"] == "pending" and j["attempts"] == 0 for j in jobs)
    diag = h.client.get("/api/settings/diagnostics").json()
    assert diag["disk"] == {"disk_full": True, "ok": False, "free_bytes": 1 * GB, "required_bytes": 13 * GB}
    assert h.client.get("/api/settings").json()["disk"] == diag["disk"]
    # Espaço liberado: só é visto na próxima verificação (a cada 1 min).
    disk["free"] = 100 * GB
    before = len(calls)
    assert h.run_jobs() == 0 and len(calls) == before
    worker._disk_next_check = 0.0  # passou 1 minuto
    for _ in range(20):
        h.run_imports()
        if not h.run_jobs():
            break
    jobs = _jobs(h, batch_id)
    assert len(jobs) == 500 and all(j["status"] == "completed" and j["attempts"] == 0 for j in jobs)
    assert h.client.get("/api/settings/diagnostics").json()["disk"]["disk_full"] is False


def test_disk_full_during_download_requeues_without_attempt(harness_factory):
    dl = FakeInstagramDownloader(items=[remote("E0")])
    dl.download_errors = {"E0:0": [OSError(errno.ENOSPC, "No space left on device")]}
    h = harness_factory(downloader=dl)
    h.ctx.worker.disk_check = lambda: (True, 100 * GB, 13 * GB)
    batch_id = _import(h, limit=10)
    assert h.run_jobs() == 1
    job = _jobs(h, batch_id)[0]
    assert job["status"] == "pending" and job["attempts"] == 0 and job["error_code"] is None
    assert h.ctx.worker._disk_full and h.run_jobs() == 0


def test_disk_ok_measures_storage(tmp_path):
    from app.services import disk

    ok, free, required = disk.disk_ok(min_free=0)
    assert ok and free > 0 and required == 0
    assert disk.required_free_bytes(2 * GB, 2, reserve=5 * GB) == 13 * GB


# ---- A3: dois pools ---------------------------------------------------------------------------------------------------
def test_slow_ffmpeg_does_not_hold_downloads(harness_factory):
    gate = threading.Event()
    dl = FakeInstagramDownloader(items=[remote(f"V{i}", media_type="video") for i in range(4)])
    h = harness_factory(downloader=dl, processor=FakeProcessor(block=gate), start_worker=True)
    preset_id = h.client.get("/api/presets").json()[0]["id"]
    batch = h.import_profile(limit=10, process=True, preset_id=preset_id)
    worker = h.ctx.worker
    try:
        # media_id é gravado antes da miniatura e do status `downloaded`: espera o estado estável (3 baixados e o
        # FFmpeg preso no 1o), não o media_id.
        def settled() -> bool:
            statuses = sorted(j["status"] for j in _jobs(h, batch["id"]))
            return statuses == ["downloaded"] * 3 + ["processing"] and worker.active_count_for("process") == 1

        # Prazo de espera folgado: threads reais e relógio real; sob carga da suíte inteira um teste chega a rodar
        # dezenas de vezes mais devagar. A condição esperada é a mesma.
        assert wait_until(settled, timeout=60)
        assert all(j["media_id"] for j in _jobs(h, batch["id"]))
    finally:
        gate.set()
    assert wait_until(lambda: h.batch(batch["id"])["status"] == "completed", timeout=60)
    assert all(j["status"] == "completed" and j["rendition_id"] for j in _jobs(h, batch["id"]))


def test_processing_does_not_wait_for_blocked_downloads(harness_factory):
    gate = threading.Event()
    dl = FakeInstagramDownloader(items=[remote("L0")])
    h = harness_factory(downloader=dl)
    _import(h, limit=10)
    h.run_jobs()
    media_id = h.ctx.db.scalar("SELECT id FROM media")
    dl.items = [remote(f"W{i}") for i in range(4)]
    dl.block = gate
    _import(h, profile="outro_perfil", limit=10)
    preset_id = h.client.get("/api/presets").json()[0]["id"]
    try:
        h.ctx.worker.start()
        assert wait_until(lambda: h.ctx.worker.active_count_for("download") == 2)
        response = h.client.post(f"/api/media/{media_id}/process", json={"preset_id": preset_id})
        assert response.status_code == 202
        job_id = response.json()["id"]
        assert wait_until(lambda: h.ctx.jobs.get(job_id)["status"] == "completed", timeout=10)
        assert h.ctx.worker.active_count_for("download") == 2  # downloads continuam presos
    finally:
        gate.set()


# ---- rodízio justo ---------------------------------------------------------------------------------------------------
def test_round_robin_claim_between_two_batches(harness_factory):
    dl = FakeInstagramDownloader(items=[remote(f"R{i}") for i in range(5)])
    h = harness_factory(downloader=dl)
    first = _import(h, limit=10)
    dl.items = [remote(f"S{i}", profile="outro_perfil") for i in range(5)]  # itens distintos (sem dedupe entre lotes)
    second = _import(h, profile="outro_perfil", limit=10)
    order = []
    for _ in range(6):
        job, _event = h.ctx.worker._claim("download")
        order.append(job["batch_id"])
    assert order == [first, second, first, second, first, second]


# ---- M7: interrupção e pausa não consomem tentativa ------------------------------------------------------------------
def test_abrupt_restart_does_not_consume_attempt(harness_factory):
    h = harness_factory(downloader=FakeInstagramDownloader(items=[remote("A0"), remote("A1")]))
    batch_id = _import(h, limit=10)
    first, second = _jobs(h, batch_id)
    h.ctx.db.execute("UPDATE jobs SET status = 'downloading', stage = 'downloading', claimed_by = 'morto',"
                     " attempts = 2 WHERE id = ?", (first["id"],))
    h.ctx.db.execute("UPDATE jobs SET status = 'downloading', claimed_by = 'morto' WHERE id = ?", (second["id"],))
    root = h.root
    h.__exit__(None, None, None)
    again = harness_factory(root=root, downloader=FakeInstagramDownloader(items=[remote("A0"), remote("A1")]))
    jobs = {j["id"]: j for j in _jobs(again, batch_id)}
    assert jobs[first["id"]]["status"] == "pending" and jobs[first["id"]]["attempts"] == 2
    assert jobs[second["id"]]["status"] == "pending" and jobs[second["id"]]["attempts"] == 0
    again.run_jobs()
    assert all(j["status"] == "completed" for j in _jobs(again, batch_id))


def test_pause_failure_does_not_consume_attempt(harness_factory):
    dl = FakeInstagramDownloader(items=[remote("Q0")])
    dl.download_errors = {"Q0:0": [InstagramError("rate_limited", retryable=True)]}
    h = harness_factory(downloader=dl)
    batch_id = _import(h, limit=10)
    h.run_jobs()
    job = _jobs(h, batch_id)[0]
    assert h.ctx.worker.downloads_paused()
    assert job["status"] == "pending" and job["attempts"] == 0 and job["error_code"] == "rate_limited"


# ---- B3: thumbs fatiados ---------------------------------------------------------------------------------------------
def test_thumbs_sharded_and_flat_ones_migrated_idempotently(harness):
    h = harness
    assert h.ctx.storage.thumb_path(300) == h.ctx.storage.thumbs / "44" / "300.jpg"
    h.downloader.items = [remote("T0")]
    # Download puro não gera miniatura (zero FFmpeg): o cenário usa um lote com processamento.
    _import(h, limit=10, process=True)
    h.run_jobs()
    media = h.ctx.db.fetchone("SELECT id, thumbnail_path FROM media")
    sharded = h.ctx.storage.thumb_path(media["id"])
    assert media["thumbnail_path"] == f"thumbs/{media['id'] % 256}/{media['id']}.jpg" and sharded.is_file()
    # Simula o layout antigo.
    flat = h.ctx.storage.thumbs / f"{media['id']}.jpg"
    os.replace(sharded, flat)
    h.ctx.media.update(media["id"], thumbnail_path=f"thumbs/{media['id']}.jpg")
    assert migrate_thumbnails(h.ctx) == 1
    assert sharded.is_file() and not flat.exists()
    assert h.ctx.media.get(media["id"])["thumbnail_path"] == f"thumbs/{media['id'] % 256}/{media['id']}.jpg"
    assert migrate_thumbnails(h.ctx) == 0  # idempotente
    assert h.client.get(f"/api/media/{media['id']}/thumbnail").status_code == 200


# ---- B6: verificação do controlador fora da thread despachante ----------------------------------------------------
def test_recovery_check_runs_off_dispatcher_thread(harness):
    worker = harness.ctx.worker
    release, started = threading.Event(), threading.Event()
    threads = []

    def slow_tick():
        threads.append(threading.current_thread())
        started.set()
        release.wait(5)

    worker.recovery.tick = slow_tick
    worker._last_recovery_check = float("-inf")
    began = time.monotonic()
    worker._recovery_if_due()
    assert time.monotonic() - began < 1.0 and started.wait(2)
    assert threads[0] is not threading.current_thread()
    worker._last_recovery_check = float("-inf")
    worker._recovery_if_due()  # verificação anterior ainda rodando: nenhuma nova
    release.set()
    worker._recovery_thread.join(2)
    assert len(threads) == 1


# ---- B7: impedir suspensão enquanto houver lote ativo -------------------------------------------------------------
def test_keep_awake_while_batch_active(harness, monkeypatch):
    calls = []
    monkeypatch.setattr(worker_module, "set_thread_execution_state", lambda keep: calls.append(keep) or True)
    h = harness
    worker = h.ctx.worker
    worker._keep_awake_if_due()
    assert calls == []  # ocioso
    h.downloader.items = [remote("K0")]
    batch_id = _import(h, limit=10)
    worker._last_keep_awake_check = float("-inf")
    worker._keep_awake_if_due()
    assert calls == [True]
    h.run_jobs()
    assert h.batch(batch_id)["status"] == "completed"
    worker._last_keep_awake_check = float("-inf")
    worker._keep_awake_if_due()
    assert calls == [True, False]


@pytest.mark.skipif(sys.platform != "win32", reason="SetThreadExecutionState só existe no Windows")
def test_set_thread_execution_state_real_call():
    assert worker_module.set_thread_execution_state(True)
    assert worker_module.set_thread_execution_state(False)


# ---- M8: FFmpeg em Job Object ----------------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Job Object só existe no Windows")
def test_job_object_kills_child_when_handle_closes():
    from media_processor import processor

    job = processor.create_kill_on_close_job()
    assert job
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert processor.assign_to_job(job, child)
        assert child.poll() is None
        processor._kernel32().CloseHandle(job)  # = o processo do Media Tool morrendo
        assert child.wait(timeout=10) is not None
    finally:
        if child.poll() is None:
            child.kill()


def test_job_object_is_noop_outside_windows(monkeypatch):
    from media_processor import processor

    monkeypatch.setattr(processor.sys, "platform", "linux")
    assert processor.create_kill_on_close_job() is None
    assert processor.assign_to_job(123, object()) is False


# ---- re-revisão: disco no pool de FFmpeg, irmãos sem pausa, escopo do disjuntor, has_active_work, crash_count ----
def test_disk_full_blocks_ffmpeg_pool_claims(harness_factory):
    h = harness_factory(downloader=FakeInstagramDownloader(items=[remote("H0", media_type="video")]))
    preset_id = h.client.get("/api/presets").json()[0]["id"]
    batch_id = _import(h, limit=10, process=True, preset_id=preset_id)
    job, _event = h.ctx.worker._claim("download")
    from app.jobs.pipeline import JobExecutor
    JobExecutor(h.ctx, h.ctx.worker, job, _event).run()
    h.ctx.jobs.release(job["id"], h.ctx.worker.owner)
    assert _jobs(h, batch_id)[0]["status"] == "downloaded"
    h.ctx.worker.disk_check = lambda: (False, 1 * GB, 17 * GB)
    assert h.ctx.worker._claim("process") is None
    assert _jobs(h, batch_id)[0]["status"] == "downloaded"


def test_ffmpeg_disk_full_requeues_downloaded_without_attempt_or_breaker(harness_factory):
    from media_processor import ProcessingFailedError

    failure = ProcessingFailedError("ffmpeg falhou (código 1)", returncode=1,
                                    stderr="av_interleaved_write_frame(): No space left on device")
    h = harness_factory(downloader=FakeInstagramDownloader(items=[remote("N0", media_type="video")]),
                        processor=FakeProcessor(fail_process=failure))
    preset_id = h.client.get("/api/presets").json()[0]["id"]
    batch_id = _import(h, limit=10, process=True, preset_id=preset_id)
    assert h.run_jobs() == 2  # download + 1 tentativa de FFmpeg; depois a pausa disk_full segura o pool
    job = _jobs(h, batch_id)[0]
    assert job["status"] == "downloaded" and job["attempts"] == 0 and job["error_code"] is None
    assert h.ctx.worker._disk_full and h.run_jobs() == 0  # sem ciclo
    assert h.ctx.db.scalar("SELECT fail_streak FROM batches WHERE id = ?", (batch_id,)) == 0


def test_required_free_bytes_counts_processing_outputs():
    from app.services import disk

    assert disk.required_free_bytes(2 * GB, 2, reserve=5 * GB, processing_concurrency=1) == 17 * GB


def test_sibling_replaying_failed_refresh_does_not_reapply_pause(harness_factory):
    dl = RefusingDownloader(items=[remote("Z0"), remote("Z1")],
                            refresh_error=InstagramError("rate_limited", retryable=True))
    h = harness_factory(downloader=dl)
    batch_id = _import(h, limit=10)
    h.run_jobs()
    assert dl.refresh_calls == [1] and h.ctx.worker.downloads_paused()
    h.ctx.worker.clear_pause()  # liberação manual
    h.run_jobs()
    assert dl.refresh_calls == [1]  # irmãos repetem o código registrado sem requisição
    assert not h.ctx.worker.downloads_paused()  # e sem religar a pausa
    assert all(j["error_code"] == "rate_limited" and j["status"] == "pending" for j in _jobs(h, batch_id))


def test_breaker_scope_processing_codes_pause_only_ffmpeg(harness_factory):
    from media_processor import ProcessingFailedError

    h = harness_factory(downloader=FakeInstagramDownloader(items=[remote(f"X{i}") for i in range(22)]),
                        processor=FakeProcessor(fail_process=ProcessingFailedError("saída inválida")))
    preset_id = h.client.get("/api/presets").json()[0]["id"]
    batch_id = _import(h, limit=50, process=True, preset_id=preset_id)
    h.run_jobs()
    statuses = [j["status"] for j in _jobs(h, batch_id)]
    assert statuses.count("failed") == 20 and statuses.count("downloaded") == 2
    assert h.ctx.jobs.batch_pause(batch_id)["paused_scope"] == "process"
    assert h.ctx.jobs.is_batch_paused(batch_id, "process") and not h.ctx.jobs.is_batch_paused(batch_id)
    assert batch_id in h.ctx.jobs.claimable_batch_ids("download")
    assert batch_id not in h.ctx.jobs.claimable_batch_ids("process")


def test_breaker_scope_download_codes_do_not_stop_ffmpeg(harness_factory):
    h = harness_factory(downloader=FakeInstagramDownloader(items=[remote("Y0")]))
    batch_id = _import(h, limit=10)
    h.ctx.jobs.pause_batch(batch_id, reason="repeated_failures", code="content_unavailable", steps=(600.0,))
    assert h.ctx.jobs.is_batch_paused(batch_id)
    assert batch_id in h.ctx.jobs.claimable_batch_ids("process")
    assert batch_id not in h.ctx.jobs.claimable_batch_ids("download")


def test_keep_awake_uses_import_runner_has_active_work(harness, monkeypatch):
    calls = []
    monkeypatch.setattr(worker_module, "set_thread_execution_state", lambda keep: calls.append(keep) or True)
    h = harness
    h.downloader.items = [remote("K1")]
    _import(h, limit=10)
    h.ctx.import_runner.has_active_work = lambda: False  # ex.: só lote em needs_attention/pausa longa
    h.ctx.worker._keep_awake_if_due()
    assert calls == []
    h.ctx.import_runner.has_active_work = lambda: True
    h.ctx.worker._last_keep_awake_check = float("-inf")
    h.ctx.worker._keep_awake_if_due()
    assert calls == [True]


def test_keep_awake_uses_batch_repository_when_runner_has_no_helper(harness, monkeypatch):
    """Caminho real: o ImportRunner não tem has_active_work; vale BatchRepository.has_active_work()."""
    calls = []
    monkeypatch.setattr(worker_module, "set_thread_execution_state", lambda keep: calls.append(keep) or True)
    h = harness
    h.downloader.items = [remote("N1")]
    batch_id = _import(h, limit=10)
    assert not callable(getattr(h.ctx.import_runner, "has_active_work", None))
    h.ctx.db.execute("UPDATE batches SET status = 'listing', listing_state = 'needs_attention' WHERE id = ?",
                     (batch_id,))
    h.ctx.db.execute("UPDATE jobs SET status = 'completed' WHERE batch_id = ?", (batch_id,))
    h.ctx.worker._last_keep_awake_check = float("-inf")
    h.ctx.worker._keep_awake_if_due()
    assert True not in calls  # needs_attention não mantém o Windows acordado


def test_job_interrupted_three_times_in_a_row_fails(harness_factory):
    h = harness_factory(downloader=FakeInstagramDownloader(items=[remote("C0"), remote("C1")]))
    batch_id = _import(h, limit=10)
    crashing, healthy = _jobs(h, batch_id)

    def crash(job_id):
        h.ctx.db.execute("UPDATE jobs SET status = 'downloading', claimed_by = 'morto' WHERE id = ?", (job_id,))
        h.ctx.jobs.recover_interrupted()
        return h.ctx.jobs.get(job_id)

    assert crash(crashing["id"])["status"] == "pending"
    assert crash(crashing["id"])["status"] == "pending"
    assert crash(healthy["id"])["attempts"] == 0
    h.run_jobs()  # execução normal zera o contador do job saudável (o que crasha também roda aqui)
    assert h.ctx.db.scalar("SELECT crash_count FROM jobs WHERE id = ?", (healthy["id"],)) == 0
    h.ctx.db.execute("UPDATE jobs SET crash_count = 2, status = 'pending', finished_at = NULL WHERE id = ?",
                     (crashing["id"],))
    final = crash(crashing["id"])
    assert final["status"] == "failed" and final["error_code"] == "interrupted_repeatedly"
    assert final["attempts"] == 0
