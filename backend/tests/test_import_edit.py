"""Importação com "Editar após baixar" (`edit` em POST /imports, /import-groups e /import-links): cada vídeo baixado
ganha a versão editada exatamente como pela página Editar; fotos só são baixadas. 100% offline, valores FAKE."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from media_processor import MediaProcessor, ProcessingOptions
from media_processor.commands import build

from app.db.repositories.common import canonical_json
from app.schemas.edits import EditSettings
from app.services.edit_service import EDIT_PRESET_NAME, edit_params
from import_group_fakes import ProfilesDownloader, closed, drive
from import_links_fakes import carousel, create_links, link, links_app, photo, video
from media_fakes import FakeInstagramDownloader, FakeProcessor, remote

SETTINGS = {"duration_mode": "15", "custom_seconds": None, "speed": 1.25, "sharpen": True, "output_type": "mp4"}
PARAMS = {"metadata_cleanup": False, "trim_start": 0.0, "mirror": False, "speed": 1.25, "auto_adjust": False,
          "reencode": True, "max_duration": 15.03, "sharpen": True, "audio_sync_start": True}


def _stop(harness):
    harness.__exit__(None, None, None)


def _renditions(h, media_id: int) -> list[dict]:
    return h.client.get(f"/api/media/{media_id}").json()["renditions"]


def _media_by_code(h) -> dict[str, dict]:
    return {m["source_post_id"]: m for m in h.ctx.db.fetchall("SELECT * FROM media")}


def _assert_edited(h, media_id: int) -> dict:
    renditions = _renditions(h, media_id)
    assert [(r["preset_id"], r["preset_name"]) for r in renditions] == [(None, EDIT_PRESET_NAME)]
    row = h.ctx.media.get_rendition(renditions[0]["id"])
    assert row["params"] == PARAMS
    return row


def _profile_app(harness_factory, **kwargs):
    h = harness_factory(**kwargs)
    h.downloader.items = [remote("VID1", media_type="video"), remote("IMG1"), remote("VID2", media_type="video")]
    return h


def _export_zip(h, source: dict, content: str) -> tuple[dict, list[str]]:
    response = h.client.post("/api/exports", json={"source": source, "destination": {"zip": True}, "content": content})
    assert response.status_code == 202, response.text
    while h.ctx.export_runner.run_next():
        pass
    export = h.client.get(f"/api/exports/{response.json()['id']}").json()
    assert export["status"] == "completed", export
    with zipfile.ZipFile(io.BytesIO(h.client.get(export["download_url"]).content)) as archive:
        return export, sorted(archive.namelist())


# --------------------------------------------------------------------------- um perfil


def test_profile_import_with_edit_edits_videos_and_only_downloads_images(harness_factory):
    h = _profile_app(harness_factory)
    batch = h.import_profile(edit=SETTINGS)
    assert (batch["preset_id"], batch["preset_name"], batch["process"], batch["source"]) == (
        None, EDIT_PRESET_NAME, True, "instagram_import")
    assert h.ctx.batches.get(batch["id"])["params"] == PARAMS == edit_params(EditSettings(**SETTINGS))

    h.run_imports()
    jobs = {j["source_post_id"]: h.ctx.jobs.get(j["id"]) for j in h.batch(batch["id"])["jobs"]}
    for code in ("VID1", "VID2"):
        assert (jobs[code]["kind"], jobs[code]["preset_id"], jobs[code]["preset_name"], jobs[code]["params"]) == (
            "ingest", None, EDIT_PRESET_NAME, PARAMS)
    # A foto do lote de Edição é download-only, sem preset (não aparece como "Edição" na fila).
    assert (jobs["IMG1"]["preset_id"], jobs["IMG1"]["preset_name"], jobs["IMG1"]["params"]) == (None, None, None)

    h.run_jobs()
    done = h.batch(batch["id"])
    assert done["status"] == "completed" and done["progress"]["failed"] == 0
    assert done["progress"]["processed"] == 2 and done["progress"]["downloaded"] == 3
    statuses = {j["source_post_id"]: (j["status"], j["stage"]) for j in done["jobs"]}
    assert statuses == {"VID1": ("completed", "done"), "VID2": ("completed", "done"), "IMG1": ("completed", "done")}
    media = _media_by_code(h)
    for code in ("VID1", "VID2"):
        _assert_edited(h, media[code]["id"])
    assert _renditions(h, media["IMG1"]["id"]) == []
    assert len(h.processor.processed) == 2


def test_import_edit_is_the_same_edit_as_the_edit_page(harness_factory, tmp_path):
    """Mesmos params (e portanto as mesmas opções e o mesmo comando FFmpeg) da página Editar: editar depois pela
    página Editar com as mesmas configurações reaproveita a versão criada na importação."""
    h = _profile_app(harness_factory)
    batch = h.import_profile(edit=SETTINGS)
    h.run_all()
    media = _media_by_code(h)
    imported = _assert_edited(h, media["VID1"]["id"])

    response = h.client.post("/api/edits", json={"media_ids": [media["VID1"]["id"]], "settings": SETTINGS})
    assert response.status_code == 202, response.text
    page_batch = h.ctx.batches.get(response.json()["id"])
    page_job = h.ctx.jobs.for_batch(page_batch["id"])[0]
    import_job = next(j for j in h.ctx.jobs.for_batch(batch["id"]) if j["source_post_id"] == "VID1")
    assert page_batch["params"] == h.ctx.batches.get(batch["id"])["params"]
    assert (page_job["preset_id"], page_job["preset_name"], page_job["params"]) == (
        import_job["preset_id"], import_job["preset_name"], import_job["params"])

    src = tmp_path / "src.mp4"
    src.write_bytes(b"FAKE-video")
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 64, "height": 48,
                "avg_frame_rate": "25/1", "r_frame_rate": "25/1", "pix_fmt": "yuv420p", "duration": "20.0"},
               {"index": 1, "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2,
                "duration": "20.0", "start_time": "0.0"}]
    probe = replace(FakeProcessor().probe(src), duration=20.0, video_codec="h264", audio_codec="aac",
                    raw={"streams": streams, "format": {"duration": "20.0"}})
    commands = [build("ffmpeg", src, tmp_path / "out.mp4", probe, ProcessingOptions.from_params(params))
                for params in (page_job["params"], import_job["params"])]
    assert commands[0] == commands[1]

    h.run_jobs()
    reused = h.ctx.jobs.get(page_job["id"])
    assert (reused["status"], reused["stage"], reused["rendition_id"]) == ("completed", "skipped", imported["id"])
    assert len(_renditions(h, media["VID1"]["id"])) == 1


@pytest.mark.parametrize("path, body", [
    ("/api/imports", {"profile": "perfil_teste", "content_type": "all", "limit": 10}),
    ("/api/import-groups", {"profiles": ["perfil_a", "perfil_b"], "content_type": "all", "limit": 10}),
    ("/api/import-links", {"links": [link("ABCDE12345")]}),
])
@pytest.mark.parametrize("conflict", [{"process": True, "preset_id": 1}, {"process": True}, {"preset_id": 1}])
def test_edit_with_preset_is_rejected_clearly(harness, path, body, conflict):
    response = harness.client.post(path, json={**body, **conflict, "edit": SETTINGS})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "edit_with_preset"
    assert "Editar após baixar" in response.json()["detail"]
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM import_groups") == 0


@pytest.mark.parametrize("path, body", [
    ("/api/imports", {"profile": "perfil_teste", "content_type": "all", "limit": 10}),
    ("/api/import-groups", {"profiles": ["perfil_a", "perfil_b"], "content_type": "all", "limit": 10}),
    ("/api/import-links", {"links": [link("ABCDE12345")]}),
])
@pytest.mark.parametrize("edit, code", [
    ({**SETTINGS, "output_type": "auto", "duration_mode": "original", "speed": 1.0, "sharpen": False},
     "edit_no_changes"),
    ({**SETTINGS, "duration_mode": "custom", "custom_seconds": None}, "validation_error"),
    ({**SETTINGS, "speed": 1.6}, "validation_error"),
    ({**SETTINGS, "extra": 1}, "validation_error"),
])
def test_edit_settings_use_the_edit_page_validation(harness, path, body, edit, code):
    response = harness.client.post(path, json={**body, "edit": edit})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == code
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0


# --------------------------------------------------------------------------- sem `edit`: como antes

_BATCH_COLUMNS = ("source", "source_profile", "content_type", "requested_limit", "preset_id", "preset_name", "params",
                  "process", "status", "import_mode", "group_id", "group_position")
_JOB_COLUMNS = ("kind", "preset_id", "preset_name", "params", "status", "stage", "media_type", "source_post_id")


def _snapshot(h) -> dict:
    return {
        "batches": [tuple(r) for r in h.ctx.db.fetchall(
            f"SELECT {', '.join(_BATCH_COLUMNS)} FROM batches ORDER BY id")],
        "groups": [tuple(r) for r in h.ctx.db.fetchall(
            "SELECT content_type, requested_limit, preset_id, preset_name, params, process FROM import_groups")],
        "jobs": [tuple(r) for r in h.ctx.db.fetchall(f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs ORDER BY id")],
        "renditions": [tuple(r) for r in h.ctx.db.fetchall(
            "SELECT media_id, preset_id, preset_name, params FROM renditions ORDER BY id")],
    }


def _run_without_edit(harness_factory, mode: str, extra: dict) -> tuple[dict, list, dict | None]:
    items = [remote("VID1", media_type="video", profile="perfil_a"), remote("IMG1", profile="perfil_a")]
    if mode == "links":
        h, _ = links_app(harness_factory, {"VIDEO00001": video("VIDEO00001"), "PHOTO00001": photo("PHOTO00001")})
        create_links(h, [link("VIDEO00001"), link("PHOTO00001")], **extra)
    elif mode == "group":
        h = harness_factory(downloader=ProfilesDownloader(profiles={
            "perfil_a": items, "perfil_b": [remote("VID2", media_type="video", profile="perfil_b")]}))
        response = h.client.post("/api/import-groups", json={
            "profiles": ["perfil_a", "perfil_b"], "content_type": "all", "limit": 10, **extra})
        assert response.status_code == 202, response.text
    else:
        h = harness_factory(downloader=FakeInstagramDownloader(items=items))
        h.import_profile(profile="perfil_a", **extra)
    drive(h, closed(h, *[r["id"] for r in h.ctx.db.fetchall("SELECT id FROM batches")]))
    options = [repr(ProcessingOptions.from_params(json.loads(r["params"]))) for r in h.ctx.db.fetchall(
        "SELECT params FROM renditions ORDER BY id")]
    return _snapshot(h), options, h.ctx.presets.get(1) if extra.get("process") else None


@pytest.mark.parametrize("mode", ["profile", "group", "links"])
@pytest.mark.parametrize("extra", [{}, {"process": False, "preset_id": None}, {"process": True},
                                   {"process": True, "preset_id": 1}])
def test_without_edit_nothing_changes(harness_factory, mode, extra):
    """Sem `edit` (omitido ou `null`) o lote, os jobs e as versões são os mesmos de sempre: download puro sem preset,
    ou o preset aplicado a TODAS as mídias (fotos inclusive), com as opções do preset."""
    omitted, omitted_options, preset = _run_without_edit(harness_factory, mode, extra)
    explicit, explicit_options, _ = _run_without_edit(harness_factory, mode, {**extra, "edit": None})
    assert omitted == explicit and omitted_options == explicit_options

    for row in omitted["batches"]:
        assert row[_BATCH_COLUMNS.index("preset_name")] != EDIT_PRESET_NAME
        assert bool(row[_BATCH_COLUMNS.index("process")]) is bool(extra.get("process"))
    if preset is None:
        assert all(job[_JOB_COLUMNS.index("params")] is None for job in omitted["jobs"])
        assert omitted["renditions"] == [] and omitted_options == []
    else:
        params = canonical_json(preset["params"])
        assert all(job[_JOB_COLUMNS.index("params")] == params for job in omitted["jobs"])
        assert all(job[_JOB_COLUMNS.index("preset_name")] == preset["name"] for job in omitted["jobs"])
        # Com preset, a foto também é processada (como sempre foi).
        assert len(omitted["renditions"]) == len(omitted["jobs"])
        assert set(omitted_options) == {repr(ProcessingOptions.from_params(preset["params"]))}


# --------------------------------------------------------------------------- vários perfis e links


def test_group_with_edit_applies_to_every_child_and_exports_the_edited_versions(harness_factory):
    profiles = {
        "perfil_a": [remote("AVID", media_type="video", profile="perfil_a"), remote("AIMG", profile="perfil_a")],
        "perfil_b": [remote("BVID", media_type="video", profile="perfil_b"), remote("BIMG", profile="perfil_b")],
    }
    h = harness_factory(downloader=ProfilesDownloader(profiles=profiles))
    response = h.client.post("/api/import-groups", json={
        "profiles": list(profiles), "content_type": "all", "limit": 10, "edit": SETTINGS})
    assert response.status_code == 202, response.text
    detail = response.json()
    group = detail["group"]
    assert (group["preset_id"], group["preset_name"], group["process"]) == (None, EDIT_PRESET_NAME, True)
    assert all((c["preset_id"], c["preset_name"], c["process"]) == (None, EDIT_PRESET_NAME, True)
               for c in detail["children"])
    children = [c["id"] for c in detail["children"]]
    assert all(h.ctx.batches.get(b)["params"] == PARAMS for b in children)

    drive(h, closed(h, *children))
    final = h.client.get(f"/api/import-groups/{group['id']}").json()
    assert final["group"]["status"] == "completed" and final["group"]["progress"]["failed"] == 0
    assert final["group"]["progress"]["processed"] == 2
    media = _media_by_code(h)
    for code in ("AVID", "BVID"):
        _assert_edited(h, media[code]["id"])
    for code in ("AIMG", "BIMG"):
        assert _renditions(h, media[code]["id"]) == []

    export, names = _export_zip(h, {"kind": "group", "id": group["id"]}, "edited")
    assert export["copied"] == 2 and len(names) == 2
    assert all(name.endswith("_editado_15s_1.25x_nitido.mp4") for name in names)
    assert re.fullmatch(r"editados-\d{4}-\d{2}-\d{2}-\d{4}\.zip", export["zip_name"])


def test_links_with_edit_edits_the_videos_of_each_post(harness_factory):
    posts = {"VIDEO00001": video("VIDEO00001"), "CAROU00001": carousel("CAROU00001"), "PHOTO00001": photo("PHOTO00001")}
    h, _ = links_app(harness_factory, posts)
    batch = create_links(h, [link(code) for code in posts], edit=SETTINGS)
    assert (batch["preset_id"], batch["preset_name"], batch["process"], batch["import_mode"]) == (
        None, EDIT_PRESET_NAME, True, "links")
    drive(h, closed(h, batch["id"]))
    done = h.batch(batch["id"])
    assert done["status"] == "completed" and done["progress"]["failed"] == 0
    assert (done["found"], done["progress"]["processed"]) == (5, 2)
    videos = {(m["source_post_id"], m["source_media_index"]): m for m in h.ctx.db.fetchall("SELECT * FROM media")}
    for key, media in videos.items():
        if media["media_type"] == "video":
            _assert_edited(h, media["id"])
        else:
            assert _renditions(h, media["id"]) == [], key
    assert sorted(k for k, m in videos.items() if m["media_type"] == "video") == [("CAROU00001", 1), ("VIDEO00001", 0)]


# --------------------------------------------------------------------------- exportação, cancelamento, retomada


def test_batch_export_of_edited_versions_and_coherent_zip_names(harness_factory):
    h = _profile_app(harness_factory)
    batch = h.import_profile(edit=SETTINGS)
    h.run_all()
    edited, names = _export_zip(h, {"kind": "batch", "id": batch["id"]}, "edited")
    assert edited["copied"] == 2 and all(n.endswith("_editado_15s_1.25x_nitido.mp4") for n in names)
    assert re.fullmatch(r"editados-\d{4}-\d{2}-\d{2}-\d{4}\.zip", edited["zip_name"])
    processed = {h.ctx.storage.resolve(r["processed_path"]).read_bytes()
                 for r in h.ctx.db.fetchall("SELECT processed_path FROM renditions")}
    data = h.client.get(edited["download_url"]).content
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert {archive.read(n) for n in archive.namelist()} == processed
    # É uma importação: os originais (as 3 mídias, fotos inclusive) saem como `importacao-...zip`.
    originals, names = _export_zip(h, {"kind": "batch", "id": batch["id"]}, "originals")
    assert originals["copied"] == 3 and len(names) == 3
    assert re.fullmatch(r"importacao-\d{4}-\d{2}-\d{2}-\d{4}\.zip", originals["zip_name"])


def test_cancel_and_retry_failed_with_edit(harness_factory):
    from media_processor import ProcessingFailedError

    h = _profile_app(harness_factory, processor=FakeProcessor(fail_process=ProcessingFailedError("falha (teste)")))
    batch = h.import_profile(edit=SETTINGS)
    h.run_all()
    failed = h.batch(batch["id"])
    assert failed["progress"]["failed"] == 2 and failed["counts"]["completed"] == 1   # a foto não falha
    h.processor.fail_process = None
    response = h.client.post(f"/api/batches/{batch['id']}/retry-failed")
    assert response.status_code == 200, response.text
    h.run_jobs()
    assert h.batch(batch["id"])["status"] == "completed"
    media = _media_by_code(h)
    for code in ("VID1", "VID2"):
        _assert_edited(h, media[code]["id"])

    other = harness_factory()
    other.downloader.items = [remote("VID9", media_type="video")]
    second = other.import_profile(edit=SETTINGS)
    other.run_imports()
    response = other.client.post(f"/api/batches/{second['id']}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    other.run_jobs()
    assert other.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == 0
    assert other.processor.processed == []


def test_restart_resumes_edit_import_before_listing_and_in_the_middle_of_editing(harness_factory):
    # 1) Reinício antes de listar: a configuração da edição está no lote (params), nada se perde.
    first = _profile_app(harness_factory)
    batch = first.import_profile(edit=SETTINGS)
    root = first.root
    _stop(first)
    second = harness_factory(root=root, downloader=FakeInstagramDownloader(
        items=[remote("VID1", media_type="video"), remote("IMG1"), remote("VID2", media_type="video")]))
    second.run_all()
    assert second.batch(batch["id"])["status"] == "completed"
    media = _media_by_code(second)
    for code in ("VID1", "VID2"):
        _assert_edited(second, media[code]["id"])
    assert _renditions(second, media["IMG1"]["id"]) == []

    # 2) Reinício no meio da edição: o job interrompido volta a `downloaded` e é editado com os mesmos params.
    ctx = second.ctx
    job = next(j for j in ctx.jobs.for_batch(batch["id"]) if j["source_post_id"] == "VID1")
    rendition = ctx.media.get_rendition(job["rendition_id"])
    ctx.storage.resolve(rendition["processed_path"]).unlink()
    ctx.db.execute("DELETE FROM renditions WHERE id = ?", (rendition["id"],))
    ctx.jobs.update(job["id"], status="processing", stage="processing", progress=0.5, rendition_id=None,
                    finished_at=None)
    ctx.batches.update(batch["id"], status="running", finished_at=None)
    _stop(second)
    third = harness_factory(root=root, downloader=FakeInstagramDownloader())
    recovered = third.ctx.jobs.get(job["id"])
    assert recovered["status"] == "downloaded" and recovered["params"] == PARAMS
    third.run_jobs()
    assert third.batch(batch["id"])["status"] == "completed"
    _assert_edited(third, media["VID1"]["id"])


# --------------------------------------------------------------------------- FFmpeg real (duração verificada)


@pytest.mark.slow
@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg/ffprobe não estão no PATH")
def test_real_ffmpeg_import_edit_cuts_to_the_limit_plus_margin(harness_factory, tmp_path):
    clip = tmp_path / "longo.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=160x120:rate=30:duration=20", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=20", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)], check=True, timeout=120)
    downloader = FakeInstagramDownloader(items=[remote("REAL", media_type="video")],
                                         source_files={"REAL:0": Path(clip)})
    h = harness_factory(downloader=downloader, processor=MediaProcessor())
    batch = h.import_profile(edit={**SETTINGS, "speed": 1.0, "sharpen": False})
    h.run_all()
    done = h.batch(batch["id"])
    assert done["status"] == "completed", done["jobs"]
    rendition = _renditions(h, _media_by_code(h)["REAL"]["id"])[0]
    assert rendition["preset_name"] == EDIT_PRESET_NAME
    assert rendition["duration"] == pytest.approx(15.03, abs=1 / 30 + 0.01)
