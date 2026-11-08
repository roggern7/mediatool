"""Importação com "Edição CapCut" após baixar (`capcut` em POST /imports, /imports/start, /import-groups e
/import-links): um lote vazio da Edição CapCut vinculado à importação (`app_state`), cada vídeo NOVO baixado entra
nele (com o job de edição nos modos com edição), status `importing` enquanto a importação está ativa. 100% offline,
valores FAKE."""

from __future__ import annotations

import pytest
from capcut_jobs_fakes import add_media
from import_group_fakes import ProfilesDownloader, closed, drive
from import_links_fakes import create_links, link, links_app, video
from media_fakes import remote

from app.services import capcut_batch_service

EDIT = {"duration_mode": "30", "speed": 1.2, "sharpen": True, "start_seconds": 0}
EDIT_SETTINGS = {"duration_mode": "15", "custom_seconds": None, "speed": 1.25, "sharpen": True, "output_type": "mp4"}


def _capcut(out, mode: str = "edit_caption", **extra) -> dict:
    body = {"mode": mode, "caption": {"overrides": {"output_dir": str(out)}}, **extra}
    if mode != "caption" and "edit" not in extra:
        body["edit"] = EDIT
    return body


def _cbatch(h, batch_id: int) -> dict:
    response = h.client.get(f"/api/capcut/batches/{batch_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _cjobs(h) -> list[dict]:
    return [dict(row) for row in h.ctx.db.fetchall(
        "SELECT media_id, capcut_batch_id, mode, edit_job_id, origin, batch_id, status FROM capcut_jobs ORDER BY id")]


def _video_ids(h) -> list[int]:
    return [int(r["id"]) for r in h.ctx.db.fetchall(
        "SELECT id FROM media WHERE media_type = 'video' AND role = 'original' ORDER BY id")]


# ---- criação ---------------------------------------------------------------------------------------------------------
def test_profile_import_creates_empty_linked_batch_and_videos_enter_with_edit(harness, tmp_path):
    out = tmp_path / "saida"
    harness.downloader.items = [remote("VID1", media_type="video"), remote("IMG1"), remote("VID2", media_type="video")]
    batch = harness.import_profile(capcut=_capcut(out))
    capcut_id = batch["capcut_batch_id"]
    assert isinstance(capcut_id, int)
    assert harness.ctx.app_settings.get_state(f"capcut_import_link:batch:{batch['id']}") == str(capcut_id)
    created = _cbatch(harness, capcut_id)
    assert created["status"] == "importing"
    assert created["counts"]["total"] == 0
    assert created["name"].startswith("Importação ")
    assert created["source"] == {"kind": "import", "batch_ids": [batch["id"]], "group_id": None}
    assert created["output_dir"] == str(out / f"Lote {capcut_id}")
    assert created["summary"]["edit"] and created["options"]["edit"]["params"]["speed"] == 1.2
    assert "job_options" not in created["options"]
    listed = harness.client.get("/api/capcut/batches", params={"status": "importing"}).json()
    assert [item["id"] for item in listed["items"]] == [capcut_id]
    # O lote de edição (`batches`) só nasce com o primeiro vídeo.
    assert harness.ctx.db.scalar("SELECT edit_batch_id FROM capcut_batches WHERE id = ?", (capcut_id,)) is None

    harness.run_all()
    assert harness.batch(batch["id"])["status"] == "completed"
    videos = _video_ids(harness)
    assert len(videos) == 2
    jobs = _cjobs(harness)
    assert [job["media_id"] for job in jobs] == videos   # foto não entra
    edit_batch_id = harness.ctx.db.scalar("SELECT edit_batch_id FROM capcut_batches WHERE id = ?", (capcut_id,))
    assert edit_batch_id is not None
    for job in jobs:
        assert (job["capcut_batch_id"], job["mode"], job["origin"], job["batch_id"]) == (
            capcut_id, "edit_caption", "auto", batch["id"])
        edit_job = harness.ctx.jobs.get(job["edit_job_id"])
        assert edit_job["batch_id"] == edit_batch_id and edit_job["kind"] == "process"
    final = _cbatch(harness, capcut_id)
    assert final["counts"]["total"] == 2 and final["status"] != "importing"


def test_caption_mode_has_no_edit_job(harness, tmp_path):
    harness.downloader.items = [remote("VIDC", media_type="video")]
    batch = harness.import_profile(capcut=_capcut(tmp_path / "s", mode="caption", name="Meu lote"))
    assert _cbatch(harness, batch["capcut_batch_id"])["name"] == "Meu lote"
    harness.run_all()
    jobs = _cjobs(harness)
    assert len(jobs) == 1 and jobs[0]["edit_job_id"] is None and jobs[0]["mode"] == "caption"
    assert harness.ctx.db.scalar("SELECT edit_batch_id FROM capcut_batches") is None


def test_import_without_videos_ends_completed_empty(harness, tmp_path):
    harness.downloader.items = [remote("IMGX")]
    batch = harness.import_profile(capcut=_capcut(tmp_path / "s", mode="edit"))
    assert _cbatch(harness, batch["capcut_batch_id"])["status"] == "importing"
    harness.run_all()
    final = _cbatch(harness, batch["capcut_batch_id"])
    assert (final["status"], final["counts"]["total"]) == ("completed", 0)


def test_reused_video_enters_new_capcut_batch_unless_already_queued(harness, tmp_path):
    """Importar de novo com "Edição CapCut" edita de novo os vídeos já baixados (caso real 2026-11-08: o lote
    "bianca-01" ficou vazio porque os 10 vídeos já existiam). Só fica fora o vídeo que já está na fila do CapCut."""
    harness.downloader.items = [remote("VDUP", media_type="video")]
    first = harness.import_profile(capcut=_capcut(tmp_path / "s", mode="caption"))
    harness.run_all()
    assert len(_cjobs(harness)) == 1
    # ainda na fila do CapCut: a nova importação não cria um segundo job para o mesmo vídeo
    busy = harness.import_profile(capcut=_capcut(tmp_path / "s", mode="caption"))
    harness.run_all()
    assert len(_cjobs(harness)) == 1 and _cbatch(harness, busy["capcut_batch_id"])["counts"]["total"] == 0
    job_id = harness.ctx.db.scalar("SELECT id FROM capcut_jobs")
    assert harness.client.post(f"/api/capcut/jobs/{job_id}/cancel").json()["status"] == "cancelled"
    again = harness.import_profile(capcut=_capcut(tmp_path / "s", mode="caption"))
    harness.run_all()
    assert harness.batch(again["id"])["status"] == "completed"
    assert len(_cjobs(harness)) == 2, "o vídeo reaproveitado entrou no lote novo"
    assert _cbatch(harness, again["capcut_batch_id"])["counts"]["total"] == 1
    assert first["capcut_batch_id"] != again["capcut_batch_id"]


def test_reused_video_without_link_does_not_trigger_auto_caption(harness, tmp_path):
    """O "legendar automaticamente" global continua só para vídeo novo."""
    harness.client.put("/api/capcut/settings", json={"auto_after_download": True})
    harness.downloader.items = [remote("VAUTO", media_type="video")]
    harness.import_profile()
    harness.run_all()
    assert len(_cjobs(harness)) == 1
    job_id = harness.ctx.db.scalar("SELECT id FROM capcut_jobs")
    harness.client.post(f"/api/capcut/jobs/{job_id}/cancel")
    harness.import_profile()
    harness.run_all()
    assert len(_cjobs(harness)) == 1


def test_cover_photo_and_busy_media_are_skipped(harness, tmp_path):
    batch = harness.import_profile(capcut=_capcut(tmp_path / "s", mode="caption"))
    capcut_id = batch["capcut_batch_id"]
    cover = add_media(harness, media_type="image", role="cover", name="capa")
    image = add_media(harness, media_type="image", name="foto")
    cover_video = add_media(harness, role="cover", name="capa_video")
    assert capcut_batch_service.add_import_video(harness.ctx, capcut_id, cover, batch["id"]) is None
    assert capcut_batch_service.add_import_video(harness.ctx, capcut_id, image, batch["id"]) is None
    assert capcut_batch_service.add_import_video(harness.ctx, capcut_id, cover_video, batch["id"]) is None
    clip = add_media(harness, name="clip")
    assert capcut_batch_service.add_import_video(harness.ctx, capcut_id, clip, batch["id"]) is not None
    assert capcut_batch_service.add_import_video(harness.ctx, capcut_id, clip, batch["id"]) is None   # já na fila
    assert [job["media_id"] for job in _cjobs(harness)] == [clip]


# ---- exclusividade e validação ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("path, body", [
    ("/api/imports", {"profile": "perfil_teste", "content_type": "all", "limit": 10}),
    ("/api/import-groups", {"profiles": ["perfil_a", "perfil_b"], "content_type": "all", "limit": 10}),
    ("/api/import-links", {"links": [link("ABCDE12345")]}),
])
@pytest.mark.parametrize("conflict", [{"process": True}, {"preset_id": 1},
                                      {"edit": EDIT_SETTINGS}])
def test_capcut_is_exclusive_with_preset_and_edit(harness, tmp_path, path, body, conflict):
    response = harness.client.post(path, json={**body, **conflict, "capcut": _capcut(tmp_path / "s")})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "capcut_with_post_processing"
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM capcut_batches") == 0


@pytest.mark.parametrize("capcut, status, code", [
    ({"mode": "edit", "edit": {"duration_mode": "original", "speed": 1.0, "sharpen": False}}, 422, "edit_no_changes"),
    ({"mode": "edit"}, 422, "edit_required"),
    ({"mode": "caption", "caption": {"style_id": 9999}}, 404, "style_not_found"),
    ({"mode": "edit", "edit": {**EDIT, "preset_id": 9999}}, 404, "preset_not_found"),
])
def test_invalid_capcut_config_is_refused_before_creating(harness, capcut, status, code):
    for path, body in [("/api/imports", {"profile": "perfil_teste", "content_type": "all", "limit": 10}),
                       ("/api/imports/start", {"text": "perfil_teste", "limit": 10, "post_processing": "capcut"})]:
        response = harness.client.post(path, json={**body, "capcut": capcut})
        assert response.status_code == status, response.text
        assert response.json()["code"] == code
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM capcut_batches") == 0


@pytest.mark.parametrize("body", [
    {"post_processing": "capcut"},
    {"post_processing": "none", "capcut": {"mode": "caption"}},
    {"post_processing": "edit", "edit": EDIT_SETTINGS,
     "capcut": {"mode": "caption"}},
])
def test_unified_capcut_requires_its_choice(harness, body):
    response = harness.client.post("/api/imports/start", json={"text": "perfil_teste", "limit": 10, **body})
    assert response.status_code == 422, response.text
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM capcut_batches") == 0


def test_unified_auto_export_edited_is_refused_with_capcut(harness, tmp_path):
    response = harness.client.post("/api/imports/start", json={
        "text": "perfil_teste", "limit": 10, "post_processing": "capcut", "capcut": _capcut(tmp_path / "s"),
        "auto_export": {"content": "edited"}})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "auto_export_capcut"


# ---- importação unificada, grupo e links ---------------------------------------------------------------------------
def test_unified_profile_and_links_share_one_capcut_batch(harness_factory, tmp_path):
    h = harness_factory(downloader=ProfilesDownloader(profiles={
        "perfil_a": [remote("VA1", media_type="video", profile="perfil_a")]}))
    response = h.client.post("/api/imports/start", json={
        "text": "perfil_a", "limit": 10, "post_processing": "capcut", "capcut": _capcut(tmp_path / "s", mode="edit")})
    assert response.status_code == 202, response.text
    result = response.json()
    capcut_id = result["capcut_batch_id"]
    assert _cbatch(h, capcut_id)["source"] == {"kind": "import", "batch_ids": result["batch_ids"], "group_id": None}
    drive(h, closed(h, *result["batch_ids"]))
    jobs = _cjobs(h)
    assert [job["capcut_batch_id"] for job in jobs] == [capcut_id] and jobs[0]["edit_job_id"] is not None


def test_group_link_covers_children(harness_factory, tmp_path):
    h = harness_factory(downloader=ProfilesDownloader(profiles={
        "perfil_a": [remote("GA1", media_type="video", profile="perfil_a"), remote("GA2", profile="perfil_a")],
        "perfil_b": [remote("GB1", media_type="video", profile="perfil_b")]}))
    response = h.client.post("/api/import-groups", json={
        "profiles": ["perfil_a", "perfil_b"], "content_type": "all", "limit": 10,
        "capcut": _capcut(tmp_path / "s", mode="edit")})
    assert response.status_code == 202, response.text
    detail = response.json()
    capcut_id = detail["capcut_batch_id"]
    group_id = detail["group"]["id"]
    assert h.ctx.app_settings.get_state(f"capcut_import_link:group:{group_id}") == str(capcut_id)
    children = [child["id"] for child in detail["children"]]
    assert all(h.ctx.app_settings.get_state(f"capcut_import_link:batch:{child}") is None for child in children)
    created = _cbatch(h, capcut_id)
    assert created["status"] == "importing"
    assert created["source"] == {"kind": "import", "batch_ids": [], "group_id": group_id}
    drive(h, closed(h, *children))
    jobs = _cjobs(h)
    assert sorted(job["media_id"] for job in jobs) == _video_ids(h)
    assert {job["capcut_batch_id"] for job in jobs} == {capcut_id}
    assert {job["batch_id"] for job in jobs} == set(children)
    assert all(job["edit_job_id"] is not None for job in jobs)
    assert _cbatch(h, capcut_id)["status"] != "importing"


def test_links_import_with_capcut(harness_factory, tmp_path):
    h, _ = links_app(harness_factory, {"VIDLINK001": video("VIDLINK001")})
    created = create_links(h, [link("VIDLINK001")], capcut=_capcut(tmp_path / "s", mode="caption"))
    capcut_id = created["capcut_batch_id"]
    assert _cbatch(h, capcut_id)["status"] == "importing"
    drive(h, closed(h, created["id"]))
    assert [job["capcut_batch_id"] for job in _cjobs(h)] == [capcut_id]


def test_links_reimport_of_downloaded_videos_enters_new_batch(harness_factory, tmp_path):
    """Caso real (2026-11-08, "bianca-01"): importar por links vídeos já baixados com "Edição CapCut" deixava o lote
    vazio. Agora eles entram (o anterior já terminou)."""
    h, _ = links_app(harness_factory, {"VIDLINK002": video("VIDLINK002")})
    first = create_links(h, [link("VIDLINK002")], capcut=_capcut(tmp_path / "s", mode="caption"))
    drive(h, closed(h, first["id"]))
    job_id = h.ctx.db.scalar("SELECT id FROM capcut_jobs")
    h.client.post(f"/api/capcut/jobs/{job_id}/cancel")
    again = create_links(h, [link("VIDLINK002")], capcut=_capcut(tmp_path / "s", mode="caption"))
    drive(h, closed(h, again["id"]))
    assert _cbatch(h, again["capcut_batch_id"])["counts"]["total"] == 1


# ---- convivência com o automático e cancelamento ---------------------------------------------------------------------
def test_auto_after_download_with_link_does_not_duplicate(harness, tmp_path):
    assert harness.client.put("/api/capcut/settings", json={"auto_after_download": True}).status_code == 200
    harness.downloader.items = [remote("VAUTO", media_type="video")]
    batch = harness.import_profile(capcut=_capcut(tmp_path / "s", mode="caption"))
    harness.run_all()
    jobs = _cjobs(harness)
    assert len(jobs) == 1 and jobs[0]["capcut_batch_id"] == batch["capcut_batch_id"]
    # Sem vínculo, o automático continua como sempre.
    harness.downloader.items = [remote("VAUTO2", media_type="video")]
    plain = harness.import_profile()
    harness.run_all()
    jobs = _cjobs(harness)
    assert len(jobs) == 2 and jobs[1]["capcut_batch_id"] is None and jobs[1]["batch_id"] == plain["id"]


def test_cancelled_capcut_batch_receives_no_more_videos(harness, tmp_path):
    harness.downloader.items = [remote("VCAN", media_type="video")]
    batch = harness.import_profile(capcut=_capcut(tmp_path / "s", mode="edit"))
    capcut_id = batch["capcut_batch_id"]
    response = harness.client.post(f"/api/capcut/batches/{capcut_id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    harness.run_all()
    assert harness.batch(batch["id"])["status"] == "completed"   # a importação não é cancelada
    assert len(_video_ids(harness)) == 1
    assert _cjobs(harness) == []
    assert _cbatch(harness, capcut_id)["status"] == "cancelled"


def test_cancelling_import_keeps_capcut_batch(harness, tmp_path):
    harness.downloader.items = [remote("VKEEP", media_type="video")]
    batch = harness.import_profile(capcut=_capcut(tmp_path / "s", mode="caption"))
    assert harness.client.post(f"/api/batches/{batch['id']}/cancel").status_code == 200
    final = _cbatch(harness, batch["capcut_batch_id"])
    assert final["cancel_requested"] is False and final["status"] == "completed"


def test_batch_waiting_downloads_cannot_be_removed_from_history(harness, tmp_path):
    """Lote "Aguardando downloads" fica no histórico: removê-lo faria os próximos vídeos da importação se perderem."""
    from app.services import capcut_batch_service as svc

    harness.downloader.items = [remote("VIDW", media_type="video")]
    batch_id = harness.import_profile(capcut=_capcut(tmp_path / "s"))["capcut_batch_id"]
    assert _cbatch(harness, batch_id)["status"] == "importing"
    assert svc.hide_batches(harness.ctx, [batch_id], False) == {"hidden": 0, "active": [batch_id]}
    r = harness.client.delete(f"/api/capcut/batches/{batch_id}")
    assert r.status_code == 409


def test_import_batch_shows_linked_capcut_progress(harness, tmp_path):
    """A lista do Importar mostra o andamento do lote CapCut vinculado (senão parece que nada está sendo editado)."""
    harness.downloader.items = [remote("VIDP", media_type="video")]
    batch = harness.import_profile(capcut=_capcut(tmp_path / "s"))
    view = harness.client.get(f"/api/batches/{batch['id']}").json()
    assert view["capcut"]["batch_id"] == batch["capcut_batch_id"] and view["capcut"]["status"] == "importing"
    harness.run_all()
    view = harness.client.get(f"/api/batches/{batch['id']}").json()
    assert view["capcut"]["total"] == 1
    listed = harness.client.get("/api/batches", params={"limit": 5}).json()["items"]
    assert any(item["id"] == batch["id"] and item["capcut"] for item in listed)
    plain = harness.import_profile(profile="outro")
    assert harness.client.get(f"/api/batches/{plain['id']}").json()["capcut"] is None
