"""PATCH /api/settings/instagram/session/cookies: só cookies auxiliares, nunca valores nas respostas."""

from __future__ import annotations

import json

import pytest

from app.integrations.instagram.session import LocalInstagramSessionProvider
from media_fakes import FAKE_SESSION

URL = "/api/settings/instagram/session/cookies"
FAKE_CSRF = "FAKECSRF000000"
FAKE_UID = "FAKEUID12345"
FAKE_MID = "FAKEMID-0000"


@pytest.fixture
def provider(tmp_path):
    return LocalInstagramSessionProvider(tmp_path / "secrets", env={})


@pytest.fixture
def app_with_session(harness_factory, provider):
    harness = harness_factory(session_provider=provider)
    saved = harness.client.put("/api/settings/instagram/session", json={"sessionid": FAKE_SESSION})
    assert saved.status_code == 200 and saved.json()["extra_keys"] == []
    return harness


def _no_values(response):
    for value in (FAKE_SESSION, FAKE_CSRF, FAKE_UID, FAKE_MID):
        assert value not in response.text


def test_add_replace_and_remove_cookies(app_with_session, provider):
    client = app_with_session.client
    added = client.patch(URL, json={"csrftoken": FAKE_CSRF, "ds_user_id": FAKE_UID})
    assert added.status_code == 200, added.text
    assert added.json()["extra_keys"] == ["csrftoken", "ds_user_id"]
    assert added.json()["session_configured"] is True and added.json()["source"] == "file"
    _no_values(added)
    assert provider.get().cookies() == {"sessionid": FAKE_SESSION, "csrftoken": FAKE_CSRF, "ds_user_id": FAKE_UID}

    removed = client.patch(URL, json={"csrftoken": "", "ds_user_id": None, "mid": FAKE_MID})
    assert removed.status_code == 200 and removed.json()["extra_keys"] == ["mid"]
    assert provider.get().cookies() == {"sessionid": FAKE_SESSION, "mid": FAKE_MID}
    _no_values(removed)
    status = client.get("/api/settings")
    assert status.json()["instagram"]["extra_keys"] == ["mid"]
    _no_values(status)


def test_changing_cookies_clears_last_test(app_with_session):
    client = app_with_session.client
    assert client.post("/api/settings/instagram/session/test").json()["last_test"] is not None
    response = client.patch(URL, json={"mid": FAKE_MID})
    assert response.json()["last_test"] is None


def test_without_file_session_is_not_editable(harness_factory, tmp_path):
    env_only = LocalInstagramSessionProvider(tmp_path / "secrets-env", env={"INSTAGRAM_SESSIONID": FAKE_SESSION})
    for provider in (LocalInstagramSessionProvider(tmp_path / "vazio", env={}), env_only):
        harness = harness_factory(session_provider=provider)
        response = harness.client.patch(URL, json={"csrftoken": FAKE_CSRF})
        assert response.status_code == 409
        assert response.json() == {"detail": "Configure primeiro o sessionid.", "code": "session_not_editable"}
        _no_values(response)


def test_validation_never_echoes_values(app_with_session):
    client = app_with_session.client
    cases = [
        {},
        {"sessionid": FAKE_SESSION},
        {"rur": "FAKERUR0000"},
        {"csrftoken": "FAKE CSRF com espaço"},
        {"csrftoken": "x" * 513},
        {"FAKE-SESSION-ZZZ1234567890": "FAKEVALUE9999"},
    ]
    for body in cases:
        response = client.patch(URL, json=body)
        assert response.status_code == 422, body
        assert response.json()["code"] == "validation_error"
        for value in body.values():
            if len(str(value)) >= 6:
                assert str(value) not in response.text
        for key in body:
            if key.startswith("FAKE"):
                assert key not in response.text
    assert client.patch(URL, json={"csrftoken": FAKE_CSRF}, headers={"X-Media-Tool-Client": ""}).status_code == 403


def test_last_test_record_and_db_have_no_cookie_values(app_with_session):
    harness = app_with_session
    harness.client.patch(URL, json={"csrftoken": FAKE_CSRF, "ds_user_id": FAKE_UID})
    harness.client.post("/api/settings/instagram/session/test")
    harness.ctx.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for path in harness.settings.data_dir.glob("media-tool.db*"):
        data = path.read_bytes()
        for value in (FAKE_SESSION, FAKE_CSRF, FAKE_UID):
            assert value.encode() not in data
    raw = harness.ctx.app_settings.get_state("instagram_session_last_test")
    assert FAKE_CSRF not in json.dumps(raw)
