"""Revisão cruzada do adapter do Instagram (sem rede, sem sessão real; só valores falsos)."""

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from app.integrations.instagram import gallerydl as gallerydl_module
from app.integrations.instagram import gallerydl_runner as runner
from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.validation import parse_profile_input, validate_shortcode

# Reaproveita os dublês do teste do construtor (o diretório tests entra no sys.path pelo pytest).
from test_gallerydl_downloader import (  # noqa: F401
    FAKE,
    FAKE_CSRF,
    BACKEND_DIR,
    fake_popen,
    lines_of,
    make_downloader,
    offline_listing,
    rest_post,
    run_list,
    run_real_runner,
)


# --------------------------------------------------------------------------- 2. runner x gallery-dl real


@pytest.mark.parametrize(
    ("url", "cls"),
    [
        ("https://www.instagram.com/id:1001/reels/", "InstagramReelsExtractor"),
        ("https://www.instagram.com/id:1001/posts/", "InstagramPostsExtractor"),
        ("https://www.instagram.com/exemplo/info/", "InstagramInfoExtractor"),
        ("https://www.instagram.com/p/ABCDE12345/", "InstagramPostExtractor"),
    ],
)
def test_runner_urls_map_to_expected_extractors(url, cls):
    from gallery_dl import extractor

    assert type(extractor.find(url)).__name__ == cls


def test_runner_configures_gallery_dl_cookies_retries_and_ignores_user_config(offline_listing):
    from gallery_dl import config

    assert config.get(("extractor", "instagram"), "retries") == runner.RETRIES == 2
    assert config.get(("extractor", "instagram"), "videos") == "merged"
    assert config.get(("extractor",), "cookies-update") is False
    job = runner._make_job("https://www.instagram.com/exemplo/info/")
    extr = job.extractor
    assert extr._retries == 2
    assert extr.cookies.get("sessionid", domain=".instagram.com") == FAKE
    assert extr.cookies_check(("sessionid",)) is True
    # intervalo padrão do gallery-dl preservado (6-12 s)
    assert extr.request_interval == (6.0, 12.0)


def test_runner_limit_stops_pagination_after_first_page(monkeypatch, offline_listing):
    """`limit` por mídia: com limit=1 o runner não pede a página seguinte (nenhuma requisição extra)."""
    from gallery_dl.extractor.instagram import InstagramRestAPI

    calls = []
    pages = [
        {"items": [{"media": rest_post("PAGEONE0001", "7001")}], "paging_info": {"more_available": True, "max_id": "2"}},
        {"items": [{"media": rest_post("PAGETWO0002", "7002")}], "paging_info": {"more_available": False}},
    ]

    def fake_call(self, endpoint, **kwargs):
        calls.append(endpoint)
        return pages[len(calls) - 1]

    monkeypatch.undo()  # remove os patches de offline_listing; refaz só o necessário
    monkeypatch.setattr(runner, "_fetch_profile", lambda job, username: {"id": "1001", "username": username,
                                                                          "is_private": False})
    monkeypatch.setattr(InstagramRestAPI, "_call", fake_call)
    runner._configure_gallery_dl(offline_listing)
    events = run_list(offline_listing, content="reels", limit=1)
    assert [e["source_post_id"] for e in events if e["type"] == "item"] == ["PAGEONE0001"]
    assert calls == ["/v1/clips/user/"]


def test_normalize_single_feed_video_is_not_reel():
    """Decisão do orquestrador: is_reel só para product_type=clips ou endpoint de reels."""
    kw = {"post_shortcode": "FEEDVIDEO01", "video_url": "https://scontent.cdninstagram.com/v.mp4", "num": 1,
          "product_type": "feed", "type": "reel", "post_url": "https://www.instagram.com/reel/FEEDVIDEO01/"}
    item = runner.normalize_file("https://scontent.cdninstagram.com/v.mp4", kw, profile="p", from_reels=False)
    assert item["is_reel"] is False
    assert item["source_url"] == "https://www.instagram.com/p/FEEDVIDEO01/"
    clips = runner.normalize_file("https://scontent.cdninstagram.com/v.mp4", dict(kw, product_type="clips"),
                                  profile="p", from_reels=False)
    assert clips["is_reel"] is True and clips["source_url"] == "https://www.instagram.com/reel/FEEDVIDEO01/"
    from_reels = runner.normalize_file("https://scontent.cdninstagram.com/v.mp4", kw, profile="p", from_reels=True)
    assert from_reels["is_reel"] is True


def test_normalize_missing_timestamp_gives_none():
    from gallery_dl import dt

    kw = {"post_shortcode": "ABCDEFGHIJK", "num": 1, "date": dt.NONE, "post_date": dt.NONE}
    item = runner.normalize_file("https://scontent.cdninstagram.com/y.jpg", kw, profile="p", from_reels=False)
    assert item["taken_at"] is None


def test_own_private_profile_is_listable(monkeypatch, offline_listing):
    request = dict(offline_listing, cookies={"sessionid": FAKE, "ds_user_id": "1001"})
    monkeypatch.setattr(runner, "_fetch_profile", lambda job, username: {
        "id": "1001", "username": username, "is_private": True, "followed_by_viewer": False})
    events = run_list(request)  # não deve lançar profile_private
    assert any(e["type"] == "item" for e in events)
    other = dict(offline_listing, cookies={"sessionid": FAKE, "ds_user_id": "2002"})
    with pytest.raises(runner.RunnerError) as info:
        run_list(other)
    assert info.value.code == "profile_private"


# --------------------------------------------------------------------------- 1. vazamento de segredo


@pytest.mark.parametrize(
    "line",
    [
        f"Set-Cookie: sessionid={FAKE}; Path=/; Secure",
        f'Set-Cookie: sessionid="{FAKE}"; Path=/',
        f"Cookie: csrftoken={FAKE_CSRF}; sessionid={FAKE}; mid=ABCDEFG",
        f'{{"cookies": {{"sessionid": "{FAKE}"}}}}',
        f"{{'sessionid': '{FAKE}'}}",  # repr de dict com o valor conhecido
        f"quoted sessionid%3D{FAKE}",
    ],
)
def test_redact_covers_known_secret_in_any_format(line):
    out = runner.redact(line, [FAKE, FAKE_CSRF])
    assert FAKE not in out and FAKE_CSRF not in out


def test_redact_filter_handles_percent_args_and_exceptions():
    handler_records = []

    class Capture(logging.Handler):
        def emit(self, record):
            handler_records.append(self.format(record))

    logger = logging.getLogger("review.redact")
    logger.setLevel(logging.INFO)
    cap = Capture()
    cap.addFilter(runner._RedactFilter([FAKE]))
    logger.addHandler(cap)
    try:
        logger.info("cookie=%s dict=%r", FAKE, {"k": FAKE})
    finally:
        logger.removeHandler(cap)
    assert handler_records and FAKE not in handler_records[0]


def test_gallery_dl_does_not_log_at_debug_by_default():
    runner._setup_logging([FAKE])
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("instagram").getEffectiveLevel() == logging.INFO


def test_error_event_with_secret_in_stderr_and_message_never_reaches_logs(tmp_path, fake_popen, caplog):
    caplog.set_level(logging.DEBUG)
    stderr = [f"Traceback...\nKeyError: 'sessionid={FAKE}'\n".encode(), f"cookies={{'sessionid': '{FAKE}'}}\n".encode()]
    fake_popen.script = [(lines_of({"type": "error", "code": "internal_error", "message": f"x {FAKE}"}), stderr, 1, False)]
    with pytest.raises(InstagramError) as info:
        make_downloader(tmp_path).get_profile("exemplo")
    assert FAKE not in info.value.message and FAKE not in repr(info.value)
    assert FAKE not in caplog.text


def test_child_env_has_no_instagram_sessionid_any_case(tmp_path, monkeypatch):
    monkeypatch.setenv("instagram_sessionid", FAKE)
    monkeypatch.setenv("INSTAGRAM_SESSIONID", FAKE)
    env = make_downloader(tmp_path)._child_env()
    assert not any(k.upper() == "INSTAGRAM_SESSIONID" for k in env)
    assert FAKE not in json.dumps(env)


# --------------------------------------------------------------------------- 4. robustez


def test_error_event_kills_still_running_child(tmp_path, fake_popen):
    fake_popen.script = [(lines_of({"type": "error", "code": "rate_limited", "message": "429"}), [], 0, True)]
    with pytest.raises(InstagramError):
        make_downloader(tmp_path).get_profile("exemplo")
    assert fake_popen.instances[0].killed is True


def test_abandoned_generator_kills_child(tmp_path, fake_popen):
    item = {"type": "item", "source_platform": "instagram", "source_profile": "e", "source_post_id": "ABCDEFGHIJK",
            "post_id": "1", "media_id": "2", "source_media_index": 0, "media_type": "image", "is_reel": False,
            "source_url": "https://www.instagram.com/p/ABCDEFGHIJK/", "media_url": "https://x.cdninstagram.com/a.jpg",
            "thumbnail_url": None, "width": 1, "height": 1, "duration": None, "taken_at": None, "extension": "jpg"}
    fake_popen.script = [(lines_of(item), [], 0, True)]
    gen = make_downloader(tmp_path).iter_profile_media("exemplo", "all", None)
    assert next(gen).media_id == "2"
    gen.close()
    assert fake_popen.instances[0].killed is True


def test_huge_and_malformed_lines_are_skipped(tmp_path, fake_popen):
    lines = [b"x" * (5 * 1024 * 1024) + b"\n", b"{not json\n", b"[1,2]\n", b"\xff\xfe\n"]
    lines += lines_of({"type": "profile", "username": "josé", "user_id": "1"}, {"type": "done", "count": 1})
    fake_popen.script = [(lines, [], 0, False)]
    assert make_downloader(tmp_path).get_profile("exemplo").username == "josé"


def test_utf8_unescaped_output_is_decoded(tmp_path, fake_popen):
    raw = (json.dumps({"type": "profile", "username": "josé", "full_name": "Ação 日本"}, ensure_ascii=False) + "\n").encode()
    fake_popen.script = [([raw] + lines_of({"type": "done", "count": 1}), [], 0, False)]
    assert make_downloader(tmp_path).get_profile("exemplo").full_name == "Ação 日本"


def test_child_gets_backend_cwd_and_utf8_env(tmp_path, fake_popen):
    fake_popen.script = [(lines_of({"type": "profile", "username": "e"}, {"type": "done", "count": 1}), [], 0, False)]
    make_downloader(tmp_path).get_profile("exemplo")
    kwargs = fake_popen.instances[0].kwargs
    assert Path(kwargs["cwd"]) == BACKEND_DIR
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert kwargs["stdin"] == subprocess.PIPE


def test_real_runner_via_downloader_from_foreign_cwd_and_no_pythonpath(tmp_path, monkeypatch):
    """uvicorn --app-dir backend roda a partir de C:\\media-tool: o filho precisa achar `app` sozinho."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    dl = make_downloader(tmp_path, timeout=60)
    with pytest.raises(InstagramError) as info:
        list(dl._run({"op": "bogus"}, timeout=60))
    assert info.value.code == "invalid_input"  # a mensagem vem do runner, logo ele importou app e gallery_dl


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"\xff\xfe\xfd", id="non-utf8"),
        pytest.param(b"[1, 2]", id="array"),
        pytest.param(b"{", id="truncated"),
        pytest.param(b"", id="empty"),
        pytest.param(b"x" * (70 * 1024), id="oversized"),
    ],
)
def test_real_runner_bad_payloads_emit_single_error_line_and_no_traceback(payload):
    proc = run_real_runner(payload)
    assert proc.returncode == 2
    events = [json.loads(line) for line in proc.stdout.decode().splitlines()]
    assert len(events) == 1 and events[0]["code"] == "invalid_input"
    assert b"Traceback" not in proc.stderr


def test_real_runner_unicode_username_rejected_cleanly():
    payload = json.dumps({"op": "profile", "username": "josé", "cookies": {"sessionid": FAKE}}).encode()
    proc = run_real_runner(payload)
    events = [json.loads(line) for line in proc.stdout.decode().splitlines()]
    assert events[0]["code"] == "invalid_input"
    assert FAKE.encode() not in proc.stdout + proc.stderr


def test_slow_consumer_does_not_lose_timeout_semantics_for_fast_listing(tmp_path, fake_popen):
    """Sanidade: listagem normal dentro do prazo não é afetada pelo prazo."""
    fake_popen.script = [(lines_of({"type": "done", "count": 0}), [], 0, False)]
    assert list(make_downloader(tmp_path, timeout=5).iter_profile_media("exemplo", "all", 3)) == []


# --------------------------------------------------------------------------- 3. classificação (extras)


def _resp(status, text="", url="https://www.instagram.com/api/v1/x/"):
    from test_gallerydl_downloader import FakeResponse

    return FakeResponse(status, text, url)


@pytest.mark.parametrize(
    ("status", "body", "op", "has_session", "expected"),
    [
        (400, '{"message":"challenge_required"}', "list", True, "checkpoint_required"),
        (400, '{"message":"feedback_required"}', "list", True, "action_blocked"),
        (403, "", "list", False, "session_missing"),
        (410, "", "resolve_post", True, "internal_error"),
        (0, "", "list", True, "network_error"),
        (503, "", "list", True, "network_error"),
    ],
)
def test_classify_http_extras(status, body, op, has_session, expected):
    from gallery_dl import exception

    exc = exception.HttpError("", _resp(status, body))
    code, _ = runner.classify_exception(exc, op=op, has_session=has_session)
    assert code == expected


def test_classify_message_strips_query_and_secret_like_values():
    from gallery_dl import exception

    url = f"https://www.instagram.com/api/v1/x/?sessionid={FAKE}&max_id=9"
    code, msg = runner.classify_exception(exception.HttpError("", _resp(429, url=url)), op="list", has_session=True)
    assert FAKE not in msg and "max_id" not in msg


# --------------------------------------------------------------------------- 5. validação (hostil)


@pytest.mark.parametrize(
    "value",
    [
        "file:///etc/passwd", "javascript:alert(1)", "http://instagram.com.evil.com/u",
        "https://instagram.com.evil.com/u", "https://www.instagram.com.evil.com/u",
        "https://evil.com/www.instagram.com/u", "https://www.instagram.com@evil.com/u",
        "https://evil.com@www.instagram.com/u", "@", "@@u", "іnstagram", "ｕser", "user name", "../x",
        "https://www.instagram.com/p/ABC123/", "https://www.instagram.com/reel/ABC123/",
        "https://www.instagram.com/%75ser", "https://www.instagram.com:444/u", "https://www.instagram.com./u",
        "https://m.instagram.com/u", "http://www.instagram.com/u", "\\\\evil\\share", "user\x00", "a" * 31, "...", "_",
        "https://www.instagram.com/stories/user/1/", "https://www.instagram.com/accounts/login/?next=/u",
        "//www.instagram.com/u", "https://www.instagram.com/id:123/", None, 5,
    ],
)
def test_parse_profile_input_rejects_hostile(value):
    with pytest.raises(ValueError):
        parse_profile_input(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("@User.Name", "user.name"), ("https://www.instagram.com/user/?hl=en#x", "user"),
        ("https://www.instagram.com/user?utm=1", "user"), ("INSTAGRAM.COM/User", "user"),
        ("https://www.instagram.com/user/reels/", "user"), ("HTTPS://WWW.INSTAGRAM.COM/user", "user"),
    ],
)
def test_parse_profile_input_accepts_normalized(value, expected):
    assert parse_profile_input(value) == expected


@pytest.mark.parametrize("value", ["../abc12", "ABCD", "x" * 65, "abc de", "p/ABCDE", "ABC12?x=1", "ábcde", None])
def test_validate_shortcode_rejects(value):
    with pytest.raises(ValueError):
        validate_shortcode(value)
