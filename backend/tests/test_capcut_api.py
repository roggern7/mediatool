"""Legendas CapCut: API (status, configurações, modelos, jobs, arquivo, pasta) com núcleo falso."""

from __future__ import annotations

import pytest
from capcut_jobs_fakes import FakeCapCutError, add_media, fake_core

from app.services import capcut_service

PANEL = {"X-Media-Tool-Client": "panel"}


@pytest.fixture
def core(monkeypatch):
    fake = fake_core()
    monkeypatch.setattr(capcut_service, "load_core", lambda: fake)
    return fake


@pytest.fixture
def no_core(monkeypatch):
    def missing():
        raise ImportError("No module named 'faster_whisper'")

    monkeypatch.setattr(capcut_service, "load_core", missing)


def test_status_without_core_is_unavailable_not_500(harness, no_core):
    response = harness.client.get("/api/capcut/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is False and body["installed"] is False and body["reason"]
    assert body["deps"] == {"faster_whisper": False, "uiautomation": False, "pywin32": False}
    assert body["queue"]["pending"] == 0 and body["queue"]["paused"] is False
    assert body["settings"]["whisper_model"] == "large-v3"


def test_status_with_core_and_core_failure(harness, core, monkeypatch):
    body = harness.client.get("/api/capcut/status").json()
    assert body["available"] is True and body["version"] == "8.0.0" and body["models"]["large-v3-turbo"] is True

    def broken(_data_dir):
        raise RuntimeError("quebrou")

    monkeypatch.setattr(core, "status", broken)
    body = harness.client.get("/api/capcut/status").json()
    assert body["available"] is False and body["installed"] is False and body["reason"]


def test_settings_defaults_update_and_validation(harness, tmp_path):
    settings = harness.client.get("/api/capcut/settings").json()
    assert settings["output_dir_is_default"] is True
    assert settings["output_dir"] == str(harness.settings.storage_dir / "capcut")
    assert (settings["resolution"], settings["bitrate"], settings["fps"]) == ("original", "recommended", "60")
    assert (settings["max_words"], settings["max_chars"], settings["max_attempts"]) == (3, 24, 3)
    assert settings["highlight"] is True and settings["auto_after_download"] is False

    target = tmp_path / "saida" / "legendas"
    response = harness.client.put("/api/capcut/settings", json={
        "output_dir": str(target), "resolution": "1080P", "max_words": 2, "highlight_color": "#FF0000",
        "auto_after_download": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["output_dir"] == str(target.resolve()) and body["output_dir_is_default"] is False
    assert target.is_dir(), "a pasta é criada ao salvar"
    assert body["resolution"] == "1080P" and body["max_words"] == 2 and body["auto_after_download"] is True
    assert body["bitrate"] == "recommended", "campos não enviados não mudam"

    for patch in ({"resolution": "8K"}, {"max_words": 0}, {"highlight_color": "vermelho"}, {"output_dir": "relativa"},
                  {"output_dir": "\\\\servidor\\share\\x"}, {"whisper_model": "tiny"}, {"desconhecido": 1},
                  {"max_attempts": 11}, {"output_dir": "C:\\"}):
        response = harness.client.put("/api/capcut/settings", json=patch)
        assert response.status_code == 422, (patch, response.text)
    assert harness.client.get("/api/capcut/settings").json()["resolution"] == "1080P"

    reset = harness.client.put("/api/capcut/settings", json={"output_dir": None, "highlight_color": None,
                                                              "resolution": None}).json()
    assert reset["output_dir_is_default"] is True and reset["highlight_color"] is None
    assert reset["resolution"] == "original"


def test_mutations_need_client_header(harness):
    response = harness.client.put("/api/capcut/settings", json={"max_words": 2}, headers={"X-Media-Tool-Client": ""})
    assert response.status_code == 403


def test_drafts(harness, core, monkeypatch, no_core):
    response = harness.client.get("/api/capcut/drafts")
    assert response.status_code == 409 and response.json()["code"] == "capcut_unavailable"
    monkeypatch.setattr(capcut_service, "load_core", lambda: core)
    assert harness.client.get("/api/capcut/drafts").json()[0]["name"] == "Meu modelo"

    def failing():
        raise FakeCapCutError("drafts_dir_missing", "Pasta de projetos do CapCut não encontrada.")

    monkeypatch.setattr(core, "list_caption_drafts", failing)
    response = harness.client.get("/api/capcut/drafts")
    assert response.status_code == 409 and response.json()["code"] == "drafts_dir_missing"


def test_styles_crud_and_default(harness, core):
    styles = harness.client.get("/api/capcut/styles").json()
    assert [(s["id"], s["builtin"], s["is_default"]) for s in styles] == [(0, True, True)]
    assert styles[0]["name"] == "Padrão (capcut-agent-skill)" and styles[0]["summary"]["font"] == "Padrão"

    created = harness.client.post("/api/capcut/styles", json={"name": "Amarelo", "draft_name": "Meu modelo",
                                                               "segment_index": 1})
    assert created.status_code == 201, created.text
    style = created.json()
    assert style["summary"]["font"] == "Montserrat" and style["source_draft"] == "Meu modelo"
    dup = harness.client.post("/api/capcut/styles", json={"name": "amarelo", "draft_name": "Meu modelo"})
    assert dup.status_code == 409 and dup.json()["code"] == "style_name_taken"
    missing = harness.client.post("/api/capcut/styles", json={"name": "Outro", "draft_name": "sumiu"})
    assert missing.status_code == 409 and missing.json()["code"] == "draft_not_found"

    styles = harness.client.post(f"/api/capcut/styles/{style['id']}/default").json()
    assert {s["id"]: s["is_default"] for s in styles} == {0: False, style["id"]: True}
    assert harness.client.get("/api/capcut/settings").json()["default_style_id"] == style["id"]
    assert harness.client.post("/api/capcut/styles/999/default").status_code == 404

    assert harness.client.delete(f"/api/capcut/styles/{style['id']}").status_code == 204
    assert harness.client.get("/api/capcut/settings").json()["default_style_id"] is None, "padrão não fica órfão"
    assert harness.client.get("/api/capcut/styles").json()[0]["is_default"] is True
    assert harness.client.delete(f"/api/capcut/styles/{style['id']}").status_code == 404


def test_create_jobs_skips_and_snapshots_options(harness, core, tmp_path):
    video = add_media(harness, name="v1")
    image = add_media(harness, media_type="image", name="i1")
    cover = add_media(harness, role="cover", name="c1")
    style = harness.client.post("/api/capcut/styles", json={"name": "Meu", "draft_name": "Meu modelo"}).json()
    out = tmp_path / "out"
    response = harness.client.post("/api/capcut/jobs", headers=PANEL, json={
        "media_ids": [video, image, cover, 999, video], "style_id": style["id"],
        "overrides": {"output_dir": str(out), "max_words": 1}})
    assert response.status_code == 202, response.text
    body = response.json()
    assert [job["media_id"] for job in body["created"]] == [video]
    assert {(s["media_id"], s["reason"]) for s in body["skipped"]} == {
        (image, "not_video"), (cover, "cover"), (999, "media_not_found")}
    job = body["created"][0]
    assert job["status"] == "pending" and job["origin"] == "manual" and job["style_name"] == "Meu"
    assert job["options"]["max_words"] == 1 and job["options"]["output_dir"] == str(out.resolve())
    assert "style" not in job["options"] and job["media_filename"] == "v1.mp4"
    stored = capcut_service.repo(harness.ctx).get_job(job["id"])
    assert stored["options"]["style"]["font"] == "Montserrat" and stored["source_path"] == "originals/test/v1.mp4"

    harness.client.put("/api/capcut/settings", json={"max_words": 5, "resolution": "720P"})
    again = harness.client.get(f"/api/capcut/jobs/{job['id']}").json()
    assert again["options"]["max_words"] == 1 and again["options"]["resolution"] == "original", "instantâneo"

    repeated = harness.client.post("/api/capcut/jobs", json={"media_ids": [video]}).json()
    assert repeated["created"] == [] and repeated["skipped"] == [{"media_id": video, "reason": "already_queued"}]
    other = add_media(harness, name="v2")
    via_api = harness.client.post("/api/capcut/jobs", json={"media_ids": [other]}).json()["created"][0]
    assert via_api["origin"] == "api" and via_api["options"]["max_words"] == 5 and via_api["style_name"].startswith(
        "Padrão")

    assert harness.client.post("/api/capcut/jobs", json={"media_ids": []}).status_code == 422
    assert harness.client.post("/api/capcut/jobs", json={"media_ids": list(range(1, 502))}).status_code == 422
    missing_style = harness.client.post("/api/capcut/jobs", json={"media_ids": [other], "style_id": 77})
    assert missing_style.status_code == 404

    listing = harness.client.get("/api/capcut/jobs").json()
    assert [item["id"] for item in listing["items"]] == [via_api["id"], job["id"]] and listing["total"] == 2
    assert listing["counts"]["pending"] == 2
    assert harness.client.get("/api/capcut/jobs", params={"media_id": video}).json()["total"] == 1
    assert harness.client.get("/api/capcut/jobs", params={"status": "failed"}).json()["total"] == 0
    assert harness.client.get("/api/capcut/jobs", params={"status": "x"}).status_code == 422


def test_cancel_retry_delete(harness):
    video = add_media(harness)
    job = harness.client.post("/api/capcut/jobs", json={"media_ids": [video]}).json()["created"][0]
    assert harness.client.delete(f"/api/capcut/jobs/{job['id']}").json()["code"] == "capcut_job_active"
    assert harness.client.post(f"/api/capcut/jobs/{job['id']}/retry").json()["code"] == "capcut_job_not_retryable"
    cancelled = harness.client.post(f"/api/capcut/jobs/{job['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and cancelled["finished_at"]
    repo = capcut_service.repo(harness.ctx)
    repo.update_job(job["id"], attempts=2, transcript=[{"text": "oi"}])
    retried = harness.client.post(f"/api/capcut/jobs/{job['id']}/retry").json()
    assert retried["status"] == "pending" and retried["attempts"] == 0 and retried["has_transcript"] is True
    harness.client.post(f"/api/capcut/jobs/{job['id']}/cancel")
    assert harness.client.delete(f"/api/capcut/jobs/{job['id']}").status_code == 204
    assert harness.client.get(f"/api/capcut/jobs/{job['id']}").status_code == 404
    assert harness.client.post("/api/capcut/jobs/999/cancel").status_code == 404


def test_file_and_reveal(harness, tmp_path, monkeypatch):
    video = add_media(harness)
    out = tmp_path / "saida"
    job = harness.client.post("/api/capcut/jobs", json={"media_ids": [video],
                                                         "overrides": {"output_dir": str(out)}}).json()["created"][0]
    assert harness.client.get(f"/api/capcut/jobs/{job['id']}/file").status_code == 404
    repo = capcut_service.repo(harness.ctx)
    final = out / "v_legendado.mp4"
    final.write_bytes(b"VIDEO")
    repo.update_job(job["id"], status="completed", output_path=str(final))
    response = harness.client.get(f"/api/capcut/jobs/{job['id']}/file")
    assert response.status_code == 200 and response.content == b"VIDEO"
    assert harness.client.get(f"/api/capcut/jobs/{job['id']}").json()["output_exists"] is True

    opened = []
    monkeypatch.setattr(capcut_service, "open_path",
                        lambda path, *, allowed_roots, target: opened.append((path, allowed_roots, target)))
    assert harness.client.post(f"/api/capcut/jobs/{job['id']}/reveal").json() == {"ok": True}
    assert opened == [(final.resolve(), [out.resolve()], "folder")]

    outside = tmp_path / "fora.mp4"
    outside.write_bytes(b"X")
    repo.update_job(job["id"], output_path=str(outside))
    assert harness.client.get(f"/api/capcut/jobs/{job['id']}/file").status_code == 403
    assert harness.client.post(f"/api/capcut/jobs/{job['id']}/reveal").status_code == 403
    final.unlink()
    repo.update_job(job["id"], output_path=str(final))
    assert harness.client.get(f"/api/capcut/jobs/{job['id']}/file").status_code == 404


def test_queue_pause_resume(harness, no_core):
    assert harness.client.post("/api/capcut/queue/pause").json() == {"paused": True}
    assert harness.client.get("/api/capcut/status").json()["queue"]["paused"] is True
    assert harness.client.post("/api/capcut/queue/resume").json() == {"paused": False}
    assert harness.ctx.app_settings.get_state("capcut_queue_paused") is None
