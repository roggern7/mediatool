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
