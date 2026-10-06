"""classify_response e InstagramWebError (offline, httpx.MockTransport). Valores FAKE."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.logging import register_secret
from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.session import InstagramSession
from app.integrations.instagram.web.errors import InstagramWebError, classify_response
from app.integrations.instagram.web.http import InstagramWebHttp, WebResponse

FAKE_SESSIONID = "FAKE_SESSIONID_ERR_0123456789"
FAKE_CSRF = "FAKE_CSRF_ERR_0123456789"


def fetch(response: httpx.Response, *, document: bool = False) -> WebResponse:
    http = InstagramWebHttp(InstagramSession({"sessionid": FAKE_SESSIONID, "csrftoken": FAKE_CSRF}),
                            transport=httpx.MockTransport(lambda request: response))
    try:
        return http.get_document("/") if document else http.get_api("/api/test/", {"q": "x"})
    finally:
        http.close()


def classify(response: httpx.Response, stage: str = "graphql", *, document: bool = False):
    return classify_response(fetch(response, document=document), stage=stage)


def jresp(status: int, body: object, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "application/json", **headers}, text=json.dumps(body))


# --------------------------------------------------------------------------- InstagramWebError


def test_error_is_instagram_error_and_keeps_new_codes():
    err = InstagramWebError("contract_changed", "Formato mudou.", stage="graphql", diagnostic={"status": 200})
    assert isinstance(err, InstagramError)
    assert err.code == "contract_changed" and err.retryable is False and err.stage == "graphql"
    assert err.message == "Formato mudou." and str(err) == "Formato mudou."
    assert err.diagnostic == {"status": 200}
    assert "contract_changed" in repr(err)
    assert InstagramWebError("rate_limited", "x", retryable=True).retryable is True


def test_error_diagnostic_redacts_registered_secrets():
    register_secret("FAKE_DIAG_SECRET_0001")
    err = InstagramWebError("network_error", "x", diagnostic={"detail": "valor FAKE_DIAG_SECRET_0001",
                                                                "keys": ["a", "b"], "n": 3})
    assert "FAKE_DIAG_SECRET_0001" not in str(err.diagnostic)
    assert err.diagnostic["keys"] == ["a", "b"] and err.diagnostic["n"] == 3


# --------------------------------------------------------------------------- respostas utilizáveis


def test_200_json_ok():
    assert classify(jresp(200, {"data": {"x": 1}})) is None


def test_200_html_document_ok():
    resp = httpx.Response(200, headers={"content-type": "text/html"}, text="<html>feedback_required spam</html>")
    assert classify(resp, "bootstrap", document=True) is None


# --------------------------------------------------------------------------- status e marcadores


def test_401_request_auth_failed():
    # Seção 7: um 401 isolado é recusa deste request (pausa de verificação), não sessão inválida.
    err = classify(jresp(401, {"status": "fail"}))
    assert err.code == "request_auth_failed" and err.retryable is True and err.diagnostic["status"] == 401


def test_403_access_refused_not_session_claim():
    err = classify(httpx.Response(403, headers={"content-type": "text/html"}, text="<html></html>"))
    assert err.code == "access_refused" and err.retryable is False


def test_429_rate_limited_retryable():
    err = classify(jresp(429, {"message": "Please wait a few minutes before you try again.", "status": "fail"}))
    assert err.code == "rate_limited" and err.retryable is True


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_network_error_retryable(status):
    err = classify(httpx.Response(status, text=""))
    assert err.code == "network_error" and err.retryable is True and err.diagnostic["status"] == status


def test_invalid_json_contract_changed_for_json_stage():
    err = classify(httpx.Response(200, headers={"content-type": "application/json"}, text="for (;;); {oops"))
    assert err.code == "contract_changed" and err.stage == "graphql"
    assert classify(httpx.Response(200, headers={"content-type": "text/plain"}, text="ok"), "bootstrap") is None


def test_anti_hijacking_prefix_is_usable_json():
    resp = httpx.Response(200, headers={"content-type": "application/json"}, text='for (;;);{"data": {"x": 1}}')
    assert classify(resp) is None
    blocked = httpx.Response(400, headers={"content-type": "application/json"}, text='for (;;); {"spam": true}')
    assert classify(blocked).code == "action_blocked"


def test_unexpected_4xx_contract_changed():
    err = classify(jresp(400, {"errors": [{"message": "doc"}]}))
    assert err.code == "contract_changed"
    assert "errors" in err.diagnostic["json_keys"]


# --------------------------------------------------------------------------- redirects


def test_redirect_home_access_refused_not_session_invalid():
    err = classify(httpx.Response(302, headers={"location": "https://www.instagram.com/"}))
    assert err.code == "access_refused"
    assert err.diagnostic["redirect"] == "redirect_home" and err.diagnostic["location_path"] == "/"
    assert err.message == "O Instagram recusou esta consulta. Isso não indica sessão inválida."
    assert err.diagnostic["detail"] == "redirect_home"


def test_redirect_login_session_expired():
    err = classify(httpx.Response(302, headers={"location": "/accounts/login/?next=/x/"}), "bootstrap", document=True)
    assert err.code == "session_expired" and err.diagnostic["location_path"] == "/accounts/login/"


@pytest.mark.parametrize("location", ["/challenge/action/?x=1", "/checkpoint/123/"])
def test_redirect_challenge_checkpoint(location):
    assert classify(httpx.Response(302, headers={"location": location})).code == "checkpoint_required"


def test_redirect_other_access_refused():
    err = classify(httpx.Response(301, headers={"location": "/privacy/checks/"}))
    assert err.code == "access_refused" and err.diagnostic["redirect"] == "redirect_consent"


def test_redirect_with_sessionid_deleted_is_session_invalid():
    resp = httpx.Response(302, headers=[("location", "/"),
                                        ("set-cookie", 'sessionid=""; Domain=.instagram.com; Path=/; Max-Age=0')])
    err = classify(resp)
    assert err.code == "session_invalid" and err.diagnostic["sessionid_deleted"] is True


# --------------------------------------------------------------------------- ordem


def test_checkpoint_beats_429():
    err = classify(jresp(429, {"message": "checkpoint_required", "checkpoint_url": "/challenge/x/"}))
    assert err.code == "checkpoint_required"


def test_429_beats_login():
    assert classify(jresp(429, {"require_login": True})).code == "rate_limited"


def test_require_login_beats_please_wait_and_feedback():
    body = {"message": "Please wait a few minutes", "require_login": True, "spam": True, "status": "fail"}
    assert classify(jresp(400, body)).code == "request_auth_failed"


def test_feedback_restriction_action_blocked():
    body = {"feedback_title": "t", "feedback_message": "m", "spam": True, "restriction_enrollment_data": {},
            "status": "fail", "message": "feedback_required"}
    err = classify(jresp(400, body))
    assert err.code == "action_blocked" and err.diagnostic["status"] == 400
    assert "feedback_title" in err.diagnostic["json_keys"]


def test_feedback_beats_redirect_and_403():
    assert classify(jresp(403, {"spam": True})).code == "action_blocked"


def test_nested_feedback_key_not_restriction():
    assert classify(jresp(200, {"data": {"feedback_x": 1}})) is None


def test_200_with_data_and_partial_login_error_is_usable():
    body = {"data": {"conn": {"edges": [{"node": {"id": "1"}}], "page_info": {"has_next_page": False}}},
            "errors": [{"message": "login_required"}]}
    assert classify(jresp(200, body)) is None
    body_top = {**body, "message": "login_required", "status": "fail"}
    assert classify(jresp(200, body_top)) is None


def test_200_with_data_and_top_require_login_is_request_auth_failed():
    body = {"data": {"conn": {"edges": []}}, "require_login": True}
    assert classify(jresp(200, body)).code == "request_auth_failed"


def test_200_without_data_login_marker_is_request_auth_failed():
    assert classify(jresp(200, {"data": {}, "message": "login_required"})).code == "request_auth_failed"


def test_require_login_on_200():
    assert classify(jresp(200, {"require_login": True, "message": "login_required"})).code == "request_auth_failed"


_JARGON = ("bootstrap", "fb_dtsg", "lsd", "graphql", "token", "doc_id", "json", "http", "sessionid", "checkpoint",
           "challenge", "feedback", "restriction", "redirect")


@pytest.mark.parametrize("response", [
    jresp(200, {"checkpoint_url": "/challenge/x/"}),
    jresp(429, {}),
    httpx.Response(302, headers={"location": "/accounts/login/"}),
    jresp(200, {"require_login": True}),
    httpx.Response(302, headers=[("location", "/"), ("set-cookie", "sessionid=; Max-Age=0; Path=/")]),
    jresp(400, {"message": "Please wait a few minutes"}),
    jresp(400, {"spam": True}),
    httpx.Response(302, headers={"location": "/"}),
    httpx.Response(302, headers={"location": "/privacy/checks/"}),
    httpx.Response(401),
    httpx.Response(403),
    httpx.Response(500),
    httpx.Response(200, text="não json"),
    httpx.Response(404),
])
def test_user_messages_have_no_technical_jargon(response):
    err = classify(response)
    assert err is not None and err.diagnostic["detail"]
    lowered = err.message.lower()
    assert not [word for word in _JARGON if word in lowered], err.message


# --------------------------------------------------------------------------- segredos


def test_no_secret_in_error_or_diagnostic():
    register_secret(FAKE_CSRF)
    body = {"message": "feedback_required", "status": "fail", "note": FAKE_CSRF}
    resp = httpx.Response(400, headers=[("content-type", "application/json"),
                                        ("set-cookie", f"csrftoken={FAKE_CSRF}; Domain=.instagram.com; Path=/")],
                          text=json.dumps(body))
    err = classify(resp)
    dump = repr(err) + str(err) + json.dumps(err.diagnostic)
    for value in (FAKE_CSRF, FAKE_SESSIONID):
        assert value not in dump
    assert err.diagnostic["set_cookie_names"] == ["csrftoken"]
    assert err.diagnostic["path"] == "/api/test/"
