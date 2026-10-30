"""API da extensão do Chrome: status, page-info, post-owner, start (importação unificada) e progress."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.security import CLIENT_HEADER
from app.integrations.instagram.downloader import InstagramError
from app.schemas.batches import UnifiedImportRequest
from app.services import import_service
from media_fakes import FakeInstagramDownloader, MemorySessionProvider, remote

EXT_ORIGIN = "chrome-extension://lnpealhnkmjaohdlkpapjmdealokkdbf"   # ID fixo da nossa extensão


def connect(harness) -> dict[str, str]:
    code = harness.client.post("/api/settings/extension/pairing-code").json()["code"]
    token = harness.client.post("/api/ext/pair", json={"code": code, "device_name": "Teste"},
                                headers={"Origin": EXT_ORIGIN}).json()["token"]
    return {"Origin": EXT_ORIGIN, CLIENT_HEADER: "extension", "Authorization": f"Bearer {token}"}


@pytest.fixture
def ext(harness):
    return harness, connect(harness)


# --------------------------------------------------------------------------- status


def test_status(ext):
    harness, headers = ext
    body = harness.client.get("/api/ext/status", headers=headers).json()
    assert body["ok"] is True
    assert body["app"]["version"]
    assert body["instagram"]["session_configured"] is True
    assert body["instagram"]["paused"] is False
    assert body["instagram"]["message"] is None
    assert body["defaults"]["media"] == "videos"
    assert body["defaults"]["limit"] == 50   # mesmo padrão da tela Importar (Últimas 50)


def test_status_without_session(harness_factory):
    harness = harness_factory(session_provider=MemorySessionProvider(configured=False))
    body = harness.client.get("/api/ext/status", headers=connect(harness)).json()
    assert body["instagram"]["session_configured"] is False
    assert body["instagram"]["message"] == import_service.SESSION_MISSING_MESSAGE


# --------------------------------------------------------------------------- page-info


@pytest.mark.parametrize("url,expected", [
    ("https://www.instagram.com/p/ABCDE12345/", {"kind": "post", "shortcode": "ABCDE12345",
                                                 "canonical_url": "https://www.instagram.com/p/ABCDE12345/"}),
    ("https://www.instagram.com/reel/ABCDE12345/?igsh=xyz", {"kind": "reel", "shortcode": "ABCDE12345",
                                                             "canonical_url": "https://www.instagram.com/reel/ABCDE12345/"}),
    ("https://instagram.com/reels/ABCDE12345", {"kind": "reel", "shortcode": "ABCDE12345",
                                                "canonical_url": "https://www.instagram.com/reel/ABCDE12345/"}),
    ("https://www.instagram.com/fulano/p/ABCDE12345/", {"kind": "post", "shortcode": "ABCDE12345",
                                                        "canonical_url": "https://www.instagram.com/p/ABCDE12345/",
                                                        "username": "fulano"}),
    ("https://www.instagram.com/tv/ABCDE12345/", {"kind": "reel", "shortcode": "ABCDE12345",
                                                  "canonical_url": "https://www.instagram.com/p/ABCDE12345/"}),
    ("https://www.instagram.com/Fulano.Silva/", {"kind": "profile", "username": "fulano.silva",
                                                 "canonical_url": "https://www.instagram.com/fulano.silva/"}),
    ("https://www.instagram.com/fulano/reels/", {"kind": "profile", "username": "fulano",
                                                 "canonical_url": "https://www.instagram.com/fulano/"}),
    ("https://www.instagram.com/", {"kind": "other"}),
    ("https://www.instagram.com/explore/", {"kind": "other"}),
    ("https://www.instagram.com/direct/inbox/", {"kind": "other"}),
    ("https://www.instagram.com/stories/fulano/123/", {"kind": "other"}),
    ("https://www.instagram.com/fulano/followers/", {"kind": "other"}),
    ("https://example.com/p/ABCDE12345/", {"kind": "other"}),
    ("chrome://extensions", {"kind": "other"}),
    ("nada", {"kind": "other"}),
])
def test_page_info(ext, url, expected):
    harness, headers = ext
    response = harness.client.post("/api/ext/page-info", json={"url": url}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == expected


def test_page_info_does_not_touch_instagram(ext, monkeypatch):
    harness, headers = ext
    monkeypatch.setattr(harness.ctx, "downloader_factory", lambda: pytest.fail("page-info chamou o Instagram"))
    assert harness.client.post("/api/ext/page-info", json={"url": "https://www.instagram.com/p/ABCDE12345/"},
                               headers=headers).status_code == 200


def test_page_info_body_limits(ext):
    harness, headers = ext
    assert harness.client.post("/api/ext/page-info", json={"url": "x" * 3000}, headers=headers).status_code == 422
    assert harness.client.post("/api/ext/page-info", json={}, headers=headers).status_code == 422


# --------------------------------------------------------------------------- post-owner


def test_post_owner_resolves_through_downloader(harness_factory):
    downloader = FakeInstagramDownloader(items=[remote("ABCDE12345", profile="Dono_Real")])
    harness = harness_factory(downloader=downloader)
    response = harness.client.post("/api/ext/post-owner", json={"url": "https://www.instagram.com/reel/ABCDE12345/"},
                                   headers=connect(harness))
    assert response.status_code == 200, response.text
    assert response.json() == {"username": "dono_real", "shortcode": "ABCDE12345"}


def test_post_owner_uses_username_in_url_without_request(ext, monkeypatch):
    harness, headers = ext
    monkeypatch.setattr(harness.ctx, "downloader_factory", lambda: pytest.fail("não precisava consultar"))
    response = harness.client.post("/api/ext/post-owner", json={"url": "https://www.instagram.com/fulano/p/ABCDE12345/"},
                                   headers=headers)
    assert response.json() == {"username": "fulano", "shortcode": "ABCDE12345"}


def test_post_owner_errors(harness_factory):
    downloader = FakeInstagramDownloader(items=[])
    harness = harness_factory(downloader=downloader)
    headers = connect(harness)
    not_post = harness.client.post("/api/ext/post-owner", json={"url": "https://www.instagram.com/fulano/"},
                                   headers=headers)
    assert not_post.status_code == 422
    missing = harness.client.post("/api/ext/post-owner", json={"url": "https://www.instagram.com/p/ABCDE12345/"},
                                  headers=headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "owner_not_found"

    def refuse(*_args, **_kwargs):
        raise InstagramError("content_unavailable", "Publicação indisponível.")

    downloader.resolve_post = refuse
    refused = harness.client.post("/api/ext/post-owner", json={"url": "https://www.instagram.com/p/ABCDE12345/"},
                                  headers=headers)
    assert refused.status_code == 404
    assert refused.json()["code"] == "content_unavailable"


# --------------------------------------------------------------------------- start


def test_start_calls_start_unified_with_same_model(ext, monkeypatch):
    harness, headers = ext
    seen: list[Any] = []

    def fake_start(ctx, req):
        seen.append((ctx, req))
        return {"batch_ids": [7], "group_id": None, "links_batch_id": None, "profiles": 1, "links": 0,
                "invalid": [], "duplicates": 0}

    monkeypatch.setattr(import_service, "start_unified", fake_start)
    body = {"inputs": ["https://www.instagram.com/fulano/"], "media": "covers", "limit": 50}
    response = harness.client.post("/api/ext/start", json=body, headers=headers)
    assert response.status_code == 202, response.text
    assert response.json()["batch_ids"] == [7]
    assert response.json()["open_url"] == "http://127.0.0.1:8765/fila?lote=7"
    ctx, req = seen[0]
    assert ctx is harness.ctx
    assert isinstance(req, UnifiedImportRequest) and req.media == "covers" and req.limit == 50


def test_start_validation_error_is_422(ext, monkeypatch):
    harness, headers = ext
    monkeypatch.setattr(import_service, "start_unified", lambda *_: pytest.fail("não devia iniciar"))
    for body in ({"media": "videos"}, {"inputs": ["fulano"], "media": "gifs"}, {"inputs": ["fulano"], "extra": 1}):
        response = harness.client.post("/api/ext/start", json=body, headers=headers)
        assert response.status_code == 422, body


def test_start_propagates_app_errors(ext, monkeypatch):
    from app.core.errors import AppError
    harness, headers = ext

    def missing(*_):
        raise AppError(409, "session_missing", import_service.SESSION_MISSING_MESSAGE)

    monkeypatch.setattr(import_service, "start_unified", missing)
    response = harness.client.post("/api/ext/start", json={"inputs": ["fulano"]}, headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "session_missing"
    assert response.headers["access-control-allow-origin"] == EXT_ORIGIN


# --------------------------------------------------------------------------- progress


def test_progress_batch_and_group(ext):
    harness, headers = ext
    batch = harness.import_profile(limit=5)
    response = harness.client.get(f"/api/ext/progress?batch_ids={batch['id']},999", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    summary = body["batches"][0]
    assert summary["id"] == batch["id"]
    assert set(summary) >= {"status", "found", "downloaded", "duplicates", "filtered", "skipped", "failed",
                            "active", "finished", "open_url"}
    assert summary["open_url"] == f"http://127.0.0.1:8765/fila?lote={batch['id']}"
    assert body["missing"] == [999]
    group = harness.client.post("/api/import-groups", json={"profiles": ["um", "dois"], "content_type": "all",
                                                            "limit": 5, "preset_id": None, "process": False})
    assert group.status_code == 202, group.text
    group_id = group.json()["group"]["id"]
    body = harness.client.get(f"/api/ext/progress?group_id={group_id}", headers=headers).json()
    assert body["group"]["id"] == group_id
    assert body["group"]["profiles"]["total"] == 2
    assert body["open_url"] == "http://127.0.0.1:8765/"


@pytest.mark.parametrize("query", ["", "batch_ids=abc", "batch_ids=1,,2", "batch_ids=0", "group_id=0",
                                   "batch_ids=" + ",".join(str(i) for i in range(1, 60))])
def test_progress_validates_ids(ext, query):
    harness, headers = ext
    assert harness.client.get(f"/api/ext/progress?{query}", headers=headers).status_code == 422


# --------------------------------------------------------------------------- integração real com start_unified


def test_start_real_unified_import_and_progress(ext):
    harness, headers = ext
    body = {"inputs": ["https://www.instagram.com/fulano/", "https://www.instagram.com/p/ABCDE12345/"],
            "media": "videos", "limit": 5}
    response = harness.client.post("/api/ext/start", json=body, headers=headers)
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["profiles"] == 1 and result["links"] == 1
    assert result["links_batch_id"] in result["batch_ids"]
    ids = ",".join(str(i) for i in result["batch_ids"])
    progress = harness.client.get(f"/api/ext/progress?batch_ids={ids}", headers=headers).json()
    assert sorted(b["id"] for b in progress["batches"]) == sorted(result["batch_ids"])
    assert progress["finished"] is False


def test_start_real_unified_without_session(harness_factory):
    harness = harness_factory(session_provider=MemorySessionProvider(configured=False))
    response = harness.client.post("/api/ext/start", json={"inputs": ["fulano"], "limit": 5},
                                   headers=connect(harness))
    assert response.status_code == 409
    assert response.json()["code"] == "session_missing"
