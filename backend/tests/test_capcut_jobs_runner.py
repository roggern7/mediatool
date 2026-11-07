"""Legendas CapCut: executor (fila, etapas, espera, retentativa, cancelamento, recuperação) e migração 0029, com
processador falso injetado em `ctx.capcut_processor`."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest
from capcut_jobs_fakes import FakeCapCutError, FakeCapCutProcessor, add_media

from app.db.connection import Database
from app.db.migrate import apply_migrations, revert_migration, schema_version
from app.jobs.capcut_runner import CapCutRunner, recover_capcut_jobs, stage_percent
from app.services import capcut_service


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def setup(harness, tmp_path):
    processor = FakeCapCutProcessor()
    harness.ctx.capcut_processor = processor
    clock = Clock()
    runner = CapCutRunner(harness.ctx, clock=clock)
    harness.ctx.capcut_runner = runner
    out = tmp_path / "saida"
    made = []

    def make_job(**body) -> dict:
        media_id = add_media(harness, name=f"v{len(made)}")
        made.append(media_id)
        payload = {"media_ids": [media_id], "overrides": {"output_dir": str(out)}, **body}
        response = harness.client.post("/api/capcut/jobs", json=payload)
        assert response.status_code == 202, response.text
        return response.json()["created"][0]

    return harness, processor, runner, clock, make_job


def job(harness, job_id) -> dict:
    return capcut_service.repo(harness.ctx).get_job(job_id)


def test_stage_percent_weights():
    assert stage_percent("transcribing", 0) == 0 and stage_percent("transcribing", 1) == 35
    assert stage_percent("drafting", None) == 35 and stage_percent("exporting", 0.5) == 70
    assert stage_percent("cleaning", 1.0) == 100 and stage_percent("desconhecida", 0.5) == 0
    assert stage_percent("exporting", 7) == 90, "fração limitada a 0..1"


def test_success_records_output_transcript_and_spec(setup):
    harness, processor, runner, _, make_job = setup
    created = make_job()
    assert runner.run_next() is True
    done = job(harness, created["id"])
    assert done["status"] == "completed" and done["progress"] == 100 and done["attempts"] == 1
    assert done["output_path"].endswith("v0_legendado.mp4") and done["chunks"] == 1
    assert done["transcript"][0]["text"] == "olá" and done["warnings"] == ["aviso falso"]
    assert done["output_probe"]["width"] == 64 and done["finished_at"]
    [spec] = processor.specs
    assert spec.draft_name == f"MT_{created['id']}_m{created['media_id']}"
    assert spec.video_path == harness.ctx.storage.resolve("originals/test/v0.mp4")
    assert spec.work_dir == harness.settings.data_dir / "capcut" / "work" / str(created["id"])
    assert spec.models_dir == harness.settings.data_dir / "models" / "whisper"
    assert spec.style is None and spec.whisper_model == "large-v3-turbo" and spec.max_words == 3
    assert runner.run_next() is False
    api_view = harness.client.get(f"/api/capcut/jobs/{created['id']}").json()
    assert api_view["output_exists"] is True and api_view["output_name"] == "v0_legendado.mp4"


def test_progress_is_written_per_stage(setup):
    harness, processor, runner, _, make_job = setup
    created = make_job()
    seen = []
    processor.during = lambda spec, cancel: seen.append(dict(job(harness, created["id"])))
    runner.run_next()
    assert seen[0]["stage"] == "drafting" and seen[0]["progress"] == 35 and seen[0]["transcript"] is not None


def test_busy_waits_without_spending_attempt(setup):
    harness, processor, runner, clock, make_job = setup
    created = make_job()
    processor.errors = [FakeCapCutError("capcut_busy", "CapCut aberto.", transient=True)]
    runner.run_next()
    waiting = job(harness, created["id"])
    assert waiting["status"] == "waiting" and waiting["wait_reason"] == "capcut_busy" and waiting["attempts"] == 0
    assert waiting["next_attempt_at"] == "2026-11-07T12:00:30.000Z"
    assert runner.run_next() is False, "espera ainda não venceu"
    clock.advance(31)
    assert runner.run_next() is True
    assert job(harness, created["id"])["status"] == "completed"


def test_desktop_busy_waits_a_minute_without_spending_attempts(setup):
    """Dono usando o PC: espera em 'waiting' (desktop_busy) quantas vezes for preciso, sem nunca virar falha."""
    harness, processor, runner, clock, make_job = setup
    created = make_job()
    processor.errors = [FakeCapCutError("desktop_busy", "PC em uso.", transient=True) for _ in range(5)]
    for _ in range(5):
        runner.run_next()
        waiting = job(harness, created["id"])
        assert waiting["status"] == "waiting" and waiting["wait_reason"] == "desktop_busy"
        assert waiting["attempts"] == 0 and "PC está em uso" in waiting["detail"]
        assert runner.run_next() is False, "espera de 1 min ainda não venceu"
        clock.advance(61)
    assert runner.run_next() is True
    assert job(harness, created["id"])["status"] == "completed"


def test_transient_error_retries_with_backoff_then_fails(setup):
    harness, processor, runner, clock, make_job = setup
    created = make_job()
    processor.errors = [FakeCapCutError("ui_timeout", "O CapCut não respondeu.", transient=True) for _ in range(3)]
    runner.run_next()
    first = job(harness, created["id"])
    assert first["status"] == "pending" and first["attempts"] == 1 and first["error_code"] == "ui_timeout"
    assert first["next_attempt_at"] == "2026-11-07T12:00:30.000Z"
    clock.advance(30)
    runner.run_next()
    second = job(harness, created["id"])
    assert second["attempts"] == 2 and second["next_attempt_at"] == "2026-11-07T12:02:30.000Z"
    clock.advance(120)
    runner.run_next()
    final = job(harness, created["id"])
    assert final["status"] == "failed" and final["attempts"] == 3 and final["error_code"] == "ui_timeout"
    assert final["error_message"] == "O CapCut não respondeu."


def test_retry_reuses_transcript(setup):
    harness, processor, runner, _, make_job = setup
    created = make_job()
    processor.errors = [FakeCapCutError("export_failed", "Exportação falhou.")]
    runner.run_next()
    failed = job(harness, created["id"])
    assert failed["status"] == "failed" and failed["error_code"] == "export_failed" and failed["transcript"]
    assert harness.client.post(f"/api/capcut/jobs/{created['id']}/retry").json()["status"] == "pending"
    runner.run_next()
    assert processor.specs[-1].transcript == failed["transcript"]
    assert job(harness, created["id"])["status"] == "completed"


def test_unexpected_error_is_generic(setup):
    harness, processor, runner, _, make_job = setup
    created = make_job()
    processor.errors = [RuntimeError("C:\\segredo\\caminho")]
    runner.run_next()
    failed = job(harness, created["id"])
    assert failed["status"] == "failed" and failed["error_code"] == "unexpected_error"
    assert "segredo" not in failed["error_message"]


def test_source_missing_fails_without_calling_processor(setup):
    harness, processor, runner, _, make_job = setup
    created = make_job()
    harness.ctx.storage.resolve("originals/test/v0.mp4").unlink()
    runner.run_next()
    failed = job(harness, created["id"])
    assert failed["status"] == "failed" and failed["error_code"] == "source_missing" and processor.specs == []


def test_without_core_or_processor_fails_deps_missing(setup, monkeypatch):
    harness, _, runner, _, make_job = setup
    harness.ctx.capcut_processor = None

    def missing():
        raise ImportError("sem núcleo")

    monkeypatch.setattr(capcut_service, "load_core", missing)
    created = make_job()
    runner.run_next()
    assert job(harness, created["id"])["error_code"] == "deps_missing"


def test_cancel_running_job_sets_event(setup):
    harness, processor, runner, _, make_job = setup
    created = make_job()
    seen = {}

    def during(spec, cancel):
        response = harness.client.post(f"/api/capcut/jobs/{created['id']}/cancel")
        seen["status"] = response.json()["status"]
        seen["event"] = cancel.is_set()
        raise FakeCapCutError("cancelled", "Cancelado.")

    processor.during = during
    runner.run_next()
    assert seen == {"status": "running", "event": True}
    assert job(harness, created["id"])["status"] == "cancelled"


def test_stop_during_run_requeues(setup):
    harness, processor, runner, _, make_job = setup
    created = make_job()

    def during(spec, cancel):
        threading.Thread(target=runner.stop).start()
        assert cancel.wait(5)
        raise FakeCapCutError("cancelled", "Cancelado.")

    processor.during = during
    runner.run_next()
    requeued = job(harness, created["id"])
    assert requeued["status"] == "pending" and requeued["attempts"] == 0
    assert requeued["detail"] == "Retomado após interrupção"


def test_pause_blocks_claims(setup):
    harness, _, runner, _, make_job = setup
    created = make_job()
    harness.client.post("/api/capcut/queue/pause")
    assert runner.run_next() is False and job(harness, created["id"])["status"] == "pending"
    harness.client.post("/api/capcut/queue/resume")
    assert runner.run_next() is True


def test_recovery_on_startup(setup):
    harness, _, _, _, make_job = setup
    running = make_job()
    cancelling = make_job()
    repo = capcut_service.repo(harness.ctx)
    repo.update_job(running["id"], status="running", stage="exporting", progress=60)
    repo.update_job(cancelling["id"], status="running", cancel_requested=1)
    assert recover_capcut_jobs(harness.ctx) == 1
    resumed = job(harness, running["id"])
    assert resumed["status"] == "pending" and resumed["detail"] == "Retomado após interrupção"
    assert resumed["stage"] is None and resumed["progress"] == 0
    assert job(harness, cancelling["id"])["status"] == "cancelled"


def test_restart_recovers_through_lifespan(harness_factory, tmp_path):
    first = harness_factory()
    media_id = add_media(first)
    created = first.client.post("/api/capcut/jobs", json={"media_ids": [media_id]}).json()["created"][0]
    capcut_service.repo(first.ctx).update_job(created["id"], status="running")
    first.__exit__(None, None, None)
    second = harness_factory(root=first.root)
    assert capcut_service.repo(second.ctx).get_job(created["id"])["status"] == "pending"


def test_migration_0029_up_down(tmp_path):
    db = Database(tmp_path / "m.db")
    try:
        apply_migrations(db)
        assert schema_version(db) == 29
        tables = {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"capcut_jobs", "capcut_styles"} <= tables
        indexes = {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"ix_capcut_jobs_queue", "ix_capcut_jobs_media"} <= indexes
        now = "2026-01-01T00:00:00.000Z"
        db.execute("INSERT INTO media (id, source_platform, media_type, original_path, original_sha256, file_size,"
                   " created_at, updated_at) VALUES (1, 'local', 'video', 'x', ?, 1, ?, ?)", ("a" * 64, now, now))
        db.execute("INSERT INTO capcut_jobs (media_id, source_path, created_at, updated_at) VALUES (1, 'x', ?, ?)",
                   (now, now))
        db.execute("DELETE FROM media WHERE id = 1")
        assert db.scalar("SELECT media_id FROM capcut_jobs") is None, "ON DELETE SET NULL"
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE capcut_jobs SET status = 'x'")
        revert_migration(db, 29)
        assert schema_version(db) == 28
        tables = {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert not {"capcut_jobs", "capcut_styles"} & tables
        assert apply_migrations(db) == [29]
    finally:
        db.close_all()


# --------------------------------------------------------------------------------------------- correções da verificação

@pytest.mark.parametrize("error", [FakeCapCutError("capcut_busy", "Ocupado.", transient=True),
                                   FakeCapCutError("ui_timeout", "Demorou.", transient=True)])
def test_cancel_between_check_and_requeue_finishes_cancelled(setup, monkeypatch, error):
    """Cancelamento gravado DEPOIS de o executor ler `cancel_requested` e ANTES de voltar o job para a fila: a gravação
    condicional não devolve o job (ficaria preso em waiting/pending com cancel_requested=1)."""
    harness, processor, runner, _, make_job = setup
    created = make_job()
    repo = capcut_service.repo(harness.ctx)

    def during(spec, cancel):
        raise error

    processor.during = during
    real_check = runner.repo.cancel_requested

    calls = []

    def stale_check(job_id):
        value = real_check(job_id)
        calls.append(job_id)
        if len(calls) == 2:  # a leitura de `_handle_failure` (a 1ª é a do início de `_run`)
            repo.update_job(job_id, cancel_requested=1)  # o dono cancela exatamente agora
        return value

    monkeypatch.setattr(runner.repo, "cancel_requested", stale_check)
    runner.run_next()
    assert len(calls) == 2
    done = job(harness, created["id"])
    assert done["status"] == "cancelled" and done["finished_at"]
    assert harness.client.post("/api/capcut/jobs", json={
        "media_ids": [created["media_id"]], "overrides": {"output_dir": done["options"]["output_dir"]}}
    ).json()["created"], "a mídia não fica bloqueada"


def test_stop_with_late_cancel_finishes_cancelled(setup, monkeypatch):
    harness, processor, runner, _, make_job = setup
    created = make_job()
    repo = capcut_service.repo(harness.ctx)

    def during(spec, cancel):
        runner._stop.set()
        raise FakeCapCutError("cancelled", "Cancelado.")

    processor.during = during
    real_check = runner.repo.cancel_requested

    calls = []

    def stale_check(job_id):
        value = real_check(job_id)
        calls.append(job_id)
        if len(calls) == 2:
            repo.update_job(job_id, cancel_requested=1)
        return value

    monkeypatch.setattr(runner.repo, "cancel_requested", stale_check)
    runner.run_next()
    assert job(harness, created["id"])["status"] == "cancelled"


def test_cancel_api_is_idempotent_for_orphan_running_and_stuck_waiting(setup):
    harness, _, runner, _, make_job = setup
    orphan, stuck = make_job(), make_job()
    repo = capcut_service.repo(harness.ctx)
    repo.update_job(orphan["id"], status="running", cancel_requested=1)
    repo.update_job(stuck["id"], status="waiting", cancel_requested=1, wait_reason="capcut_busy")
    for item in (orphan, stuck):
        response = harness.client.post(f"/api/capcut/jobs/{item['id']}/cancel")
        assert response.status_code == 200 and response.json()["status"] == "cancelled"
        assert job(harness, item["id"])["finished_at"]


def test_recovery_cancels_open_jobs_with_cancel_and_never_exceeds_attempts(setup):
    harness, _, _, _, make_job = setup
    repo = capcut_service.repo(harness.ctx)
    waiting, pending, last_try = make_job(), make_job(), make_job()
    repo.update_job(waiting["id"], status="waiting", cancel_requested=1)
    repo.update_job(pending["id"], status="pending", cancel_requested=1)
    repo.update_job(last_try["id"], status="running", attempts=3, max_attempts=3)
    assert recover_capcut_jobs(harness.ctx) == 1
    assert job(harness, waiting["id"])["status"] == "cancelled"
    assert job(harness, pending["id"])["status"] == "cancelled"
    resumed = job(harness, last_try["id"])
    assert resumed["status"] == "pending" and resumed["attempts"] == 2


def test_delete_job_removes_work_dir(setup):
    harness, _, runner, _, make_job = setup
    created = make_job()
    runner.run_next()
    work = capcut_service.work_dir(harness.ctx, created["id"])
    work.mkdir(parents=True, exist_ok=True)
    (work / "exports.json").write_text("{}", encoding="utf-8")
    assert harness.client.delete(f"/api/capcut/jobs/{created['id']}").status_code == 204
    assert not work.exists()


def test_stop_waits_long_enough_for_capcut_shutdown():
    import inspect

    from app.jobs import capcut_runner

    assert inspect.signature(CapCutRunner.stop).parameters["timeout"].default == capcut_runner.STOP_TIMEOUT_SECONDS
    assert 40 < capcut_runner.STOP_TIMEOUT_SECONDS <= 50
