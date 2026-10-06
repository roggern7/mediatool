from __future__ import annotations

import sys
from pathlib import Path

from app.integrations.instagram.downloader import SessionTestResult
from app.integrations.instagram.session import LocalInstagramSessionProvider
from media_fakes import FAKE_SESSION


def _provider(root):
    return LocalInstagramSessionProvider(root / "secrets", env={})


def _assert_no_secret_anywhere(harness, responses):
    for response in responses:
        assert FAKE_SESSION not in response.text
    for path in (harness.settings.data_dir).glob("media-tool.db*"):
        assert FAKE_SESSION.encode() not in path.read_bytes(), path.name
    for path in harness.settings.log_dir.glob("*"):
        if path.is_file():
            assert FAKE_SESSION not in path.read_text(encoding="utf-8", errors="replace")


def test_settings_never_expose_session(harness_factory, tmp_path):
    harness = harness_factory(session_provider=_provider(tmp_path))
    client = harness.client
    before = client.get("/api/settings")
    assert before.json()["instagram"]["session_configured"] is False

    saved = client.put("/api/settings/instagram/session", json={"sessionid": FAKE_SESSION, "csrftoken": "FAKECSRF0000"})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"session_configured": True, "source": "file", "updated_at": saved.json()["updated_at"],
                            "extra_keys": ["csrftoken"], "paused_until": None, "pause_reason": None,
                            "pause_scope": None, "next_attempt_at": None, "action_required": False,
                            "last_test": None}

    harness.downloader.session_result = SessionTestResult(ok=True, code="ok", message="Sessão válida.",
                                                          username="conta_teste")
    tested = client.post("/api/settings/instagram/session/test")
    assert tested.status_code == 200
    last = tested.json()["last_test"]
    assert last["ok"] is True and last["username"] == "conta_teste" and last["tested_at"].endswith("Z")

    after = client.get("/api/settings")
    diagnostics = client.get("/api/diagnostics")
    assert after.json()["instagram"]["session_configured"] is True
    _assert_no_secret_anywhere(harness, [saved, tested, after, diagnostics])
    harness.ctx.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _assert_no_secret_anywhere(harness, [])

    cleared = client.delete("/api/settings/instagram/session")
    assert cleared.json()["session_configured"] is False


def test_invalid_session_is_rejected_without_echo(harness):
    secret_like = "FAKE SESSION 0000 with spaces"
    response = harness.client.put("/api/settings/instagram/session", json={"sessionid": secret_like})
    assert response.status_code == 422
    assert secret_like not in response.text
    extra = harness.client.put("/api/settings/instagram/session", json={"sessionid": FAKE_SESSION, "rur": "x"})
    assert extra.status_code == 422 and FAKE_SESSION not in extra.text


def test_failed_session_test_is_recorded(harness):
    harness.downloader.session_result = SessionTestResult(ok=False, code="session_expired",
                                                          message="A sessão expirou.", username=None)
    response = harness.client.post("/api/settings/instagram/session/test")
    assert response.json()["last_test"]["ok"] is False
    assert response.json()["last_test"]["code"] == "session_expired"


def test_update_settings_and_runtime_concurrency(harness):
    client = harness.client
    response = client.put("/api/settings", json={"concurrency": 2, "download_concurrency": 3})
    assert response.status_code == 200 and response.json()["concurrency"] == 2
    assert response.json()["download_concurrency"] == 3
    assert harness.ctx.worker.concurrency == 2 and harness.ctx.worker.download_concurrency == 3
    assert client.put("/api/settings", json={"concurrency": 3}).status_code == 422  # FFmpeg: máximo 2
    assert client.put("/api/settings", json={"download_concurrency": 5}).status_code == 422
    assert client.put("/api/settings", json={"concurrency": 0}).status_code == 422
    assert client.put("/api/settings", json={"unknown": 1}).status_code == 422

    preset_id = client.get("/api/presets").json()[1]["id"]
    assert client.put("/api/settings", json={"default_preset_id": preset_id}).json()["default_preset_id"] == preset_id
    assert client.put("/api/settings", json={"default_preset_id": 999}).status_code == 404
    assert client.put("/api/settings", json={"default_preset_id": None}).json()["default_preset_id"] is None

    assert client.put("/api/settings", json={"ffmpeg_path": "relativo/ffmpeg.exe"}).status_code == 422
    assert client.put("/api/settings", json={"ffmpeg_path": None}).json()["ffmpeg_path"] is None

    body = client.get("/api/settings").json()
    assert set(body) == {"ffmpeg_path", "ffprobe_path", "concurrency", "download_concurrency",
                         "processing_concurrency", "disk", "default_preset_id",
                         "instagram_provider",
                         "instagram_web_doc_ids", "effective", "storage", "database_path", "instagram"}
    assert set(body["storage"]) == {"originals", "processed", "temp", "thumbs"}


def test_mutations_require_client_header(harness):
    from fastapi.testclient import TestClient

    bare = TestClient(harness.app, base_url="http://127.0.0.1:8765")
    response = bare.put("/api/settings", json={"concurrency": 2})
    assert response.status_code == 403
    assert bare.get("/api/settings").status_code == 200


# ---- caminhos de ferramenta, segredos e origens ---------------------------------------------------------------


def test_tool_path_validation(harness, tmp_path):
    import shutil as _shutil

    client = harness.client
    python_exe = client.put("/api/settings", json={"ffmpeg_path": sys.executable})
    assert python_exe.status_code == 422 and python_exe.json()["code"] == "invalid_path"
    unc = client.put("/api/settings", json={"ffmpeg_path": r"\\servidor\share\ffmpeg.exe"})
    assert unc.status_code == 422 and "UNC" in unc.json()["detail"]
    assert client.put("/api/settings", json={"ffmpeg_path": "//servidor/share/ffmpeg.exe"}).status_code == 422
    missing = client.put("/api/settings", json={"ffmpeg_path": str(tmp_path / "ffmpeg.exe")})
    assert missing.status_code == 422 and missing.json()["code"] == "invalid_path"

    impostor = tmp_path / "fake" / "ffmpeg.exe"
    impostor.parent.mkdir()
    _shutil.copyfile(sys.executable, impostor)
    rejected = client.put("/api/settings", json={"ffmpeg_path": str(impostor)})
    assert rejected.status_code == 422 and rejected.json()["code"] == "invalid_tool"

    real_ffmpeg = _shutil.which("ffmpeg")
    real_ffprobe = _shutil.which("ffprobe")
    if real_ffmpeg and real_ffprobe:
        swapped = client.put("/api/settings", json={"ffprobe_path": real_ffmpeg})
        assert swapped.status_code == 422 and swapped.json()["code"] == "invalid_path"
        ok = client.put("/api/settings", json={"ffmpeg_path": real_ffmpeg, "ffprobe_path": real_ffprobe})
        assert ok.status_code == 200, ok.text
        assert ok.json()["ffmpeg_path"].lower() == str(Path(real_ffmpeg).resolve()).lower()
    # Falha de validação não grava nada parcialmente
    before = client.get("/api/settings").json()
    assert client.put("/api/settings", json={"concurrency": 2, "ffmpeg_path": sys.executable}).status_code == 422
    assert client.get("/api/settings").json()["concurrency"] == before["concurrency"]


def test_tool_info_is_cached(harness):
    calls = {"ffmpeg": 0, "ffprobe": 0}
    original_ffmpeg, original_ffprobe = harness.processor.ffmpeg_info, harness.processor.ffprobe_info

    def count_ffmpeg():
        calls["ffmpeg"] += 1
        return original_ffmpeg()

    def count_ffprobe():
        calls["ffprobe"] += 1
        return original_ffprobe()

    harness.processor.ffmpeg_info = count_ffmpeg
    harness.processor.ffprobe_info = count_ffprobe
    harness.ctx.processors.invalidate()
    for _ in range(3):
        harness.client.get("/api/settings")
        harness.client.get("/api/diagnostics")
    assert calls == {"ffmpeg": 1, "ffprobe": 1}


def test_unknown_session_keys_are_not_echoed(harness):
    secret_key = "FAKE-SESSION-ZZZ1234567890"
    response = harness.client.put("/api/settings/instagram/session", json={secret_key: "x"})
    assert response.status_code == 422
    assert secret_key not in response.text
    assert "<campo desconhecido>" in response.text
    nested = harness.client.put("/api/settings/instagram/session",
                                json={"sessionid": FAKE_SESSION, "sessionid_FAKEZZZ999": "x"})
    assert nested.status_code == 422 and "FAKEZZZ999" not in nested.text and FAKE_SESSION not in nested.text


def test_session_values_are_secret_until_provider(harness):
    from app.schemas.settings import SessionUpdate

    model = SessionUpdate(sessionid=FAKE_SESSION, csrftoken="FAKECSRF0000")
    assert FAKE_SESSION not in repr(model) and FAKE_SESSION not in str(model.model_dump())
    saved: list = []

    class Recorder(type(harness.session_provider)):
        def save(self, sessionid, extra=None):
            saved.append((sessionid, extra))

    harness.ctx.session_provider = Recorder()
    harness.client.put("/api/settings/instagram/session", json={"sessionid": FAKE_SESSION, "csrftoken": "FAKECSRF0000"})
    assert saved == [(FAKE_SESSION, {"csrftoken": "FAKECSRF0000"})]


def test_instagram_sessionid_is_removed_from_process_environment(monkeypatch, tmp_path):
    import os

    from app.core.config import load_settings

    monkeypatch.setenv("INSTAGRAM_SESSIONID", FAKE_SESSION)
    settings = load_settings(root=tmp_path)
    assert "INSTAGRAM_SESSIONID" not in os.environ
    assert settings.env["INSTAGRAM_SESSIONID"] == FAKE_SESSION
    assert FAKE_SESSION not in repr(settings)
    explicit = load_settings({"INSTAGRAM_SESSIONID": FAKE_SESSION}, root=tmp_path)
    assert explicit.env["INSTAGRAM_SESSIONID"] == FAKE_SESSION


def test_dev_origins_only_in_dev_mode(harness_factory):
    from fastapi.testclient import TestClient

    body = {"concurrency": 2}
    prod = harness_factory()
    vite = {"Origin": "http://127.0.0.1:5173", "X-Media-Tool-Client": "panel"}
    bare = TestClient(prod.app, base_url="http://127.0.0.1:8765")
    assert bare.put("/api/settings", json=body, headers=vite).status_code == 403
    same = {"Origin": "http://127.0.0.1:8765", "X-Media-Tool-Client": "panel"}
    assert bare.put("/api/settings", json=body, headers=same).status_code == 200
    preflight = bare.options("/api/settings", headers={"Origin": "http://localhost:5173",
                                                       "Access-Control-Request-Method": "PUT"})
    assert "access-control-allow-origin" not in preflight.headers

    dev = harness_factory(dev_mode=True)
    dev_client = TestClient(dev.app, base_url="http://127.0.0.1:8765")
    assert dev_client.put("/api/settings", json=body, headers=vite).status_code == 200
    preflight = dev_client.options("/api/settings", headers={"Origin": "http://localhost:5173",
                                                             "Access-Control-Request-Method": "PUT",
                                                             "Access-Control-Request-Headers": "X-Media-Tool-Client"})
    assert preflight.headers.get("access-control-allow-origin") == "http://localhost:5173"
