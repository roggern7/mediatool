import copy
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.logging import redact
from app.integrations.instagram import session as session_mod
from app.integrations.instagram.session import (
    SESSION_FILENAME,
    InstagramSession,
    InstagramSessionProvider,
    LocalInstagramSessionProvider,
    SessionStatus,
    SessionStorageError,
    SessionValidationError,
)

FAKE_SESSIONID = "FAKE-SESSION-0000000000"
FAKE_ENV_SESSIONID = "FAKE-SESSION-ENV-1111111111"
FAKE_CSRF = "FAKECSRF0000000"
BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture()
def provider(tmp_path):
    return LocalInstagramSessionProvider(tmp_path / "secrets", env={})


def test_is_provider_subclass(provider):
    assert isinstance(provider, InstagramSessionProvider)


def test_status_empty(provider):
    status = provider.status()
    assert isinstance(status, SessionStatus)
    assert status.configured is False
    assert status.source is None
    assert provider.get() is None


def test_save_get_clear_roundtrip(provider):
    provider.save(FAKE_SESSIONID, {"csrftoken": FAKE_CSRF, "ds_user_id": "1234567"})
    assert provider.path.name == SESSION_FILENAME
    assert provider.path.is_file()
    status = provider.status()
    assert status.configured is True
    assert status.source == "file"
    assert status.updated_at and status.updated_at.endswith("Z")
    sess = provider.get()
    assert sess is not None
    assert sess.cookies() == {"sessionid": FAKE_SESSIONID, "csrftoken": FAKE_CSRF, "ds_user_id": "1234567"}
    provider.clear()
    assert not provider.path.exists()
    assert provider.status().configured is False
    provider.clear()  # idempotente


def test_file_never_contains_plaintext(provider):
    provider.save(FAKE_SESSIONID, {"csrftoken": FAKE_CSRF})
    raw = provider.path.read_bytes()
    assert FAKE_SESSIONID.encode() not in raw
    assert FAKE_CSRF.encode() not in raw
    leftovers = [p.name for p in provider.path.parent.iterdir() if p.name != SESSION_FILENAME]
    assert leftovers == []


@pytest.mark.skipif(os.name != "nt", reason="DPAPI só existe no Windows")
def test_real_dpapi_roundtrip_and_file_header(provider):
    blob = session_mod.dpapi_protect(b"payload-de-teste")
    assert blob != b"payload-de-teste"
    assert session_mod.dpapi_unprotect(blob) == b"payload-de-teste"
    provider.save(FAKE_SESSIONID)
    raw = provider.path.read_bytes()
    assert raw.startswith(b"MTDPAPI1\n")
    plain = json.loads(session_mod.dpapi_unprotect(raw[len(b"MTDPAPI1\n"):]))
    assert plain["cookies"]["sessionid"] == FAKE_SESSIONID


@pytest.mark.skipif(os.name != "nt", reason="DPAPI só existe no Windows")
def test_dpapi_entropy_mismatch_is_not_readable(provider, monkeypatch):
    provider.save(FAKE_SESSIONID)
    monkeypatch.setattr(session_mod, "_DPAPI_ENTROPY", b"outra-entropia")
    status = provider.status()
    assert status.configured is False
    assert status.error and "decifrar" in status.error
    assert provider.get() is None


def test_env_fallback_and_file_precedence(tmp_path):
    p = LocalInstagramSessionProvider(tmp_path / "s", env={"INSTAGRAM_SESSIONID": FAKE_ENV_SESSIONID})
    status = p.status()
    assert (status.configured, status.source, status.updated_at) == (True, "env", None)
    assert p.get().cookies() == {"sessionid": FAKE_ENV_SESSIONID}
    p.save(FAKE_SESSIONID)
    assert p.status().source == "file"
    assert p.get().cookies()["sessionid"] == FAKE_SESSIONID
    p.clear()
    assert p.status().source == "env"


def test_invalid_env_value_is_not_configured(tmp_path):
    p = LocalInstagramSessionProvider(tmp_path / "s", env={"INSTAGRAM_SESSIONID": "FAKE SESSION WITH SPACES"})
    status = p.status()
    assert status.configured is False
    assert "SPACES" not in (status.error or "")
    assert p.get() is None


@pytest.mark.parametrize(
    "content",
    [b"", b"lixo", b"MTDPAPI1\n" + b"\x00" * 40, b"MTPLAIN1\n{nao-json", b'MTPLAIN1\n{"cookies": {}}',
     b'MTPLAIN1\n{"cookies": {"sessionid": "curto"}}'],
)
def test_corrupted_file_reports_safe_reason(provider, content):
    provider.path.parent.mkdir(parents=True, exist_ok=True)
    provider.path.write_bytes(content)
    status = provider.status()
    assert status.configured is False
    assert status.error
    assert provider.get() is None


def test_corrupted_file_does_not_fall_back_to_env(tmp_path):
    p = LocalInstagramSessionProvider(tmp_path / "s", env={"INSTAGRAM_SESSIONID": FAKE_ENV_SESSIONID})
    p.path.parent.mkdir(parents=True)
    p.path.write_bytes(b"corrompido")
    assert p.status().configured is False
    assert p.get() is None


@pytest.mark.parametrize(
    "bad",
    ["", "short", "x" * 513, "FAKE SESSION 0000000000", "FAKE-SESSION-000\n0000000",
     "FAKE-SESSION-0000000000;evil=1", "FAKE\"SESSION\"0000000000", "FAKE-SESSION-ção-000000"],
)
def test_invalid_sessionid_rejected_without_value_in_error(provider, bad):
    with pytest.raises(SessionValidationError) as info:
        provider.save(bad)
    if len(bad) >= 6:
        assert bad not in str(info.value)
        assert bad not in repr(info.value)
    assert not provider.path.exists()


def test_extra_keys_validation(provider):
    with pytest.raises(SessionValidationError) as info:
        provider.save(FAKE_SESSIONID, {"authorization": "FAKE-TOKEN-000000"})
    assert "FAKE-TOKEN-000000" not in str(info.value)
    with pytest.raises(SessionValidationError) as info:
        provider.save(FAKE_SESSIONID, {"csrftoken": "FAKE CSRF COM ESPACO"})
    assert "FAKE CSRF COM ESPACO" not in str(info.value)
    with pytest.raises(SessionValidationError):
        provider.save(FAKE_SESSIONID, {"rur": "a" * 600})
    provider.save(FAKE_SESSIONID, {"mid": "FAKE-MID-0000", "ig_did": None, "rur": ""})
    assert provider.get().cookies() == {"sessionid": FAKE_SESSIONID, "mid": "FAKE-MID-0000"}


def test_session_object_is_opaque():
    sess = InstagramSession({"sessionid": FAKE_SESSIONID})
    for text in (repr(sess), str(sess), f"{sess}", f"{sess!r}", "%s" % sess, repr([sess]), repr({"s": sess})):
        assert FAKE_SESSIONID not in text
        assert "<redacted>" in text
    with pytest.raises(TypeError):
        json.dumps(sess)
    with pytest.raises(TypeError):
        pickle.dumps(sess)
    with pytest.raises(TypeError):
        copy.deepcopy(sess)
    with pytest.raises(AttributeError):
        sess.foo = 1
    returned = sess.cookies()
    returned["sessionid"] = "mutado"
    assert sess.cookies()["sessionid"] == FAKE_SESSIONID


def test_saved_and_loaded_values_are_registered_for_redaction(tmp_path):
    p = LocalInstagramSessionProvider(tmp_path / "s", env={})
    p.save(FAKE_SESSIONID, {"csrftoken": FAKE_CSRF})
    assert FAKE_SESSIONID not in redact(f"valor {FAKE_SESSIONID} no texto")
    assert FAKE_CSRF not in redact(f"token {FAKE_CSRF}")
    env_value = "FAKE-SESSION-ENV-REG-2222222"
    LocalInstagramSessionProvider(tmp_path / "x", env={"INSTAGRAM_SESSIONID": env_value}).get()
    assert env_value not in redact(env_value)


def test_status_model_has_no_secret(provider):
    provider.save(FAKE_SESSIONID)
    dumped = provider.status().model_dump_json()
    assert FAKE_SESSIONID not in dumped


def _run_cli(args, secrets_dir, stdin_text=""):
    env = {k: v for k, v in os.environ.items() if k != "INSTAGRAM_SESSIONID"}
    env["MEDIA_TOOL_SECRETS_DIR"] = str(secrets_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "app.integrations.instagram.session_cli", *args],
        input=stdin_text.encode("utf-8"), capture_output=True, cwd=BACKEND_DIR, env=env, timeout=60, shell=False,
    )


def test_cli_set_status_clear_never_echoes(tmp_path):
    secrets_dir = tmp_path / "cli-secrets"
    result = _run_cli(["set"], secrets_dir, f"{FAKE_SESSIONID}\ncsrftoken={FAKE_CSRF}\n")
    out = (result.stdout + result.stderr).decode("utf-8")
    assert result.returncode == 0, out
    assert "Sessão salva" in out
    assert FAKE_SESSIONID not in out and FAKE_CSRF not in out
    p = LocalInstagramSessionProvider(secrets_dir, env={})
    assert p.get().cookies() == {"sessionid": FAKE_SESSIONID, "csrftoken": FAKE_CSRF}

    status = _run_cli(["status"], secrets_dir)
    assert status.returncode == 0
    assert "Sessão configurada" in status.stdout.decode("utf-8")

    cleared = _run_cli(["clear"], secrets_dir)
    assert cleared.returncode == 0
    assert not p.path.exists()


def test_cli_rejects_invalid_without_echo(tmp_path):
    bad = "FAKE SESSION INVALIDA 0000"
    result = _run_cli(["set"], tmp_path / "s", bad + "\n")
    out = (result.stdout + result.stderr).decode("utf-8")
    assert result.returncode == 2
    assert bad not in out
    empty = _run_cli(["set"], tmp_path / "s", "")
    assert empty.returncode == 2


@pytest.mark.parametrize("argv", [["set", FAKE_SESSIONID], [FAKE_SESSIONID], ["status", "--x", FAKE_SESSIONID]])
def test_cli_does_not_accept_or_echo_value_in_argv(tmp_path, argv):
    result = _run_cli(argv, tmp_path / "s")
    out = (result.stdout + result.stderr).decode("utf-8")
    assert result.returncode == 2
    assert FAKE_SESSIONID not in out
    assert "argumentos inválidos" in out
    assert not (tmp_path / "s" / SESSION_FILENAME).exists()


def test_save_with_invalid_directory_is_controlled(tmp_path):
    blocker = tmp_path / "arquivo"
    blocker.write_bytes(b"x")
    for bad_dir in (blocker, blocker / "sub", tmp_path / "inva<lido"):
        p = LocalInstagramSessionProvider(bad_dir, env={})
        with pytest.raises(SessionStorageError) as info:
            p.save(FAKE_SESSIONID)
        assert FAKE_SESSIONID not in str(info.value)
    assert sorted(x.name for x in tmp_path.iterdir()) == ["arquivo"]


def _acl_lines(path):
    out = subprocess.run(["icacls", str(path)], capture_output=True, shell=False, timeout=30)
    text = out.stdout.decode("mbcs", errors="replace")
    return [line for line in text.splitlines() if ":(" in line]


@pytest.mark.skipif(os.name != "nt", reason="ACL NTFS")
def test_windows_acl_restricted_to_current_user(tmp_path):
    secrets_dir = tmp_path / "novo" / "secrets"
    p = LocalInstagramSessionProvider(secrets_dir, env={})
    p.save(FAKE_SESSIONID)
    user = os.environ["USERNAME"].lower()
    for target in (p.path, secrets_dir):
        lines = _acl_lines(target)
        assert len(lines) == 1, (target, lines)
        assert user in lines[0].lower()
        assert "(I)" not in lines[0]
    p.save(FAKE_SESSIONID)  # regravar sobre arquivo restrito continua funcionando
    assert len(_acl_lines(p.path)) == 1
    assert p.get().cookies()["sessionid"] == FAKE_SESSIONID


@pytest.mark.skipif(os.name != "nt", reason="ACL NTFS")
def test_acl_failure_only_warns(tmp_path, monkeypatch, caplog):
    def broken_run(*args, **kwargs):
        raise OSError("icacls indisponível")

    monkeypatch.setattr(session_mod.subprocess, "run", broken_run)
    p = LocalInstagramSessionProvider(tmp_path / "s", env={})
    with caplog.at_level("WARNING", logger="media_tool.session"):
        p.save(FAKE_SESSIONID)
    assert p.get().cookies()["sessionid"] == FAKE_SESSIONID
    warnings = [r.getMessage() for r in caplog.records if r.name == "media_tool.session"]
    assert warnings and all(FAKE_SESSIONID not in w for w in warnings)


@pytest.mark.skipif(os.name != "nt", reason="SID do Windows")
def test_current_user_sid_format():
    sid = session_mod.current_user_sid()
    assert sid and sid.startswith("S-1-5-")


# --------------------------------------------------------------------------- update_extra / extra_keys


def test_update_extra_keeps_sessionid_and_lists_only_key_names(provider):
    provider.save(FAKE_SESSIONID, {"csrftoken": FAKE_CSRF})
    assert provider.status().extra_keys == ["csrftoken"]
    provider.update_extra({"ds_user_id": "1234567", "mid": "FAKE-MID-0000", "ig_did": "FAKE-IGDID-0000"})
    assert provider.get().cookies() == {
        "sessionid": FAKE_SESSIONID, "csrftoken": FAKE_CSRF, "ds_user_id": "1234567",
        "mid": "FAKE-MID-0000", "ig_did": "FAKE-IGDID-0000",
    }
    status = provider.status()
    assert status.extra_keys == ["csrftoken", "ds_user_id", "ig_did", "mid"]
    dumped = status.model_dump_json()
    for value in (FAKE_SESSIONID, FAKE_CSRF, "FAKE-MID-0000", "FAKE-IGDID-0000"):
        assert value not in dumped
    raw = provider.path.read_bytes()
    assert b"FAKE-MID-0000" not in raw and FAKE_SESSIONID.encode() not in raw
    assert FAKE_SESSIONID not in redact("FAKE-IGDID-0000 " + FAKE_SESSIONID)
    assert "FAKE-IGDID-0000" not in redact("valor FAKE-IGDID-0000")


def test_update_extra_replaces_and_removes(provider):
    provider.save(FAKE_SESSIONID, {"csrftoken": FAKE_CSRF, "mid": "FAKE-MID-0000"})
    provider.update_extra({"csrftoken": "FAKECSRF1111111", "mid": None, "rur": ""})
    assert provider.get().cookies() == {"sessionid": FAKE_SESSIONID, "csrftoken": "FAKECSRF1111111"}
    assert provider.status().extra_keys == ["csrftoken"]
    leftovers = [p.name for p in provider.path.parent.iterdir()]
    assert leftovers == [SESSION_FILENAME]


def test_update_extra_validation_does_not_touch_file(provider):
    provider.save(FAKE_SESSIONID, {"csrftoken": FAKE_CSRF})
    before = provider.path.read_bytes()
    for bad in ({"sessionid": "FAKE-SESSION-9999999999"}, {"authorization": "FAKE-TOKEN-000000"},
                {"csrftoken": "FAKE CSRF COM ESPACO"}, {}):
        with pytest.raises(SessionValidationError) as info:
            provider.update_extra(bad)
        for value in bad.values():
            assert value not in str(info.value)
    assert provider.path.read_bytes() == before


def test_update_extra_requires_file_session(tmp_path):
    env_only = LocalInstagramSessionProvider(tmp_path / "s", env={"INSTAGRAM_SESSIONID": FAKE_ENV_SESSIONID})
    assert env_only.status().extra_keys == []
    with pytest.raises(SessionStorageError) as info:
        env_only.update_extra({"csrftoken": FAKE_CSRF})
    assert "variável de ambiente" in str(info.value)
    assert FAKE_CSRF not in str(info.value)
    assert not env_only.path.exists()

    corrupt = LocalInstagramSessionProvider(tmp_path / "c", env={})
    corrupt.path.parent.mkdir(parents=True)
    corrupt.path.write_bytes(b"corrompido")
    with pytest.raises(SessionStorageError):
        corrupt.update_extra({"csrftoken": FAKE_CSRF})
    assert corrupt.path.read_bytes() == b"corrompido"


@pytest.mark.skipif(os.name != "nt", reason="ACL NTFS")
def test_update_extra_keeps_restricted_acl(tmp_path):
    p = LocalInstagramSessionProvider(tmp_path / "novo" / "secrets", env={})
    p.save(FAKE_SESSIONID)
    p.update_extra({"csrftoken": FAKE_CSRF})
    lines = _acl_lines(p.path)
    assert len(lines) == 1 and "(I)" not in lines[0]


def test_base_provider_update_extra_not_supported():
    class Minimal(InstagramSessionProvider):
        def status(self):
            return SessionStatus(configured=False)

        def get(self):
            return None

        def save(self, sessionid, extra=None):
            pass

        def clear(self):
            pass

    with pytest.raises(SessionStorageError):
        Minimal().update_extra({"csrftoken": FAKE_CSRF})
