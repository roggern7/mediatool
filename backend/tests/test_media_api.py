from __future__ import annotations

import os

import pytest

from app.services import media_service
from app.services.system_open import SystemOpenError
from media_fakes import remote


@pytest.fixture
def library(harness):
    harness.downloader.items = [remote("IMG", profile="alpha"), remote("VID", media_type="video", profile="beta"),
                                remote("CAR", index=1, profile="alpha")]
    harness.import_profile()
    harness.run_all()
    media = {m["source_post_id"]: m for m in harness.client.get("/api/media").json()["items"]}
    preset_id = harness.client.get("/api/presets").json()[0]["id"]
    harness.client.post(f"/api/media/{media['VID']['id']}/process", json={"preset_id": preset_id})
    harness.run_jobs()
    return harness, {k: harness.client.get(f"/api/media/{v['id']}").json() for k, v in media.items()}


def test_list_views_and_filters(library):
    harness, media = library
    client = harness.client
    assert client.get("/api/media").json()["total"] == 3
    processed = client.get("/api/media", params={"view": "processed"}).json()
    assert [m["source_post_id"] for m in processed["items"]] == ["VID"]
    originals = client.get("/api/media", params={"view": "originals"}).json()
    assert sorted(m["source_post_id"] for m in originals["items"]) == ["CAR", "IMG"]
    assert client.get("/api/media", params={"media_type": "video"}).json()["total"] == 1
    assert client.get("/api/media", params={"profile": "@alpha"}).json()["total"] == 2
    assert client.get("/api/media", params={"q": "VI"}).json()["total"] == 1
    assert client.get("/api/media", params={"q": "%"}).json()["total"] == 0
    page = client.get("/api/media", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    for params in ({"limit": 201}, {"limit": 0}, {"view": "x"}, {"media_type": "audio"}, {"offset": -1}):
        response = client.get("/api/media", params=params)
        assert response.status_code == 422 and response.json()["code"] == "validation_error"


def test_media_shape_and_files(library):
    harness, media = library
    client = harness.client
    video = media["VID"]
    assert video["original_url"] == f"/api/media/{video['id']}/original"
    assert video["thumbnail_url"] == f"/api/media/{video['id']}/thumbnail"
    assert len(video["renditions"]) == 1 and video["last_job"]["kind"] == "process"
    rendition = video["renditions"][0]
    assert rendition["file_url"] == f"/api/renditions/{rendition['id']}/file"
    assert "processed_path" not in rendition and "original_path" not in video

    original = client.get(video["original_url"])
    assert original.status_code == 200 and original.headers["content-type"] == "video/mp4"
    thumb = client.get(video["thumbnail_url"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert client.get(rendition["file_url"]).status_code == 200
    assert client.get("/api/media/999").status_code == 404
    assert client.get("/api/renditions/999/file").status_code == 404


def test_open_file_and_folder(library, monkeypatch):
    harness, media = library
    calls = []

    def fake_open(path, *, allowed_roots, target):
        calls.append((path, list(allowed_roots), target))

    monkeypatch.setattr(media_service, "open_path", fake_open)
    video = media["VID"]
    response = harness.client.post(f"/api/media/{video['id']}/open", json={"target": "folder"})
    assert response.status_code == 200 and response.json() == {"ok": True}
    rendition_id = video["renditions"][0]["id"]
    harness.client.post(f"/api/media/{video['id']}/open", json={"target": "file", "rendition_id": rendition_id})
    storage_root = harness.ctx.storage.root
    assert all(allowed == [storage_root] for _, allowed, _ in calls)
    assert calls[0][0].is_relative_to(storage_root / "originals") and calls[0][2] == "folder"
    assert calls[1][0].is_relative_to(storage_root / "processed") and calls[1][2] == "file"

    other = media["IMG"]
    wrong = harness.client.post(f"/api/media/{other['id']}/open", json={"target": "file", "rendition_id": rendition_id})
    assert wrong.status_code == 404
    assert harness.client.post(f"/api/media/{video['id']}/open", json={"target": "exe"}).status_code == 422

    def failing(path, *, allowed_roots, target):
        raise SystemOpenError("unsupported", "Sem desktop.")

    monkeypatch.setattr(media_service, "open_path", failing)
    failed = harness.client.post(f"/api/media/{video['id']}/open", json={"target": "file"})
    assert failed.status_code == 501 and failed.json() == {"detail": "Sem desktop.", "code": "unsupported"}


def test_delete_rendition(library):
    harness, media = library
    video = media["VID"]
    rendition = video["renditions"][0]
    path = harness.ctx.storage.resolve(harness.ctx.media.get_rendition(rendition["id"])["processed_path"])
    assert path.is_file()
    assert harness.client.delete(f"/api/renditions/{rendition['id']}").status_code == 204
    assert not path.exists()
    assert harness.client.get(f"/api/media/{video['id']}").json()["renditions"] == []
    original = harness.ctx.storage.resolve(harness.ctx.media.get(video["id"])["original_path"])
    assert original.is_file()
    assert harness.client.delete(f"/api/renditions/{rendition['id']}").status_code == 404


def test_delete_media_requires_confirmation_and_removes_everything(library):
    harness, media = library
    video = media["VID"]
    row = harness.ctx.media.get(video["id"])
    storage = harness.ctx.storage
    files = [storage.resolve(row["original_path"]), storage.resolve(row["thumbnail_path"])]
    files += [storage.resolve(r["processed_path"]) for r in harness.ctx.media.renditions_for([video["id"]])[video["id"]]]
    assert all(f.is_file() for f in files)

    refused = harness.client.delete(f"/api/media/{video['id']}")
    assert refused.status_code == 400 and refused.json()["code"] == "confirmation_required"
    assert harness.client.delete(f"/api/media/{video['id']}", params={"delete_original": "false"}).status_code == 400
    assert all(f.is_file() for f in files)

    assert harness.client.delete(f"/api/media/{video['id']}", params={"delete_original": "true"}).status_code == 204
    assert not any(f.exists() for f in files)
    assert harness.client.get(f"/api/media/{video['id']}").status_code == 404
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM renditions WHERE media_id = ?", (video["id"],)) == 0
    leftovers = [p for p in storage.root.rglob("*") if p.is_file() and ".deleting-" in p.name]
    assert leftovers == []
    assert harness.client.get("/api/media").json()["total"] == 2


def test_delete_media_file_in_use_keeps_rows_and_files(library, monkeypatch):
    harness, media = library
    video = media["VID"]
    row = harness.ctx.media.get(video["id"])
    original = harness.ctx.storage.resolve(row["original_path"])
    real_rename = os.rename

    def rename(src, dst):
        if os.fspath(src) == os.fspath(original):
            raise PermissionError(32, "arquivo em uso")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    response = harness.client.delete(f"/api/media/{video['id']}", params={"delete_original": "true"})
    assert response.status_code == 409 and response.json()["code"] == "file_in_use"
    monkeypatch.setattr(os, "rename", real_rename)
    # Nada foi apagado: linha e todos os arquivos continuam consistentes
    again = harness.client.get(f"/api/media/{video['id']}").json()
    assert len(again["renditions"]) == 1
    for rendition in harness.ctx.media.renditions_for([video["id"]])[video["id"]]:
        assert harness.ctx.storage.resolve(rendition["processed_path"]).is_file()
    assert original.is_file() and harness.ctx.storage.resolve(row["thumbnail_path"]).is_file()


def test_delete_media_with_active_job_is_refused(library):
    harness, media = library
    image = media["IMG"]
    job = harness.ctx.media.last_jobs([image["id"]])[image["id"]]
    harness.ctx.jobs.update(job["id"], status="processing")
    response = harness.client.delete(f"/api/media/{image['id']}", params={"delete_original": "true"})
    assert response.status_code == 409 and response.json()["code"] == "media_busy"
