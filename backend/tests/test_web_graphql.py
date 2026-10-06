"""web/graphql.py: POST /graphql/query, re-bootstrap, contrato quebrado e paginação (fakes próprios, offline)."""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.graphql import DEFAULT_PAGE_DELAY, InstagramWebGraphQL
from app.integrations.instagram.web.http import WebResponse
from app.integrations.instagram.web.queries import PROFILE_POSTS

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web" / "graphql"
ROOT = PROFILE_POSTS.root_field
CSRF = "FAKE_CSRF_TOKEN_0001"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def resp(status: int = 200, text: str = "", *, content_type: str = "application/json; charset=utf-8",
         location: str | None = None, sessionid_deleted: bool = False) -> WebResponse:
    return WebResponse(status=status, location_path=location, content_type=content_type, elapsed_ms=5,
                       set_cookie_names=[], sessionid_deleted=sessionid_deleted, text=text, path="/graphql/query")


def ok(name: str) -> WebResponse:
    return resp(200, fixture_text(name))


def page_json(nodes: list[dict], has_next: bool, cursor: str | None) -> WebResponse:
    payload = {"data": {ROOT: {"edges": [{"node": n} for n in nodes],
                               "page_info": {"end_cursor": cursor, "has_next_page": has_next}}}}
    return resp(200, json.dumps(payload))


@dataclass
class FakeState:
    fb_dtsg: str
    lsd: str
    jazoest: str
    viewer_id: str | None
    app_id: str | None
    created_at: float = 0.0


class FakeBootstrap:
    def __init__(self) -> None:
        self.calls: list[bool] = []
        self.invalidations = 0
        self.states: list[FakeState] = []

    def get(self, *, force: bool = False) -> FakeState:
        self.calls.append(force)
        n = len(self.states) + 1 if (force or not self.states) else len(self.states)
        if force or not self.states:
            self.states.append(FakeState(f"FAKE_DTSG_{n}_aaaaaaaa", f"FAKE_LSD_{n}_bbbbbbbb", f"FAKE_JAZOEST_{n}_c",
                                         "9000000001", "FAKE_APP_ID_1"))
        return self.states[-1]

    def invalidate(self) -> None:
        self.invalidations += 1

    def tokens(self) -> list[str]:
        return [v for s in self.states for v in (s.fb_dtsg, s.lsd, s.jazoest)]


class FakeHttp:
    """Devolve respostas da fila; um item pode ser função (form) -> WebResponse."""

    def __init__(self, *responses) -> None:
        self.queue = list(responses)
        self.requests: list[tuple[str, dict, dict]] = []

    def post_form(self, path, form, headers):
        self.requests.append((path, dict(form), dict(headers)))
        if not self.queue:
            raise AssertionError("requisição inesperada")
        item = self.queue.pop(0)
        return item(form) if callable(item) else item

    def csrftoken(self) -> str | None:
        return CSRF

    account = None   # ds_user_id FAKE (None = sem cookie)

    def account_id(self) -> str | None:
        return self.account

    def variables(self, i: int) -> dict:
        return json.loads(self.requests[i][1]["variables"])


@pytest.fixture
def boot():
    return FakeBootstrap()


@pytest.fixture(autouse=True)
def _no_token_in_logs(caplog, boot):
    caplog.set_level(logging.DEBUG)
    yield
    for token in boot.tokens() + [CSRF]:
        assert token not in caplog.text


def assert_no_tokens(exc: InstagramWebError, boot: FakeBootstrap) -> None:
    blob = " ".join([str(exc), repr(exc), exc.message, json.dumps(exc.diagnostic, default=str)])
    for token in boot.tokens() + [CSRF]:
        assert token not in blob


def gql(http, boot, **kw) -> InstagramWebGraphQL:
    kw.setdefault("page_delay", (0, 0))
    return InstagramWebGraphQL(http, boot, **kw)


# --------------------------------------------------------------------------- formulário e headers


def test_minimal_form_and_headers(boot, monkeypatch):
    monkeypatch.delenv("MEDIA_TOOL_IG_DOC_POLARISPROFILEPOSTSQUERY", raising=False)
    http = FakeHttp(ok("image.json"))
    nodes = list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))
    assert len(nodes) == 1
    path, form, headers = http.requests[0]
    assert path == "/graphql/query"
    assert set(form) == {"av", "__user", "__a", "__comet_req", "fb_dtsg", "jazoest", "lsd", "fb_api_caller_class",
                         "fb_api_req_friendly_name", "variables", "server_timestamps", "doc_id"}
    state = boot.states[0]
    assert form["av"] == "9000000001" and form["__user"] == "0" and form["__a"] == "1"
    assert form["__comet_req"] == "7" and form["server_timestamps"] == "true"
    assert (form["fb_dtsg"], form["lsd"], form["jazoest"]) == (state.fb_dtsg, state.lsd, state.jazoest)
    assert form["fb_api_caller_class"] == "RelayModern"
    assert form["fb_api_req_friendly_name"] == "PolarisProfilePostsQuery"
    assert form["doc_id"] == "28991540097136703"
    assert " " not in form["variables"]  # JSON compacto
    variables = json.loads(form["variables"])
    assert variables["username"] == "perfil_fake" and variables["data"]["count"] == 12
    assert "after" not in variables
    lower = {k.lower(): v for k, v in headers.items()}
    assert lower["content-type"] == "application/x-www-form-urlencoded"
    assert lower["x-csrftoken"] == CSRF
    assert lower["x-ig-app-id"] == "FAKE_APP_ID_1"
    assert lower["x-fb-lsd"] == state.lsd
    assert lower["x-fb-friendly-name"] == "PolarisProfilePostsQuery"
    assert lower["x-root-field-name"] == ROOT
    assert lower["origin"] == "https://www.instagram.com"
    assert lower["referer"] == "https://www.instagram.com/perfil_fake/"
    assert (lower["sec-fetch-dest"], lower["sec-fetch-mode"], lower["sec-fetch-site"]) == ("empty", "cors",
                                                                                           "same-origin")
    assert len(lower) == len(headers)  # sem chaves duplicadas por grafia


def test_doc_id_override_from_constructor(boot, monkeypatch):
    monkeypatch.delenv("MEDIA_TOOL_IG_DOC_POLARISPROFILEPOSTSQUERY", raising=False)
    http = FakeHttp(ok("empty.json"))
    list(gql(http, boot, overrides={"PolarisProfilePostsQuery": "1234567890"}).iter_profile_posts(
        "perfil_fake", max_media=None))
    assert http.requests[0][1]["doc_id"] == "1234567890"


def test_max_media_reduces_first_page_count(boot):
    http = FakeHttp(ok("page1_has_next.json"), ok("page2_last.json"))
    list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=1, page_size=12))
    assert http.variables(0)["data"]["count"] == 1
    assert http.variables(1)["data"]["count"] == 12


# --------------------------------------------------------------------------- query / contrato


def test_query_returns_root_field(boot):
    http = FakeHttp(ok("image.json"))
    root = gql(http, boot).query(PROFILE_POSTS, {"username": "x"}, referer_path="/x/")
    assert set(root) == {"edges", "page_info"}


def test_empty_listing(boot):
    http = FakeHttp(ok("empty.json"))
    assert list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None)) == []
    assert len(http.requests) == 1


@pytest.mark.parametrize("name,detail", [
    ("no_data.json", "missing_data"),
    ("no_root_field.json", "missing_root_field"),
    ("error_doc_not_found.json", "persisted_query_error"),
])
def test_contract_changed_cases(boot, name, detail):
    http = FakeHttp(ok(name))
    with pytest.raises(InstagramWebError) as info:
        list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))
    exc = info.value
    assert exc.code == "contract_changed" and exc.stage == "graphql"
    assert exc.diagnostic["detail"] == detail and exc.diagnostic["status"] == 200
    assert "top_keys" in exc.diagnostic
    assert len(http.requests) == 1 and boot.invalidations == 0
    assert_no_tokens(exc, boot)


def test_missing_root_lists_data_keys(boot):
    with pytest.raises(InstagramWebError) as info:
        gql(FakeHttp(ok("no_root_field.json")), boot).query(PROFILE_POSTS, {})
    assert info.value.diagnostic["data_keys"] == ["xdt_some_other_root"]


def test_doc_not_found_diagnostic_has_code_and_no_quoted_values(boot):
    with pytest.raises(InstagramWebError) as info:
        gql(FakeHttp(ok("error_doc_not_found.json")), boot).query(PROFILE_POSTS, {})
    assert info.value.diagnostic["error_codes"] == [1675002]
    [message] = info.value.diagnostic["error_messages"]
    assert "PersistedQueryNotFound" in message and '"FAKE"' not in message


def test_invalid_json_is_contract_changed(boot):
    http = FakeHttp(resp(200, "<html>nope</html>", content_type="text/html"))
    with pytest.raises(InstagramWebError) as info:
        gql(http, boot).query(PROFILE_POSTS, {})
    assert info.value.code == "contract_changed"
    assert len(http.requests) == 1


def test_invalid_json_with_json_content_type(boot):
    with pytest.raises(InstagramWebError) as info:
        gql(FakeHttp(resp(200, "{not json")), boot).query(PROFILE_POSTS, {})
    assert info.value.code == "contract_changed"


@pytest.mark.parametrize("conn,detail", [
    ({"edges": {}, "page_info": {}}, "edges_not_list"),
    ({"edges": []}, "page_info_missing"),
])
def test_connection_shape_broken(boot, conn, detail):
    http = FakeHttp(resp(200, json.dumps({"data": {ROOT: conn}})))
    with pytest.raises(InstagramWebError) as info:
        list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))
    assert info.value.code == "contract_changed" and info.value.diagnostic["detail"] == detail


# --------------------------------------------------------------------------- re-bootstrap


def test_rebootstrap_once_on_token_error_then_succeeds(boot):
    stale = resp(200, "for (;;);" + fixture_text("error_token_1357004.json"))
    http = FakeHttp(stale, ok("image.json"))
    nodes = list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))
    assert len(nodes) == 1
    assert boot.calls == [False, True] and boot.invalidations == 1
    assert http.requests[0][1]["fb_dtsg"] == boot.states[0].fb_dtsg
    assert http.requests[1][1]["fb_dtsg"] == boot.states[1].fb_dtsg


def test_rebootstrap_only_once_then_fails(boot):
    stale = resp(200, fixture_text("error_token_1357004.json"))
    http = FakeHttp(stale, stale)
    with pytest.raises(InstagramWebError) as info:
        gql(http, boot).query(PROFILE_POSTS, {})
    exc = info.value
    # 1357004 persistente sem evidência explícita de login -> contract_changed (BATCH_ROBUSTNESS seção 3).
    assert exc.code == "contract_changed" and exc.diagnostic["detail"] == "web_token_rejected_unexplained"
    assert len(http.requests) == 2 and boot.invalidations == 1
    assert_no_tokens(exc, boot)


def test_403_with_token_body_rebootstraps(boot):
    body = json.dumps({"message": "CSRF token missing or incorrect", "status": "fail"})
    http = FakeHttp(resp(403, body), ok("empty.json"))
    gql(http, boot).query(PROFILE_POSTS, {})
    assert boot.invalidations == 1 and len(http.requests) == 2


@pytest.mark.parametrize("response", [
    resp(403, '<html><script>var csrf_token="FAKE_CSRF_X"; var lsd={"token":"FAKE_T"};</script></html>',
         content_type="text/html; charset=utf-8"),
    resp(403, "CSRF token missing or incorrect", content_type="text/plain"),
])
def test_403_non_json_body_is_access_refused_without_rebootstrap(boot, response):
    # Regressão: JS/texto com csrf/fb_dtsg/lsd/token num 403 não é "token recusado" (só JSON estruturado conta).
    http = FakeHttp(response)
    with pytest.raises(InstagramWebError) as info:
        gql(http, boot).query(PROFILE_POSTS, {})
    assert info.value.code == "access_refused" and info.value.diagnostic.get("detail") != "web_token_rejected"
    assert boot.invalidations == 0 and len(http.requests) == 1


@pytest.mark.parametrize("message", ["This query requires login", "Please log in", "csrf check"])
def test_partial_errors_with_useful_data_do_not_invalidate_session(boot, message):
    # Regressão: 200 com edges + xdt_viewer e errors parcial citando login -> usa os dados, sem re-bootstrap.
    payload = json.loads(fixture_text("viewer_match.json"))
    payload["errors"] = [{"message": message, "severity": "WARNING"}]
    http = FakeHttp(resp(200, json.dumps(payload)))
    client = gql(http, boot)
    root = client.query(PROFILE_POSTS, {})
    assert isinstance(root["edges"], list) and root["edges"]
    assert client.last_viewer_id == "70000000001"
    assert boot.invalidations == 0 and len(http.requests) == 1


def test_login_message_without_data_still_rebootstraps(boot):
    body = json.dumps({"errors": [{"message": "This query requires login"}]})
    http = FakeHttp(resp(200, body), ok("empty.json"))
    gql(http, boot).query(PROFILE_POSTS, {})
    assert boot.invalidations == 1 and len(http.requests) == 2


def test_errors_message_echoing_token_is_scrubbed(boot):
    def echo(form):
        body = {"errors": [{"message": f"invalid fb_dtsg {form['fb_dtsg']} lsd={form['lsd']}", "code": 1357004}]}
        return resp(200, json.dumps(body))

    http = FakeHttp(echo, echo)
    with pytest.raises(InstagramWebError) as info:
        gql(http, boot).query(PROFILE_POSTS, {})
    assert info.value.code == "contract_changed"
    assert info.value.diagnostic["error_codes"] == [1357004]
    assert_no_tokens(info.value, boot)


# --------------------------------------------------------------------------- classificação HTTP


@pytest.mark.parametrize("response,code", [
    (resp(302, "", content_type="text/html", location="/"), "access_refused"),
    (resp(302, "", content_type="text/html", location="/challenge/"), "checkpoint_required"),
    (resp(400, json.dumps({"message": "bad request", "status": "fail"})), "contract_changed"),
    (resp(403, "", content_type="text/html"), "access_refused"),
    (resp(429, "", content_type="text/plain"), "rate_limited"),
    (resp(400, fixture_text("feedback_restriction.json")), "action_blocked"),
    (resp(500, "", content_type="text/html"), "network_error"),
])
def test_http_classification_no_rebootstrap(boot, response, code):
    http = FakeHttp(response)
    with pytest.raises(InstagramWebError) as info:
        list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))
    exc = info.value
    assert exc.code == code and exc.stage == "graphql"
    assert exc.retryable is (code in ("rate_limited", "network_error", "request_auth_failed"))
    assert len(http.requests) == 1 and boot.invalidations == 0
    assert_no_tokens(exc, boot)


@pytest.mark.parametrize("make", [
    lambda: resp(401, json.dumps({"message": "Please wait a few minutes before you try again.", "require_login": True,
                                  "status": "fail"})),
    lambda: resp(401, "", content_type="text/html"),
    lambda: resp(400, json.dumps({"require_login": True, "status": "fail"})),
    lambda: resp(302, "", content_type="text/html", location="/accounts/login/"),
])
def test_auth_refusal_without_token_marker_renews_once_then_request_auth_failed(boot, make):
    """Seção 7: 401/require_login sem marcador de token passam pela única renovação antes de request_auth_failed."""
    http = FakeHttp(make(), make())
    with pytest.raises(InstagramWebError) as info:
        list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))
    exc = info.value
    assert exc.code == "request_auth_failed" and exc.retryable is True and exc.stage == "graphql"
    assert len(http.requests) == 2 and boot.invalidations == 1 and boot.calls == [False, True]
    assert_no_tokens(exc, boot)


def test_auth_refusal_then_success_after_single_renewal(boot):
    http = FakeHttp(resp(401, "", content_type="text/html"), ok("page2_last.json"))
    nodes = list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))
    assert nodes and len(http.requests) == 2 and boot.invalidations == 1


# --------------------------------------------------------------------------- paginação


def test_pagination_lazy_with_dedupe(boot):
    http = FakeHttp(ok("page1_has_next.json"), ok("page2_last.json"))
    it = gql(http, boot).iter_profile_posts("perfil_fake", max_media=None)
    first = next(it)
    second = next(it)
    assert [first["code"], second["code"]] == ["FAKEIMG0001", "FAKEREEL001"]
    assert len(http.requests) == 1  # a página 2 só é buscada quando o consumidor pede
    rest = [n["code"] for n in it]
    assert rest == ["FAKECAR0001", "FAKEIMG0002"]  # FAKEREEL001 repetido foi pulado
    assert len(http.requests) == 2
    variables = http.variables(1)
    assert variables["after"] == "FAKE_CURSOR_PAGE_1" and variables["first"] == 12
    assert variables["before"] is None and variables["last"] is None


def test_consumer_stop_fetches_no_more_pages(boot):
    http = FakeHttp(ok("page1_has_next.json"), ok("page2_last.json"))
    it = gql(http, boot).iter_profile_posts("perfil_fake", max_media=None)
    next(it), next(it)
    it.close()
    assert len(http.requests) == 1


def test_repeated_cursor_is_contract_changed(boot):
    http = FakeHttp(ok("page1_has_next.json"), ok("page_repeated_cursor.json"))
    with pytest.raises(InstagramWebError) as info:
        list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))
    assert info.value.code == "contract_changed"
    assert info.value.diagnostic["detail"] == "repeated_cursor"
    assert "FAKE_CURSOR" not in json.dumps(info.value.diagnostic)


def test_empty_page_with_has_next_stops_with_warning(boot, caplog):
    http = FakeHttp(ok("page1_has_next.json"), ok("page_empty_has_next.json"))
    nodes = list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))
    assert len(nodes) == 2 and len(http.requests) == 2
    assert "empty_page" in caplog.text


def test_has_next_without_cursor_stops(boot):
    http = FakeHttp(page_json([{"pk": "1", "code": "A"}], True, None))
    assert len(list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))) == 1
    assert len(http.requests) == 1


def test_max_pages_defensive(boot, caplog):
    pages = [page_json([{"pk": str(i), "code": f"C{i}"}], True, f"FAKE_CURSOR_{i}") for i in range(10)]
    http = FakeHttp(*pages)
    nodes = list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None, max_pages=3))
    assert len(nodes) == 3 and len(http.requests) == 3
    assert "max_pages" in caplog.text


def test_cancel_before_first_request(boot):
    cancel = threading.Event()
    cancel.set()
    http = FakeHttp(ok("image.json"))
    assert list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None, cancel=cancel)) == []
    assert http.requests == [] and boot.calls == []


def test_cancel_checked_before_next_page(boot):
    cancel = threading.Event()
    http = FakeHttp(ok("page1_has_next.json"), ok("page2_last.json"))
    it = gql(http, boot).iter_profile_posts("perfil_fake", max_media=None, cancel=cancel)
    nodes = [next(it), next(it)]
    cancel.set()
    nodes.extend(it)
    assert len(nodes) == 2 and len(http.requests) == 1


def test_form_body_has_no_token_outside_token_fields(boot):
    http = FakeHttp(ok("image.json"))
    list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))
    form = http.requests[0][1]
    encoded = urllib.parse.urlencode({k: v for k, v in form.items() if k not in ("fb_dtsg", "lsd", "jazoest")})
    for token in boot.tokens():
        assert token not in encoded


# --------------------------------------------------------------------------- rodada de correção


def test_default_page_delay_is_2_to_4_seconds():
    assert DEFAULT_PAGE_DELAY == (2.0, 4.0)


def test_page_delay_only_between_pages(boot):
    slept: list[float] = []
    http = FakeHttp(ok("page1_has_next.json"), ok("page2_last.json"))
    it = InstagramWebGraphQL(http, boot, page_delay=(2.0, 4.0), sleep=slept.append).iter_profile_posts(
        "perfil_fake", max_media=None)
    next(it), next(it)
    assert slept == []  # nenhuma espera antes da 1a página
    list(it)
    assert len(slept) == 1 and 2.0 <= slept[0] <= 4.0
    assert len(http.requests) == 2


def test_page_delay_uses_cancel_wait_and_stops(boot):
    class Cancel(threading.Event):
        def wait(self, timeout=None):
            self.waited = timeout
            self.set()  # cancelado durante a espera
            return True

    cancel = Cancel()
    http = FakeHttp(ok("page1_has_next.json"), ok("page2_last.json"))
    nodes = list(InstagramWebGraphQL(http, boot, page_delay=(2.0, 2.0)).iter_profile_posts(
        "perfil_fake", max_media=None, cancel=cancel))
    assert cancel.waited == 2.0
    assert len(nodes) == 2 and len(http.requests) == 1


def test_rebootstrap_is_once_per_operation_not_per_page(boot):
    stale = resp(200, fixture_text("error_token_1357004.json"))
    http = FakeHttp(stale, ok("page1_has_next.json"), stale)
    it = gql(http, boot).iter_profile_posts("perfil_fake", max_media=None)
    assert len([next(it), next(it)]) == 2
    with pytest.raises(InstagramWebError) as info:
        list(it)
    assert info.value.code == "contract_changed" and info.value.diagnostic["detail"] == "web_token_rejected_unexplained"
    assert boot.invalidations == 1 and len(http.requests) == 3
    assert_no_tokens(info.value, boot)


def test_stops_after_two_pages_without_new_nodes(boot, caplog):
    a, b = {"pk": "1", "code": "A"}, {"pk": "2", "code": "B"}
    http = FakeHttp(page_json([a, b], True, "FAKE_CURSOR_1"), page_json([a], True, "FAKE_CURSOR_2"),
                    page_json([b], True, "FAKE_CURSOR_3"), page_json([{"pk": "3", "code": "C"}], False, None))
    nodes = list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None))
    assert [n["code"] for n in nodes] == ["A", "B"]
    assert len(http.requests) == 3
    assert "no_new_items" in caplog.text and "FAKE_CURSOR" not in caplog.text


def test_one_page_without_new_nodes_continues(boot):
    a = {"pk": "1", "code": "A"}
    http = FakeHttp(page_json([a], True, "FAKE_CURSOR_1"), page_json([a], True, "FAKE_CURSOR_2"),
                    page_json([{"pk": "3", "code": "C"}], False, None))
    assert [n["code"] for n in gql(http, boot).iter_profile_posts("perfil_fake", max_media=None)] == ["A", "C"]


def test_partial_errors_with_useful_data_are_not_contract_changed(boot):
    payload = json.loads(fixture_text("image.json"))
    payload["errors"] = [{"message": "document field deprecated; persisted query warning", "code": 1}]
    root = gql(FakeHttp(resp(200, json.dumps(payload))), boot).query(PROFILE_POSTS, {})
    assert len(root["edges"]) == 1


def test_env_mapping_passed_to_resolve(boot):
    http = FakeHttp(ok("empty.json"))
    list(gql(http, boot, env={"MEDIA_TOOL_IG_DOC_POLARISPROFILEPOSTSQUERY": "77777"}).iter_profile_posts(
        "perfil_fake", max_media=None))
    assert http.requests[0][1]["doc_id"] == "77777"


# --------------------------------------------------------------------------- av e xdt_viewer (viewer do HTML não prova sessão)


class ZeroViewerBootstrap(FakeBootstrap):
    def get(self, *, force: bool = False) -> FakeState:
        state = super().get(force=force)
        state.viewer_id = None   # HTML inicial com "USER_ID":"0"
        return state


@pytest.mark.parametrize("account, expected", [("70000000001", "70000000001"), (None, "0")])
def test_av_falls_back_to_session_account_id(account, expected):
    http = FakeHttp(ok("viewer_match.json"))
    http.account = account
    gql(http, ZeroViewerBootstrap()).query(PROFILE_POSTS, {"data": {"count": 1}, "username": "instagram"})
    assert http.requests[0][1]["av"] == expected


def test_av_prefers_real_html_viewer(boot):
    http = FakeHttp(ok("viewer_match.json"))
    http.account = "70000000001"
    gql(http, boot).query(PROFILE_POSTS, {"data": {"count": 1}, "username": "instagram"})
    assert http.requests[0][1]["av"] == "9000000001"


@pytest.mark.parametrize("name, expected", [("viewer_match.json", "70000000001"),
                                            ("viewer_other.json", "70000000999"),
                                            ("viewer_absent.json", None)])
def test_last_viewer_id_from_response(boot, caplog, name, expected):
    client = gql(FakeHttp(ok(name)), boot)
    root = client.query(PROFILE_POSTS, {"data": {"count": 1}, "username": "instagram"})
    assert isinstance(root["edges"], list) and client.last_viewer_id == expected
    assert "70000000001" not in caplog.text and "70000000999" not in caplog.text


@pytest.mark.parametrize("viewer", [None, {"user": None}, {"user": {}}, {"user": {"id": None}}, {"user": {"id": "0"}},
                                    "texto", {"user": {"id": True}}])
def test_last_viewer_id_tolerates_missing_or_null(boot, viewer):
    payload = {"data": {ROOT: {"edges": [], "page_info": {"has_next_page": False, "end_cursor": None}},
                        "xdt_viewer": viewer}}
    client = gql(FakeHttp(resp(200, json.dumps(payload))), boot)
    client.query(PROFILE_POSTS, {})
    assert client.last_viewer_id is None


@pytest.mark.parametrize("data_root, message, code", [
    (None, "This query requires login", "content_unavailable"),
    ("absent", "Please log in (csrf/lsd)", "content_unavailable"),
    (None, "This account is private", "profile_private"),
])
def test_authenticated_viewer_with_null_root_is_content_error(boot, data_root, message, code):
    # Sonda: raiz null/ausente + errors citando login, mas xdt_viewer.user.id == ds_user_id -> sem re-bootstrap.
    data = {"xdt_viewer": {"user": {"id": "70000000001"}}}
    if data_root != "absent":
        data[ROOT] = data_root
    http = FakeHttp(resp(200, json.dumps({"data": data, "errors": [{"message": message}]})))
    http.account = "70000000001"
    client = gql(http, boot)
    with pytest.raises(InstagramWebError) as info:
        client.query(PROFILE_POSTS, {})
    assert info.value.code == code and info.value.code != "session_invalid"
    assert boot.invalidations == 0 and len(http.requests) == 1 and client.last_viewer_id == "70000000001"
    assert_no_tokens(info.value, boot)


def test_null_root_with_other_viewer_keeps_token_rule(boot):
    data = {ROOT: None, "xdt_viewer": {"user": {"id": "70000000999"}}}
    body = json.dumps({"data": data, "errors": [{"message": "This query requires login"}]})
    http = FakeHttp(resp(200, body), ok("empty.json"))
    http.account = "70000000001"
    gql(http, boot).query(PROFILE_POSTS, {})
    assert boot.invalidations == 1 and len(http.requests) == 2


# --------------------------------------------------------------------------- account_confirmation e on_page


def viewer_page(nodes: list[dict], has_next: bool, cursor: str | None, viewer: object = "absent",
                status: int = 200) -> WebResponse:
    data: dict = {ROOT: {"edges": [{"node": n} for n in nodes],
                         "page_info": {"end_cursor": cursor, "has_next_page": has_next}}}
    if viewer != "absent":
        data["xdt_viewer"] = viewer
    return resp(status, json.dumps({"data": data}))


@pytest.mark.parametrize("viewer, account, expected", [
    ("70000000001", "70000000001", "confirmed"),
    (" 70000000001 ", 70000000001, "confirmed"),
    ("70000000999", "70000000001", "different_account"),
    (None, "70000000001", "not_confirmed"),
    ("0", "70000000001", "not_confirmed"),
    ("", "70000000001", "not_confirmed"),
    ("70000000001", None, "not_confirmed"),
    (True, "70000000001", "not_confirmed"),
])
def test_account_confirmation_states(viewer, account, expected):
    from app.integrations.instagram.web.graphql import account_confirmation

    assert account_confirmation(viewer, account) == expected


@pytest.mark.parametrize("viewer, expected", [({"user": {"id": "70000000001"}}, "confirmed"),
                                              ({"user": {"id": "70000000999"}}, "different_account"),
                                              ({"user": None}, "not_confirmed"),
                                              ({"user": {"id": "0"}}, "not_confirmed"),
                                              (None, "not_confirmed"),
                                              ("absent", "not_confirmed")])
def test_listing_account_confirmation_from_first_page(boot, caplog, viewer, expected):
    http = FakeHttp(viewer_page([{"pk": "1", "code": "A"}], False, None, viewer))
    http.account = "70000000001"
    client = gql(http, boot)
    assert client.listing_account_confirmation is None
    assert [n["code"] for n in client.iter_profile_posts("perfil_fake", max_media=None)] == ["A"]
    assert client.listing_account_confirmation == expected
    assert "70000000001" not in caplog.text and "70000000999" not in caplog.text


def test_listing_account_confirmation_keeps_first_page_state(boot):
    http = FakeHttp(viewer_page([{"pk": "1"}], True, "FAKE_CURSOR_A", {"user": {"id": "70000000001"}}),
                    viewer_page([{"pk": "2"}], False, None, "absent"))
    http.account = "70000000001"
    client = gql(http, boot)
    pages: list[dict] = []
    assert len(list(client.iter_profile_posts("perfil_fake", max_media=None, on_page=pages.append))) == 2
    assert client.listing_account_confirmation == "confirmed"
    assert [p["account_confirmation"] for p in pages] == ["confirmed", "not_confirmed"]


def test_on_page_metadata_two_pages(boot, caplog):
    http = FakeHttp(viewer_page([{"pk": "1"}, {"pk": "2"}], True, "FAKE_CURSOR_PAGE_A", {"user": {"id": "7001"}}),
                    viewer_page([{"pk": "2"}, {"pk": "3"}], False, "FAKE_CURSOR_PAGE_B", {"user": {"id": "7002"}}))
    http.account = "7001"
    pages: list[dict] = []
    it = gql(http, boot).iter_profile_posts("perfil_fake", max_media=1, page_size=1, on_page=pages.append)
    assert next(it)["pk"] == "1"
    assert len(pages) == 1   # chamado antes de entregar os nós da página
    assert [n["pk"] for n in it] == ["2", "3"]
    first, second = pages
    assert first == {"page_index": 1, "http_status": 200, "edges_count": 2, "has_next_page": True,
                     "end_cursor_present": True, "cursor_changed": True,
                     "variables_keys": list(http.variables(0)), "new_nodes": 2, "duplicate_nodes": 0,
                     "account_confirmation": "confirmed"}
    assert "after" not in first["variables_keys"] and "username" in first["variables_keys"]
    assert second["page_index"] == 2 and second["has_next_page"] is False and second["cursor_changed"] is True
    assert second["new_nodes"] == 1 and second["duplicate_nodes"] == 1
    assert second["account_confirmation"] == "different_account"
    assert {"after", "before", "first", "last", "data", "username"} <= set(second["variables_keys"])
    assert second["variables_keys"] == list(http.variables(1))
    blob = json.dumps(pages) + caplog.text
    assert "FAKE_CURSOR_PAGE" not in blob and "7001" not in blob and "7002" not in blob


def test_on_page_cursor_absent(boot):
    http = FakeHttp(viewer_page([{"pk": "1"}], True, None))
    pages: list[dict] = []
    list(gql(http, boot).iter_profile_posts("perfil_fake", max_media=None, on_page=pages.append))
    assert pages[0]["end_cursor_present"] is False and pages[0]["cursor_changed"] is False


def test_on_page_called_even_if_consumer_stops_on_page(boot):
    http = FakeHttp(viewer_page([{"pk": "1"}, {"pk": "2"}], True, "FAKE_CURSOR_A"))
    pages: list[dict] = []
    it = gql(http, boot).iter_profile_posts("perfil_fake", max_media=None, on_page=pages.append)
    next(it)
    it.close()
    assert len(pages) == 1 and len(http.requests) == 1


# --------------------------------------------------------------------------- classificação após a renovação (seção 3)


def _token_json(status: int, payload: dict) -> WebResponse:
    return resp(status, json.dumps(payload))


@pytest.mark.parametrize("second", [
    _token_json(200, {"errors": [{"message": "login_required", "code": 1357001}]}),
    _token_json(200, {"require_login": True, "errors": [{"code": 1357004}]}),
    _token_json(401, {"errors": [{"code": 1357001}]}),
    _token_json(200, {"errors": [{"message": "checkpoint_required", "code": 1357004}]}),
])
def test_after_renewal_explicit_evidence_is_request_auth_failed(boot, second):
    # Seção 7: recusa de autenticação após a única renovação é request_auth_failed (pausa), não sessão inválida.
    http = FakeHttp(ok("error_token_1357004.json"), second)
    with pytest.raises(InstagramWebError) as info:
        gql(http, boot).query(PROFILE_POSTS, {})
    assert info.value.code == "request_auth_failed" and info.value.diagnostic["detail"] == "web_token_rejected"
    assert info.value.retryable is True
    assert boot.invalidations == 1 and len(http.requests) == 2


def test_after_renewal_403_without_evidence_is_access_refused(boot):
    body = {"message": "CSRF token missing or incorrect", "status": "fail"}
    http = FakeHttp(_token_json(403, body), _token_json(403, body))
    with pytest.raises(InstagramWebError) as info:
        gql(http, boot).query(PROFILE_POSTS, {})
    assert info.value.code == "access_refused" and info.value.diagnostic["detail"] == "web_token_rejected_403"
    assert boot.invalidations == 1 and len(http.requests) == 2
    assert_no_tokens(info.value, boot)


@pytest.mark.parametrize("second", [
    _token_json(200, {"errors": [{"message": "Please reload the page", "code": 1357004}]}),
    _token_json(200, {"error": 1357004, "errorSummary": "Sorry, something went wrong"}),
    _token_json(200, {"errors": [{"message": "fb_dtsg mismatch"}]}),
])
def test_after_renewal_other_refusals_are_contract_changed(boot, second):
    http = FakeHttp(ok("error_token_1357004.json"), second)
    with pytest.raises(InstagramWebError) as info:
        gql(http, boot).query(PROFILE_POSTS, {})
    assert info.value.code == "contract_changed"
    assert info.value.diagnostic["detail"] == "web_token_rejected_unexplained"
    assert boot.invalidations == 1 and len(http.requests) == 2


# --------------------------------------------------------------------------- log da última página e página única


def test_last_page_is_logged_when_consumer_stops_at_limit(boot, caplog):
    a, b = {"pk": "1", "code": "A"}, {"pk": "2", "code": "B"}
    http = FakeHttp(page_json([a, b], True, "FAKE_CURSOR_1"))
    it = gql(http, boot).iter_profile_posts("perfil_fake", max_media=1)
    assert next(it)["code"] == "A"
    it.close()
    assert "GraphQL página 1: 2 nós (2 novos)" in caplog.text and len(http.requests) == 1
    assert "FAKE_CURSOR" not in caplog.text


def test_resume_starts_at_cursor_and_page_index(boot):
    http = FakeHttp(page_json([{"pk": "9", "code": "Z"}], False, None))
    client = gql(http, boot)
    nodes = list(client.iter_profile_posts("perfil_fake", max_media=5, start_cursor="FAKE_CURSOR_7", start_page=8))
    assert [n["code"] for n in nodes] == ["Z"]
    variables = json.loads(http.requests[0][1]["variables"])
    assert variables["after"] == "FAKE_CURSOR_7" and variables["data"]["count"] == 12   # count cheio na retomada
    assert client.current_page.index == 8 and client.current_page.after == "FAKE_CURSOR_7"
    assert "FAKE_CURSOR" not in repr(client.current_page)


def test_profile_page_is_one_post_with_given_cursor_and_count(boot):
    http = FakeHttp(page_json([{"pk": "1", "code": "A"}], True, "FAKE_CURSOR_2"))
    nodes = gql(http, boot).profile_page("perfil_fake", after="FAKE_CURSOR_1", count=7)
    assert [n["code"] for n in nodes] == ["A"] and len(http.requests) == 1
    variables = json.loads(http.requests[0][1]["variables"])
    assert variables["after"] == "FAKE_CURSOR_1" and variables["data"]["count"] == 7 and variables["first"] == 7
    http = FakeHttp(page_json([], False, None))
    assert gql(http, boot).profile_page("perfil_fake", after=None, count=12) == []
    assert "after" not in json.loads(http.requests[0][1]["variables"])
