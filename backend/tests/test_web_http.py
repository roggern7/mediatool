"""InstagramWebHttp offline (httpx.MockTransport). Nenhuma requisição real; valores de cookies são FAKE."""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import pickle
import time

import httpx
import pytest

from app.integrations.instagram.session import InstagramSession
from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.http import (
    DEFAULT_USER_AGENT,
    InstagramWebHttp,
    WebHttpConfig,
    WebResponse,
)

FAKE_SESSIONID = "FAKE_SESSIONID_0123456789"
FAKE_CSRF = "FAKE_CSRF_0123456789"
FAKE_CSRF_NEW = "FAKE_CSRF_RENEWED_9876543210"
FAKE_DS_USER = "FAKE_DSUSER_424242"
FAKE_VALUES = (FAKE_SESSIONID, FAKE_CSRF, FAKE_CSRF_NEW, FAKE_DS_USER)


def make_session() -> InstagramSession:
    return InstagramSession({"sessionid": FAKE_SESSIONID, "csrftoken": FAKE_CSRF, "ds_user_id": FAKE_DS_USER})


class Recorder:
    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self.responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def make_http(responder, config: WebHttpConfig = WebHttpConfig()) -> tuple[InstagramWebHttp, Recorder]:
    rec = Recorder(responder)
    return InstagramWebHttp(make_session(), config, transport=httpx.MockTransport(rec)), rec


def html_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text="<html>ok</html>")


def assert_no_secret(text: str) -> None:
    for value in FAKE_VALUES:
        assert value not in text


# --------------------------------------------------------------------------- requisição básica


def test_document_request_headers_and_cookies():
    http, rec = make_http(html_ok)
    resp = http.get_document("/instagram/")
    assert resp.status == 200 and resp.text == "<html>ok</html>"
    assert resp.content_type.startswith("text/html")
    assert resp.location_path is None and resp.set_cookie_names == [] and resp.sessionid_deleted is False
    assert resp.path == "/instagram/"
    req = rec.requests[0]
    assert str(req.url) == "https://www.instagram.com/instagram/"
    assert req.headers["user-agent"] == DEFAULT_USER_AGENT
    assert req.headers["sec-fetch-mode"] == "navigate"
    assert req.headers["sec-fetch-dest"] == "document"
    assert req.headers["sec-fetch-site"] == "none"
    assert req.headers["upgrade-insecure-requests"] == "1"
    assert req.headers["accept"].startswith("text/html")
    assert req.headers["accept-language"].startswith("pt-BR")
    assert "x-csrftoken" not in req.headers
    cookie = req.headers["cookie"]
    assert f"sessionid={FAKE_SESSIONID}" in cookie and f"csrftoken={FAKE_CSRF}" in cookie
    http.close()


def test_user_agent_matches_gallery_dl_chrome_and_is_configurable():
    from gallery_dl.util import USERAGENT_CHROME

    assert DEFAULT_USER_AGENT == USERAGENT_CHROME
    http, rec = make_http(html_ok, WebHttpConfig(user_agent="Mozilla/5.0 Chrome/999.0.0.0 TestUA"))
    http.get_document("/")
    assert rec.requests[0].headers["user-agent"] == "Mozilla/5.0 Chrome/999.0.0.0 TestUA"
    assert 'v="999"' in rec.requests[0].headers["sec-ch-ua"]


def test_jar_domain_and_path():
    http, _ = make_http(html_ok)
    cookies = {c.name: c for c in http._client.cookies.jar}
    assert set(cookies) == {"sessionid", "csrftoken", "ds_user_id"}
    for cookie in cookies.values():
        assert cookie.domain == ".instagram.com" and cookie.path == "/"
    assert http.csrftoken() == FAKE_CSRF


def test_client_settings_no_redirect_no_env():
    http, _ = make_http(html_ok, WebHttpConfig(timeout=7.0))
    assert http._client.follow_redirects is False
    assert http._client._trust_env is False
    assert http._client.timeout.read == 7.0 and http._client.timeout.connect == 7.0


def test_api_headers_and_params():
    def responder(request):
        return httpx.Response(200, json={"users": []})

    http, rec = make_http(responder)
    resp = http.get_api("/web/search/topsearch/", {"query": "perfil_teste"}, referer="/perfil_teste/")
    assert resp.json() == {"users": []}
    req = rec.requests[0]
    assert req.url.path == "/web/search/topsearch/" and req.url.params["query"] == "perfil_teste"
    assert resp.path == "/web/search/topsearch/"
    assert req.headers["sec-fetch-mode"] == "cors" and req.headers["sec-fetch-dest"] == "empty"
    assert req.headers["x-ig-app-id"] == "936619743392459"
    assert req.headers["x-asbd-id"] == "359341"
    assert req.headers["x-requested-with"] == "XMLHttpRequest"
    assert req.headers["x-csrftoken"] == FAKE_CSRF
    assert req.headers["referer"] == "https://www.instagram.com/perfil_teste/"


def test_post_form_merges_headers_without_duplicates():
    http, rec = make_http(lambda r: httpx.Response(200, json={"data": {}}))
    http.post_form("/graphql/query", {"doc_id": "123", "variables": "{}"},
                   {"x-csrftoken": "FAKE_CSRF_OVERRIDE", "x-fb-friendly-name": "PolarisProfilePostsQuery"})
    req = rec.requests[0]
    assert req.method == "POST"
    assert req.headers.get_list("x-csrftoken") == ["FAKE_CSRF_OVERRIDE"]
    assert req.headers.get_list("content-type") == ["application/x-www-form-urlencoded"]
    assert req.headers["origin"] == "https://www.instagram.com"
    assert req.headers["x-fb-friendly-name"] == "PolarisProfilePostsQuery"
    assert "x-requested-with" not in req.headers
    assert b"doc_id=123" in req.content


def test_post_form_x_requested_with_only_when_caller_passes():
    http, rec = make_http(lambda r: httpx.Response(200, json={"data": {}}))
    http.post_form("/graphql/query", {}, {"X-Requested-With": "XMLHttpRequest"})
    assert rec.requests[0].headers.get_list("x-requested-with") == ["XMLHttpRequest"]


def test_json_accepts_anti_hijacking_prefix_only():
    def make(text: str) -> WebResponse:
        return WebResponse(200, None, "application/json", 1, [], False, text)

    assert make('for (;;);{"data": {"a": 1}}').json() == {"data": {"a": 1}}
    assert make('  for ( ; ; ) ;  {"a": 1}').json() == {"a": 1}
    with pytest.raises(ValueError):
        make('while(1);{"a": 1}').json()
    with pytest.raises(ValueError):
        make('for (;;); for (;;);{"a": 1}').json()


# --------------------------------------------------------------------------- redirects e cookies


def test_redirect_not_followed_and_location_without_query():
    def responder(request):
        return httpx.Response(302, headers={"location": "https://www.instagram.com/accounts/login/?next=%2Fx%2F"})

    http, rec = make_http(responder)
    resp = http.get_api("/api/v1/feed/user/1/")
    assert len(rec.requests) == 1
    assert resp.status == 302 and resp.location_path == "/accounts/login/"


def test_redirect_relative_location_home():
    http, _ = make_http(lambda r: httpx.Response(302, headers={"location": "/?hl=pt"}))
    assert http.get_api("/x/").location_path == "/"


def test_set_cookie_renews_csrftoken_in_jar():
    calls = []

    def responder(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, headers=[
                ("set-cookie", f"csrftoken={FAKE_CSRF_NEW}; Domain=.instagram.com; Path=/; Max-Age=31449600; Secure"),
                ("set-cookie", "rur=\"FAKE_RUR_VALUE\"; Domain=.instagram.com; Path=/; Secure; HttpOnly"),
            ], json={})
        return httpx.Response(200, json={})

    http, _ = make_http(responder)
    resp = http.get_api("/a/")
    assert resp.set_cookie_names == ["csrftoken", "rur"]
    assert resp.sessionid_deleted is False
    assert http.csrftoken() == FAKE_CSRF_NEW
    http.get_api("/b/")
    assert calls[1].headers["x-csrftoken"] == FAKE_CSRF_NEW
    assert f"csrftoken={FAKE_CSRF_NEW}" in calls[1].headers["cookie"]
    assert FAKE_CSRF + ";" not in calls[1].headers["cookie"] + ";"


def test_set_cookie_without_domain_does_not_duplicate():
    http, _ = make_http(lambda r: httpx.Response(200, headers={"set-cookie": f"csrftoken={FAKE_CSRF_NEW}; Path=/"},
                                                 json={}))
    http.get_api("/a/")
    names = [c.name for c in http._client.cookies.jar]
    assert names.count("csrftoken") == 1
    assert http.csrftoken() == FAKE_CSRF_NEW


@pytest.mark.parametrize("set_cookie", [
    "sessionid=\"\"; Domain=.instagram.com; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0",
    "sessionid=; Domain=.instagram.com; Path=/",
    "sessionid=FAKE_OTHER_SESSION; Domain=.instagram.com; Path=/; Max-Age=0",
    "sessionid=FAKE_OTHER_SESSION; Domain=.instagram.com; Path=/; Expires=Thu, 01 Jan 2015 00:00:00 GMT",
])
def test_set_cookie_deleting_sessionid(set_cookie):
    http, rec = make_http(lambda r: httpx.Response(302, headers=[("location", "/"), ("set-cookie", set_cookie)]))
    resp = http.get_api("/a/")
    assert resp.sessionid_deleted is True
    assert resp.set_cookie_names == ["sessionid"]
    assert "sessionid" not in {c.name for c in http._client.cookies.jar}
    assert_no_secret(repr(resp))


def test_set_cookie_other_cookie_deleted_is_not_sessionid():
    http, _ = make_http(lambda r: httpx.Response(200, headers={"set-cookie": "rur=; Max-Age=0; Path=/"}, json={}))
    resp = http.get_api("/a/")
    assert resp.sessionid_deleted is False
    assert "sessionid" in {c.name for c in http._client.cookies.jar}


# --------------------------------------------------------------------------- paths inválidos


@pytest.mark.parametrize("path", [
    "", "instagram/", "//evil.example/x", "/a//b", "https://evil.example/", "http://www.instagram.com/",
    "/a\\b", "/a\r\nX-Injected: 1", "/a\nb", "/a\x00b", None,
])
def test_invalid_path_rejected(path):
    http, rec = make_http(html_ok)
    for call in (lambda: http.get_document(path), lambda: http.get_api(path),
                 lambda: http.post_form(path, {}, {})):
        with pytest.raises(ValueError):
            call()
    assert rec.requests == []


def test_invalid_referer_rejected():
    http, rec = make_http(html_ok)
    with pytest.raises(ValueError):
        http.get_document("/", referer="https://evil.example/")
    with pytest.raises(ValueError):
        http.get_api("/a/", referer="//evil.example/")
    assert rec.requests == []


# --------------------------------------------------------------------------- rede e timeout


def test_read_timeout_becomes_timeout_error():
    def responder(request):
        raise httpx.ReadTimeout("simulated", request=request)

    http, _ = make_http(responder)
    with pytest.raises(InstagramWebError) as info:
        http.get_api("/a/", {"q": FAKE_CSRF})
    err = info.value
    assert err.code == "timeout" and err.retryable is True
    assert err.__cause__ is None and err.__suppress_context__ is True
    assert err.diagnostic["path"] == "/a/"
    assert_no_secret(repr(err) + str(err) + str(err.diagnostic))


def test_connect_error_becomes_network_error():
    def responder(request):
        raise httpx.ConnectError("simulated", request=request)

    http, _ = make_http(responder)
    with pytest.raises(InstagramWebError) as info:
        http.get_document("/")
    assert info.value.code == "network_error" and info.value.retryable is True


def test_total_deadline_enforced_on_slow_body():
    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(5):
                time.sleep(0.03)
                yield b"x" * 10

    http, _ = make_http(lambda r: httpx.Response(200, stream=SlowStream()), WebHttpConfig(timeout=0.05))
    with pytest.raises(InstagramWebError) as info:
        http.get_document("/")
    assert info.value.code == "timeout"


# --------------------------------------------------------------------------- segredo


def test_web_response_repr_and_serialization_blocked():
    resp = WebResponse(status=200, location_path=None, content_type="text/html", elapsed_ms=1,
                       set_cookie_names=["csrftoken"], sessionid_deleted=False,
                       text=f"<html>{FAKE_CSRF}</html>", path="/")
    for text in (repr(resp), str(resp), f"{resp}", "%s" % (resp,)):
        assert FAKE_CSRF not in text and "<html>" not in text
    with pytest.raises(TypeError):
        pickle.dumps(resp)
    for expose in (dataclasses.asdict, dataclasses.astuple, vars, copy.copy, copy.deepcopy, json.dumps):
        with pytest.raises(TypeError):
            expose(resp)
    assert not hasattr(resp, "__dict__")
    with pytest.raises(ValueError):
        WebResponse(400, None, "text/html", 1, [], False, "não é json").json()


def test_no_secret_in_repr_or_logs(caplog):
    def responder(request):
        return httpx.Response(200, headers={"set-cookie": f"csrftoken={FAKE_CSRF_NEW}; Domain=.instagram.com; Path=/"},
                              text=f"body {FAKE_SESSIONID}")

    caplog.set_level(logging.DEBUG)
    http, _ = make_http(responder)
    http.get_document("/")
    http.get_api("/a/")
    http.post_form("/graphql/query", {"fb_dtsg": "FAKE_DTSG_FORM_000"}, {"x-csrftoken": FAKE_CSRF_NEW})
    assert_no_secret(repr(http) + str(http))
    assert "FAKE_DTSG_FORM_000" not in caplog.text
    assert_no_secret(caplog.text)
    with pytest.raises(TypeError):
        pickle.dumps(http)


def test_account_id_reads_ds_user_id_from_jar_and_registers_secret():
    from app.core.logging import redact

    with_id = InstagramWebHttp(InstagramSession({"sessionid": "FAKE_SESSIONID_ACC_01", "ds_user_id": "70000000001"}))
    without = InstagramWebHttp(InstagramSession({"sessionid": "FAKE_SESSIONID_ACC_02"}))
    try:
        assert with_id.account_id() == "70000000001" and without.account_id() is None
        assert "70000000001" not in redact("conta 70000000001") and "70000000001" not in repr(with_id)
    finally:
        with_id.close()
        without.close()


# --------------------------------------------------------------------------- cliente anônimo e documento da publicação


def test_anonymous_client_has_an_empty_jar_and_sends_no_cookie():
    rec = Recorder(html_ok)
    http = InstagramWebHttp.anonymous(transport=httpx.MockTransport(rec))
    try:
        assert list(http._client.cookies.jar) == []
        resp = http.get_document("/reel/FAKECODE001/")
        assert resp.status == 200 and http.csrftoken() is None and http.account_id() is None
    finally:
        http.close()
    [request] = rec.requests
    assert "cookie" not in request.headers and request.headers["sec-fetch-site"] == "none"
    assert "x-csrftoken" not in request.headers and "referer" not in request.headers


def test_anonymous_client_uses_the_gate_and_the_config():
    slots: list[object] = []

    class Gate:
        def slot(self, cancel=None):
            from contextlib import nullcontext

            slots.append(cancel)
            return nullcontext()

    rec = Recorder(html_ok)
    http = InstagramWebHttp.anonymous(WebHttpConfig(user_agent="FAKE-UA/1.0"), transport=httpx.MockTransport(rec),
                                      gate=Gate())
    try:
        http.get_document("/p/FAKECODE001/")
    finally:
        http.close()
    assert slots == [None] and rec.requests[0].headers["user-agent"] == "FAKE-UA/1.0"


def test_diagnostic_path_replaces_the_real_path_in_response_log_and_errors(caplog):
    caplog.set_level(logging.DEBUG, logger="media_tool.instagram.web.http")
    http, rec = make_http(html_ok)
    try:
        resp = http.get_document("/reel/FAKECODE001/", diagnostic_path="/reel/")
    finally:
        http.close()
    assert resp.path == "/reel/" and rec.requests[0].url.path == "/reel/FAKECODE001/"
    assert "instagram web GET /reel/ -> 200" in caplog.text and "FAKECODE001" not in caplog.text

    def broken(request):
        raise httpx.ConnectError("falha FAKE")

    http, _ = make_http(broken)
    try:
        with pytest.raises(InstagramWebError) as info:
            http.get_document("/reel/FAKECODE001/", diagnostic_path="/reel/")
    finally:
        http.close()
    assert info.value.diagnostic["path"] == "/reel/" and "FAKECODE001" not in str(info.value.diagnostic)


def test_max_bytes_stops_reading_and_raises_contract_changed():
    def big(request):
        return httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/html"})

    http, _ = make_http(big)
    try:
        assert len(http.get_document("/", max_bytes=5000).text) == 5000, "no teto exato ainda passa"
        assert len(http.get_document("/").text) == 5000, "sem teto por padrão (bootstrap)"
        with pytest.raises(InstagramWebError) as info:
            http.get_document("/p/FAKECODE001/", max_bytes=4999, diagnostic_path="/p/")
    finally:
        http.close()
    error = info.value
    assert (error.code, error.retryable, error.stage) == ("contract_changed", False, "http")
    assert error.diagnostic == {"detail": "document_too_large", "method": "GET", "path": "/p/"}


def test_unknown_charset_is_read_as_utf8_instead_of_crashing():
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="<html>olá</html>".encode(),
                              headers={"content-type": "text/html; charset=nope-8"})

    http, _ = make_http(responder)
    assert http.get_document("/").text == "<html>olá</html>"
