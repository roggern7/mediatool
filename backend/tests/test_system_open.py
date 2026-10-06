import pytest

from app.services import system_open as so
from app.services.system_open import SystemOpenError, open_path


@pytest.fixture()
def calls(monkeypatch):
    recorded = {"startfile": [], "popen": []}
    monkeypatch.setattr(so.os, "startfile", lambda p: recorded["startfile"].append(p), raising=False)

    def fake_popen(args, **kwargs):
        recorded["popen"].append((args, kwargs))
        return None

    monkeypatch.setattr(so.subprocess, "Popen", fake_popen)
    return recorded


@pytest.fixture()
def root(tmp_path):
    r = tmp_path / "storage"
    (r / "originals").mkdir(parents=True)
    (r / "originals" / "a.mp4").write_bytes(b"x")
    (r / "originals" / "run.bat").write_bytes(b"echo")
    return r


def test_open_file(calls, root):
    open_path(root / "originals" / "a.mp4", allowed_roots=[root], target="file")
    assert calls["startfile"] == [str((root / "originals" / "a.mp4").resolve())]
    assert calls["popen"] == []


def test_open_folder_uses_explorer_select_without_shell(calls, root):
    open_path(root / "originals" / "a.mp4", allowed_roots=[root], target="folder")
    assert len(calls["popen"]) == 1
    args, kwargs = calls["popen"][0]
    assert isinstance(args, list)
    assert args[0].lower().endswith("explorer.exe")
    assert args[1] == f"/select,{(root / 'originals' / 'a.mp4').resolve()}"
    assert kwargs.get("shell") is False
    assert calls["startfile"] == []


def test_outside_allowed_roots(calls, root, tmp_path):
    other = tmp_path / "other.mp4"
    other.write_bytes(b"x")
    with pytest.raises(SystemOpenError) as info:
        open_path(other, allowed_roots=[root], target="file")
    assert info.value.code == "not_allowed"
    with pytest.raises(SystemOpenError) as info:
        open_path(root / ".." / "other.mp4", allowed_roots=[root], target="folder")
    assert info.value.code == "not_allowed"
    assert calls == {"startfile": [], "popen": []}


def test_missing_path(calls, root):
    with pytest.raises(SystemOpenError) as info:
        open_path(root / "originals" / "nope.mp4", allowed_roots=[root], target="file")
    assert info.value.code == "not_found"


def test_folder_as_file_target(calls, root):
    with pytest.raises(SystemOpenError) as info:
        open_path(root / "originals", allowed_roots=[root], target="file")
    assert info.value.code == "not_found"


def test_non_media_file_is_not_executed(calls, root):
    with pytest.raises(SystemOpenError) as info:
        open_path(root / "originals" / "run.bat", allowed_roots=[root], target="file")
    assert info.value.code == "not_allowed"
    assert calls["startfile"] == []


def test_invalid_target(calls, root):
    with pytest.raises(SystemOpenError) as info:
        open_path(root / "originals" / "a.mp4", allowed_roots=[root], target="run")  # type: ignore[arg-type]
    assert info.value.code == "invalid_target"


def test_unsupported_platform(monkeypatch, root):
    monkeypatch.delattr(so.os, "startfile", raising=False)
    with pytest.raises(SystemOpenError) as info:
        open_path(root / "originals" / "a.mp4", allowed_roots=[root], target="file")
    assert info.value.code == "unsupported"


def test_os_error_is_controlled(calls, monkeypatch, root):
    def boom(p):
        raise OSError("sem desktop")

    monkeypatch.setattr(so.os, "startfile", boom, raising=False)
    with pytest.raises(SystemOpenError) as info:
        open_path(root / "originals" / "a.mp4", allowed_roots=[root], target="file")
    assert info.value.code == "failed"


def test_explorer_env_is_filtered(calls, monkeypatch, root):
    monkeypatch.setenv("INSTAGRAM_SESSIONID", "FAKE-SESSION-0000000000")
    monkeypatch.setenv("instagram_other", "x")
    monkeypatch.setenv("MY_SESSION_FILE", "x")
    monkeypatch.setenv("Api_Token", "x")
    monkeypatch.setenv("GH_COOKIE_JAR", "x")
    monkeypatch.setenv("APP_SECRET_KEY", "x")
    monkeypatch.setenv("db_password", "x")
    monkeypatch.setenv("MEDIA_TOOL_KEEP_ME", "1")
    open_path(root / "originals" / "a.mp4", allowed_roots=[root], target="folder")
    env = calls["popen"][0][1]["env"]
    upper = {k.upper() for k in env}
    for removed in ("INSTAGRAM_SESSIONID", "INSTAGRAM_OTHER", "MY_SESSION_FILE", "API_TOKEN", "GH_COOKIE_JAR",
                    "APP_SECRET_KEY", "DB_PASSWORD"):
        assert removed not in upper
    assert env.get("MEDIA_TOOL_KEEP_ME") == "1"
    assert "FAKE-SESSION-0000000000" not in env.values()
    assert any(k.upper() == "SYSTEMROOT" for k in env)
