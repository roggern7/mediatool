"""Edição CapCut: visão da lista do seletor ("Limpar lista", `app_state` capcut_library_view). Nada é apagado; vale
para GET /api/capcut/library e para o `filter` do lote ("todos os filtrados"), não para `media_ids` explícitos."""

from __future__ import annotations

import json

import pytest
from capcut_jobs_fakes import FakeCapCutProcessor, add_media

from app.jobs.capcut_runner import CapCutRunner
from app.services import capcut_batch_service


@pytest.fixture
def setup(harness, tmp_path):
    harness.ctx.capcut_processor = FakeCapCutProcessor()
    harness.ctx.capcut_runner = CapCutRunner(harness.ctx)
    return harness, tmp_path / "saida"


def _videos(harness, count: int, prefix: str) -> list[int]:
    return [add_media(harness, name=f"{prefix}{index}") for index in range(count)]


def _library(harness, **params) -> tuple[list[int], int]:
    response = harness.client.get("/api/capcut/library", params={"limit": 200, **params})
    assert response.status_code == 200, response.text
    page = response.json()
    return [item["id"] for item in page["items"]], page["total"]


def _post(harness, path: str, body: dict | None = None) -> dict:
    response = harness.client.post(f"/api/capcut/library/{path}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _batch_ids(harness, out, **selection) -> list[int]:
    payload = {"mode": "caption", "caption": {"overrides": {"output_dir": str(out)}}, **selection}
    response = harness.client.post("/api/capcut/batches", json=payload)
    assert response.status_code == 202, response.text
    batch_id = response.json()["batch"]["id"]
    jobs = harness.client.get(f"/api/capcut/batches/{batch_id}/jobs").json()["items"]
    return sorted(job["media_id"] for job in jobs)


def test_clear_all_then_new_video_appears(setup):
    harness, _ = setup
    old = _videos(harness, 3, "old")
    assert harness.client.get("/api/capcut/library/view").json() == {"cleared": False, "hidden_count": 0}
    assert _post(harness, "clear") == {"cleared": True, "hidden_count": 0}
    assert _library(harness) == ([], 0)
    new = add_media(harness, name="novo")
    assert _library(harness) == ([new], 1)
    stored = json.loads(harness.ctx.app_settings.get_state("capcut_library_view"))
    assert stored == {"cleared_before_id": max(old), "hidden": [], "shown": []}
    # Nada foi apagado.
    assert all(harness.ctx.media.get(media_id) is not None for media_id in old)


def test_clear_all_without_body_and_with_empty_object(setup):
    harness, _ = setup
    first = add_media(harness, name="a")
    response = harness.client.post("/api/capcut/library/clear")
    assert response.status_code == 200, response.text
    assert _library(harness) == ([], 0)
    second = add_media(harness, name="b")
    assert _post(harness, "clear", {}) == {"cleared": True, "hidden_count": 0}
    assert _library(harness) == ([], 0)
    assert harness.ctx.media.get(first) and harness.ctx.media.get(second)


def test_clear_selected_keeps_the_others(setup):
    harness, _ = setup
    ids = _videos(harness, 4, "v")
    assert _post(harness, "clear", {"media_ids": [ids[0], ids[2], ids[2]]}) == {"cleared": False, "hidden_count": 2}
    assert _library(harness) == ([ids[3], ids[1]], 2)
    assert harness.client.get("/api/capcut/library/view").json() == {"cleared": False, "hidden_count": 2}


def test_show_brings_back_hidden_and_cleared(setup):
    harness, _ = setup
    ids = _videos(harness, 3, "v")
    _post(harness, "clear", {"media_ids": [ids[1]]})
    _post(harness, "clear")
    assert _library(harness) == ([], 0)
    # Envio do computador que voltou como duplicata de um vídeo antigo.
    assert _post(harness, "show", {"media_ids": [ids[0]]}) == {"cleared": True, "hidden_count": 0}
    assert _library(harness) == ([ids[0]], 1)
    # Tirar de novo vence o "mostrar".
    _post(harness, "clear", {"media_ids": [ids[0]]})
    assert _library(harness) == ([], 0)
    _post(harness, "show", {"media_ids": [ids[0], ids[2]]})
    assert _library(harness) == ([ids[2], ids[0]], 2)


def test_restore_brings_everything_back(setup):
    harness, _ = setup
    ids = _videos(harness, 3, "v")
    _post(harness, "clear")
    _post(harness, "clear", {"media_ids": [ids[0]]})
    assert _post(harness, "restore") == {"cleared": False, "hidden_count": 0}
    assert _library(harness) == (list(reversed(ids)), 3)
    assert harness.ctx.app_settings.get_state("capcut_library_view") is None


def test_filter_batch_respects_view_and_explicit_ids_do_not(setup):
    harness, out = setup
    old = _videos(harness, 2, "old")
    _post(harness, "clear")
    new = _videos(harness, 2, "new")
    _post(harness, "clear", {"media_ids": [new[0]]})
    assert _batch_ids(harness, out, filter={}) == [new[1]]
    # media_ids explícitos não passam pela visão.
    assert _batch_ids(harness, out, media_ids=[old[0], new[0]]) == [old[0], new[0]]


def test_filter_batch_with_nothing_visible_is_empty(setup):
    harness, out = setup
    _videos(harness, 2, "v")
    _post(harness, "clear")
    payload = {"mode": "caption", "caption": {"overrides": {"output_dir": str(out)}}, "filter": {}}
    response = harness.client.post("/api/capcut/batches", json=payload)
    assert response.status_code == 422 and response.json()["code"] == "batch_empty"


def test_view_lists_are_capped_and_deduplicated(setup, monkeypatch):
    harness, _ = setup
    monkeypatch.setattr(capcut_batch_service, "LIBRARY_VIEW_MAX_IDS", 3)
    _post(harness, "clear", {"media_ids": [1, 2, 2, 3]})
    _post(harness, "clear", {"media_ids": [4, 1]})
    stored = json.loads(harness.ctx.app_settings.get_state("capcut_library_view"))
    assert stored["hidden"] == [3, 4, 1]


def test_corrupt_state_is_an_empty_view(setup):
    harness, _ = setup
    ids = _videos(harness, 2, "v")
    harness.ctx.app_settings.set_state("capcut_library_view", "{nao é json")
    assert harness.client.get("/api/capcut/library/view").json() == {"cleared": False, "hidden_count": 0}
    assert _library(harness)[1] == len(ids)


def test_validation_and_client_header(setup):
    harness, _ = setup
    assert harness.client.post("/api/capcut/library/clear", json={"media_ids": []}).status_code == 422
    assert harness.client.post("/api/capcut/library/clear", json={"media_ids": [0]}).status_code == 422
    assert harness.client.post("/api/capcut/library/clear", json={"extra": 1}).status_code == 422
    assert harness.client.post("/api/capcut/library/show", json={}).status_code == 422
    too_many = list(range(1, 20_002))
    assert harness.client.post("/api/capcut/library/show", json={"media_ids": too_many}).status_code == 422
    for path in ("clear", "show", "restore"):
        response = harness.client.post(f"/api/capcut/library/{path}", json={"media_ids": [1]},
                                       headers={"X-Media-Tool-Client": ""})
        assert response.status_code == 403, path
