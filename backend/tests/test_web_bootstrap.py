"""parse_bootstrap e WebBootstrap offline (fixtures sintéticas em tests/fixtures/instagram_web). Valores FAKE."""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import pickle
from pathlib import Path

import httpx
import pytest

from app.core.logging import redact
from app.integrations.instagram.session import InstagramSession
from app.integrations.instagram.web.bootstrap import (
    BootstrapParse,
    WebBootstrap,
    WebState,
    compute_jazoest,
    parse_bootstrap,
)
from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.http import InstagramWebHttp

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- parse


def test_compute_jazoest():
    assert compute_jazoest("ABC") == "2" + str(65 + 66 + 67)
    assert compute_jazoest("") == "20"


def test_parse_define_format():
    state, parse = parse_bootstrap(fixture("bootstrap_define.html"))
    assert state is not None
    assert state.fb_dtsg == "FAKE_DTSG_DEFINE_0001:17:1700000000"
    assert state.lsd == "FAKE_LSD_DEFINE_0001"
    assert state.jazoest == compute_jazoest(state.fb_dtsg)
    assert state.viewer_id == "10000000001" and state.app_id == "936619743392459"
    assert parse == BootstrapParse(markers_found=["DTSGInitialData", "LSD", "USER_ID", "APP_ID"],
                                   markers_missing=[], html_viewer_hint="real")


def test_parse_alt_format_spaces_order_and_escaped_slash():
    state, parse = parse_bootstrap(fixture("bootstrap_alt.html"))
    assert state.fb_dtsg == "FAKE_DTSG_ALT_0002/x/y"
    assert state.lsd == "FAKE_LSD_ALT_0002"
    assert state.viewer_id == "10000000002" and state.app_id == "936619743392459"
    assert parse.markers_found == ["DTSGInitData", "lsd", "viewerId", "appId"]
    assert parse.html_viewer_hint == "real"


def test_parse_escaped_json_format():
    state, parse = parse_bootstrap(fixture("bootstrap_escaped.html"))
    assert state.fb_dtsg == "FAKE_DTSG_ESC_0003"
    assert state.lsd == "FAKE_LSD_ESC_0003"
    assert state.viewer_id == "10000000003" and state.app_id is None
    assert parse.markers_found == ["dtsg", "LSD", "actorID"]


def test_parse_input_name_value_format_both_orders():
    state, parse = parse_bootstrap(fixture("bootstrap_input.html"))
    assert state.fb_dtsg == "FAKE_DTSG_INPUT_0004" and state.lsd == "FAKE_LSD_INPUT_0004"
    assert parse.markers_found[0] == "fb_dtsg_input"
    reversed_html = '{"value": "FAKE_DTSG_INPUT_REV", "name": "fb_dtsg"} {"lsd":"FAKE_LSD_REV_000"} "USER_ID":"1"'
    state, _ = parse_bootstrap(reversed_html)
    assert state.fb_dtsg == "FAKE_DTSG_INPUT_REV"


def test_first_marker_wins():
    html = '"dtsg":{"token":"FAKE_DTSG_SECOND"} ["DTSGInitialData",[],{"token":"FAKE_DTSG_FIRST"}] "lsd":"FAKE_LSD_X1"'
    state, parse = parse_bootstrap(html)
    assert state.fb_dtsg == "FAKE_DTSG_FIRST" and parse.markers_found[0] == "DTSGInitialData"


def test_parse_no_tokens():
    state, parse = parse_bootstrap(fixture("bootstrap_no_tokens.html"))
    assert state is None
    assert parse.markers_missing == ["fb_dtsg", "lsd"] and parse.markers_found == ["USER_ID"]
    assert parse.html_viewer_hint == "real"


def test_parse_logged_out():
    state, parse = parse_bootstrap(fixture("bootstrap_logged_out.html"))
    assert state is None
    assert parse.html_viewer_hint == "zero"
    assert "USER_ID" in parse.markers_found and "fb_dtsg" in parse.markers_missing


def test_parse_viewer_zero_with_tokens_is_normal_state():
    # Caso NORMAL: o navegador logado também recebe "USER_ID":"0" no HTML inicial.
    state, parse = parse_bootstrap(fixture("bootstrap_viewer_zero.html"))
    assert state is not None and state.viewer_id is None and parse.html_viewer_hint == "zero"
    assert state.fb_dtsg == "FAKE_DTSG_ZERO_0007" and state.lsd == "FAKE_LSD_ZERO_0007"
    assert state.app_id == "936619743392459"


def test_parse_changed_structure():
    state, parse = parse_bootstrap(fixture("bootstrap_changed.html"))
    assert state is None
    assert parse.markers_found == [] and parse.markers_missing == ["fb_dtsg", "lsd", "viewer"]
    assert parse.html_viewer_hint == "absent"


def test_parse_registers_secrets():
    state, _ = parse_bootstrap(fixture("bootstrap_define.html"))
    assert redact(f"x {state.fb_dtsg} y {state.lsd}") == "x <redacted> y <redacted>"


# --------------------------------------------------------------------------- WebState


def test_web_state_redacted_and_not_serializable():
    state, _ = parse_bootstrap(fixture("bootstrap_define.html"))
    for text in (repr(state), str(state), f"{state}", "%s" % (state,)):
        assert "FAKE_" not in text and "10000000001" not in text and state.jazoest not in text
    with pytest.raises(TypeError):
        pickle.dumps(state)
    with pytest.raises(TypeError):
        json.dumps(state)
    for expose in (dataclasses.asdict, dataclasses.astuple, vars, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            expose(state)
    assert not hasattr(state, "__dict__")


# --------------------------------------------------------------------------- WebBootstrap


class Server:
    def __init__(self, *responses: httpx.Response):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses[min(len(self.requests), len(self.responses)) - 1]


def html_response(name: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=fixture(name))


def make_bootstrap(server: Server, **kwargs) -> WebBootstrap:
    session = InstagramSession({"sessionid": "FAKE_SESSIONID_BOOT_0123", "csrftoken": "FAKE_CSRF_BOOT_0123"})
    return WebBootstrap(InstagramWebHttp(session, transport=httpx.MockTransport(server)), **kwargs)


def test_bootstrap_get_caches_and_one_get_per_call():
    server = Server(html_response("bootstrap_define.html"))
    boot = make_bootstrap(server)
    state = boot.get()
    assert isinstance(state, WebState) and state.lsd == "FAKE_LSD_DEFINE_0001"
    assert boot.get() is state
    assert len(server.requests) == 1
    req = server.requests[0]
    assert req.method == "GET" and str(req.url) == "https://www.instagram.com/"
    assert req.headers["sec-fetch-mode"] == "navigate"


def test_bootstrap_force_invalidate_and_ttl():
    server = Server(html_response("bootstrap_define.html"))
    boot = make_bootstrap(server)
    first = boot.get()
    assert boot.get(force=True) is not first and len(server.requests) == 2
    boot.invalidate()
    boot.get()
    assert len(server.requests) == 3
    expired = make_bootstrap(server, ttl=0.0)
    expired.get()
    expired.get()
    assert len(server.requests) == 5


def test_bootstrap_custom_path():
    server = Server(html_response("bootstrap_define.html"))
    make_bootstrap(server, path="/instagram/").get()
    assert server.requests[0].url.path == "/instagram/"


def test_bootstrap_redirect_login_classified_before_parse():
    server = Server(httpx.Response(302, headers={"location": "/accounts/login/?next=/"}))
    with pytest.raises(InstagramWebError) as info:
        make_bootstrap(server).get()
    assert info.value.code == "session_expired" and info.value.stage == "bootstrap"


def test_bootstrap_redirect_home_access_refused():
    server = Server(httpx.Response(302, headers={"location": "https://www.instagram.com/"}))
    with pytest.raises(InstagramWebError) as info:
        make_bootstrap(server, path="/instagram/").get()
    assert info.value.code == "access_refused"


def test_bootstrap_429_and_5xx():
    with pytest.raises(InstagramWebError) as info:
        make_bootstrap(Server(httpx.Response(429))).get()
    assert info.value.code == "rate_limited" and info.value.retryable
    with pytest.raises(InstagramWebError) as info:
        make_bootstrap(Server(httpx.Response(503))).get()
    assert info.value.code == "network_error" and info.value.retryable


def test_bootstrap_timeout():
    def responder(request):
        raise httpx.ReadTimeout("simulated", request=request)

    boot = WebBootstrap(InstagramWebHttp(InstagramSession({"sessionid": "FAKE_SESSIONID_BOOT_0123"}),
                                         transport=httpx.MockTransport(responder)))
    with pytest.raises(InstagramWebError) as info:
        boot.get()
    assert info.value.code == "timeout"


def test_bootstrap_no_tokens_contract_changed():
    with pytest.raises(InstagramWebError) as info:
        make_bootstrap(Server(html_response("bootstrap_no_tokens.html"))).get()
    err = info.value
    assert err.code == "contract_changed"
    assert err.diagnostic["markers_missing"] == ["fb_dtsg", "lsd"]
    assert err.diagnostic["size"] == len(fixture("bootstrap_no_tokens.html"))


def test_bootstrap_changed_structure_contract_changed():
    with pytest.raises(InstagramWebError) as info:
        make_bootstrap(Server(html_response("bootstrap_changed.html"))).get()
    assert info.value.code == "contract_changed"
    assert info.value.diagnostic["markers_missing"] == ["fb_dtsg", "lsd", "viewer"]


def test_bootstrap_viewer_zero_without_dtsg_is_contract_changed():
    # Viewer "0" não classifica a sessão; só a falta de fb_dtsg/lsd é erro (contract_changed).
    server = Server(html_response("bootstrap_logged_out.html"))
    boot = make_bootstrap(server)
    with pytest.raises(InstagramWebError) as info:
        boot.get()
    assert info.value.code == "contract_changed"
    assert info.value.diagnostic["detail"] == "token_marker_missing"
    assert info.value.diagnostic["html_viewer_hint"] == "zero"
    # Erro não fica em cache: próxima chamada faz novo GET.
    with pytest.raises(InstagramWebError):
        boot.get()
    assert len(server.requests) == 2


def test_bootstrap_tokens_without_viewer_marker_is_normal_state():
    html = '["DTSGInitialData",[],{"token":"FAKE_DTSG_NOVIEW"}] ["LSD",[],{"token":"FAKE_LSD_NOVIEW"}]'
    server = Server(httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    state = make_bootstrap(server).get()
    assert state.fb_dtsg == "FAKE_DTSG_NOVIEW" and state.viewer_id is None


def test_bootstrap_viewer_zero_with_tokens_is_normal_state(caplog):
    caplog.set_level(logging.DEBUG)
    server = Server(html_response("bootstrap_viewer_zero.html"))
    boot = make_bootstrap(server)
    state = boot.get()
    assert state.fb_dtsg == "FAKE_DTSG_ZERO_0007" and state.viewer_id is None
    assert boot.get() is state and len(server.requests) == 1   # vai para o cache como qualquer estado
    assert "pista de viewer no HTML zero" in caplog.text
    assert "FAKE_DTSG_ZERO_0007" not in caplog.text and "FAKE_LSD_ZERO_0007" not in caplog.text


@pytest.mark.parametrize("name", ["bootstrap_no_tokens.html", "bootstrap_logged_out.html",
                                  "bootstrap_changed.html"])
def test_bootstrap_messages_have_no_technical_jargon(name):
    with pytest.raises(InstagramWebError) as info:
        make_bootstrap(Server(html_response(name))).get()
    lowered = info.value.message.lower()
    for word in ("bootstrap", "fb_dtsg", "lsd", "graphql", "token", "doc_id", "marcador"):
        assert word not in lowered
    assert info.value.diagnostic["detail"]


def test_bootstrap_no_secret_in_logs_errors_or_diagnostic(caplog):
    caplog.set_level(logging.DEBUG)
    boot = make_bootstrap(Server(html_response("bootstrap_define.html")))
    state = boot.get()
    secrets = (state.fb_dtsg, state.lsd, "FAKE_SESSIONID_BOOT_0123", "FAKE_CSRF_BOOT_0123")
    errors = []
    for name in ("bootstrap_no_tokens.html", "bootstrap_logged_out.html", "bootstrap_changed.html"):
        with pytest.raises(InstagramWebError) as info:
            make_bootstrap(Server(html_response(name))).get()
        errors.append(info.value)
    dump = caplog.text + repr(boot) + "".join(repr(e) + str(e) + json.dumps(e.diagnostic) for e in errors)
    for value in secrets + ("FAKE_LSD_LOGGEDOUT_0006", "FAKE_DTSG_CHANGED_0007", "<html", "<script"):
        assert value not in dump
    assert "DTSGInitialData" in caplog.text  # só nomes de marcadores
