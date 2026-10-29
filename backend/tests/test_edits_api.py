"""POST /api/edits: lote de edição (preset_id NULL, preset_name "Edição") com o FakeProcessor."""

from __future__ import annotations

from pathlib import Path

import pytest
from media_processor import ProcessingFailedError

from app.services.edit_service import EDIT_DURATION_MARGIN, EDIT_PRESET_NAME
from media_fakes import FakeProcessor, remote

SETTINGS = {"duration_mode": "15", "custom_seconds": None, "speed": 1.25, "sharpen": True, "output_type": "mp4"}


def _library(harness) -> dict[str, int]:
    harness.downloader.items = [remote("V1", media_type="video"), remote("V2", media_type="video"),
                                remote("V3", media_type="video"), remote("IMG")]
    harness.import_profile()
    harness.run_all()
    return {m["source_post_id"]: m["id"] for m in harness.client.get("/api/media").json()["items"]}


def _post(harness, media_ids, **settings):
    return harness.client.post("/api/edits", json={"media_ids": media_ids, "settings": {**SETTINGS, **settings}})


def _processed_files(harness) -> list[Path]:
    root = harness.ctx.storage.root / "processed"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


@pytest.fixture
def lib(harness):
    return harness, _library(harness)


def test_edit_batch_created_and_processed(lib):
    harness, media = lib
    ids = [media["V1"], media["V2"], media["V3"]]
    originals = {i: harness.ctx.storage.resolve(harness.ctx.media.get(i)["original_path"]).read_bytes() for i in ids}
    response = _post(harness, ids)
    assert response.status_code == 202, response.text
    batch = response.json()
    assert (batch["preset_id"], batch["preset_name"], batch["source"], batch["process"]) == (
        None, EDIT_PRESET_NAME, "library", True)
    assert batch["found"] == 3

    jobs = harness.batch(batch["id"])["jobs"]
    assert len(jobs) == 3 and {j["media_id"] for j in jobs} == set(ids)
    assert all(j["kind"] == "process" and j["preset_id"] is None and j["preset_name"] == "Edição" for j in jobs)
    params = harness.ctx.jobs.get(jobs[0]["id"])["params"]
    assert params == {"metadata_cleanup": False, "trim_start": 0.0, "mirror": False, "speed": 1.25,
                      "auto_adjust": False, "reencode": True, "max_duration": 15.03, "sharpen": True,
                      "audio_sync_start": True}
    assert harness.ctx.batches.get(batch["id"])["params"] == params

    harness.run_jobs()
    done = harness.batch(batch["id"])
    assert done["status"] == "completed" and done["counts"]["completed"] == 3
    assert all(j["stage"] == "done" and j["rendition_id"] for j in done["jobs"])
    for media_id in ids:
        item = harness.client.get(f"/api/media/{media_id}").json()
        assert [(r["preset_id"], r["preset_name"]) for r in item["renditions"]] == [(None, "Edição")]
        path = harness.ctx.storage.resolve(harness.ctx.media.get(media_id)["original_path"])
        assert path.read_bytes() == originals[media_id]  # original intacto
    assert len(_processed_files(harness)) == 3


@pytest.mark.parametrize("settings, expected", [
    ({"duration_mode": "custom", "custom_seconds": 30}, 30.03),
    ({"duration_mode": "custom", "custom_seconds": 12.5}, 12.53),
    ({"duration_mode": "30"}, 30.03),
    ({"duration_mode": "60"}, 60.03),
])
def test_duration_margin(lib, settings, expected):
    harness, media = lib
    response = _post(harness, [media["V1"]], **settings)
    assert response.status_code == 202, response.text
    job = harness.batch(response.json()["id"])["jobs"][0]
    assert harness.ctx.jobs.get(job["id"])["params"]["max_duration"] == expected
    assert EDIT_DURATION_MARGIN == 0.03


def test_original_duration_and_neutral_keys_omitted(lib):
    harness, media = lib
    response = _post(harness, [media["V1"]], duration_mode="original", speed=1.0, sharpen=False, output_type="mp4")
    assert response.status_code == 202, response.text
    job = harness.batch(response.json()["id"])["jobs"][0]
    params = harness.ctx.jobs.get(job["id"])["params"]
    assert params == {"metadata_cleanup": False, "trim_start": 0.0, "mirror": False, "speed": 1.0,
                      "auto_adjust": False, "reencode": True, "audio_sync_start": True}
    # custom_seconds ignorado fora do modo personalizado
    response = _post(harness, [media["V1"]], duration_mode="15", custom_seconds=99, output_type="auto")
    params = harness.ctx.jobs.get(harness.batch(response.json()["id"])["jobs"][0]["id"])["params"]
    assert params["max_duration"] == 15.03 and params["reencode"] is False


def test_no_changes(lib):
    harness, media = lib
    response = _post(harness, [media["V1"]], duration_mode="original", speed=1.0, sharpen=False, output_type="auto")
    assert response.status_code == 422
    assert response.json()["code"] == "edit_no_changes" and response.json()["detail"] == "Nenhuma edição selecionada."
    assert harness.ctx.batches.list(limit=50, offset=0, source="library")[1] == 0


def test_requires_video_and_not_found(lib):
    harness, media = lib
    response = _post(harness, [media["V1"], media["IMG"]])
    assert response.status_code == 422 and response.json()["code"] == "edit_requires_video"
    assert str(media["IMG"]) in response.json()["detail"]
    response = _post(harness, [media["V1"], 99999])
    assert response.status_code == 404 and response.json()["code"] == "media_not_found"
    assert harness.ctx.batches.list(limit=50, offset=0, source="library")[1] == 0


@pytest.mark.parametrize("change", [
    {"speed": 1.6}, {"speed": 0.9}, {"speed": "1.2"}, {"speed": None},
    {"duration_mode": "custom", "custom_seconds": 0}, {"duration_mode": "custom", "custom_seconds": -1},
    {"duration_mode": "custom", "custom_seconds": 7200.5}, {"duration_mode": "custom", "custom_seconds": None},
    {"duration_mode": "custom", "custom_seconds": "30"},
    {"duration_mode": "45"}, {"duration_mode": 15}, {"sharpen": "true"}, {"sharpen": 1}, {"output_type": "mkv"},
    {"extra": 1},
])
def test_settings_validation(lib, change):
    harness, media = lib
    response = _post(harness, [media["V1"]], **change)
    assert response.status_code == 422, change
    assert response.json()["code"] == "validation_error"


def test_request_validation(lib):
    harness, media = lib
    client = harness.client
    bad = [
        {"media_ids": [], "settings": SETTINGS},
        {"media_ids": list(range(1, 502)), "settings": SETTINGS},
        {"media_ids": [0], "settings": SETTINGS},
        {"media_ids": ["1"], "settings": SETTINGS},
        {"media_ids": [media["V1"]]},
        {"media_ids": [media["V1"]], "settings": SETTINGS, "extra": True},
        {"media_ids": [media["V1"]], "settings": {k: v for k, v in SETTINGS.items() if k != "speed"}},
    ]
    for body in bad:
        response = client.post("/api/edits", json=body)
        assert response.status_code == 422 and response.json()["code"] == "validation_error", body
    # 500 é o teto aceito (ids inexistentes -> 404, não validação)
    response = client.post("/api/edits", json={"media_ids": list(range(100000, 100500)), "settings": SETTINGS})
    assert response.status_code == 404 and response.json()["code"] == "media_not_found"


def test_integer_speed_is_canonical(lib):
    harness, media = lib
    first = _post(harness, [media["V1"]], speed=1, sharpen=False, output_type="mp4")
    second = _post(harness, [media["V1"]], speed=1.0, sharpen=False, output_type="mp4")
    jobs = [harness.batch(r.json()["id"])["jobs"][0]["id"] for r in (first, second)]
    assert [harness.ctx.jobs.get(j)["params"] for j in jobs][0] == harness.ctx.jobs.get(jobs[1])["params"]
    assert harness.ctx.jobs.get(jobs[0])["params"]["speed"] == 1.0


def test_identical_edit_reuses_rendition(lib):
    harness, media = lib
    ids = [media["V1"], media["V2"]]
    first = _post(harness, ids)
    harness.run_jobs()
    files = _processed_files(harness)
    assert len(files) == 2
    first_jobs = {j["media_id"]: j["rendition_id"] for j in harness.batch(first.json()["id"])["jobs"]}

    second = _post(harness, ids)
    assert second.status_code == 202
    harness.run_jobs()
    batch = harness.batch(second.json()["id"])
    assert batch["status"] == "completed"
    assert all(j["stage"] == "skipped" and j["rendition_id"] == first_jobs[j["media_id"]] for j in batch["jobs"])
    assert _processed_files(harness) == files  # nenhum arquivo novo
    # configuração diferente gera outra rendition
    _post(harness, [media["V1"]], speed=1.5)
    harness.run_jobs()
    assert len(_processed_files(harness)) == 3
    assert len(harness.client.get(f"/api/media/{media['V1']}").json()["renditions"]) == 2


class FailingOneProcessor(FakeProcessor):
    """Falha só para o original cujo conteúdo contém `marker`."""

    def __init__(self, marker: bytes):
        super().__init__()
        self.marker = marker

    def process(self, src, dst, opts, **kwargs):
        if self.marker in Path(src).read_bytes():
            raise ProcessingFailedError("duração final fora da tolerância: teste")
        return super().process(src, dst, opts, **kwargs)


def test_individual_failure_does_not_stop_batch(harness_factory):
    harness = harness_factory(processor=FailingOneProcessor(b"V2:0"))
    media = _library(harness)
    ids = [media["V1"], media["V2"], media["V3"]]
    response = _post(harness, ids)
    assert response.status_code == 202
    harness.run_jobs()
    batch = harness.batch(response.json()["id"])
    by_media = {j["media_id"]: j for j in batch["jobs"]}
    assert by_media[media["V2"]]["status"] == "failed"
    assert "duração final fora da tolerância" in (by_media[media["V2"]]["error_message"] or "")
    assert by_media[media["V1"]]["status"] == by_media[media["V3"]]["status"] == "completed"
    assert batch["counts"]["completed"] == 2 and batch["counts"]["failed"] == 1
    assert batch["status"] in ("completed", "failed")
    temp = harness.ctx.storage.root / "temp"
    assert not temp.exists() or not any(p.name.startswith("job-") for p in temp.iterdir())


def test_edit_batch_exports_the_edited_versions_as_zip(lib):
    """Fluxo Editar -> ZIP: o lote da edição exporta só as versões editadas (bytes das renditions, nunca o original)."""
    import io
    import zipfile

    harness, media = lib
    ids = [media["V1"], media["V2"]]
    batch_id = _post(harness, ids).json()["id"]
    harness.run_jobs()
    response = harness.client.post("/api/exports", json={
        "source": {"kind": "batch", "id": batch_id}, "destination": {"zip": True}, "content": "edited"})
    assert response.status_code == 202, response.text
    assert (response.json()["total"], response.json()["content"]) == (2, "edited")
    while harness.ctx.export_runner.run_next():
        pass
    export = harness.client.get(f"/api/exports/{response.json()['id']}").json()
    assert (export["status"], export["copied"]) == ("completed", 2) and export["zip_name"].startswith("editados-")
    data = harness.client.get(export["download_url"]).content
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = sorted(archive.namelist())
        contents = {archive.read(name) for name in names}
    assert all(name.endswith("_editado_15s_1.25x_nitido.mp4") for name in names) and len(names) == 2
    renditions = [r for i in ids for r in harness.client.get(f"/api/media/{i}").json()["renditions"]]
    processed = {harness.ctx.storage.resolve(harness.ctx.media.get_rendition(r["id"])["processed_path"]).read_bytes()
                 for r in renditions}
    originals = {harness.ctx.storage.resolve(harness.ctx.media.get(i)["original_path"]).read_bytes() for i in ids}
    assert contents == processed and not contents & originals


# ---------------- recorte manual por vídeo ----------------

def _post_trims(harness, media_ids, trims, **settings):
    return harness.client.post("/api/edits", json={"media_ids": media_ids, "settings": {**SETTINGS, **settings},
                                                   "trims": trims})


def test_trim_goes_to_job_params_and_ignores_duration(lib):
    harness, media = lib
    response = _post_trims(harness, [media["V1"], media["V2"]],
                           [{"media_id": media["V1"], "start": 0.25, "end": 1.8}])
    assert response.status_code == 202, response.text
    batch = response.json()
    jobs = {j["media_id"]: harness.ctx.jobs.get(j["id"])["params"] for j in harness.batch(batch["id"])["jobs"]}
    # vídeo recortado: sem max_duration (duração geral ignorada), início e fim exatos, velocidade e nitidez gerais
    assert jobs[media["V1"]] == {"metadata_cleanup": False, "trim_start": 0.25, "mirror": False, "speed": 1.25,
                                 "auto_adjust": False, "reencode": True, "sharpen": True, "audio_sync_start": True,
                                 "cut_end": 1.8}
    # vídeo sem recorte: como antes; os params do lote são os gerais
    assert jobs[media["V2"]]["max_duration"] == 15.03 and "cut_end" not in jobs[media["V2"]]
    assert harness.ctx.batches.get(batch["id"])["params"] == jobs[media["V2"]]

    harness.run_jobs()  # o pipeline monta ProcessingOptions.from_params com o recorte
    done = harness.batch(batch["id"])
    assert done["status"] == "completed" and done["counts"]["completed"] == 2


def test_trim_without_other_changes_is_a_change(lib):
    harness, media = lib
    response = _post_trims(harness, [media["V1"]], [{"media_id": media["V1"], "start": 0.5, "end": 1.5}],
                           duration_mode="original", speed=1.0, sharpen=False, output_type="auto")
    assert response.status_code == 202, response.text
    params = harness.ctx.jobs.get(harness.batch(response.json()["id"])["jobs"][0]["id"])["params"]
    assert (params["trim_start"], params["cut_end"], params["reencode"]) == (0.5, 1.5, False)


def test_trim_rounds_to_millisecond_and_accepts_long_start(lib):
    harness, media = lib
    with harness.ctx.db.transaction():
        harness.ctx.db.execute("UPDATE media SET duration = 4000 WHERE id = ?", (media["V1"],))
    response = _post_trims(harness, [media["V1"]],
                           [{"media_id": media["V1"], "start": 3600.12349, "end": 3700.0004}])
    assert response.status_code == 202, response.text
    params = harness.ctx.jobs.get(harness.batch(response.json()["id"])["jobs"][0]["id"])["params"]
    assert (params["trim_start"], params["cut_end"]) == (3600.123, 3700.0)


@pytest.mark.parametrize("trim", [
    {"start": 1.0, "end": 1.05},        # menos de 0,1 s
    {"start": 1.0, "end": 0.5},         # fim antes do início
    {"start": -0.1, "end": 1.0},
    {"start": 0.0, "end": 86400.5},
    {"start": "0", "end": 1.0},
    {"start": 0.0, "end": None},
    {"start": 0.0},
    {"start": 0.0, "end": 1.0, "extra": 1},
])
def test_trim_validation(lib, trim):
    harness, media = lib
    response = _post_trims(harness, [media["V1"]], [{"media_id": media["V1"], **trim}])
    assert response.status_code == 422 and response.json()["code"] == "validation_error", trim
    assert harness.ctx.batches.list(limit=50, offset=0, source="library")[1] == 0


def test_trim_minimum_is_accepted(lib):
    harness, media = lib
    response = _post_trims(harness, [media["V1"]], [{"media_id": media["V1"], "start": 1.2, "end": 1.3}])
    assert response.status_code == 202, response.text


def test_trim_must_match_selection(lib):
    harness, media = lib
    outside = _post_trims(harness, [media["V1"]], [{"media_id": media["V2"], "start": 0.0, "end": 1.0}])
    twice = _post_trims(harness, [media["V1"]], [{"media_id": media["V1"], "start": 0.0, "end": 1.0},
                                                 {"media_id": media["V1"], "start": 0.5, "end": 1.0}])
    for response in (outside, twice):
        assert response.status_code == 422 and response.json()["code"] == "validation_error"
    assert harness.ctx.batches.list(limit=50, offset=0, source="library")[1] == 0


def test_trim_end_beyond_known_duration(lib):
    harness, media = lib
    with harness.ctx.db.transaction():
        harness.ctx.db.execute("UPDATE media SET duration = 2.0 WHERE id = ?", (media["V1"],))
    # até 1 ms além da duração é aceito (duração medida pelo navegador, recorte arredondado ao milissegundo)
    ok = _post_trims(harness, [media["V1"]], [{"media_id": media["V1"], "start": 0.0, "end": 2.001}])
    assert ok.status_code == 202, ok.text
    response = _post_trims(harness, [media["V1"]], [{"media_id": media["V1"], "start": 0.0, "end": 2.5}])
    assert response.status_code == 422 and response.json()["code"] == "edit_trim_out_of_range"
    assert str(media["V1"]) in response.json()["detail"]
    assert harness.ctx.batches.list(limit=50, offset=0, source="library")[1] == 1


def test_trim_dedup(lib):
    harness, media = lib
    first = _post_trims(harness, [media["V1"]], [{"media_id": media["V1"], "start": 0.25, "end": 1.75}])
    harness.run_jobs()
    files = _processed_files(harness)
    rendition = harness.batch(first.json()["id"])["jobs"][0]["rendition_id"]
    # mesmo recorte (igual ao milissegundo) e mesmas configurações: reaproveita a versão
    again = _post_trims(harness, [media["V1"]], [{"media_id": media["V1"], "start": 0.2500004, "end": 1.75}])
    harness.run_jobs()
    job = harness.batch(again.json()["id"])["jobs"][0]
    assert (job["stage"], job["rendition_id"]) == ("skipped", rendition) and _processed_files(harness) == files
    # outro recorte (ou sem recorte) gera outra versão
    _post_trims(harness, [media["V1"]], [{"media_id": media["V1"], "start": 0.5, "end": 1.75}])
    _post(harness, [media["V1"]])
    harness.run_jobs()
    assert len(_processed_files(harness)) == 3


def test_trimmed_edit_exports_with_trim_in_name(lib):
    import io
    import zipfile

    harness, media = lib
    batch_id = _post_trims(harness, [media["V1"], media["V2"]],
                           [{"media_id": media["V1"], "start": 0.25, "end": 1.8}]).json()["id"]
    harness.run_jobs()
    response = harness.client.post("/api/exports", json={
        "source": {"kind": "batch", "id": batch_id}, "destination": {"zip": True}, "content": "edited"})
    assert response.status_code == 202, response.text
    while harness.ctx.export_runner.run_next():
        pass
    export = harness.client.get(f"/api/exports/{response.json()['id']}").json()
    with zipfile.ZipFile(io.BytesIO(harness.client.get(export["download_url"]).content)) as archive:
        names = sorted(archive.namelist())
    assert len(names) == 2
    assert sum(name.endswith("_editado_0.25-1.8_1.25x_nitido.mp4") for name in names) == 1
    assert sum(name.endswith("_editado_15s_1.25x_nitido.mp4") for name in names) == 1


def _zip_names(harness, batch_id):
    import io
    import zipfile

    response = harness.client.post("/api/exports", json={
        "source": {"kind": "batch", "id": batch_id}, "destination": {"zip": True}, "content": "edited"})
    assert response.status_code == 202, response.text
    while harness.ctx.export_runner.run_next():
        pass
    export = harness.client.get(f"/api/exports/{response.json()['id']}").json()
    with zipfile.ZipFile(io.BytesIO(harness.client.get(export["download_url"]).content)) as archive:
        return sorted(archive.namelist())


def test_trimmed_edit_export_survives_job_retention(lib):
    from app.db.connection import utc_now

    harness, media = lib
    ids = [media["V1"], media["V2"]]
    batch_id = _post_trims(harness, ids, [{"media_id": media["V1"], "start": 0.25, "end": 1.8}]).json()["id"]
    harness.run_jobs()
    before = _zip_names(harness, batch_id)
    # lote posterior nas mesmas mídias: os jobs do primeiro deixam de ser "o último de cada mídia" e a retenção apaga
    _post(harness, ids, speed=1.5)
    harness.run_jobs()
    assert harness.ctx.jobs.purge_finished_before(utc_now(60)) >= 2
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ?", (batch_id,)) == 0
    stored = {row["media_id"]: row["params"] for row in harness.ctx.db.fetchall(
        "SELECT media_id, params FROM batch_media WHERE batch_id = ?", (batch_id,))}
    assert set(stored) == set(ids) and '"cut_end":1.8' in stored[media["V1"]].replace(" ", "")
    after = _zip_names(harness, batch_id)
    assert after == before and len(after) == 2
    assert sum(name.endswith("_editado_0.25-1.8_1.25x_nitido.mp4") for name in after) == 1
