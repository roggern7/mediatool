"""POC de post isolado (tools/poc_instagram_post.py): offline, httpx.MockTransport, valores FAKE, sem banco.

Nenhuma requisição real: a POC só roda contra o Instagram com autorização do dono.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.parse
from pathlib import Path

import httpx
import pytest

from app.integrations.instagram.session import InstagramSession, InstagramSessionProvider, SessionStatus
from app.integrations.instagram.web.queries import POST_BY_SHORTCODE
from conftest import make_settings

_SPEC = importlib.util.spec_from_file_location(
    "poc_instagram_post", Path(__file__).resolve().parents[1] / "tools" / "poc_instagram_post.py"
)
poc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(poc)

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web"
ROOT = POST_BY_SHORTCODE.root_field
OTHER_ROOT = "xdt_api__v1__media__shortcode__web_info"
SID = "FAKE-SESSIONID-POSTPOC-0001"
DS_USER = "10000000321"
CODE = "FAKEPOSTIMG1"
LINK = f"https://www.instagram.com/p/{CODE}/?igsh=FAKEQUERY123"
GQL = ("POST", "/graphql/query")
SECRETS = (SID, DS_USER, CODE, "FAKEPOSTREEL", "FAKEPOSTCAR1", "FAKEPOSTV1A1", "FAKEQUERY123", "FAKE_OH",
           "scontent-fake", "video-fake", "FAKE_DTSG", "FAKE_LSD", "dono_fake", "Dono_Fake", "320000000000000")


class Provider(InstagramSessionProvider):
    def __init__(self, configured: bool = True) -> None:
        self.configured = configured

    def status(self) -> SessionStatus:
        return SessionStatus(configured=self.configured, source="file" if self.configured else None)

    def get(self):
        return InstagramSession({"sessionid": SID, "ds_user_id": DS_USER}) if self.configured else None

    def save(self, sessionid, extra=None) -> None: ...

    def clear(self) -> None: ...


def fixture(name: str) -> httpx.Response:
    text = (FIXTURES / "graphql" / name).read_text(encoding="utf-8")
    return httpx.Response(200, text=text, headers={"content-type": "application/json"})


def fixture_root(name: str):
    return json.loads((FIXTURES / "graphql" / name).read_text(encoding="utf-8"))["data"][ROOT]


class Server:
    def __init__(self, *graphql: httpx.Response | BaseException) -> None:
        self.graphql = list(graphql)
        self.requests: list[tuple[str, str, str]] = []
        self.forms: list[dict] = []
        self.referers: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.host, request.url.path))
        assert request.url.host == "www.instagram.com"
        if request.method == "GET" and request.url.path == "/":
            html = (FIXTURES / "bootstrap_viewer_zero.html").read_text(encoding="utf-8")
            return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})
        if request.method == "POST" and request.url.path == "/graphql/query":
            self.forms.append(dict(urllib.parse.parse_qsl(request.content.decode())))
            self.referers.append(request.headers.get("referer", ""))
            if not self.graphql:
                raise AssertionError("requisição GraphQL inesperada")
            item = self.graphql.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        raise AssertionError("requisição inesperada")


def run_tool(server: Server, url: str = LINK, *, running: bool = False, provider=None, tmp_path: Path, env=None):
    settings = make_settings(tmp_path)
    if env:
        settings.env.update(env)
    code, report = poc.run(
        poc.parse_args(["--url", url]), settings=settings, session_provider=provider or Provider(),
        transport=httpx.MockTransport(server.handle), is_app_running=lambda s: running,
    )
    text = json.dumps(report, ensure_ascii=True)
    for secret in SECRETS:
        assert secret not in text, secret
    return code, report


ONE_GET_ONE_POST = [{"method": "GET", "path": "/", "status": 200},
                    {"method": "POST", "path": "/graphql/query", "status": 200}]


def test_image_post_report_and_exactly_two_requests(tmp_path):
    server = Server(fixture("post_image.json"))
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 0 and report["outcome"] == "ok" and "error" not in report
    assert report["requests"] == ONE_GET_ONE_POST
    assert report["instagram_requests"] == 2 and report["cdn_requests"] == 0 and report["other_requests"] == 0
    assert report["query"] == {"name": POST_BY_SHORTCODE.name, "doc_id": POST_BY_SHORTCODE.doc_id,
                               "root_field": ROOT, "observed_at": "unverified"}
    assert report["link_kind"] == "p" and report["media_count"] == 1
    assert report["items"] == [{
        "media_type": "image", "is_reel": False, "source_media_index": 0, "taken_at": "2023-11-14T22:13:20Z",
        "cdn_url_valid": True, "cdn_domain_suffix": ".cdninstagram.com", "shortcode_matches": True,
        "owner_present": True}]
    shape = report["response_shape"]
    assert shape["format"] == "legacy" and shape["typename"] == "XDTGraphImage"
    assert {"shortcode", "display_url", "display_resources", "owner"} <= set(shape["root_keys"])
    # a consulta enviada: nome, doc_id, variáveis e Referer do post
    form = server.forms[0]
    assert form["fb_api_req_friendly_name"] == POST_BY_SHORTCODE.name and form["doc_id"] == POST_BY_SHORTCODE.doc_id
    assert json.loads(form["variables"]) == {"shortcode": CODE, "fetch_tagged_user_count": None,
                                             "hoisted_comment_id": None, "hoisted_reply_id": None}
    assert server.referers == [f"https://www.instagram.com/p/{CODE}/"]


def test_reel_link_and_video_on_fbcdn(tmp_path):
    server = Server(fixture("post_video_reel.json"))
    code, report = run_tool(server, "instagram.com/reels/FAKEPOSTREEL/", tmp_path=tmp_path)
    assert code == 0 and report["link_kind"] == "reel"
    [item] = report["items"]
    assert item["media_type"] == "video" and item["is_reel"] is True and item["cdn_url_valid"] is True
    assert item["cdn_domain_suffix"] == ".fbcdn.net" and item["shortcode_matches"] is True


def test_carousel_lists_every_item(tmp_path):
    server = Server(fixture("post_carousel_mixed.json"))
    code, report = run_tool(server, "https://www.instagram.com/p/FAKEPOSTCAR1/", tmp_path=tmp_path)
    assert code == 0 and report["media_count"] == 4 and report["requests"] == ONE_GET_ONE_POST
    assert [(i["source_media_index"], i["media_type"], i["is_reel"]) for i in report["items"]] == [
        (0, "image", False), (1, "video", False), (2, "image", False), (3, "video", False)]
    shape = report["response_shape"]
    assert shape["typename"] == "XDTGraphSidecar" and shape["children_count"] == 4
    assert "display_url" in shape["child_keys"]


def test_v1_items_format_with_root_and_doc_id_from_env(tmp_path):
    payload = {"data": {OTHER_ROOT: fixture_root("post_v1_items.json")}}
    server = Server(httpx.Response(200, json=payload))
    env = {"MEDIA_TOOL_IG_ROOT_POLARISPOSTACTIONLOADPOSTQUERYQUERY": OTHER_ROOT,
           "MEDIA_TOOL_IG_DOC_POLARISPOSTACTIONLOADPOSTQUERYQUERY": "1234567890"}
    code, report = run_tool(server, "https://www.instagram.com/p/FAKEPOSTV1A1/", tmp_path=tmp_path, env=env)
    assert code == 0 and report["media_count"] == 2
    assert report["query"]["root_field"] == OTHER_ROOT and report["query"]["doc_id"] == "1234567890"
    assert server.forms[0]["doc_id"] == "1234567890"
    shape = report["response_shape"]
    assert shape["format"] == "v1_items" and shape["items_count"] == 1 and "carousel_media" in shape["item_keys"]


def test_item_with_url_outside_cdn_is_flagged(tmp_path):
    root = fixture_root("post_image.json")
    root["display_resources"] = []
    root["display_url"] = "https://evil.example.com/a.jpg"
    server = Server(httpx.Response(200, json={"data": {ROOT: root}}))
    _, report = run_tool(server, tmp_path=tmp_path)
    assert report["items"][0]["cdn_url_valid"] is False and report["items"][0]["cdn_domain_suffix"] is None


def test_shortcode_mismatch_is_flagged_without_printing_codes(tmp_path):
    server = Server(fixture("post_video_reel.json"))   # a resposta traz outra publicação
    code, report = run_tool(server, tmp_path=tmp_path)
    # O provider recusa a resposta de outra publicação (`post_mismatch`): nenhuma mídia chega ao relatório.
    assert code == 1 and report["outcome"] == "contract_changed"
    assert report["error"]["detail"] == "post_mismatch" and report["error"]["stage"] == "normalize"
    assert report["items"] == [] and report["media_count"] == 0
    assert "FAKEPOSTREEL" not in json.dumps(report)


def test_null_root_is_link_error(tmp_path):
    server = Server(fixture("post_null.json"))
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 1 and report["outcome"] == "content_unavailable"
    assert report["error"]["detail"] == "post_null" and report["error"]["stage"] == "graphql"
    assert report["error"]["data_keys"] == [ROOT] and report["media_count"] == 0
    assert report["response_shape"] is None and report["requests"] == ONE_GET_ONE_POST


def test_wrong_root_reports_the_key_names_instagram_sent(tmp_path):
    server = Server(httpx.Response(200, json={"data": {OTHER_ROOT: {"items": []}}}))
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 1 and report["outcome"] == "contract_changed"
    assert report["error"]["detail"] == "missing_root_field" and report["error"]["data_keys"] == [OTHER_ROOT]


def _html_403() -> httpx.Response:
    return httpx.Response(403, text="<html>Forbidden</html>", headers={"content-type": "text/html"})


@pytest.mark.parametrize("failure, code", [
    (httpx.Response(429, json={"message": "Please wait a few minutes", "status": "fail"}), "rate_limited"),
    (httpx.Response(302, headers={"location": "https://www.instagram.com/challenge/FAKE/"}), "checkpoint_required"),
    (httpx.Response(400, text=(FIXTURES / "graphql" / "feedback_restriction.json").read_text(encoding="utf-8"),
                    headers={"content-type": "application/json"}), "action_blocked"),
    (_html_403(), "access_refused"),
    (httpx.Response(200, text="isto não é json", headers={"content-type": "text/html"}), "contract_changed"),
    (httpx.Response(200, text=(FIXTURES / "graphql" / "error_doc_not_found.json").read_text(encoding="utf-8"),
                    headers={"content-type": "application/json"}), "contract_changed"),
    (httpx.Response(200, json={"errors": [{"code": 1357004, "message": "Please reload"}]}), "session_invalid"),
    (httpx.ConnectError("falha FAKE"), "network_error"),
    (RuntimeError("falha FAKE"), "internal_error"),
])
def test_error_stops_immediately_without_retry(tmp_path, failure, code):
    server = Server(failure)
    exit_code, report = run_tool(server, tmp_path=tmp_path)
    assert exit_code == 1 and report["outcome"] == code and report["error"]["code"] == code
    assert {"code", "message", "stage", "detail"} <= set(report["error"])
    # Para no ato: 1 GET + 1 POST, sem re-bootstrap e sem nova tentativa.
    assert [r[2] for r in server.requests] == ["/", "/graphql/query"]
    assert report["cdn_requests"] == 0 and report["items"] == [] and report["media_count"] == 0
    assert "falha FAKE" not in json.dumps(report)


def test_refusal_by_403_reports_safe_http_diagnostic(tmp_path):
    server = Server(_html_403())
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 1 and report["outcome"] == "access_refused"
    assert report["error"] == {
        "code": "access_refused", "message": report["error"]["message"], "stage": "graphql", "detail": "http_403",
        "status": 403, "content_type": "text/html", "size": len("<html>Forbidden</html>")}
    assert "redirect" not in report["error"]
    assert report["requests"] == [{"method": "GET", "path": "/", "status": 200},
                                  {"method": "POST", "path": "/graphql/query", "status": 403}]


def test_refusal_by_redirect_home_reports_redirect_kind(tmp_path):
    server = Server(httpx.Response(302, headers={"location": "https://www.instagram.com/?next=FAKEQUERY123"}))
    code, report = run_tool(server, tmp_path=tmp_path)
    error = report["error"]
    assert code == 1 and report["outcome"] == "access_refused"
    assert error["status"] == 302 and error["redirect"] == "redirect_home" and error["detail"] == "redirect_home"
    assert error["stage"] == "graphql" and error["size"] == 0
    assert "location_path" not in error and "path" not in error and "set_cookie_names" not in error
    assert report["requests"][-1] == {"method": "POST", "path": "/graphql/query", "status": 302}
    assert [r[2] for r in server.requests] == ["/", "/graphql/query"]


PLANTED = ("FAKE-COOKIE-PLANTED-01", "FAKE_TOKEN_PLANTED_01", "FAKEPLANTSC1", "igsh=FAKEPLANTQS",
           "scontent-plant.cdninstagram.com", "plantcookie")


def test_planted_sensitive_values_never_reach_the_report(tmp_path):
    # Pela resposta: cookie, token, shortcode, query string e URL de CDN no Location, Set-Cookie, content-type e corpo.
    body = (f'<html>https://scontent-plant.cdninstagram.com/v/FAKEPLANTSC1.jpg?oh=FAKE_TOKEN_PLANTED_01 '
            f'sessionid={PLANTED[0]}</html>')
    server = Server(httpx.Response(302, text=body, headers=[
        ("location", "https://www.instagram.com/p/FAKEPLANTSC1/?igsh=FAKEPLANTQS&token=FAKE_TOKEN_PLANTED_01"),
        ("set-cookie", f"plantcookie={PLANTED[0]}; Path=/"),
        ("content-type", "text/html; charset=utf-8; token=FAKE_TOKEN_PLANTED_01")]))
    code, report = run_tool(server, tmp_path=tmp_path)
    text = json.dumps(report, ensure_ascii=True)
    for secret in PLANTED:
        assert secret not in text, secret
    error = report["error"]
    assert code == 1 and error["status"] == 302 and error["redirect"] == "redirect_other"
    assert error["content_type"] == "text/html" and error["size"] == len(body)
    assert set(error) == {"code", "message", "stage", "detail", "status", "redirect", "content_type", "size"}

    # Pelo próprio diagnóstico: valor fora do formato é omitido, e campo fora da lista de permissão nunca sai.
    exc = poc.InstagramWebError("access_refused", "Recusado.", stage="graphql", diagnostic={
        "status": f"403 {PLANTED[1]}", "redirect": "https://scontent-plant.cdninstagram.com/v/FAKEPLANTSC1.jpg",
        "detail": f"sessionid={PLANTED[0]}", "content_type": f"text/{PLANTED[1]}?igsh=FAKEPLANTQS", "size": PLANTED[2],
        "path": "/p/FAKEPLANTSC1/", "location_path": "/p/FAKEPLANTSC1/", "set_cookie_names": ["plantcookie"],
        "json_keys": [PLANTED[1]], "token": PLANTED[1]})
    error = poc.safe_error(exc)
    assert error == {"code": "access_refused", "message": "Recusado.", "stage": "graphql", "detail": None}


def test_token_rejected_does_not_log_rebootstrap(tmp_path, caplog):
    caplog.set_level("DEBUG")
    server = Server(httpx.Response(200, json={"errors": [{"code": 1357004, "message": "Please reload"}]}))
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 1 and report["error"]["detail"] == "web_token_rejected"
    assert "refazendo o bootstrap" not in caplog.text and CODE not in caplog.text


@pytest.mark.parametrize("url", [
    "https://www.instagram.com/perfil_teste/", "https://evil.example.com/p/FAKEBAD0001/?token=FAKEBAD",
    "https://www.instagram.com/stories/perfil_teste/123/", "FAKEBAD0001", "https://www.instagram.com/p/abc/",
])
def test_invalid_link_makes_no_request_and_is_not_echoed(tmp_path, url):
    server = Server()
    code, report = run_tool(server, url, tmp_path=tmp_path)
    assert code == 1 and report["outcome"] == "invalid_input" and report["requests"] == []
    assert server.requests == []
    assert "FAKEBAD" not in json.dumps(report) and "perfil_teste" not in json.dumps(report)


def test_missing_session_stops_without_requests(tmp_path):
    server = Server()
    code, report = run_tool(server, provider=Provider(configured=False), tmp_path=tmp_path)
    assert code == 1 and report["outcome"] == "session_missing" and report["requests"] == []
    assert server.requests == []


def test_refuses_when_app_is_running(tmp_path):
    server = Server()
    code, report = run_tool(server, running=True, tmp_path=tmp_path)
    assert code == 2 and report["outcome"] == "app_running" and server.requests == []
    assert "requests" not in report


def test_no_database_or_storage_created(tmp_path):
    server = Server(fixture("post_image.json"))
    run_tool(server, tmp_path=tmp_path)
    settings = make_settings(tmp_path)
    assert not settings.database_path.exists() and not settings.storage_dir.exists()


def test_keyboard_interrupt_during_request_reports_json(tmp_path):
    server = Server(KeyboardInterrupt())
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 130 and report["outcome"] == "interrupted" and report["error"]["code"] == "interrupted"
    assert report["items"] == []
    json.dumps(report)


def test_keyboard_interrupt_in_main_prints_json(monkeypatch, capsys):
    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(poc, "load_settings", interrupted)
    assert poc.main(["--url", LINK]) == 130
    assert json.loads(capsys.readouterr().out)["outcome"] == "interrupted"


def test_script_passes_the_url_and_does_not_run_anything_else():
    script = (Path(__file__).resolve().parents[2] / "scripts" / "poc-instagram-post.ps1").read_text(
        encoding="utf-8-sig")
    assert "poc_instagram_post.py" in script and "--url $Url" in script and "[string]$Url" in script
