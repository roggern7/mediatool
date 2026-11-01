from __future__ import annotations

import pytest

from app.main import create_app
from app.schemas.profile_analyses import MAX_TEXT_CHARS
from app.services.profile_analysis_service import MAX_PROFILES, parse_profile_list
from conftest import BASE_URL, make_settings
from fastapi.testclient import TestClient
from media_fakes import FakeInstagramDownloader, FakeProcessor, MemorySessionProvider
from profile_fakes import ScriptedFetcher, SimClock, analysis_of, create, install_runner, items_of, snapshot

API = "/api/profile-analyses"


# ---- lista colada -------------------------------------------------------------------------------------------------
def test_list_accepts_every_input_form():
    text = "\n".join([
        "@Alpha", "beta", "https://www.instagram.com/gamma/", "https://instagram.com/delta", "instagram.com/epsilon/",
        "www.instagram.com/zeta", "https://www.instagram.com/eta/?hl=pt-br&utm=x", "  theta  ", "﻿iota",
    ])
    parsed = parse_profile_list(text)
    assert parsed.usernames == ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota"]
    assert parsed.invalid == [] and parsed.duplicate_count == 0


def test_list_accepts_http_links_of_instagram_only():
    parsed = parse_profile_list("http://instagram.com/alfa\nHTTP://www.instagram.com/beta/\nhttp://example.com/gama")
    assert parsed.usernames == ["alfa", "beta"]
    assert [entry["line"] for entry in parsed.invalid] == ["http://example.com/gama"]


def test_list_duplicates_compare_after_normalizing_and_keep_first_order():
    parsed = parse_profile_list("@a1\nb2\nA1\nhttps://instagram.com/a1/\n\n   \nB2\nc3")
    assert parsed.usernames == ["a1", "b2", "c3"] and parsed.duplicate_count == 3


def test_list_invalid_lines_have_reason_and_are_truncated():
    long_line = "x" * 500
    parsed = parse_profile_list(f"bom\nhttps://example.com/x\nnome com espaço\n/explore/\n{long_line}")
    assert parsed.usernames == ["bom"]
    assert [entry["line"] for entry in parsed.invalid[:2]] == ["https://example.com/x", "nome com espaço"]
    assert all(entry["reason"] for entry in parsed.invalid)
    assert len(parsed.invalid[-1]["line"]) == 120


# ---- criação ------------------------------------------------------------------------------------------------------
def test_post_returns_analysis_contract(harness):
    response = harness.client.post(API, json={"text": "@a1\nA1\n#ruim\nb2"})
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {
        "id", "status", "total", "done", "pending", "running_username", "invalid_count", "duplicate_count",
        "invalid_lines", "waiting_until", "wait_reason", "error", "created_at", "started_at", "finished_at",
        "created_date", "created_time", "finished_time", "report_ready", "resumable", "summary", "cautious_pace",
    }
    assert body["status"] == "pending" and body["total"] == 2 and body["pending"] == 2 and body["done"] == 0
    assert body["invalid_count"] == 1 and body["duplicate_count"] == 1
    assert body["invalid_lines"][0]["line"] == "#ruim" and body["report_ready"] is False
    assert set(body["summary"]) == {"active", "private", "restricted", "unavailable", "unverified", "cancelled", "stories_yes",
                                    "highlights_yes", "views_total"}
    assert body["created_date"].count("/") == 2 and body["created_time"].count(":") == 2


def test_invalid_lines_stored_are_capped_but_count_is_total(harness):
    text = "bom\n" + "\n".join(f"#ruim{i}" for i in range(60))
    body = harness.client.post(API, json={"text": text}).json()
    assert body["invalid_count"] == 60 and len(body["invalid_lines"]) == 50


@pytest.mark.parametrize("text", ["", "   \n\n", "#ruim\nhttps://example.com/x"])
def test_no_valid_profile_is_400(harness, text):
    response = harness.client.post(API, json={"text": text})
    assert response.status_code == 400 and response.json()["code"] == "no_profiles"
    assert harness.ctx.profile_analyses.count() == 0


def test_too_many_profiles_is_400(harness):
    text = "\n".join(f"perfil{i}" for i in range(MAX_PROFILES + 1))
    response = harness.client.post(API, json={"text": text})
    assert response.status_code == 400 and response.json()["code"] == "too_many_profiles"
    assert "10.000 perfis por análise" in response.json()["detail"]


def test_exact_limit_is_accepted_and_duplicates_do_not_count(harness):
    text = "\n".join(f"perfil{i}" for i in range(MAX_PROFILES)) + "\nperfil0"
    response = harness.client.post(API, json={"text": text})
    assert response.status_code == 202 and response.json()["total"] == MAX_PROFILES


def test_text_longer_than_schema_limit_is_422(harness):
    too_big = harness.client.post(API, json={"text": "a" * (MAX_TEXT_CHARS + 1)})
    assert too_big.status_code == 413 and too_big.json()["code"] == "payload_too_large"


def test_extra_field_is_rejected(harness):
    assert harness.client.post(API, json={"text": "a1", "extra": 1}).status_code == 422


def test_post_needs_client_header(harness):
    client = TestClient(harness.app, base_url=BASE_URL)
    response = client.post(API, json={"text": "a1"})
    assert response.status_code == 403 and response.json()["code"] == "client_header_required"


# ---- leitura ------------------------------------------------------------------------------------------------------
def test_list_is_id_desc_and_paginated(harness):
    ids = [create(harness, f"p{i}")["id"] for i in range(3)]
    body = harness.client.get(API, params={"limit": 2}).json()
    assert body["total"] == 3 and [item["id"] for item in body["items"]] == [ids[2], ids[1]]
    body = harness.client.get(API, params={"limit": 2, "offset": 2}).json()
    assert [item["id"] for item in body["items"]] == [ids[0]]


def test_items_contract_and_pagination(harness):
    analysis = create(harness, "c1", "a2", "b3")
    url = f"{API}/{analysis['id']}/items"
    body = harness.client.get(url, params={"limit": 2, "offset": 1}).json()
    assert body["total"] == 3 and [item["username"] for item in body["items"]] == ["a2", "b3"]
    assert set(body["items"][0]) == {
        "position", "username", "state", "account_status", "display_name", "posts_state", "posts_count", "posts_exact",
        "posts_text", "views_state", "views_total", "views_reels", "stories", "highlights", "error_code",
        "error_message", "checked_at", "checked_date", "checked_time", "attempts",
    }
    assert harness.client.get(url, params={"limit": 201}).status_code == 422
    assert harness.client.get(url, params={"limit": 200}).status_code == 200


@pytest.mark.parametrize(("method", "path"), [
    ("get", "/999"), ("get", "/999/items"), ("post", "/999/cancel"), ("post", "/999/resume"), ("get", "/999/report"),
])
def test_unknown_analysis_is_404(harness, method, path):
    response = getattr(harness.client, method)(f"{API}{path}")
    assert response.status_code == 404 and response.json()["code"] == "analysis_not_found"


def test_running_username_is_reported(harness):
    analysis = create(harness, "a1", "a2")
    repo = harness.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    repo.start_item(repo.next_item(analysis["id"])["id"])
    body = analysis_of(harness, analysis["id"])
    assert body["status"] == "running" and body["running_username"] == "a1" and body["pending"] == 2


# ---- cancelamento -------------------------------------------------------------------------------------------------
def test_cancel_queued_analysis_finishes_now(harness):
    analysis = create(harness, "a1", "a2")
    url = f"{API}/{analysis['id']}/cancel"
    body = harness.client.post(url).json()
    assert body["status"] == "cancelled" and body["report_ready"] is True and body["summary"]["cancelled"] == 2
    assert harness.client.post(url).json()["status"] == "cancelled"


def test_cancel_open_is_idempotent_and_terminal_stays(harness):
    clock = SimClock()
    runner = install_runner(harness, ScriptedFetcher(clock), clock)
    analysis = create(harness, "a1")
    repo = harness.ctx.profile_analyses
    repo.claim_next("2026-11-01T12:00:00.000Z")
    url = f"{API}/{analysis['id']}/cancel"
    assert harness.client.post(url).json()["status"] == "running"
    assert harness.client.post(url).json()["status"] == "running"
    runner.run_next()
    assert analysis_of(harness, analysis["id"])["status"] == "cancelled"

    finished = create(harness, "b1")
    runner.run_next()
    assert harness.client.post(f"{API}/{finished['id']}/cancel").json()["status"] == "completed"


# ---- relatório ----------------------------------------------------------------------------------------------------
def test_report_of_open_analysis_is_409(harness):
    analysis = create(harness, "a1")
    response = harness.client.get(f"{API}/{analysis['id']}/report")
    assert response.status_code == 409 and response.json()["code"] == "analysis_running"


def test_report_downloads_same_origin_without_mutation_header(harness):
    clock = SimClock()
    runner = install_runner(harness, ScriptedFetcher(clock), clock)
    analysis = create(harness, "a1")
    runner.run_next()
    # Navegação comum de `<a href>`: sem header do painel e sem Origin.
    response = TestClient(harness.app, base_url=BASE_URL).get(f"{API}/{analysis['id']}/report")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="relatorio-perfis-2026-11-01.zip"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert int(response.headers["content-length"]) == len(response.content) > 0


def test_extension_origin_cannot_reach_analysis_routes(harness):
    response = harness.client.get(API, headers={"Origin": "chrome-extension://" + "a" * 32})
    assert response.status_code == 403


def test_openapi_lists_routes(harness):
    paths = set(harness.client.get("/openapi.json").json()["paths"])
    assert {API, f"{API}/{{analysis_id}}", f"{API}/{{analysis_id}}/items", f"{API}/{{analysis_id}}/cancel",
            f"{API}/{{analysis_id}}/resume", f"{API}/{{analysis_id}}/report"} <= paths


# ---- persistência -------------------------------------------------------------------------------------------------
def test_history_survives_recreating_the_app(harness_factory):
    first = harness_factory()
    clock = SimClock()
    fetcher = ScriptedFetcher(clock, {"a2": [snapshot("a2", "private", stories="unverified")]})
    runner = install_runner(first, fetcher, clock)
    analysis = create(first, "a1", "a2")
    runner.run_next()
    before = items_of(first, analysis["id"])
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)
    assert items_of(second, analysis["id"]) == before
    body = analysis_of(second, analysis["id"])
    assert body["status"] == "completed" and body["summary"]["private"] == 1 and body["summary"]["active"] == 1
    assert second.client.get(API).json()["total"] == 1


def test_profile_fetcher_is_injectable_through_create_app(tmp_path):
    fetcher = ScriptedFetcher()
    app = create_app(make_settings(tmp_path), session_provider=MemorySessionProvider(),
                     downloader_factory=FakeInstagramDownloader, processor=FakeProcessor(), start_worker=False,
                     profile_fetcher=fetcher)
    assert app.state.ctx.profile_fetcher is fetcher
