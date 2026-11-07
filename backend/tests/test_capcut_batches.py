"""Lotes das Legendas CapCut (migration 0031): criação, seleção por filtro, duplicação, espera da edição, modo só
edição, pausas, cancelamento, reprocessamento, recuperação, estimativa e ZIP. Processador do CapCut falso
(`capcut_jobs_fakes`) e worker com o processor falso do conftest."""

from __future__ import annotations

import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from capcut_jobs_fakes import FakeCapCutProcessor, add_media

from app.db.connection import Database
from app.db.migrate import apply_migrations, revert_migration, schema_version
from app.jobs.capcut_runner import CapCutRunner
from app.services import capcut_batch_service, capcut_service, system_open

EDIT = {"duration_mode": "30", "speed": 1.2, "sharpen": True, "start_seconds": 0}


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
    return harness, processor, runner, clock, out


def _videos(harness, count: int, prefix: str = "v") -> list[int]:
    return [add_media(harness, name=f"{prefix}{index}") for index in range(count)]


def _create(harness, out: Path, **body) -> dict:
    payload = {"mode": "caption", "caption": {"overrides": {"output_dir": str(out)}}, **body}
    response = harness.client.post("/api/capcut/batches", json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def _jobs(harness, batch_id: int) -> list[dict]:
    response = harness.client.get(f"/api/capcut/batches/{batch_id}/jobs")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _batch(harness, batch_id: int) -> dict:
    response = harness.client.get(f"/api/capcut/batches/{batch_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _row(harness, job_id: int) -> dict:
    return capcut_service.repo(harness.ctx).get_job(job_id)


# ---- criação ---------------------------------------------------------------------------------------------------------
def test_create_by_ids_snapshot_folder_and_order(setup):
    harness, _, runner, _, out = setup
    ids = _videos(harness, 3)
    created = _create(harness, out, media_ids=ids, name="  Meu lote  ")
    batch = created["batch"]
    assert created["skipped"] == []
    assert batch["name"] == "Meu lote" and batch["mode"] == "caption" and batch["status"] == "running"
    assert batch["output_dir"] == str(out.resolve() / f"Lote {batch['id']}") and batch["output_exists"] is False
    assert batch["counts"] == {"total": 3, "pending": 3, "running": 0, "waiting": 0, "held": 0, "completed": 0,
                               "failed": 0, "cancelled": 0}
    assert batch["summary"]["style_name"] == capcut_service.BUILTIN_STYLE_NAME and batch["summary"]["edit"] is None
    assert batch["summary"]["fps"] == "60" and batch["options"]["caption"]["fps"] == "60"
    jobs = _jobs(harness, batch["id"])
    assert [job["media_id"] for job in jobs] == ids, "ordem da fila = ordem dos ids"
    assert all(job["capcut_batch_id"] == batch["id"] and job["mode"] == "caption" and job["held"] is False
               and job["options"]["output_dir"] == batch["output_dir"] for job in jobs)
    assert runner.run_next() is True
    assert _row(harness, jobs[0]["id"])["output_path"].endswith(f"Lote {batch['id']}\\v0_legendado.mp4") or \
        _row(harness, jobs[0]["id"])["output_path"].endswith(f"Lote {batch['id']}/v0_legendado.mp4")
    assert _batch(harness, batch["id"])["output_exists"] is True


def test_default_name_and_not_video_cover_skipped(setup):
    harness, _, _, _, out = setup
    video = add_media(harness, name="a")
    image = add_media(harness, media_type="image", name="img")
    cover = add_media(harness, role="cover", name="capa")
    created = _create(harness, out, media_ids=[video, image, cover, 999_999])
    assert created["batch"]["name"].startswith(f"Lote {created['batch']['id']} · ")
    assert {(s["media_id"], s["reason"]) for s in created["skipped"]} == {
        (image, "not_video"), (cover, "cover"), (999_999, "media_not_found")}


def test_create_by_filter_takes_all_filtered_not_just_a_page(setup):
    harness, _, _, _, out = setup
    ids = _videos(harness, 65)
    add_media(harness, media_type="image", name="foto")
    add_media(harness, role="cover", name="capa")
    page = harness.client.get("/api/capcut/library", params={"limit": 60}).json()
    assert len(page["items"]) == 60 and page["total"] == 65 and page["next_before_id"] is not None
    assert page["items"][0]["capcut"] == {"done": 0, "last_output_name": None, "open": False}
    created = _create(harness, out, filter={"captioned": "any"})
    assert created["batch"]["counts"]["total"] == 65
    assert sorted(job["media_id"] for job in _jobs(harness, created["batch"]["id"])) == ids
    queued = harness.client.post("/api/capcut/batches", json={"mode": "caption", "filter": {"q": "v6"}})
    assert queued.status_code == 422 and queued.json()["code"] == "batch_empty", "todos já estão na fila"


def test_filter_captioned_and_library_marks(setup):
    harness, _, runner, _, out = setup
    first, second = _videos(harness, 2)
    _create(harness, out, media_ids=[first])
    assert runner.run_next() is True
    page = harness.client.get("/api/capcut/library").json()
    marks = {item["id"]: item["capcut"] for item in page["items"]}
    assert marks[first] == {"done": 1, "last_output_name": "v0_legendado.mp4", "open": False}
    assert marks[second]["done"] == 0
    only_new = harness.client.get("/api/capcut/library", params={"captioned": "no"}).json()
    assert [item["id"] for item in only_new["items"]] == [second]
    created = _create(harness, out, filter={"captioned": "yes"}, include_done=True)
    assert [job["media_id"] for job in _jobs(harness, created["batch"]["id"])] == [first]


def test_duplicates_open_done_and_include_done(setup):
    harness, _, runner, _, out = setup
    done_id, open_id, fresh_id = _videos(harness, 3)
    _create(harness, out, media_ids=[done_id])
    assert runner.run_next() is True
    _create(harness, out, media_ids=[open_id])
    created = _create(harness, out, media_ids=[done_id, open_id, fresh_id])
    assert {(s["media_id"], s["reason"]) for s in created["skipped"]} == {
        (done_id, "already_done"), (open_id, "already_queued")}
    assert [job["media_id"] for job in _jobs(harness, created["batch"]["id"])] == [fresh_id]
    again = _create(harness, out, media_ids=[done_id], include_done=True)
    assert again["skipped"] == [] and again["batch"]["counts"]["total"] == 1
    # Concluído em OUTRO modo não conta como feito (e nada mais está aberto).
    assert runner.run_next() and runner.run_next() and runner.run_next()
    other = _create(harness, out, mode="edit", media_ids=[done_id], edit=EDIT)
    assert other["skipped"] == [] and other["batch"]["counts"]["total"] == 1


def test_empty_batch_writes_nothing_and_too_large(setup, monkeypatch):
    harness, _, _, _, out = setup
    image = add_media(harness, media_type="image", name="img")
    response = harness.client.post("/api/capcut/batches", json={"mode": "caption", "media_ids": [image]})
    assert response.status_code == 422 and response.json()["code"] == "batch_empty"
    empty_filter = harness.client.post("/api/capcut/batches", json={"mode": "caption", "filter": {"q": "nada"}})
    assert empty_filter.json()["code"] == "batch_empty"
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM capcut_batches") == 0
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM capcut_jobs") == 0
    ids = _videos(harness, 3)
    monkeypatch.setattr(capcut_batch_service, "MAX_BATCH_VIDEOS", 2)
    for body in ({"media_ids": ids}, {"filter": {}}):
        response = harness.client.post("/api/capcut/batches", json={"mode": "caption", **body})
        assert response.status_code == 422 and response.json()["code"] == "batch_too_large"
        assert "3 selecionados" in response.json()["detail"]
    both = harness.client.post("/api/capcut/batches", json={"mode": "caption", "media_ids": ids, "filter": {}})
    assert both.status_code == 422


def test_edit_validation_errors(setup):
    harness, _, _, _, out = setup
    [media_id] = _videos(harness, 1)

    def code(body):
        response = harness.client.post("/api/capcut/batches", json={"media_ids": [media_id], **body})
        assert response.status_code in (404, 422), response.text
        return response.json()["code"]

    assert code({"mode": "edit_caption"}) == "edit_required"
    assert code({"mode": "edit", "edit": {"duration_mode": "original", "speed": 1.0, "sharpen": False}}) == \
        "edit_no_changes"
    assert code({"mode": "edit", "edit": {**EDIT, "preset_id": 999}}) == "preset_not_found"
    assert code({"mode": "caption", "caption": {"style_id": 999}}) == "style_not_found"
    assert code({"mode": "edit", "edit": {**EDIT, "speed": 2.5}}) == "validation_error"
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM capcut_batches") == 0
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'process'") == 0


# ---- edição ----------------------------------------------------------------------------------------------------------
def test_edit_params_applied_with_and_without_preset(setup):
    harness, _, _, _, out = setup
    first, second = _videos(harness, 2)
    created = _create(harness, out, mode="edit", media_ids=[first],
                      edit={"duration_mode": "custom", "custom_seconds": 12.5, "speed": 1.5, "sharpen": True,
                            "start_seconds": 45.25})
    job = _jobs(harness, created["batch"]["id"])[0]
    edit_job = harness.ctx.jobs.get(job["edit_job_id"])
    assert edit_job["kind"] == "process" and edit_job["preset_id"] is None and edit_job["preset_name"] == "Edição"
    assert edit_job["params"] == {"metadata_cleanup": False, "trim_start": 45.25, "mirror": False, "speed": 1.5,
                                  "auto_adjust": False, "reencode": True, "max_duration": 12.53, "sharpen": True,
                                  "audio_sync_start": True}
    edit_batch = harness.ctx.batches.get(edit_job["batch_id"])
    assert edit_batch["source"] == "library" and edit_batch["preset_id"] is None and edit_batch["process"]
    assert created["batch"]["summary"]["edit"] == "12,5 s · 1,5x · nitidez · início em 45,25 s"
    preset = harness.client.post("/api/presets", json={"name": "Meu preset", "params": {
        "metadata_cleanup": True, "trim_start": 0.5, "mirror": True, "speed": 1.05, "auto_adjust": True,
        "reencode": False, "zoom": 2.0, "sharpen": True}})
    assert preset.status_code == 201, preset.text
    with_preset = _create(harness, out, mode="edit_caption", media_ids=[second],
                          edit={"preset_id": preset.json()["id"], "duration_mode": "60", "speed": 0.5,
                                "sharpen": False, "start_seconds": 3})
    params = harness.ctx.jobs.get(_jobs(harness, with_preset["batch"]["id"])[0]["edit_job_id"])["params"]
    assert params == {"metadata_cleanup": True, "trim_start": 3.0, "mirror": True, "speed": 0.5, "auto_adjust": True,
                      "reencode": True, "zoom": 2.0, "max_duration": 60.03, "audio_sync_start": True}
    assert with_preset["batch"]["summary"]["edit"] == "Meu preset · 60 s · 0,5x · início em 3 s"


def test_edit_caption_waits_for_edit_and_uses_rendition(setup):
    harness, processor, runner, _, out = setup
    [media_id] = _videos(harness, 1)
    created = _create(harness, out, mode="edit_caption", media_ids=[media_id], edit=EDIT)
    [job] = _jobs(harness, created["batch"]["id"])
    assert job["edit_status"] == "pending" and job["detail"] == "Editando" and job["edit_progress"] is None
    assert runner.run_next() is False, "a edição ainda não terminou"
    assert _row(harness, job["id"])["status"] == "pending"
    harness.run_jobs()
    edit_job = harness.ctx.jobs.get(job["edit_job_id"])
    assert edit_job["status"] == "completed" and edit_job["rendition_id"]
    rendition = harness.ctx.media.get_rendition(edit_job["rendition_id"])
    assert runner.run_next() is True
    [spec] = processor.specs
    assert spec.video_path == harness.ctx.storage.resolve(rendition["processed_path"])
    assert spec.output_name == "v0_legendado" and spec.output_dir == Path(created["batch"]["output_dir"])
    done = _jobs(harness, created["batch"]["id"])[0]
    assert done["status"] == "completed" and done["edit_progress"] == 100 and done["detail"] is None
    assert _batch(harness, created["batch"]["id"])["status"] == "completed"


def test_edit_failure_fails_capcut_job_without_retries(setup):
    harness, processor, runner, _, out = setup
    [media_id] = _videos(harness, 1)
    created = _create(harness, out, mode="edit_caption", media_ids=[media_id], edit=EDIT)
    [job] = _jobs(harness, created["batch"]["id"])
    harness.ctx.jobs.update(job["edit_job_id"], status="failed", error_code="processing_failed",
                            error_message="O FFmpeg falhou.")
    assert runner.run_next() is True
    row = _row(harness, job["id"])
    assert row["status"] == "failed" and row["error_code"] == "edit_failed"
    assert row["error_message"] == "A edição deste vídeo falhou: O FFmpeg falhou." and row["attempts"] == 1
    assert processor.specs == [] and runner.run_next() is False
    assert _batch(harness, created["batch"]["id"])["status"] == "completed_with_errors"


def test_edit_only_copies_rendition_without_capcut(setup):
    harness, processor, runner, _, out = setup
    harness.ctx.capcut_processor = None   # sem CapCut e sem núcleo: o modo só edição não precisa deles
    [media_id] = _videos(harness, 1)
    first = _create(harness, out, mode="edit", media_ids=[media_id], edit=EDIT)
    harness.run_jobs()
    assert runner.run_next() is True
    [job] = _jobs(harness, first["batch"]["id"])
    assert job["status"] == "completed" and job["progress"] == 100 and job["output_name"] == "v0_editado.mp4"
    rendition = harness.ctx.media.get_rendition(harness.ctx.jobs.get(job["edit_job_id"])["rendition_id"])
    copied = Path(job["output_path"])
    assert copied.parent == Path(first["batch"]["output_dir"])
    assert copied.read_bytes() == harness.ctx.storage.resolve(rendition["processed_path"]).read_bytes()
    assert job["output_probe"]["width"] == 64 and job["output_exists"] is True
    assert processor.specs == []
    # Mesmo nome de novo na mesma pasta: não sobrescreve.
    harness.ctx.capcut_processor = processor
    folder = Path(first["batch"]["output_dir"])
    second_job = capcut_service.repo(harness.ctx).create_job(
        media_id=media_id, source_path="x", options={**_row(harness, job["id"])["options"]}, max_attempts=3,
        origin="manual", batch_id=None, capcut_batch_id=first["batch"]["id"], mode="edit",
        edit_job_id=job["edit_job_id"])
    assert runner.run_next() is True
    assert Path(_row(harness, second_job)["output_path"]) == folder / "v0_editado (2).mp4"
    assert copied.is_file() and processor.specs == []


def test_global_pause_holds_edit_only_jobs(setup):
    harness, _, runner, _, out = setup
    [media_id] = _videos(harness, 1)
    _create(harness, out, mode="edit", media_ids=[media_id], edit=EDIT)
    harness.run_jobs()
    capcut_service.set_queue_paused(harness.ctx, True)
    assert runner.run_next() is False
    capcut_service.set_queue_paused(harness.ctx, False)
    assert runner.run_next() is True


# ---- pausas ----------------------------------------------------------------------------------------------------------
def test_batch_pause_resume_and_individual_hold(setup):
    harness, _, runner, _, out = setup
    ids = _videos(harness, 3)
    batch = _create(harness, out, media_ids=ids[:2])["batch"]
    other = _create(harness, out, media_ids=ids[2:])["batch"]
    paused = harness.client.post(f"/api/capcut/batches/{batch['id']}/pause").json()
    assert paused["paused"] is True and paused["status"] == "paused"
    assert runner.run_next() is True, "o outro lote segue"
    assert _jobs(harness, other["id"])[0]["status"] == "completed"
    assert runner.run_next() is False
    resumed = harness.client.post(f"/api/capcut/batches/{batch['id']}/resume").json()
    assert resumed["paused"] is False and resumed["status"] == "running"
    first, second = _jobs(harness, batch["id"])
    held = harness.client.post(f"/api/capcut/jobs/{first['id']}/hold")
    assert held.status_code == 200 and held.json()["held"] is True
    status = harness.client.get("/api/capcut/status").json()
    assert status["queue"]["held"] == 1
    assert _batch(harness, batch["id"])["counts"]["held"] == 1
    assert runner.run_next() is True
    assert _row(harness, second["id"])["status"] == "completed" and _row(harness, first["id"])["status"] == "pending"
    assert runner.run_next() is False
    assert _batch(harness, batch["id"])["status"] == "paused", "só resta o job pausado"
    assert harness.client.post(f"/api/capcut/jobs/{second['id']}/hold").status_code == 409
    released = harness.client.post(f"/api/capcut/jobs/{first['id']}/release")
    assert released.status_code == 200 and released.json()["held"] is False
    assert runner.run_next() is True and _batch(harness, batch["id"])["status"] == "completed"


def test_hold_running_job_is_conflict(setup):
    harness, _, _, _, out = setup
    [media_id] = _videos(harness, 1)
    job = _jobs(harness, _create(harness, out, media_ids=[media_id])["batch"]["id"])[0]
    capcut_service.repo(harness.ctx).update_job(job["id"], status="running")
    response = harness.client.post(f"/api/capcut/jobs/{job['id']}/hold")
    assert response.status_code == 409 and response.json()["code"] == "capcut_job_not_holdable"


# ---- cancelamento e reprocessamento ----------------------------------------------------------------------------------
def test_cancel_batch_cancels_open_jobs_and_edits_keeps_completed(setup):
    harness, _, runner, _, out = setup
    ids = _videos(harness, 4)
    batch = _create(harness, out, mode="edit_caption", media_ids=ids, edit=EDIT)["batch"]
    jobs = _jobs(harness, batch["id"])
    harness.run_jobs()   # as 4 edições terminam
    assert runner.run_next() is True   # o 1º vídeo conclui
    capcut_service.repo(harness.ctx).update_job(jobs[1]["id"], status="running")   # em execução sem executor
    # Um 5º vídeo com a edição ainda na fila do worker.
    extra = add_media(harness, name="extra")
    late = _create(harness, out, mode="edit_caption", media_ids=[extra], edit=EDIT)["batch"]
    [late_job] = _jobs(harness, late["id"])
    cancelled = harness.client.post(f"/api/capcut/batches/{batch['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and cancelled["cancel_requested"] is True
    assert cancelled["counts"]["completed"] == 1 and cancelled["counts"]["cancelled"] == 3
    assert cancelled["finished_at"] is not None
    assert _row(harness, jobs[0]["id"])["status"] == "completed"
    assert all(_row(harness, job["id"])["status"] == "cancelled" for job in jobs[1:])
    late_cancel = harness.client.post(f"/api/capcut/batches/{late['id']}/cancel").json()
    assert late_cancel["counts"]["cancelled"] == 1
    assert harness.ctx.jobs.get(late_job["edit_job_id"])["status"] == "cancelled", "edição aberta cancelada"
    assert harness.ctx.jobs.get(jobs[0]["edit_job_id"])["status"] == "completed"
    assert runner.run_next() is False


def test_cancel_batch_signals_running_job_in_runner(setup):
    harness, processor, runner, _, out = setup
    ids = _videos(harness, 2)
    batch = _create(harness, out, media_ids=ids)["batch"]
    seen = {}

    def during(spec, cancel):
        seen["response"] = harness.client.post(f"/api/capcut/batches/{batch['id']}/cancel").json()
        seen["signalled"] = cancel.is_set()
        if cancel.is_set():
            from capcut_jobs_fakes import FakeCapCutError
            raise FakeCapCutError("cancelled", "Cancelado.")

    processor.during = during
    assert runner.run_next() is True
    assert seen["signalled"] is True
    final = _batch(harness, batch["id"])
    assert final["counts"]["cancelled"] == 2 and final["status"] == "cancelled" and final["finished_at"]
    assert runner.run_next() is False


def test_retry_failed_only_failures_and_recreates_failed_edit(setup):
    harness, processor, runner, _, out = setup
    ids = _videos(harness, 3)
    batch = _create(harness, out, mode="edit_caption", media_ids=ids, edit=EDIT)["batch"]
    jobs = _jobs(harness, batch["id"])
    harness.ctx.jobs.update(jobs[2]["edit_job_id"], status="failed", error_message="falhou")
    harness.run_jobs()
    assert runner.run_next() and runner.run_next() and runner.run_next()
    assert [_row(harness, job["id"])["status"] for job in jobs] == ["completed", "completed", "failed"]
    harness.client.post(f"/api/capcut/jobs/{jobs[1]['id']}/cancel")   # concluído: nada muda
    response = harness.client.post(f"/api/capcut/batches/{batch['id']}/retry-failed")
    assert response.status_code == 200 and response.json()["status"] == "running"
    assert response.json()["finished_at"] is None
    first, second, third = (_row(harness, job["id"]) for job in jobs)
    assert first["status"] == second["status"] == "completed"
    assert third["status"] == "pending" and third["attempts"] == 0 and third["error_code"] is None
    assert third["edit_job_id"] != jobs[2]["edit_job_id"], "edição refeita num job novo"
    new_edit = harness.ctx.jobs.get(third["edit_job_id"])
    old_edit = harness.ctx.jobs.get(jobs[2]["edit_job_id"])
    assert new_edit["status"] == "pending" and new_edit["params"] == old_edit["params"]
    assert new_edit["batch_id"] == old_edit["batch_id"]
    assert runner.run_next() is False, "espera a nova edição"
    harness.run_jobs()
    assert runner.run_next() is True and _row(harness, jobs[2]["id"])["status"] == "completed"
    assert len(processor.specs) == 3
    again = harness.client.post(f"/api/capcut/batches/{batch['id']}/retry-failed")
    assert again.status_code == 409 and again.json()["code"] == "nothing_to_retry"


def test_individual_retry_after_batch_cancel_runs_again(setup):
    harness, _, runner, _, out = setup
    ids = _videos(harness, 2)
    batch = _create(harness, out, media_ids=ids)["batch"]
    harness.client.post(f"/api/capcut/batches/{batch['id']}/cancel")
    first = _jobs(harness, batch["id"])[0]
    assert harness.client.post(f"/api/capcut/jobs/{first['id']}/retry").status_code == 200
    assert runner.run_next() is True and _row(harness, first["id"])["status"] == "completed"
    assert _batch(harness, batch["id"])["status"] == "completed_with_errors"


def test_delete_batch_hides_only_when_finished(setup):
    harness, _, runner, _, out = setup
    [media_id] = _videos(harness, 1)
    batch = _create(harness, out, media_ids=[media_id])["batch"]
    busy = harness.client.delete(f"/api/capcut/batches/{batch['id']}")
    assert busy.status_code == 409 and busy.json()["code"] == "capcut_batch_active"
    assert runner.run_next() is True
    output = Path(_jobs(harness, batch["id"])[0]["output_path"])
    assert harness.client.delete(f"/api/capcut/batches/{batch['id']}").status_code == 204
    assert harness.client.get(f"/api/capcut/batches/{batch['id']}").status_code == 404
    assert harness.client.get("/api/capcut/batches").json()["total"] == 0
    assert output.is_file(), "os arquivos ficam"


def test_list_batches_filters(setup):
    harness, _, runner, _, out = setup
    ids = _videos(harness, 3)
    done = _create(harness, out, media_ids=ids[:1], name="Primeiro")["batch"]
    assert runner.run_next() is True
    _create(harness, out, mode="edit", media_ids=ids[1:2], edit=EDIT, name="Segundo")
    third = _create(harness, out, media_ids=ids[2:], name="Terceiro 100%")["batch"]
    harness.client.post(f"/api/capcut/batches/{third['id']}/pause")

    def names(**params):
        body = harness.client.get("/api/capcut/batches", params=params).json()
        return [item["name"] for item in body["items"]], body["total"]

    assert names() == (["Terceiro 100%", "Segundo", "Primeiro"], 3)
    assert names(status="completed") == (["Primeiro"], 1)
    assert names(status="paused") == (["Terceiro 100%"], 1)
    assert names(mode="edit") == (["Segundo"], 1)
    assert names(q="100%") == (["Terceiro 100%"], 1)
    assert names(q="seg") == (["Segundo"], 1)
    assert _batch(harness, done["id"])["progress"] == 100


# ---- recuperação, estimativa, ZIP, pasta --------------------------------------------------------------------------
def test_restart_recovers_running_job_keeping_batch_link_and_options(harness_factory, tmp_path):
    first = harness_factory()
    [media_id] = [add_media(first, name="r0")]
    created = first.client.post("/api/capcut/batches", json={
        "mode": "edit_caption", "media_ids": [media_id], "edit": EDIT,
        "caption": {"overrides": {"output_dir": str(tmp_path / "out"), "fps": "30"}}}).json()
    batch = created["batch"]
    job = _jobs(first, batch["id"])[0]
    capcut_service.repo(first.ctx).update_job(job["id"], status="running", attempts=1)
    first.__exit__(None, None, None)
    second = harness_factory(root=first.root)
    row = capcut_service.repo(second.ctx).get_job(job["id"])
    assert row["status"] == "pending" and row["attempts"] == 0 and row["detail"] == "Retomado após interrupção"
    assert row["capcut_batch_id"] == batch["id"] and row["edit_job_id"] == job["edit_job_id"]
    assert row["mode"] == "edit_caption" and row["options"]["fps"] == "30"
    assert row["options"]["edit_params"]["speed"] == 1.2
    again = _batch(second, batch["id"])
    assert again["options"] == batch["options"] and again["summary"] == batch["summary"]
    assert again["status"] == "running"


def test_estimate_from_batch_then_mode_history(setup):
    harness, _, runner, clock, out = setup
    assert harness.client.get("/api/capcut/estimate", params={"mode": "caption", "count": 5}).json() == {
        "per_video_seconds": None, "total_seconds": None, "samples": 0}
    ids = _videos(harness, 4)
    old = _create(harness, out, media_ids=ids[:2])["batch"]
    repo = capcut_service.repo(harness.ctx)
    for index, job in enumerate(_jobs(harness, old["id"])):
        repo.update_job(job["id"], status="completed", started_at="2026-11-07T10:00:00.000Z",
                        finished_at=f"2026-11-07T10:0{index + 1}:00.000Z")   # 60 s e 120 s
    estimate = harness.client.get("/api/capcut/estimate", params={"mode": "caption", "count": 10}).json()
    assert estimate == {"per_video_seconds": 90.0, "total_seconds": 900.0, "samples": 2}
    assert harness.client.get("/api/capcut/estimate", params={"mode": "edit"}).json()["samples"] == 0
    batch = _create(harness, out, media_ids=ids[2:])["batch"]
    assert batch["per_video_seconds"] == 90.0 and batch["eta_seconds"] == 180.0, "sem concluído no lote: histórico"
    jobs = _jobs(harness, batch["id"])
    repo.update_job(jobs[0]["id"], status="completed", started_at="2026-11-07T10:00:00.000Z",
                    finished_at="2026-11-07T10:00:30.000Z")
    current = _batch(harness, batch["id"])
    assert current["per_video_seconds"] == 30.0 and current["eta_seconds"] == 30.0
    assert current["progress"] == 50 and current["elapsed_seconds"] is not None


def test_zip_contains_only_existing_completed_outputs(setup):
    harness, processor, runner, _, out = setup
    ids = _videos(harness, 3)
    batch = _create(harness, out, media_ids=ids)["batch"]
    nothing = harness.client.get(f"/api/capcut/batches/{batch['id']}/zip")
    assert nothing.status_code == 404 and nothing.json()["code"] == "batch_no_results"
    assert runner.run_next() and runner.run_next()
    jobs = _jobs(harness, batch["id"])
    capcut_service.repo(harness.ctx).update_job(jobs[2]["id"], status="failed")
    Path(jobs[1]["output_path"]).unlink()
    response = harness.client.get(f"/api/capcut/batches/{batch['id']}/zip")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert f"lote-{batch['id']}.zip" in response.headers["content-disposition"]
    archive = zipfile.ZipFile(BytesIO(response.content))
    assert archive.namelist() == ["v0_legendado.mp4"]
    assert archive.getinfo("v0_legendado.mp4").compress_type == zipfile.ZIP_STORED
    assert list((harness.settings.data_dir / "tmp").glob("*.zip")) == [], "temporário apagado"


def test_reveal_batch_folder(setup, monkeypatch):
    harness, _, runner, _, out = setup
    [media_id] = _videos(harness, 1)
    batch = _create(harness, out, media_ids=[media_id])["batch"]
    missing = harness.client.post(f"/api/capcut/batches/{batch['id']}/reveal")
    assert missing.status_code == 404 and missing.json()["code"] == "batch_folder_not_found"
    assert runner.run_next() is True
    opened = []
    monkeypatch.setattr(system_open, "open_folder", lambda path, allowed_roots: opened.append((path, allowed_roots)))
    assert harness.client.post(f"/api/capcut/batches/{batch['id']}/reveal").json() == {"ok": True}
    assert opened == [(Path(batch["output_dir"]), [Path(batch["output_dir"])])]


def test_old_jobs_keep_working_without_batch(setup):
    harness, processor, runner, _, out = setup
    [media_id] = _videos(harness, 1)
    response = harness.client.post("/api/capcut/jobs", json={"media_ids": [media_id],
                                                              "overrides": {"output_dir": str(out)}})
    job = response.json()["created"][0]
    assert job["capcut_batch_id"] is None and job["mode"] == "caption" and job["held"] is False
    assert job["edit_job_id"] is None and job["edit_status"] is None
    assert runner.run_next() is True
    assert _row(harness, job["id"])["output_path"] == str(out.resolve() / "v0_legendado.mp4")


def test_migration_0031_up_down(tmp_path):
    db = Database(tmp_path / "m.db")
    try:
        apply_migrations(db)
        assert schema_version(db) == 31
        now = "2026-01-01T00:00:00.000Z"
        db.execute("INSERT INTO capcut_jobs (source_path, created_at, updated_at) VALUES ('x', ?, ?)", (now, now))
        row = db.fetchone("SELECT mode, held, capcut_batch_id, edit_job_id FROM capcut_jobs")
        assert dict(row) == {"mode": "caption", "held": 0, "capcut_batch_id": None, "edit_job_id": None}
        db.execute("INSERT INTO capcut_batches (name, mode, output_dir, created_at, updated_at)"
                   " VALUES ('L', 'edit', 'C:/x', ?, ?)", (now, now))
        db.execute("UPDATE capcut_jobs SET capcut_batch_id = 1")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO capcut_batches (name, mode, output_dir, created_at, updated_at)"
                       " VALUES ('L', 'outro', 'C:/x', ?, ?)", (now, now))
        db.execute("DELETE FROM capcut_batches")
        assert db.scalar("SELECT capcut_batch_id FROM capcut_jobs") is None, "ON DELETE SET NULL"
        indexes = {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "ix_capcut_jobs_batch" in indexes
        revert_migration(db, 31)
        assert schema_version(db) == 30
        columns = {row["name"] for row in db.fetchall("PRAGMA table_info(capcut_jobs)")}
        assert not {"capcut_batch_id", "mode", "edit_job_id", "held"} & columns
        assert db.scalar("SELECT COUNT(*) FROM capcut_jobs") == 1, "linhas existentes ficam"
        tables = {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "capcut_batches" not in tables
        assert db.fetchall("PRAGMA foreign_key_check") == [] and db.scalar("PRAGMA integrity_check") == "ok"
        assert apply_migrations(db) == [31]
    finally:
        db.close_all()


# ---- correções da verificação ----------------------------------------------------------------------------------------
def _runner_for(harness) -> CapCutRunner:
    harness.ctx.capcut_processor = FakeCapCutProcessor()
    runner = CapCutRunner(harness.ctx)
    harness.ctx.capcut_runner = runner
    return runner


def test_held_job_cancelled_then_retried_is_claimed_again(setup):
    """D1: cancelar zera a pausa individual; o retry devolve o job à fila sem `held`."""
    harness, _, runner, _, out = setup
    ids = _videos(harness, 2)
    batch = _create(harness, out, media_ids=ids[:1])["batch"]
    [job] = _jobs(harness, batch["id"])
    harness.client.post(f"/api/capcut/jobs/{job['id']}/hold")
    harness.client.post(f"/api/capcut/batches/{batch['id']}/cancel")
    assert _row(harness, job["id"])["held"] == 0 and _row(harness, job["id"])["status"] == "cancelled"
    retried = harness.client.post(f"/api/capcut/batches/{batch['id']}/retry-failed").json()
    assert retried["status"] == "running" and retried["counts"]["held"] == 0 and retried["counts"]["pending"] == 1
    assert runner.run_next() is True and _row(harness, job["id"])["status"] == "completed"
    # Cancelamento e retry individuais.
    single = _jobs(harness, _create(harness, out, media_ids=ids[1:])["batch"]["id"])[0]
    harness.client.post(f"/api/capcut/jobs/{single['id']}/hold")
    assert harness.client.post(f"/api/capcut/jobs/{single['id']}/cancel").json()["held"] is False
    assert harness.client.post(f"/api/capcut/jobs/{single['id']}/retry").json()["held"] is False
    assert runner.run_next() is True and _row(harness, single["id"])["status"] == "completed"


def test_held_reset_by_retry_even_if_left_set(setup):
    """D1: `retry` zera `held` mesmo se a marca ficou num job encerrado."""
    harness, _, runner, _, out = setup
    [media_id] = _videos(harness, 1)
    [job] = _jobs(harness, _create(harness, out, media_ids=[media_id])["batch"]["id"])
    capcut_service.repo(harness.ctx).update_job(job["id"], status="failed", held=1)
    assert harness.client.post(f"/api/capcut/jobs/{job['id']}/retry").json()["held"] is False
    assert runner.run_next() is True


def test_restart_finishes_interrupted_batch_cancel_and_retry_clears_it(harness_factory, tmp_path):
    """D2: queda depois de gravar o cancelamento do lote e antes de cancelar os jobs."""
    first = harness_factory()
    ids = [add_media(first, name=f"c{index}") for index in range(3)]
    created = first.client.post("/api/capcut/batches", json={
        "mode": "caption", "media_ids": ids, "caption": {"overrides": {"output_dir": str(tmp_path / "out")}}}).json()
    batch_id = created["batch"]["id"]
    jobs = _jobs(first, batch_id)
    repo = capcut_service.repo(first.ctx)
    repo.update_job(jobs[0]["id"], status="running")
    repo.update_job(jobs[1]["id"], held=1)
    repo.update_batch(batch_id, cancel_requested=1)
    first.__exit__(None, None, None)
    second = harness_factory(root=first.root)
    rows = [capcut_service.repo(second.ctx).get_job(job["id"]) for job in jobs]
    assert [row["status"] for row in rows] == ["cancelled"] * 3 and all(row["held"] == 0 for row in rows)
    batch = _batch(second, batch_id)
    assert batch["status"] == "cancelled" and batch["finished_at"] is not None
    runner = _runner_for(second)
    assert runner.run_next() is False
    retried = second.client.post(f"/api/capcut/batches/{batch_id}/retry-failed").json()
    assert retried["cancel_requested"] is False and retried["status"] == "running"
    assert runner.run_next() and runner.run_next() and runner.run_next()
    assert _batch(second, batch_id)["status"] == "completed"


def test_cancel_finished_batch_is_conflict_and_writes_nothing(setup):
    """D3."""
    harness, _, runner, _, out = setup
    [media_id] = _videos(harness, 1)
    batch = _create(harness, out, media_ids=[media_id])["batch"]
    assert runner.run_next() is True
    response = harness.client.post(f"/api/capcut/batches/{batch['id']}/cancel")
    assert response.status_code == 409 and response.json()["code"] == "capcut_batch_finished"
    after = _batch(harness, batch["id"])
    assert after["status"] == "completed" and after["cancel_requested"] is False


def test_retry_redoes_edit_when_rendition_file_is_gone(setup):
    """D5: edição concluída mas o arquivo da rendition sumiu."""
    harness, _, runner, _, out = setup
    [media_id] = _videos(harness, 1)
    batch = _create(harness, out, mode="edit_caption", media_ids=[media_id], edit=EDIT)["batch"]
    [job] = _jobs(harness, batch["id"])
    harness.run_jobs()
    old_edit = harness.ctx.jobs.get(job["edit_job_id"])
    rendition = harness.ctx.media.get_rendition(old_edit["rendition_id"])
    harness.ctx.storage.resolve(rendition["processed_path"]).unlink()
    assert runner.run_next() is True
    assert _row(harness, job["id"])["error_code"] == "source_missing"
    harness.client.post(f"/api/capcut/batches/{batch['id']}/retry-failed")
    new_edit_id = _row(harness, job["id"])["edit_job_id"]
    assert new_edit_id != old_edit["id"] and harness.ctx.jobs.get(new_edit_id)["status"] == "pending"
    assert runner.run_next() is False
    harness.run_jobs()
    assert runner.run_next() is True and _row(harness, job["id"])["status"] == "completed"
    new_rendition = harness.ctx.media.get_rendition(harness.ctx.jobs.get(new_edit_id)["rendition_id"])
    assert harness.ctx.storage.resolve(new_rendition["processed_path"]).is_file()


def test_retry_redoes_edit_that_is_being_cancelled(setup):
    """D5: edição ainda aberta com cancelamento pedido."""
    harness, _, _, _, out = setup
    [media_id] = _videos(harness, 1)
    batch = _create(harness, out, mode="edit_caption", media_ids=[media_id], edit=EDIT)["batch"]
    [job] = _jobs(harness, batch["id"])
    harness.ctx.jobs.update(job["edit_job_id"], cancel_requested=1)
    capcut_service.repo(harness.ctx).update_job(job["id"], status="failed", error_code="x")
    assert harness.client.post(f"/api/capcut/jobs/{job['id']}/retry").status_code == 200
    new_edit_id = _row(harness, job["id"])["edit_job_id"]
    assert new_edit_id != job["edit_job_id"]
    assert harness.ctx.jobs.get(new_edit_id)["cancel_requested"] == 0
    # Edição aberta SEM cancelamento pedido: não é refeita.
    capcut_service.repo(harness.ctx).update_job(job["id"], status="failed")
    harness.client.post(f"/api/capcut/jobs/{job['id']}/retry")
    assert _row(harness, job["id"])["edit_job_id"] == new_edit_id


def test_zip_checks_space_and_cleans_temporaries(setup, monkeypatch, harness_factory):
    """D6: sem espaço => 507 sem temporário; falha no meio apaga o temporário; o startup apaga as sobras."""
    from app.services import export_zip

    harness, _, runner, _, out = setup
    [media_id] = _videos(harness, 1)
    batch = _create(harness, out, media_ids=[media_id])["batch"]
    assert runner.run_next() is True
    temp = harness.settings.data_dir / "tmp"
    with monkeypatch.context() as patch:
        patch.setattr(export_zip, "free_bytes", lambda path: 1024)
        response = harness.client.get(f"/api/capcut/batches/{batch['id']}/zip")
        assert response.status_code == 507 and response.json()["code"] == "export_no_space"
        assert "Sem espaço" in response.json()["detail"]
        assert not temp.exists() or list(temp.iterdir()) == []
    with monkeypatch.context() as patch:
        patch.setattr(export_zip, "free_bytes", lambda path: -1)

        def broken(self, *args, **kwargs):
            raise OSError("disco cheio")

        patch.setattr(zipfile.ZipFile, "write", broken)
        with pytest.raises(OSError):
            capcut_batch_service.build_zip(harness.ctx, batch["id"])
        assert list(temp.iterdir()) == [], "temporário apagado na falha"
    leftover = temp / "capcut-lote-7-0123456789ab.zip"
    other = temp / "outro.zip"
    leftover.write_bytes(b"x")
    other.write_bytes(b"y")
    root = harness.root
    harness.__exit__(None, None, None)
    harness_factory(root=root)
    assert not leftover.exists() and other.exists(), "só os temporários do ZIP de lote"
