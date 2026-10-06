import io
import json
import logging
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integrations.instagram import gallerydl as gallerydl_module
from app.integrations.instagram import gallerydl_runner as runner
from app.integrations.instagram.downloader import InstagramError, RemoteMedia
from app.integrations.instagram.gallerydl import GalleryDlInstagramDownloader
from app.integrations.instagram.session import InstagramSession
from app.services.cdn_downloader import DownloadError

FAKE = "FAKE-SESSION-0000"
FAKE_CSRF = "FAKE-CSRF-1111"
BACKEND_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "instagram"


# --------------------------------------------------------------------------- doubles


class FakeProvider:
    def __init__(self, cookies=None):
        self._cookies = cookies

    def get(self):
        return InstagramSession(self._cookies) if self._cookies is not None else None


class StdinRecorder(io.BytesIO):
    def close(self):
        self.recorded = self.getvalue()
        super().close()


class BlockingStream:
    """stdout/stderr de processo falso: entrega linhas e, se `block`, espera até kill()."""

    def __init__(self, lines, block=False):
        self._queue = queue.Queue()
        for line in lines:
            self._queue.put(line)
        if not block:
            self._queue.put(None)
        self._buf = b""
        self._eof = False

    def end(self):
        self._queue.put(None)

    def readline(self, limit=-1):
        if not self._buf:
            if self._eof:
                return b""
            line = self._queue.get()
            if line is None:
                self._eof = True
                return b""
            self._buf = line
        if limit is None or limit < 0:
            limit = len(self._buf)
        out, self._buf = self._buf[:limit], self._buf[limit:]
        return out

    def close(self):
        pass


class FakePopen:
    """Substitui subprocess.Popen; script = lista de (stdout_lines, stderr_lines, returncode, block)."""

    instances = []
    script = []

    def __init__(self, args, **kwargs):
        stdout_lines, stderr_lines, returncode, block = FakePopen.script.pop(0)
        self.args = args
        self.kwargs = kwargs
        self.stdin = StdinRecorder()
        self.stdout = BlockingStream(stdout_lines, block=block)
        self.stderr = BlockingStream(stderr_lines)
        self._final = returncode
        self.returncode = None
        self.killed = False
        FakePopen.instances.append(self)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self._final
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.end()

    @property
    def request(self):
        return json.loads(self.stdin.recorded.decode("utf-8"))


def lines_of(*events):
    return [(json.dumps(e) + "\n").encode() for e in events]


def fixture_lines(name):
    return [line.encode() + b"\n" for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def fake_popen(monkeypatch):
    FakePopen.instances = []
    FakePopen.script = []
    monkeypatch.setattr(gallerydl_module.subprocess, "Popen", FakePopen)
    return FakePopen


def make_downloader(tmp_path, cookies=None, **kwargs):
    if cookies is None:
        cookies = {"sessionid": FAKE, "csrftoken": FAKE_CSRF}
    return GalleryDlInstagramDownloader(FakeProvider(cookies), temp_dir=tmp_path, **kwargs)


# --------------------------------------------------------------------------- sessão


def test_session_missing_raises_without_spawning(tmp_path, fake_popen):
    dl = GalleryDlInstagramDownloader(FakeProvider(None), temp_dir=tmp_path)
    with pytest.raises(InstagramError) as info:
        dl.get_profile("instagram")
    assert info.value.code == "session_missing"
    assert info.value.retryable is False
    with pytest.raises(InstagramError) as info:
        list(dl.iter_profile_media("instagram", "reels", 1))
    assert info.value.code == "session_missing"
    result = dl.test_session()
    assert (result.ok, result.code, result.username) == (False, "session_missing", None)
    assert (result.result, result.http_status) == ("session_missing", None)
    assert fake_popen.instances == []


def session_event(result, code, username=None, http_status=200, message="msg"):
    return {"type": "session_test", "result": result, "code": code, "message": message, "username": username,
            "http_status": http_status}


def test_test_session_ok(tmp_path, fake_popen):
    fake_popen.script = [(lines_of(session_event("valid", "ok", "dono"), {"type": "done", "count": 1}), [], 0, False)]
    result = make_downloader(tmp_path).test_session()
    assert (result.ok, result.code, result.username, result.result) == (True, "ok", "dono", "valid")
    assert result.http_status == 200
    assert isinstance(result.duration_ms, int) and result.duration_ms >= 0
    assert fake_popen.instances[0].request["op"] == "test_session"


@pytest.mark.parametrize(
    ("result", "code", "status"),
    [
        ("invalid", "session_invalid", 302),
        ("expired", "session_expired", 401),
        ("checkpoint_required", "checkpoint_required", 302),
        ("rate_limited", "rate_limited", 429),
        ("network_error", "network_error", None),
        ("timeout", "network_error", None),
        ("instagram_error", "internal_error", 500),
    ],
)
def test_test_session_passes_runner_result_through(tmp_path, fake_popen, result, code, status):
    fake_popen.script = [(lines_of(session_event(result, code, "ignorado", status), {"type": "done", "count": 1}),
                          [], 0, False)]
    got = make_downloader(tmp_path).test_session()
    assert (got.ok, got.result, got.code, got.http_status, got.username) == (False, result, code, status, None)


def test_test_session_unknown_result_is_instagram_error(tmp_path, fake_popen):
    fake_popen.script = [(lines_of(session_event("bogus", "x"), {"type": "done", "count": 1}), [], 0, False)]
    got = make_downloader(tmp_path).test_session()
    assert (got.ok, got.result, got.code) == (False, "instagram_error", "internal_error")


def test_test_session_total_timeout_kills_child(tmp_path, fake_popen):
    fake_popen.script = [([], [], 0, True)]
    dl = make_downloader(tmp_path, timeout=300, test_timeout=1.0)
    started = time.monotonic()
    result = dl.test_session()
    elapsed = time.monotonic() - started
    assert elapsed < 1.0 + 2.0
    assert (result.ok, result.result, result.code, result.http_status) == (False, "timeout", "network_error", None)
    assert result.duration_ms >= 900
    assert "1 s" in result.message
    assert fake_popen.instances[0].killed is True


def test_get_profile_preview_has_short_total_cap(tmp_path, fake_popen):
    fake_popen.script = [([], [], 0, True)]
    dl = make_downloader(tmp_path, timeout=300, profile_timeout=0.5)
    started = time.monotonic()
    with pytest.raises(InstagramError) as info:
        dl.get_profile("exemplo")
    assert time.monotonic() - started < 0.5 + 2.0
    assert info.value.code == "timeout" and info.value.retryable is True
    assert "limite" in info.value.message
    assert fake_popen.instances[0].killed is True


def test_default_caps_for_sync_operations(tmp_path):
    dl = make_downloader(tmp_path)
    assert dl._test_timeout == 30.0
    assert dl._profile_timeout <= 45.0


def test_test_session_never_leaks_secret(tmp_path, fake_popen, monkeypatch, caplog):
    monkeypatch.setenv("INSTAGRAM_SESSIONID", FAKE)
    caplog.set_level(logging.DEBUG)
    leaky = session_event("expired", "session_expired", message=f"sessionid={FAKE} {FAKE_CSRF}")
    stderr = [f"[instagram][DEBUG] Cookie: sessionid={FAKE}; csrftoken={FAKE_CSRF}\n".encode()]
    fake_popen.script = [(lines_of(leaky, {"type": "done", "count": 1}), stderr, 0, False)]
    result = make_downloader(tmp_path).test_session()
    proc = fake_popen.instances[0]
    assert not any(FAKE in str(a) for a in proc.args)
    assert FAKE not in json.dumps(proc.kwargs["env"])
    assert proc.request["cookies"]["sessionid"] == FAKE
    dumped = repr(result) + json.dumps(result.__dict__)
    assert FAKE not in dumped and FAKE_CSRF not in dumped
    assert FAKE not in caplog.text and FAKE_CSRF not in caplog.text


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("session_invalid", False),
        ("session_expired", False),
        ("checkpoint_required", False),
        ("profile_not_found", False),
        ("profile_private", False),
        ("rate_limited", True),
        ("network_error", True),
    ],
)
def test_runner_error_lines_become_instagram_errors(tmp_path, fake_popen, code, retryable):
    fake_popen.script = [(lines_of({"type": "error", "code": code, "message": "detalhe"}), [], 1, False)]
    with pytest.raises(InstagramError) as info:
        make_downloader(tmp_path).get_profile("instagram")
    assert info.value.code == code
    assert info.value.retryable is retryable
    assert info.value.message == "detalhe"


def test_test_session_reports_runner_error(tmp_path, fake_popen):
    fake_popen.script = [(lines_of({"type": "error", "code": "session_expired", "message": "login"}), [], 1, False)]
    result = make_downloader(tmp_path).test_session()
    assert (result.ok, result.code, result.message) == (False, "session_expired", "login")
    assert result.result == "expired"


def test_runner_crash_without_done_is_internal_error(tmp_path, fake_popen):
    fake_popen.script = [([b"not json\n"], [b"Traceback ...\n"], 1, False)]
    with pytest.raises(InstagramError) as info:
        make_downloader(tmp_path).get_profile("instagram")
    assert info.value.code == "internal_error"


# --------------------------------------------------------------------------- listagem


def test_get_profile_parses_profile_line(tmp_path, fake_popen):
    fake_popen.script = [(fixture_lines("list_posts.jsonl")[:1] + lines_of({"type": "done", "count": 1}), [], 0, False)]
    profile = make_downloader(tmp_path).get_profile("@Exemplo")
    assert profile.username == "exemplo"
    assert profile.user_id == "1001"
    assert profile.post_count == 3
    assert profile.is_verified is True
    assert fake_popen.instances[0].request == {
        "op": "profile", "username": "exemplo", "cookies": {"sessionid": FAKE, "csrftoken": FAKE_CSRF},
    }


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("all", ["CAROUSEL001#0", "CAROUSEL001#1", "REEL0000002#0", "IMAGE000003#0"]),
        ("posts", ["CAROUSEL001#0", "CAROUSEL001#1", "REEL0000002#0", "IMAGE000003#0"]),
        ("videos", ["CAROUSEL001#1", "REEL0000002#0"]),
        ("images", ["CAROUSEL001#0", "IMAGE000003#0"]),
    ],
)
def test_iter_profile_media_content_filter(tmp_path, fake_popen, content, expected):
    fake_popen.script = [(fixture_lines("list_posts.jsonl"), [], 0, False)]
    items = list(make_downloader(tmp_path).iter_profile_media("exemplo", content, None))
    assert [f"{i.source_post_id}#{i.source_media_index}" for i in items] == expected
    assert all(isinstance(i, RemoteMedia) for i in items)
    request = fake_popen.instances[0].request
    assert request["content"] == content
    assert request["limit"] == 5000


def test_iter_profile_media_item_fields(tmp_path, fake_popen):
    fake_popen.script = [(fixture_lines("list_posts.jsonl"), [], 0, False)]
    items = list(make_downloader(tmp_path).iter_profile_media("exemplo", "videos", None))
    reel = items[1]
    assert reel.is_reel is True
    assert reel.media_type == "video"
    assert reel.source_url == "https://www.instagram.com/reel/REEL0000002/"
    assert reel.duration == 30.0
    assert (reel.width, reel.height) == (1080, 1920)
    assert reel.taken_at == "2023-11-15T10:00:00Z"
    assert reel.source_platform == "instagram"


def test_limit_counts_media_and_stops_process(tmp_path, fake_popen):
    fake_popen.script = [(fixture_lines("list_posts.jsonl")[:3], [], 0, True)]
    items = list(make_downloader(tmp_path).iter_profile_media("exemplo", "all", 2))
    assert [i.media_id for i in items] == ["4001", "4002"]
    assert fake_popen.instances[0].request["limit"] == 2
    assert fake_popen.instances[0].killed is True


def test_limit_is_capped(tmp_path, fake_popen):
    fake_popen.script = [(lines_of({"type": "done", "count": 0}), [], 0, False)]
    assert list(make_downloader(tmp_path).iter_profile_media("exemplo", "reels", 99999)) == []
    assert fake_popen.instances[0].request["limit"] == 5000


@pytest.mark.parametrize(
    ("username", "content", "limit"),
    [("../x", "all", 1), ("exemplo", "stories", 1), ("exemplo", "all", 0), ("exemplo", "all", -5), ("exemplo", "all", True)],
)
def test_iter_profile_media_invalid_input(tmp_path, fake_popen, username, content, limit):
    with pytest.raises(InstagramError) as info:
        make_downloader(tmp_path).iter_profile_media(username, content, limit)
    assert info.value.code == "invalid_input"
    assert fake_popen.instances == []


def test_partial_listing_then_error(tmp_path, fake_popen):
    lines = fixture_lines("list_posts.jsonl")[:2] + lines_of({"type": "error", "code": "rate_limited", "message": "429"})
    fake_popen.script = [(lines, [], 1, False)]
    gen = make_downloader(tmp_path).iter_profile_media("exemplo", "all", None)
    assert next(gen).media_id == "4001"
    with pytest.raises(InstagramError) as info:
        next(gen)
    assert info.value.code == "rate_limited"
    assert info.value.retryable is True


def test_cancel_stops_listing_and_kills_process(tmp_path, fake_popen):
    fake_popen.script = [(fixture_lines("list_posts.jsonl")[:2], [], 0, True)]
    cancel = threading.Event()
    gen = make_downloader(tmp_path).iter_profile_media("exemplo", "all", None, cancel=cancel)
    first = next(gen)
    assert first.media_id == "4001"
    cancel.set()
    started = time.monotonic()
    assert list(gen) == []
    assert time.monotonic() - started < 5
    assert fake_popen.instances[0].killed is True


def test_timeout_kills_process(tmp_path, fake_popen):
    fake_popen.script = [([], [], 0, True)]
    dl = make_downloader(tmp_path, timeout=0.5)
    with pytest.raises(InstagramError) as info:
        dl.get_profile("exemplo")
    assert info.value.code == "timeout"  # prévia: estouro de tempo vira 504 no endpoint
    assert fake_popen.instances[0].killed is True


def test_slow_consumer_does_not_trigger_idle_timeout(tmp_path, fake_popen):
    fake_popen.script = [(fixture_lines("list_posts.jsonl"), [], 0, False)]
    gen = make_downloader(tmp_path, timeout=0.3).iter_profile_media("exemplo", "all", None)
    received = []
    for media in gen:
        received.append(media.media_id)
        time.sleep(0.5)
    assert received == ["4001", "4002", "4003", "4004"]


def test_stderr_activity_keeps_child_alive(tmp_path, fake_popen):
    proc_lines = lines_of({"type": "profile", "username": "e"})
    fake_popen.script = [(proc_lines, [], 0, True)]
    dl = make_downloader(tmp_path, timeout=0.6)

    def feed_stderr_then_finish():
        proc = None
        while proc is None:
            proc = fake_popen.instances[0] if fake_popen.instances else None
            time.sleep(0.05)
        for _ in range(4):
            time.sleep(0.3)
            proc.stderr._queue.put(b"[instagram][INFO] Waiting (429 Too Many Requests)\n")
        proc.stdout._queue.put(lines_of({"type": "done", "count": 1})[0])
        proc.stdout.end()

    threading.Thread(target=feed_stderr_then_finish, daemon=True).start()
    assert dl.get_profile("exemplo").username == "e"


def test_optional_total_cap(tmp_path, fake_popen):
    fake_popen.script = [(fixture_lines("list_posts.jsonl")[:1], [], 0, True)]
    dl = make_downloader(tmp_path, timeout=60, max_total_time=0.5)
    with pytest.raises(InstagramError) as info:
        list(dl.iter_profile_media("exemplo", "all", None))
    assert info.value.code == "network_error"
    assert fake_popen.instances[0].killed is True


def test_oversized_line_is_discarded_in_chunks(tmp_path, fake_popen, monkeypatch):
    monkeypatch.setattr(gallerydl_module, "MAX_LINE_BYTES", 64)
    big = b'{"type": "profile", "username": "' + b"x" * 200 + b'"}\n'
    fake_popen.script = [([big] + lines_of({"type": "profile", "username": "ok"}, {"type": "done", "count": 1}),
                          [], 0, False)]
    assert make_downloader(tmp_path).get_profile("exemplo").username == "ok"


# --------------------------------------------------------------------------- segredos


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("humid=42 rumid: 7", "humid=42 rumid: 7"),
        ("{'sessionid': 'abc123', 'mid': 'zz'}", "{'sessionid': '<redacted>', 'mid': '<redacted>'}"),
        ('{"csrftoken": "abc"}', '{"csrftoken": "<redacted>"}'),
        ("Cookie: sessionid=abc; ds_user_id=1", "Cookie: sessionid=<redacted>; ds_user_id=<redacted>"),
    ],
)
def test_redact_patterns(text, expected):
    assert runner.redact(text, []) == expected


def test_cookies_only_in_stdin_never_in_argv_env_errors_or_logs(tmp_path, fake_popen, monkeypatch, caplog):
    monkeypatch.setenv("INSTAGRAM_SESSIONID", FAKE)
    leaky_error = {"type": "error", "code": "session_invalid", "message": f"falhou sessionid={FAKE} {FAKE_CSRF}"}
    stderr = [f"[instagram][DEBUG] Cookie: sessionid={FAKE}; csrftoken={FAKE_CSRF}\n".encode(),
              f'{{"sessionid": "{FAKE}"}}\n'.encode()]
    fake_popen.script = [(lines_of(leaky_error), stderr, 1, False)]
    caplog.set_level(logging.DEBUG)
    with pytest.raises(InstagramError) as info:
        make_downloader(tmp_path).get_profile("exemplo")

    proc = fake_popen.instances[0]
    assert proc.kwargs["shell"] is False
    assert isinstance(proc.args, list)
    assert proc.args[1:] == ["-m", "app.integrations.instagram.gallerydl_runner"]
    assert not any(FAKE in str(a) for a in proc.args)
    assert FAKE not in json.dumps(proc.kwargs["env"])
    assert proc.request["cookies"]["sessionid"] == FAKE

    assert FAKE not in str(info.value) and FAKE_CSRF not in str(info.value)
    assert FAKE not in repr(info.value)
    assert "gallery-dl (redigido)" in caplog.text
    assert FAKE not in caplog.text and FAKE_CSRF not in caplog.text


def test_session_repr_is_redacted():
    assert FAKE not in repr(InstagramSession({"sessionid": FAKE}))


# --------------------------------------------------------------------------- download


def item_from_fixture(index):
    event = json.loads((FIXTURES / "list_posts.jsonl").read_text(encoding="utf-8").splitlines()[index])
    event.pop("type")
    return RemoteMedia(**event)


class FakeCdn:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, dest_dir, *, max_bytes, timeout=60, cancel=None, progress=None):
        self.calls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        path = Path(dest_dir) / "file.bin"
        path.write_bytes(b"data")
        return SimpleNamespace(path=path, size=4, content_type="video/mp4", suffix=".mp4")


def test_download_ok_without_resolve(tmp_path, fake_popen):
    cdn = FakeCdn(["ok"])
    path = make_downloader(tmp_path, cdn_downloader=cdn).download(item_from_fixture(2), tmp_path)
    assert path.read_bytes() == b"data"
    assert cdn.calls == ["https://scontent.cdninstagram.com/v/vid-b.mp4?efg=1"]
    assert fake_popen.instances == []


@pytest.mark.parametrize("code", ["forbidden", "not_found"])
def test_download_expired_url_resolves_once(tmp_path, fake_popen, code):
    fake_popen.script = [(fixture_lines("resolve_post.jsonl"), [], 0, False)]
    cdn = FakeCdn([DownloadError(code, "expirou"), "ok"])
    path = make_downloader(tmp_path, cdn_downloader=cdn).download(item_from_fixture(2), tmp_path)
    assert path.exists()
    assert cdn.calls == [
        "https://scontent.cdninstagram.com/v/vid-b.mp4?efg=1",
        "https://scontent.cdninstagram.com/v/vid-b-fresh.mp4?efg=9",
    ]
    assert fake_popen.instances[0].request["op"] == "resolve_post"
    assert fake_popen.instances[0].request["shortcode"] == "CAROUSEL001"


def test_download_expired_twice_fails_without_more_retries(tmp_path, fake_popen):
    fake_popen.script = [(fixture_lines("resolve_post.jsonl"), [], 0, False)]
    cdn = FakeCdn([DownloadError("forbidden", "x"), DownloadError("forbidden", "y")])
    with pytest.raises(InstagramError) as info:
        make_downloader(tmp_path, cdn_downloader=cdn).download(item_from_fixture(2), tmp_path)
    assert info.value.code == "download_failed"
    assert len(cdn.calls) == 2
    assert len(fake_popen.instances) == 1


def test_download_media_gone_after_resolve(tmp_path, fake_popen):
    fake_popen.script = [(fixture_lines("resolve_post.jsonl")[:1] + lines_of({"type": "done", "count": 1}), [], 0, False)]
    cdn = FakeCdn([DownloadError("not_found", "x")])
    with pytest.raises(InstagramError) as info:
        make_downloader(tmp_path, cdn_downloader=cdn).download(item_from_fixture(2), tmp_path)
    assert info.value.code == "content_unavailable"


def test_download_network_error_is_retryable_and_not_resolved(tmp_path, fake_popen):
    cdn = FakeCdn([DownloadError("network_error", "reset")])
    with pytest.raises(InstagramError) as info:
        make_downloader(tmp_path, cdn_downloader=cdn).download(item_from_fixture(2), tmp_path)
    assert (info.value.code, info.value.retryable) == ("download_failed", True)
    assert fake_popen.instances == []


def test_download_cancelled_propagates(tmp_path, fake_popen):
    cdn = FakeCdn([DownloadError("cancelled", "cancelado")])
    with pytest.raises(DownloadError) as info:
        make_downloader(tmp_path, cdn_downloader=cdn).download(item_from_fixture(2), tmp_path)
    assert info.value.code == "cancelled"


# --------------------------------------------------------------------------- runner: classificação


class FakeResponse:
    def __init__(self, status, text="", url="https://www.instagram.com/api/v1/x/?max_id=1"):
        self.status_code = status
        self.text = text
        self.reason = "R"
        self.url = url


def http_error(status, text="", headers=None):
    from gallery_dl import exception

    response = FakeResponse(status, text)
    response.headers = dict(headers or {})
    return exception.HttpError("", response)


def gdl():
    from gallery_dl import exception

    return exception


@pytest.mark.parametrize(
    ("make_exc", "op", "has_session", "expected"),
    [
        (lambda: gdl().AbortExtraction("HTTP redirect to login page (https://www.instagram.com/accounts/login/)"),
         "list", True, "request_auth_failed"),
        (lambda: gdl().AbortExtraction("HTTP redirect to login page (https://www.instagram.com/accounts/login/)"),
         "list", False, "session_missing"),
        (lambda: gdl().AbortExtraction("HTTP redirect to home page (https://www.instagram.com/)"),
         "list", True, "access_refused"),
        (lambda: gdl().AbortExtraction("HTTP redirect to challenge page (https://www.instagram.com/challenge/)"),
         "list", True, "checkpoint_required"),
        (lambda: gdl().AbortExtraction("exemplo's posts are private"), "list", True, "profile_private"),
        (lambda: http_error(400, '{"message":"checkpoint_required","status":"fail"}'), "list", True,
         "checkpoint_required"),
        (lambda: http_error(429), "list", True, "rate_limited"),
        (lambda: http_error(400, '{"feedback_title":"x","feedback_message":"x","message":"x","spam":true,'
                                 '"restriction_enrollment_data":{},"status":"fail"}'), "list", True, "action_blocked"),
        (lambda: http_error(400, '{"message":"x", "spam": true}'), "profile", True, "action_blocked"),
        (lambda: http_error(400, '{"message":"x","feedback_url":"FAKE"}'), "list", True, "action_blocked"),
        (lambda: http_error(401, '{"message":"Please wait a few minutes before you try again.","require_login":true}'),
         "profile", True, "request_auth_failed"),
        (lambda: http_error(401, '{"message": "Please wait a few minutes before you try again.", "require_login": true}'),
         "list", True, "request_auth_failed"),
        (lambda: http_error(401, '{"message":"Please wait a few minutes before you try again.","require_login":true}'),
         "resolve_post", False, "session_missing"),
        (lambda: http_error(400, '{"message":"Please wait a few minutes before you try again.","status":"fail"}'),
         "list", True, "rate_limited"),
        (lambda: http_error(401, '{"message":"","require_login":true}'), "profile", True, "request_auth_failed"),
        (lambda: http_error(404), "profile", True, "profile_not_found"),
        (lambda: http_error(404), "resolve_post", True, "content_unavailable"),
        (lambda: http_error(502), "list", True, "network_error"),
        (lambda: gdl().NotFoundError("user"), "profile", True, "profile_not_found"),
        (lambda: gdl().AuthRequired("authenticated cookies"), "list", True, "request_auth_failed"),
        (lambda: json.JSONDecodeError("x", "<html>", 0), "list", True, "request_auth_failed"),
        (lambda: __import__("requests").exceptions.ConnectionError("dns"), "list", True, "network_error"),
        (lambda: KeyError("items"), "list", True, "internal_error"),
    ],
)
def test_classify_exception(make_exc, op, has_session, expected):
    code, message = runner.classify_exception(make_exc(), op=op, has_session=has_session)
    assert code == expected
    assert "max_id=" not in message


@pytest.mark.parametrize("body", ['{"message":"","require_login":true}', '{"message":"login_required"}', ""])
@pytest.mark.parametrize("status", [401, 403])
def test_classify_exception_session_invalid_only_with_sessionid_deleted(status, body):
    """Seção 7: session_invalid só com Set-Cookie apagando o sessionid; sem ele, request_auth_failed."""
    deleted = 'sessionid=""; Domain=.instagram.com; expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; Path=/'
    kept = "csrftoken=FAKEcsrf; Domain=.instagram.com; Max-Age=31449600; Path=/; Secure"
    code, message = runner.classify_exception(http_error(status, body, {"set-cookie": deleted}), op="list",
                                              has_session=True)
    assert (code, message) == ("session_invalid", runner.SESSION_ENDED_MESSAGE)
    for headers in ({"set-cookie": kept}, {}):
        code, _ = runner.classify_exception(http_error(status, body, headers), op="list", has_session=True)
        assert code == "request_auth_failed"
    out = runner.classify_session_response(status, None, body, kept)
    assert (out["result"], out["code"]) == ("instagram_error", "request_auth_failed")
    out = runner.classify_session_response(status, None, body, deleted)
    assert (out["result"], out["code"]) == ("invalid", "session_invalid")


def test_not_logged_in_and_redirect_home_are_actionable_session_invalid():
    from app.integrations.instagram.downloader import ERROR_CODES

    not_logged = http_error(401, NOT_LOGGED_IN)
    home = gdl().AbortExtraction("HTTP redirect to home page (https://www.instagram.com/)")
    for op in ("profile", "list", "resolve_post"):
        code, message = runner.classify_exception(not_logged, op=op, has_session=True)
        assert (code, message) == ("request_auth_failed", runner.REQUEST_AUTH_MESSAGE)
        code, message = runner.classify_exception(home, op=op, has_session=True)
        assert (code, message) == ("access_refused", runner.SESSION_REFUSED_MESSAGE)
    assert ERROR_CODES["session_invalid"][1] is False
    assert ERROR_CODES["access_refused"][1] is False
    home_test = runner.classify_session_response(302, "https://www.instagram.com/", "")
    assert (home_test["result"], home_test["code"], home_test["detail"]) == (
        "api_refused", "access_refused", "redirect_home")
    assert (home_test["session_recognized"], home_test["listing_api"]) == (True, "refused")
    assert home_test["message"] == runner.REDIRECT_HOME_TEST_MESSAGE
    # A mensagem padrão do código (exibida ao usuário) não usa jargão técnico.
    assert "GraphQL" in home_test["message"] and "GraphQL" not in runner.SESSION_REFUSED_MESSAGE
    for text in (home_test["message"], runner.SESSION_REFUSED_MESSAGE):
        assert "não indica sessão inválida nem restrição da conta" in text
        for name in ("ds_user_id", "csrftoken", "sessionid novo", "investigação", "horas", "automatizad",
                     "pausad"):
            assert name not in text


def test_action_blocked_is_restricted_and_not_retryable():
    from app.integrations.instagram.downloader import ERROR_CODES, InstagramError, SessionTestResult

    body = ('{"feedback_action":"x","feedback_appeal_label":"x","feedback_message":"x","feedback_title":"x",'
            '"feedback_url":"x","message":"x","restriction_enrollment_data":{},"spam":true,"status":"fail"}')
    outcome = runner.classify_session_response(400, None, body)
    assert (outcome["result"], outcome["code"], outcome["http_status"]) == ("restricted", "action_blocked", 400)
    assert outcome["message"] == (
        "O Instagram respondeu a esta chamada com um aviso de restrição (campos feedback/restriction).")
    assert (outcome["session_recognized"], outcome["listing_api"]) == (True, "refused")
    code, message = runner.classify_exception(http_error(400, body), op="list", has_session=True)
    assert (code, message) == ("action_blocked", runner.ACTION_BLOCKED_MESSAGE)
    assert "1 hora" in message and "Configurações" in message and "proteção" in message
    assert "6 horas" not in message and "24 horas" not in message
    assert ERROR_CODES["action_blocked"][1] is False and InstagramError("action_blocked").retryable is False
    assert SessionTestResult(ok=False, code="action_blocked", message="m", username=None).result == "restricted"


# --------------------------------------------------------------------------- runner: teste de sessão


NOT_LOGGED_IN = '{"message":"Please wait a few minutes before you try again.","require_login":true,"status":"fail"}'


@pytest.mark.parametrize(
    ("status", "location", "body", "expected"),
    [
        (200, None, '{"user": {"username": "Dono", "pk": 1}, "status": "ok"}', "valid"),
        (200, None, '{"items": [{"pk": "1", "user": {"username": "Dono"}}], "num_results": 1, "status": "ok"}',
         "valid"),
        (302, "https://www.instagram.com/", "", "api_refused"),
        (302, "https://www.instagram.com/consent/?flow=x", "", "api_refused"),
        (302, "https://www.instagram.com/explore/", "", "api_refused"),
        (302, "https://www.instagram.com/accounts/login/?next=/api/", "", "request_auth_failed"),
        (302, "https://www.instagram.com/challenge/?next=/", "", "checkpoint_required"),
        (401, None, NOT_LOGGED_IN, "request_auth_failed"),
        (403, None, '{"message":"login_required","status":"fail"}', "request_auth_failed"),
        (403, None, "<html>login_required</html>", "request_auth_failed"),
        (200, None, '{"user": {"username": "Dono", "biography": "checkpoint_required login_required"}}', "valid"),
        (200, None, '{"user": {"username": "Dono", "biography": "Please wait a few minutes, feedback_required"},'
                    ' "status": "ok"}', "valid"),
        (400, None, '{"message": "x", "user": {"biography": "checkpoint_required please wait a few minutes"}}',
         "instagram_error"),
        (403, None, "", "request_auth_failed"),
        (400, None, '{"message":"checkpoint_required","checkpoint_url":"/challenge/x/"}', "checkpoint_required"),
        (429, None, "", "rate_limited"),
        (429, None, NOT_LOGGED_IN, "rate_limited"),
        (400, None, '{"message":"Please wait a few minutes before you try again.","status":"fail"}', "rate_limited"),
        (400, None, '{"message":"feedback_required","spam":true}', "restricted"),
        (400, None, '{"feedback_action":"x","feedback_appeal_label":"x","feedback_message":"x","feedback_title":"x",'
                    '"feedback_url":"x","message":"x","restriction_enrollment_data":{},"spam":true,"status":"fail"}',
         "restricted"),
        (400, None, '{"message":"x","spam":true,"status":"fail"}', "restricted"),
        (400, None, '{"message":"x","feedback_title":"FAKE","status":"fail"}', "restricted"),
        (400, None, '{"message":"x","restriction_enrollment_data":null,"status":"fail"}', "restricted"),
        (200, None, '{"status": "ok"}', "instagram_error"),
        (200, None, "<html>", "instagram_error"),
        (500, None, "", "instagram_error"),
        (404, None, "", "instagram_error"),
    ],
)
def test_classify_session_response(status, location, body, expected):
    from app.integrations.instagram.downloader import SESSION_RESULT_CODES

    out = runner.classify_session_response(status, location, body)
    if expected == "request_auth_failed":   # seção 7: recusa sem Set-Cookie apagando o sessionid
        assert (out["result"], out["code"]) == ("instagram_error", "request_auth_failed")
    else:
        assert out["result"] == expected
        assert out["code"] == SESSION_RESULT_CODES[expected]
    assert out["http_status"] == status
    assert out["username"] == ("dono" if expected == "valid" else None)
    assert out["message"] and "next=" not in out["message"]


@pytest.fixture
def fake_http(monkeypatch):
    """Substitui requests.Session.request; cada item é (status, headers, body) ou uma exceção."""
    import requests
    from gallery_dl.extractor.common import Extractor

    monkeypatch.setattr(Extractor, "request_timestamp", 0.0)
    state = SimpleNamespace(responses=[], calls=[])

    def request(self, method, url, **kwargs):
        state.calls.append(SimpleNamespace(method=method, url=url, kwargs=kwargs,
                                           cookies=requests.utils.dict_from_cookiejar(self.cookies)))
        item = state.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, headers, body = item
        response = requests.Response()
        response.status_code = status
        response.reason = "R"
        response.headers.update(headers)
        response._content = body.encode()
        response.encoding = "utf-8"
        response.url = url
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return state


def run_test_session(cookies=None):
    if cookies is None:
        cookies = {"sessionid": FAKE, "ds_user_id": "123"}
    request = runner.parse_request(json.dumps({"op": "test_session", "cookies": cookies}).encode())
    runner._configure_gallery_dl(request)
    events = []
    runner.op_test_session(request, events.append)
    assert len(events) == 1 and events[0]["type"] == "session_test"
    return events[0]


def test_runner_test_session_429_returns_rate_limited_without_sleeping(fake_http, caplog):
    caplog.set_level(logging.DEBUG)
    fake_http.responses = [(429, {"Content-Type": "text/plain"}, "")]
    started = time.monotonic()
    event = run_test_session()
    assert time.monotonic() - started < 2.0
    assert (event["result"], event["code"], event["http_status"]) == ("rate_limited", "rate_limited", 429)
    assert "Aguarde alguns minutos" in event["message"]
    assert len(fake_http.calls) == 1
    call = fake_http.calls[0]
    assert call.url == "https://www.instagram.com/api/v1/feed/user/123/"
    assert call.kwargs["params"] == {"count": "1"}
    assert call.kwargs["allow_redirects"] is False
    assert call.kwargs["timeout"] == runner.TEST_HTTP_TIMEOUT
    assert FAKE not in json.dumps(event) and FAKE not in caplog.text


def test_runner_test_session_redirect_home_is_api_refused_without_fallback(fake_http):
    fake_http.responses = [(302, {"Location": "https://www.instagram.com/"}, "")]
    event = run_test_session()
    assert (event["result"], event["code"], event["http_status"]) == ("api_refused", "access_refused", 302)
    assert (event["detail"], event["session_recognized"], event["listing_api"]) == ("redirect_home", True, "refused")
    assert len(fake_http.calls) == 1


@pytest.mark.parametrize(("location", "kind"), [
    ("https://www.instagram.com/", "redirect_home"),
    ("https://www.instagram.com/accounts/login/?next=/api/", "redirect_login"),
    ("https://www.instagram.com/challenge/?next=/", "redirect_challenge"),
    ("https://www.instagram.com/checkpoint/1/", "redirect_checkpoint"),
    ("https://www.instagram.com/consent/?flow=user_cookie_choice", "redirect_consent"),
    ("https://www.instagram.com/privacy/checks/", "redirect_consent"),
    ("https://www.instagram.com/explore/", "redirect_other"),
    (None, "redirect_other"),
    ("", "redirect_other"),
])
def test_redirect_kind(location, kind):
    assert runner.redirect_kind(location) == kind


def test_downloader_passes_detail_through(tmp_path, fake_popen):
    event = dict(session_event("instagram_error", "internal_error", http_status=302), detail="redirect_home")
    fake_popen.script = [(lines_of(event, {"type": "done", "count": 1}), [], 0, False)]
    got = make_downloader(tmp_path).test_session()
    assert (got.result, got.http_status, got.detail) == ("instagram_error", 302, "redirect_home")


# --------------------------------------------------------------------------- cadeia de cookies e diagnóstico

CHAIN_SESSIONID = "4242%3AFAKEsessionFAKEsessionFAKE%3A27%3AFAKEsignatureFAKEsignature"
CHAIN_EXTRA = {"ds_user_id": "4242", "csrftoken": "FAKEcsrfFAKEcsrfFAKEcsrf", "mid": "FAKE-MID-2222",
               "ig_did": "FAKE-IGDID-3333"}


def chain_provider(tmp_path):
    from app.integrations.instagram.session import LocalInstagramSessionProvider

    provider = LocalInstagramSessionProvider(tmp_path / "secrets", env={})
    provider.save(CHAIN_SESSIONID, {"ds_user_id": CHAIN_EXTRA["ds_user_id"]})
    provider.update_extra({k: v for k, v in CHAIN_EXTRA.items() if k != "ds_user_id"})
    return provider


def test_cookie_chain_reaches_prepared_request_in_process(tmp_path):
    """provider.save + update_extra -> cookies() -> pedido -> config do gallery-dl -> jar -> PreparedRequest."""
    cookies = chain_provider(tmp_path).get().cookies()
    assert cookies == {"sessionid": CHAIN_SESSIONID, **CHAIN_EXTRA}
    payload = json.dumps({"op": "test_session", "cookies": cookies}).encode()
    request = runner.parse_request(payload)
    runner._configure_gallery_dl(request)
    extr = runner._make_job("https://www.instagram.com/instagram/info/").extractor
    extr.login()
    extr.request_json = extr.request
    adapter, redirects = runner._capture_on(extr)
    runner._probe_session(extr, "/v1/feed/user/4242/", {"count": "1"}, runner.TEST_HTTP_TIMEOUT)
    prepared = adapter.captured[0]
    sent = dict(part.strip().split("=", 1) for part in prepared.headers["Cookie"].split(";"))
    assert sent == {"sessionid": CHAIN_SESSIONID, **CHAIN_EXTRA}
    assert prepared.headers["X-CSRFToken"] == CHAIN_EXTRA["csrftoken"] == sent["csrftoken"]
    assert redirects == [False]
    for cookie in extr.session.cookies:
        assert (cookie.domain, cookie.path) == (".instagram.com", "/")


def test_inspect_request_through_real_runner_reports_presence_only(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    dl = GalleryDlInstagramDownloader(chain_provider(tmp_path), temp_dir=tmp_path, timeout=60)
    events = dl.inspect_request("exemplo")
    assert [e["flow"] for e in events] == ["test_session", "list_profile"]
    for event in events:
        assert event["cookie_header_present"] == list(runner.INSPECT_COOKIES)
        assert event["cookie_header_absent"] == []
        assert event["x_csrftoken_matches_cookie"] is True
        assert {(c["domain"], c["path"]) for c in event["jar"]} == {(".instagram.com", "/")}
        assert {c["name"] for c in event["jar"]} == {"sessionid", *CHAIN_EXTRA}
        assert event["headers"]["Cookie"] == "<present>" and event["headers"]["X-CSRFToken"] == "<present>"
        assert event["headers"]["X-IG-App-ID"] == "936619743392459"
        assert event["gallery_dl_version"]
    assert events[0]["url"] == "https://www.instagram.com/api/v1/feed/user/<own id>/"
    assert events[0]["query_keys"] == ["count"] and events[0]["allow_redirects"] is False
    assert events[1]["url"] == "https://www.instagram.com/web/search/topsearch/"
    assert events[1]["query_keys"] == ["query"] and events[1]["allow_redirects"] is True
    dumped = json.dumps(events) + caplog.text
    for value in (CHAIN_SESSIONID, *CHAIN_EXTRA.values()):
        if value != "4242":
            assert value not in dumped


@pytest.mark.parametrize("payload", [
    {"op": "probe", "endpoint": "/api/v1/../x/"},
    {"op": "probe", "endpoint": "https://evil.example/api/v1/x/"},
    {"op": "probe", "endpoint": "/api/v1/x/", "method": "DELETE"},
    {"op": "probe", "endpoint": "/api/v1/x/", "params": {"a": "b c"}},
])
def test_probe_rejects_bad_input(payload):
    with pytest.raises(runner.RunnerError):
        runner.parse_request(json.dumps({**payload, "cookies": {"sessionid": FAKE}}).encode())


def run_probe(**fields):
    request = runner.parse_request(json.dumps({"op": "probe", "cookies": {"sessionid": FAKE}, **fields}).encode())
    runner._configure_gallery_dl(request)
    events = []
    runner.op_probe(request, events.append)
    return events[0]


def test_probe_reports_safe_metadata_once(fake_http):
    fake_http.responses = [(302, {"Location": "https://www.instagram.com/accounts/login/?next=%2Fsecret",
                                  "Content-Type": "text/html"}, "")]
    event = run_probe(endpoint="/api/v1/accounts/current_user/", params={"edit": "true"})
    assert (event["status"], event["redirect"], event["location"]) == (
        302, "redirect_login", "https://www.instagram.com/accounts/login/")
    assert event["body_length"] == 0 and event["json_keys"] is None
    assert len(fake_http.calls) == 1
    call = fake_http.calls[0]
    assert call.method == "GET" and call.kwargs["allow_redirects"] is False
    assert call.kwargs["timeout"] == runner.TEST_HTTP_TIMEOUT

    fake_http.calls.clear()
    fake_http.responses = [(429, {"Content-Type": "application/json"}, '{"status": "fail", "message": "x"}')]
    started = time.monotonic()
    event = run_probe(endpoint="/api/v1/feed/user/1/", params={"count": "1"})
    assert time.monotonic() - started < 3.0
    assert (event["status"], event["redirect"], event["json_keys"]) == (429, None, ["message", "status"])
    assert fake_http.calls[0].method == "GET" and len(fake_http.calls) == 1
    assert FAKE not in json.dumps(event)


def test_probe_rejects_post():
    with pytest.raises(runner.RunnerError):
        runner.parse_request(json.dumps({"op": "probe", "cookies": {"sessionid": FAKE}, "method": "POST",
                                         "endpoint": "/api/v1/clips/user/"}).encode())


def test_session_test_without_location_is_not_home():
    outcome = runner.classify_session_response(302, None, "")
    assert (outcome["result"], outcome["detail"]) == ("api_refused", "redirect_other")
    assert "página inicial" not in outcome["message"]


@pytest.mark.parametrize(("body", "blocked"), [
    ('{"message":"x","feedback_title":"FAKE","status":"fail"}', True),
    ('{"message":"x","spam":true}', True),
    ('{"message":"feedback_required"}', True),
    ('{"message":"x","restriction_enrollment_data":null}', True),
    ('{"message":"x","user":{"feedback_title":"FAKE","spam":true}}', False),
    ('{"message":"Please wait a few minutes before you try again.","status":"fail"}', False),
    ("<html>feedback_required</html>", True),
    ("<html>ok</html>", False),
])
def test_action_blocked_detection_is_shared(body, blocked):
    assert runner.is_action_blocked(body) is blocked
    outcome = runner.classify_session_response(400, None, body)
    assert (outcome["result"] == "restricted") is blocked
    code, _ = runner.classify_exception(http_error(400, body), op="list", has_session=True)
    assert (code == "action_blocked") is blocked


def test_runner_test_session_valid(fake_http):
    fake_http.responses = [(200, {"Content-Type": "application/json"}, '{"user": {"username": "Dono"}}')]
    event = run_test_session()
    assert (event["result"], event["code"], event["username"], event["http_status"]) == ("valid", "ok", "dono", 200)


def test_runner_test_session_uses_own_feed_once_without_fallback(fake_http):
    fake_http.responses = [(200, {"Content-Type": "application/json"},
                            '{"items": [{"user": {"username": "Dono"}}], "num_results": 1, "status": "ok"}')]
    event = run_test_session()
    assert (event["result"], event["username"]) == ("valid", "dono")
    fake_http.calls.clear()
    fake_http.responses = [(200, {}, '{"items": [], "num_results": 0, "status": "ok"}')]
    event = run_test_session({"sessionid": "777%3AFAKEsessionFAKE%3A27%3AFAKEsig"})
    assert (event["result"], event["username"]) == ("valid", None)
    assert fake_http.calls[0].url == "https://www.instagram.com/api/v1/feed/user/777/"
    fake_http.calls.clear()
    fake_http.responses = [(500, {}, "")]
    assert run_test_session()["result"] == "instagram_error"
    assert len(fake_http.calls) == 1


def test_runner_test_session_without_user_id_makes_no_request(fake_http):
    event = run_test_session({"sessionid": "FAKEsessionWithoutNumericPrefix"})
    assert (event["result"], event["code"], event["http_status"]) == ("invalid", "session_invalid", None)
    assert fake_http.calls == []


def test_runner_test_session_network_and_timeout(fake_http):
    import requests

    fake_http.responses = [requests.exceptions.ConnectionError("dns")]
    event = run_test_session()
    assert (event["result"], event["code"], event["http_status"]) == ("network_error", "network_error", None)
    fake_http.responses = [requests.exceptions.ReadTimeout("Read timed out. (read timeout=15)")]
    event = run_test_session()
    assert (event["result"], event["code"], event["http_status"]) == ("timeout", "network_error", None)
    assert len(fake_http.calls) == 2  # sem novas tentativas


def test_runner_sends_extra_cookies_and_session_csrftoken(fake_http):
    extra = {"ds_user_id": "123", "csrftoken": FAKE_CSRF, "mid": "FAKE-MID-2222", "ig_did": "FAKE-IGDID-3333",
             "rur": "FAKE-RUR"}
    fake_http.responses = [(200, {}, '{"user": {"username": "dono"}}')]
    assert run_test_session({"sessionid": FAKE, **extra})["result"] == "valid"
    call = fake_http.calls[0]
    assert call.cookies == {"sessionid": FAKE, **extra}
    assert call.kwargs["headers"]["X-CSRFToken"] == FAKE_CSRF


def test_runner_without_csrftoken_keeps_generated_token_coherent(fake_http):
    fake_http.responses = [(200, {}, '{"user": {"username": "dono"}}')]
    run_test_session({"sessionid": FAKE, "ds_user_id": "123"})
    call = fake_http.calls[0]
    assert call.cookies["csrftoken"] and call.cookies["csrftoken"] != FAKE_CSRF
    assert call.kwargs["headers"]["X-CSRFToken"] == call.cookies["csrftoken"]


def test_runner_test_session_config_never_waits_on_429():
    from gallery_dl import config

    request = runner.parse_request(json.dumps({"op": "test_session", "cookies": {"sessionid": FAKE},
                                               "sleep_request": [20, 30]}).encode())
    runner._configure_gallery_dl(request)
    extr = runner._make_job("https://www.instagram.com/instagram/info/").extractor
    assert extr._retries == 0
    assert extr._interval_429 is None
    assert extr._timeout == runner.TEST_HTTP_TIMEOUT
    assert extr._interval_request() <= runner.TEST_SLEEP_REQUEST
    assert config.get(("extractor", "instagram"), "retries") == 0


@pytest.mark.parametrize(("op", "extra", "retries"), [
    ("profile", {"username": "exemplo"}, 1),
    ("resolve_post", {"shortcode": "ABCDEFGHIJK"}, 1),
    ("list", {"username": "exemplo", "content": "all"}, 2),
])
def test_runner_background_ops_wait_on_429_but_limited(op, extra, retries):
    request = runner.parse_request(json.dumps({"op": op, "cookies": {"sessionid": FAKE}, **extra}).encode())
    runner._configure_gallery_dl(request)
    extr = runner._make_job("https://www.instagram.com/instagram/info/").extractor
    assert extr._retries == retries
    assert extr._interval_429(1) == runner.SLEEP_429 == 30.0
    assert extr.request_interval == (6.0, 12.0)


# --------------------------------------------------------------------------- runner: normalização com gallery-dl real


def rest_post(code, pk, *, carousel=False, product_type="clips"):
    video = {
        "pk": f"{pk}1",
        "media_type": 2,
        "image_versions2": {"candidates": [{"url": f"https://scontent.cdninstagram.com/v/{code}.jpg?a=1",
                                            "width": 720, "height": 1280}]},
        "video_versions": [
            {"url": f"https://scontent.cdninstagram.com/v/{code}-lo.mp4?a=1", "width": 480, "height": 854, "type": 102},
            {"url": f"https://scontent.cdninstagram.com/v/{code}.mp4?a=1", "width": 720, "height": 1280, "type": 101},
        ],
        "video_dash_manifest": "<MPD/>",
        "video_duration": 12.5,
        "original_width": 720,
        "original_height": 1280,
    }
    image = {
        "pk": f"{pk}2",
        "media_type": 1,
        "image_versions2": {"candidates": [{"url": f"https://scontent.cdninstagram.com/v/{code}-img.jpg?a=1",
                                            "width": 1080, "height": 1350}]},
        "original_width": 1080,
        "original_height": 1350,
    }
    post = {
        "pk": pk,
        "code": code,
        "taken_at": 1700000000,
        "caption": {"text": "legenda #tag"},
        "user": {"pk": "1001", "username": "Exemplo", "full_name": "Perfil Exemplo"},
        "product_type": "carousel_container" if carousel else product_type,
    }
    if carousel:
        post["carousel_media"] = [image, video]
    else:
        post.update({k: v for k, v in video.items() if k != "pk"})
    return post


@pytest.fixture
def offline_listing(monkeypatch):
    from gallery_dl.extractor.instagram import InstagramRestAPI

    posts = [rest_post("REELAAAAAAA", "5001"), rest_post("CAROUSELBBB", "5002", carousel=True),
             rest_post("REELCCCCCCC", "5003")]

    def fake_fetch_profile(job, username):
        return {"id": "1001", "username": username, "is_private": False, "edge_owner_to_timeline_media": {"count": 3}}

    def forbid_network(self, *args, **kwargs):
        raise AssertionError("acesso de rede no teste")

    monkeypatch.setattr(runner, "_fetch_profile", fake_fetch_profile)
    monkeypatch.setattr(InstagramRestAPI, "user_feed", lambda self, uid: iter(posts))
    monkeypatch.setattr(InstagramRestAPI, "user_clips", lambda self, uid: iter([posts[0], posts[2]]))
    monkeypatch.setattr(InstagramRestAPI, "_call", forbid_network)
    request = runner.parse_request(json.dumps({"op": "list", "username": "exemplo", "content": "all",
                                               "cookies": {"sessionid": FAKE}}).encode())
    runner._configure_gallery_dl(request)
    return request


def run_list(request, **overrides):
    events = []
    runner.op_list({**request, **overrides}, events.append)
    return events


def test_runner_list_normalizes_real_gallerydl_output(offline_listing):
    events = run_list(offline_listing)
    assert events[0]["type"] == "profile"
    items = [e for e in events if e["type"] == "item"]
    assert [(i["source_post_id"], i["source_media_index"], i["media_type"]) for i in items] == [
        ("REELAAAAAAA", 0, "video"), ("CAROUSELBBB", 0, "image"), ("CAROUSELBBB", 1, "video"),
        ("REELCCCCCCC", 0, "video"),
    ]
    reel = items[0]
    assert reel["media_url"] == "https://scontent.cdninstagram.com/v/REELAAAAAAA.mp4?a=1"
    assert reel["thumbnail_url"] == "https://scontent.cdninstagram.com/v/REELAAAAAAA.jpg?a=1"
    assert reel["is_reel"] is True
    assert reel["source_url"] == "https://www.instagram.com/reel/REELAAAAAAA/"
    assert reel["duration"] == 12.5
    assert (reel["width"], reel["height"]) == (720, 1280)
    assert reel["taken_at"] == "2023-11-14T22:13:20Z"
    assert reel["extension"] == "mp4"
    assert reel["post_id"] == "5001" and reel["media_id"] == "5001"
    assert items[1]["media_id"] == "50022" and items[2]["media_id"] == "50021"
    assert reel["source_profile"] == "exemplo"
    carousel_video = items[2]
    assert carousel_video["is_reel"] is False
    assert carousel_video["source_url"] == "https://www.instagram.com/p/CAROUSELBBB/"
    assert items[1]["thumbnail_url"] is None and items[1]["duration"] is None
    for item in items:
        RemoteMedia(**{k: v for k, v in item.items() if k != "type"})


def test_runner_list_filters_and_limits(offline_listing):
    images = [e for e in run_list(offline_listing, content="images") if e["type"] == "item"]
    assert [(i["source_post_id"], i["media_type"]) for i in images] == [("CAROUSELBBB", "image")]
    limited = [e for e in run_list(offline_listing, content="videos", limit=2) if e["type"] == "item"]
    assert [i["source_post_id"] for i in limited] == ["REELAAAAAAA", "CAROUSELBBB"]
    reels = [e for e in run_list(offline_listing, content="reels") if e["type"] == "item"]
    assert [i["source_post_id"] for i in reels] == ["REELAAAAAAA", "REELCCCCCCC"]
    assert all(i["is_reel"] for i in reels)


def test_runner_list_private_profile(offline_listing, monkeypatch):
    monkeypatch.setattr(runner, "_fetch_profile", lambda job, username: {
        "id": "1001", "username": username, "is_private": True, "followed_by_viewer": False})
    with pytest.raises(runner.RunnerError) as info:
        run_list(offline_listing)
    assert info.value.code == "profile_private"


def test_runner_list_private_profile_without_friendship_info_tries_listing(offline_listing, monkeypatch):
    monkeypatch.setattr(runner, "_fetch_profile", lambda job, username: {
        "id": "1001", "username": username, "is_private": True})
    assert any(e["type"] == "item" for e in run_list(offline_listing))


def test_runner_list_private_profile_not_followed_via_topsearch(offline_listing, monkeypatch):
    monkeypatch.setattr(runner, "_fetch_profile", lambda job, username: {
        "id": "1001", "username": username, "is_private": True, "friendship_status": {"following": False}})
    with pytest.raises(runner.RunnerError) as info:
        run_list(offline_listing)
    assert info.value.code == "profile_private"


def test_runner_list_private_profile_followed_via_topsearch_friendship(offline_listing, monkeypatch):
    monkeypatch.setattr(runner, "_fetch_profile", lambda job, username: {
        "id": "1001", "username": username, "is_private": True, "friendship_status": {"following": True}})
    assert any(e["type"] == "item" for e in run_list(offline_listing))


# --------------------------------------------------------------------------- runner: resolução do perfil

TOPSEARCH = "https://www.instagram.com/web/search/topsearch/"
PROFILE_PAGE = "https://www.instagram.com/exemplo"
WEB_PROFILE_INFO = "https://www.instagram.com/api/v1/users/web_profile_info/"
SEARCH_HIT = ('{"users": [{"position": 0, "user": {"pk": "1001", "id": "1001", "username": "Exemplo", '
              '"full_name": "Perfil", "is_private": false, "profile_pic_url": "https://x.cdninstagram.com/a.jpg"}}],'
              ' "status": "ok"}')
SEARCH_MISS = '{"users": [{"user": {"pk": "9", "id": "9", "username": "outro"}}], "status": "ok"}'


def fetch_profile(fake_http):
    from gallery_dl import config
    from gallery_dl.extractor import common

    common.CACHE_MEMORY.clear()  # cache de usuários do gallery-dl é global ao processo
    request = runner.parse_request(json.dumps({"op": "probe", "endpoint": "/api/v1/x/",
                                               "cookies": {"sessionid": FAKE}}).encode())
    runner._configure_gallery_dl(request)  # sem nova tentativa e sem dormir em 429
    config.set(("extractor", "instagram"), "sleep-request", 0)
    config.set(("extractor", "instagram"), "user-cache", "memory")  # só CACHE_MEMORY, limpo acima
    return runner._fetch_profile(runner._make_job("https://www.instagram.com/exemplo/info/"), "exemplo")


def test_fetch_profile_uses_topsearch_first(fake_http):
    fake_http.responses = [(200, {"Content-Type": "application/json"}, SEARCH_HIT)]
    user = fetch_profile(fake_http)
    assert [c.url for c in fake_http.calls] == [TOPSEARCH]
    assert fake_http.calls[0].kwargs["params"] == {"query": "exemplo"}
    profile = runner.profile_from_user(user)
    assert (profile["username"], profile["user_id"], profile["is_private"]) == ("exemplo", "1001", False)


def test_fetch_profile_falls_back_to_profile_page_then_web_profile_info(fake_http):
    fake_http.responses = [(200, {}, SEARCH_MISS), (200, {}, '<html>"profile_id":"777"</html>')]
    user = fetch_profile(fake_http)
    assert [c.url for c in fake_http.calls] == [TOPSEARCH, PROFILE_PAGE]
    assert fake_http.calls[1].kwargs["headers"]["Sec-Fetch-Dest"] == "document"
    assert runner.profile_from_user(user)["user_id"] == "777"
    assert runner.profile_from_user(user)["username"] == "exemplo"

    fake_http.calls.clear()
    fake_http.responses = [(200, {}, SEARCH_MISS), (200, {}, "<html>sem id</html>"),
                           (200, {}, '{"data": {"user": {"id": "555", "username": "exemplo"}}}')]
    assert fetch_profile(fake_http)["id"] == "555"
    assert [c.url for c in fake_http.calls] == [TOPSEARCH, PROFILE_PAGE, WEB_PROFILE_INFO]


@pytest.mark.parametrize(("responses", "calls", "code"), [
    ([(429, {"Content-Type": "text/plain"}, "")], 1, "rate_limited"),
    ([(401, {}, NOT_LOGGED_IN)], 1, "request_auth_failed"),
    ([(200, {}, SEARCH_MISS), (404, {}, "<html></html>")], 2, "profile_not_found"),
    ([(200, {}, SEARCH_MISS), (429, {}, "")], 2, "rate_limited"),
])
def test_fetch_profile_http_failure_is_classified_without_next_strategy(fake_http, responses, calls, code):
    fake_http.responses = list(responses)
    with pytest.raises(Exception) as info:
        fetch_profile(fake_http)
    assert len(fake_http.calls) == calls
    assert runner.classify_exception(info.value, op="profile", has_session=True)[0] == code


def test_normalize_skips_non_https_and_missing_shortcode():
    kw = {"post_shortcode": "ABCDEFGHIJK", "video_url": None, "num": 1, "date": datetime(2024, 1, 2, 3, 4, 5)}
    assert runner.normalize_file("http://x/y.jpg", kw, profile="p", from_reels=False) is None
    assert runner.normalize_file("https://scontent.cdninstagram.com/y.jpg", {}, profile="p", from_reels=False) is None
    item = runner.normalize_file("https://scontent.cdninstagram.com/y.jpg", kw, profile="P", from_reels=False)
    assert item["taken_at"] == "2024-01-02T03:04:05Z" and item["source_profile"] == "p"


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b'{"op": "rm"}',
        b'{"op": "profile", "username": "../etc"}',
        b'{"op": "list", "username": "x", "content": "stories"}',
        b'{"op": "list", "username": "x", "content": "all", "limit": 0}',
        b'{"op": "resolve_post", "shortcode": "../x"}',
        b'{"op": "test_session", "cookies": {"evil": "1"}}',
        b'{"op": "test_session", "cookies": {"sessionid": "a b"}}',
        b'{"op": "test_session", "sleep_request": "fast"}',
        pytest.param(b"{" + b" " * (70 * 1024) + b"}", id="too-large"),
    ],
)
def test_parse_request_rejects_bad_input(payload):
    with pytest.raises(runner.RunnerError) as info:
        runner.parse_request(payload)
    assert info.value.code == "invalid_input"


def test_parse_request_never_lowers_sleep_interval():
    request = runner.parse_request(b'{"op": "test_session", "sleep_request": [0, 0]}')
    assert request["sleep_request"] == [6.0, 12.0]


# --------------------------------------------------------------------------- runner: processo real (sem rede)


def run_real_runner(payload: bytes):
    return subprocess.run(
        [sys.executable, "-m", "app.integrations.instagram.gallerydl_runner"],
        input=payload, capture_output=True, cwd=str(BACKEND_DIR), timeout=60, shell=False,
    )


def test_real_runner_invalid_operation():
    proc = run_real_runner(b'{"op": "nope", "cookies": {"sessionid": "FAKE-SESSION-0000"}}')
    assert proc.returncode == 2
    events = [json.loads(line) for line in proc.stdout.decode().splitlines()]
    assert events == [{"type": "error", "code": "invalid_input", "message": "operação inválida"}]
    assert FAKE.encode() not in proc.stdout + proc.stderr


def test_real_runner_session_missing():
    proc = run_real_runner(b'{"op": "test_session", "cookies": {}}')
    assert proc.returncode == 1
    events = [json.loads(line) for line in proc.stdout.decode().splitlines()]
    assert events[-1]["type"] == "error" and events[-1]["code"] == "session_missing"


def test_real_runner_through_downloader_without_network(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    dl = make_downloader(tmp_path, timeout=60)
    with pytest.raises(InstagramError) as info:
        list(dl._run({"op": "bogus"}, timeout=60))
    assert info.value.code == "invalid_input"
    assert FAKE not in str(info.value) and FAKE not in caplog.text


# --------------------------------------------------------------------------- sessão reconhecida x listagem

FEED_ITEMS = '{"items": [{"pk": "1", "user": {"username": "Dono"}}], "num_results": 1, "status": "ok"}'
FEEDBACK_BODY = ('{"feedback_title":"FAKE","feedback_message":"FAKE","message":"FAKE","spam":true,'
                 '"restriction_enrollment_data":{},"status":"fail"}')
HOME = {"Location": "https://www.instagram.com/"}
DELETE_SESSIONID = "sessionid=\"\"; Domain=.instagram.com; expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; Path=/"
KEEP_COOKIES = "csrftoken=FAKEcsrf; Domain=.instagram.com; Max-Age=31449600; Path=/; Secure"


@pytest.mark.parametrize(("status", "headers", "body", "expected"), [
    (302, HOME, "", ("api_refused", "access_refused", True, "refused")),
    (302, {**HOME, "Set-Cookie": KEEP_COOKIES}, "", ("api_refused", "access_refused", True, "refused")),
    (302, {**HOME, "Set-Cookie": DELETE_SESSIONID}, "", ("invalid", "session_invalid", False, "refused")),
    (302, {"Location": "https://www.instagram.com/accounts/login/?next=/api/"}, "",
     ("instagram_error", "request_auth_failed", None, "unknown")),
    (200, {"Content-Type": "application/json"}, FEED_ITEMS, ("valid", "ok", True, "ok")),
    (429, {"Content-Type": "text/plain"}, "", ("rate_limited", "rate_limited", True, "rate_limited")),
    (400, {"Content-Type": "application/json"}, FEEDBACK_BODY, ("restricted", "action_blocked", True, "refused")),
    (401, {"Content-Type": "application/json"}, NOT_LOGGED_IN, ("instagram_error", "request_auth_failed", None, "unknown")),
])
def test_runner_session_test_separates_session_and_listing(fake_http, caplog, status, headers, body, expected):
    caplog.set_level(logging.DEBUG)
    fake_http.responses = [(status, headers, body)]
    event = run_test_session()
    assert (event["result"], event["code"], event["session_recognized"], event["listing_api"]) == expected
    assert event["http_status"] == status and len(fake_http.calls) == 1
    assert FAKE not in json.dumps(event) and FAKE not in caplog.text
    assert "FAKEcsrf" not in caplog.text


def test_api_refused_message_states_only_proven_facts():
    out = runner.classify_session_response(302, "https://www.instagram.com/", "")
    assert out["message"] == (
        "O Instagram reconheceu a sessão, mas recusou a chamada de listagem usada pelo Media Tool (gallery-dl, "
        "endpoint feed/user), redirecionando para a página inicial. O Instagram Web atual carrega perfis por outra "
        "API (GraphQL), então isso não indica sessão inválida nem restrição da conta. A listagem por perfil ainda "
        "não funciona com esta sessão.")
    assert "restrição de atividade automatizada" not in out["message"]


def test_network_failure_leaves_session_unknown(fake_http):
    import requests

    fake_http.responses = [requests.exceptions.ConnectTimeout("timed out")]
    event = run_test_session()
    assert event["result"] in ("timeout", "network_error")
    assert (event["session_recognized"], event["listing_api"]) == (None, "unknown")


@pytest.mark.parametrize(("header", "reasons"), [
    (None, []),
    ("", []),
    (KEEP_COOKIES, []),
    ("sessionid=FAKEVALUE; Domain=.instagram.com; Max-Age=31449600; Path=/", []),
    ('sessionid=""; Domain=.instagram.com; Path=/', ["empty_value"]),
    ("sessionid=; Path=/", ["empty_value"]),
    ("sessionid=FAKEVALUE; Max-Age=0; Path=/", ["max_age_0"]),
    ("sessionid=FAKEVALUE; expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/", ["expires_past"]),
    (KEEP_COOKIES + ", " + DELETE_SESSIONID, ["empty_value", "expires_past", "max_age_0"]),
    ("ds_user_id=; Max-Age=0, sessionid=FAKEVALUE; Path=/", []),
])
def test_sessionid_deletion_detection(header, reasons):
    got = runner.sessionid_deletion(header)
    assert sorted(got) == reasons
    assert all("FAKEVALUE" not in reason for reason in got)


def test_downloader_passes_session_recognized_and_listing_api(tmp_path, fake_popen):
    event = dict(session_event("api_refused", "access_refused", http_status=302), detail="redirect_home",
                 session_recognized=True, listing_api="refused")
    fake_popen.script = [(lines_of(event, {"type": "done", "count": 1}), [], 0, False)]
    got = make_downloader(tmp_path).test_session()
    assert (got.result, got.code, got.session_recognized, got.listing_api) == (
        "api_refused", "access_refused", True, "refused")
    bad = dict(session_event("api_refused", "access_refused", http_status=302), session_recognized="sim",
               listing_api="talvez")
    fake_popen.script = [(lines_of(bad, {"type": "done", "count": 1}), [], 0, False)]
    got = make_downloader(tmp_path).test_session()
    assert (got.session_recognized, got.listing_api) == (None, "unknown")
