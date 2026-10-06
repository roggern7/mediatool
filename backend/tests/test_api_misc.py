from __future__ import annotations

from fastapi.testclient import TestClient

from media_fakes import remote


def test_health(harness):
    body = harness.client.get("/api/health").json()
    assert body["status"] == "ok" and body["version"] and body["time"].endswith("Z")


def test_diagnostics_shape(harness):
    response = harness.client.get("/api/diagnostics")
    assert response.status_code == 200
    body = response.json()
    for key in ("generated_at", "overall", "checks", "python", "sqlite", "ffmpeg", "ffprobe", "gallery_dl",
                "storage", "worker", "instagram", "backend"):
        assert key in body
    assert body["sqlite"]["schema_version"] >= 1
    assert body["backend"]["port"] == 8765
    assert body["instagram"]["session_configured"] is True


def test_docs_and_openapi(harness):
    assert harness.client.get("/docs").status_code == 200
    spec = harness.client.get("/openapi.json").json()
    paths = set(spec["paths"])
    expected = {
        "/api/health", "/api/diagnostics", "/api/presets", "/api/presets/{preset_id}",
        "/api/presets/{preset_id}/duplicate", "/api/instagram/profile", "/api/imports", "/api/batches",
        "/api/batches/{batch_id}", "/api/batches/{batch_id}/cancel", "/api/batches/{batch_id}/retry-failed",
        "/api/media", "/api/media/{media_id}", "/api/media/{media_id}/thumbnail", "/api/media/{media_id}/original",
        "/api/renditions/{rendition_id}/file", "/api/media/{media_id}/process", "/api/media/process",
        "/api/renditions/{rendition_id}", "/api/media/{media_id}/open", "/api/jobs", "/api/jobs/{job_id}",
        "/api/jobs/{job_id}/cancel", "/api/jobs/{job_id}/retry", "/api/settings", "/api/settings/instagram/session",
        "/api/settings/instagram/session/test",
    }
    assert expected <= paths


def test_frontend_placeholder_and_spa_fallback(harness_factory):
    harness = harness_factory()
    page = harness.client.get("/")
    assert page.status_code == 200 and "npm run build" in page.text

    dist = harness.settings.frontend_dist
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>painel</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    assert harness.client.get("/").text == "<html>painel</html>"
    assert harness.client.get("/biblioteca").text == "<html>painel</html>"
    assert harness.client.get("/assets/app.js").text == "console.log(1)"
    assert harness.client.get("/assets/nao-existe.js").status_code == 404
    assert harness.client.get("/..%2F..%2Fsecret.txt").status_code == 404
    api_missing = harness.client.get("/api/nao-existe")
    assert api_missing.status_code == 404 and api_missing.json()["code"] == "not_found"


def test_host_validation(harness):
    other = TestClient(harness.app, base_url="http://evil.example:8765")
    response = other.get("/api/health")
    assert response.status_code == 400


def test_jobs_listing_filters(harness):
    harness.downloader.items = [remote("J1"), remote("J2"), remote("J3")]
    batch = harness.import_profile()
    harness.run_imports()
    client = harness.client
    all_jobs = client.get("/api/jobs").json()
    assert all_jobs["total"] == 3 and all_jobs["counts"]["pending"] == 3
    ids = [j["id"] for j in all_jobs["items"]]
    subset = client.get("/api/jobs", params={"ids": f"{ids[0]},{ids[1]}"}).json()
    assert subset["total"] == 2
    harness.run_jobs()
    done = client.get("/api/jobs", params={"status": "completed,failed", "batch_id": batch["id"]}).json()
    assert done["total"] == 3 and done["counts"]["completed"] == 3
    assert done["items"][0]["elapsed_seconds"] is not None
    assert client.get("/api/jobs", params={"status": "pending"}).json()["total"] == 0
    for params in ({"status": "bogus"}, {"ids": "1,abc"}, {"ids": "0"}, {"limit": 501}, {"batch_id": 0}):
        response = client.get("/api/jobs", params=params)
        assert response.status_code == 422, params
        assert response.json()["code"] == "validation_error"
    assert client.get("/api/jobs/999").status_code == 404
    assert client.get("/api/jobs/abc").status_code == 422


def test_error_format_and_oversized_ids(harness):
    response = harness.client.get(f"/api/presets/{2**60}")
    assert response.status_code == 422 and set(response.json()) >= {"detail", "code"}
    response = harness.client.post("/api/presets", content=b"{not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and response.json()["code"] == "validation_error"


def test_body_size_limit(harness):
    big = b"x" * (1024 * 1024 + 1)
    response = harness.client.post("/api/presets", content=big, headers={"Content-Type": "application/json"})
    assert response.status_code == 413 and response.json()["code"] == "payload_too_large"

    def chunks():
        for _ in range(17):
            yield b"y" * 65536

    streamed = harness.client.post("/api/presets", content=chunks(), headers={"Content-Type": "application/json"})
    assert streamed.status_code == 413
    small = harness.client.post("/api/presets", json={"name": "Pequeno", "params": {
        "metadata_cleanup": True, "trim_start": 0, "mirror": True, "speed": 1, "auto_adjust": True,
        "reencode": True}})
    assert small.status_code == 201


def test_storage_error_is_mapped(harness, monkeypatch):
    from app.services.storage import StorageError

    harness.downloader.items = [remote("SE")]
    harness.import_profile()
    harness.run_all()
    media_id = harness.client.get("/api/media").json()["items"][0]["id"]

    def broken(relative):
        raise StorageError("caminho estranho")

    monkeypatch.setattr(harness.ctx.storage, "resolve", broken)
    response = harness.client.delete(f"/api/media/{media_id}", params={"delete_original": "true"})
    assert response.status_code == 500 and response.json()["code"] == "storage_error"
    assert "caminho estranho" not in response.text


def test_stage_deletion_only_touches_media_files(harness):
    import pytest as _pytest

    from app.services.storage import StorageError

    storage = harness.ctx.storage
    folder = storage.originals / "2026"
    folder.mkdir(parents=True)
    for target in (storage.root, storage.originals, folder, storage.temp / "x.txt", storage.root / "fora.txt"):
        with _pytest.raises(StorageError):
            storage.stage_deletion([target])
    assert folder.is_dir() and storage.originals.is_dir()
