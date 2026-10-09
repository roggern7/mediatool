"""web/graphql.py: consulta de post isolado (post_by_shortcode). Offline, com os fakes de test_web_graphql.

A consulta NÃO foi verificada no Instagram real: as respostas aqui são fixtures falsas no formato esperado.
"""

from __future__ import annotations

import json

import pytest

from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.http import WebResponse
from test_web_graphql import (  # noqa: F401 - boot e _no_token_in_logs são fixtures
    ROOT,
    FakeHttp,
    _no_token_in_logs,
    assert_no_tokens,
    boot,
    fixture_text,
    gql,
    ok,
    resp,
)

POST_NAME = "PolarisPostActionLoadPostQueryQuery"
POST_ROOT = "xdt_shortcode_media"
OTHER_ROOT = "xdt_api__v1__media__shortcode__web_info"
SHORTCODE = "FAKEPOSTIMG1"
VIEWER = "70000000001"


def post_json(root, *, errors=None, viewer: str | None = None, root_field: str = POST_ROOT) -> WebResponse:
    data: dict = {root_field: root}
    if viewer is not None:
        data["xdt_viewer"] = {"user": {"id": viewer}}
    payload: dict = {"data": data}
    if errors is not None:
        payload["errors"] = errors
    return resp(200, json.dumps(payload))


def post_error(http, boot, **kw) -> InstagramWebError:
    with pytest.raises(InstagramWebError) as info:
        gql(http, boot, **kw).post_by_shortcode(SHORTCODE)
    assert_no_tokens(info.value, boot)
    blob = " ".join([str(info.value), repr(info.value), json.dumps(info.value.diagnostic, default=str)])
    assert SHORTCODE not in blob
    return info.value


def test_post_query_form_headers_and_single_request(boot, caplog):
    http = FakeHttp(ok("post_image.json"))
    root = gql(http, boot).post_by_shortcode(SHORTCODE)
    assert root["shortcode"] == SHORTCODE and root["__typename"] == "XDTGraphImage"
    assert len(http.requests) == 1 and boot.calls == [False] and boot.invalidations == 0
    path, form, headers = http.requests[0]
    assert path == "/graphql/query"
    assert form["fb_api_req_friendly_name"] == POST_NAME and form["doc_id"] == "8845758582119845"
    assert http.variables(0) == {"shortcode": SHORTCODE, "fetch_tagged_user_count": None,
                                 "hoisted_comment_id": None, "hoisted_reply_id": None}
    lower = {k.lower(): v for k, v in headers.items()}
    assert lower["referer"] == f"https://www.instagram.com/p/{SHORTCODE}/"
    assert lower["x-fb-friendly-name"] == POST_NAME and lower["x-root-field-name"] == POST_ROOT
    assert SHORTCODE not in caplog.text and "cdninstagram" not in caplog.text


def test_post_query_doc_id_and_root_field_overrides(boot):
    env = {"MEDIA_TOOL_IG_ROOT_POLARISPOSTACTIONLOADPOSTQUERYQUERY": OTHER_ROOT}
    v1 = json.loads(fixture_text("post_v1_items.json"))["data"][POST_ROOT]
    http = FakeHttp(post_json(v1, root_field=OTHER_ROOT))
    root = gql(http, boot, overrides={POST_NAME: "1234567890"}, env=env).post_by_shortcode(SHORTCODE)
    assert root["items"][0]["code"] == "FAKEPOSTV1A1"
    _, form, headers = http.requests[0]
    assert form["doc_id"] == "1234567890" and headers["X-Root-Field-Name"] == OTHER_ROOT


@pytest.mark.parametrize("viewer", [None, VIEWER, "70000000999"])
def test_post_null_root_is_link_error_without_rebootstrap(boot, viewer, caplog):
    http = FakeHttp(post_json(None, viewer=viewer) if viewer else ok("post_null.json"))
    http.account = VIEWER
    err = post_error(http, boot)
    assert err.code == "content_unavailable" and err.retryable is False and err.stage == "graphql"
    assert err.diagnostic["detail"] == "post_null"
    assert err.message == "A publicação não existe mais ou não está disponível para a conta da sessão."
    assert len(http.requests) == 1 and boot.calls == [False] and boot.invalidations == 0
    assert SHORTCODE not in caplog.text and VIEWER not in caplog.text


@pytest.mark.parametrize("viewer", [None, VIEWER])
@pytest.mark.parametrize("message", ["This account is private", "Conta privada"])
def test_post_null_root_with_private_error_is_profile_private(boot, viewer, message):
    http = FakeHttp(post_json(None, errors=[{"message": message}], viewer=viewer))
    http.account = VIEWER
    err = post_error(http, boot)
    assert err.code == "profile_private" and err.diagnostic["detail"] == "post_null_private"
    assert "publicação" in err.message and len(http.requests) == 1


def test_post_null_root_with_confirmed_account_ignores_login_text_in_errors(boot):
    http = FakeHttp(post_json(None, errors=[{"message": "login required for FAKE field"}], viewer=VIEWER))
    http.account = VIEWER
    err = post_error(http, boot)
    assert err.code == "content_unavailable" and len(http.requests) == 1 and boot.invalidations == 0


def test_post_token_rejected_rebootstraps_once_then_succeeds(boot):
    http = FakeHttp(ok("error_token_1357004.json"), ok("post_image.json"))
    root = gql(http, boot).post_by_shortcode(SHORTCODE)
    assert root["shortcode"] == SHORTCODE
    assert len(http.requests) == 2 and boot.calls == [False, True] and boot.invalidations == 1
    assert http.requests[0][1]["fb_dtsg"] != http.requests[1][1]["fb_dtsg"]


def test_post_token_rejected_twice_is_not_a_link_error(boot):
    token_null = post_json(None, errors=[{"code": 1357004, "message": "Please reload"}])
    http = FakeHttp(token_null, token_null)
    err = post_error(http, boot)
    assert err.code == "contract_changed" and err.diagnostic["detail"] == "web_token_rejected_unexplained"
    assert len(http.requests) == 2 and boot.calls == [False, True] and boot.invalidations == 1


@pytest.mark.parametrize("response, detail", [
    (lambda: ok("image.json"), "missing_root_field"),                   # data sem o campo raiz do post
    (lambda: ok("no_data.json"), "missing_data"),
    (lambda: ok("error_doc_not_found.json"), "persisted_query_error"),
    (lambda: resp(200, "isto não é json"), "invalid_json"),
    (lambda: resp(200, "[1, 2]"), "not_object"),
    (lambda: post_json([{"fake": 1}]), "root_not_object"),
    (lambda: post_json("FAKE_TEXT"), "root_not_object"),
    (lambda: resp(400, "{}"), "http_400"),
])
def test_post_unexpected_format_is_contract_changed(boot, response, detail):
    http = FakeHttp(response())
    err = post_error(http, boot)
    assert err.code == "contract_changed" and err.diagnostic["detail"] == detail
    assert len(http.requests) == 1


def test_post_missing_root_with_confirmed_account_is_contract_not_link_error(boot):
    http = FakeHttp(ok("viewer_match.json"))   # data com xdt_viewer da conta, sem o campo raiz do post
    http.account = VIEWER
    err = post_error(http, boot)
    assert err.code == "contract_changed" and err.diagnostic["detail"] == "missing_root_field"


@pytest.mark.parametrize("response, code", [
    (lambda: resp(429, '{"message": "Please wait a few minutes", "status": "fail"}'), "rate_limited"),
    (lambda: resp(302, "", location="/challenge/FAKE/"), "checkpoint_required"),
    (lambda: resp(400, fixture_text("feedback_restriction.json")), "action_blocked"),
    (lambda: resp(403, "<html>Forbidden</html>", content_type="text/html"), "access_refused"),
    (lambda: resp(500, "{}"), "network_error"),
])
def test_post_keeps_existing_http_classification(boot, response, code):
    http = FakeHttp(response())
    err = post_error(http, boot)
    assert err.code == code and len(http.requests) == 1 and boot.invalidations == 0


def test_post_partial_errors_with_data_are_used_and_logged_safely(boot, caplog):
    payload = json.loads(fixture_text("post_image.json"))
    payload["errors"] = [{"message": f"field failed for {SHORTCODE} at https://fake.example.com/x",
                          "path": [POST_ROOT, "owner"]}]
    http = FakeHttp(resp(200, json.dumps(payload)))
    assert gql(http, boot).post_by_shortcode(SHORTCODE)["shortcode"] == SHORTCODE
    assert "errors parciais" in caplog.text
    assert SHORTCODE not in caplog.text and "fake.example.com" not in caplog.text


def test_profile_listing_null_root_is_still_contract_changed(boot):
    http = FakeHttp(resp(200, json.dumps({"data": {ROOT: None}})))
    with pytest.raises(InstagramWebError) as info:
        list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))
    assert info.value.code == "contract_changed" and info.value.diagnostic["detail"] == "root_not_object"
