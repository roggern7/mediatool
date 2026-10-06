"""POC de paginação (tools/poc_instagram_pagination.py): offline, httpx.MockTransport, valores FAKE, sem banco."""

from __future__ import annotations

import importlib.util
import json
import urllib.parse
from pathlib import Path

import httpx
import pytest

from app.integrations.instagram.session import InstagramSession, InstagramSessionProvider, SessionStatus
from app.integrations.instagram.web.queries import PROFILE_POSTS
from conftest import make_settings

_SPEC = importlib.util.spec_from_file_location(
    "poc_instagram_pagination", Path(__file__).resolve().parents[1] / "tools" / "poc_instagram_pagination.py"
)
poc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(poc)

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web"
ROOT = PROFILE_POSTS.root_field
SID = "FAKE-SESSIONID-PAGINATION-0001"
DS_USER = "10000000321"
OTHER_USER = "10000000654"
CURSOR_1 = "FAKE_CURSOR_PAGINATION_PAGE_1"
CURSOR_2 = "FAKE_CURSOR_PAGINATION_PAGE_2"
CDN = "https://scontent-fake.cdninstagram.com/v/t51.fake/"
GQL = ("POST", "/graphql/query")


class Provider(InstagramSessionProvider):
    def __init__(self, configured: bool = True) -> None:
        self.configured = configured

    def status(self) -> SessionStatus:
        return SessionStatus(configured=self.configured, source="file" if self.configured else None)

    def get(self):
        return InstagramSession({"sessionid": SID, "ds_user_id": DS_USER}) if self.configured else None

    def save(self, sessionid, extra=None) -> None: ...

    def clear(self) -> None: ...


def node(code: str, pk: str, *, video: bool = False, url: str | None = None) -> dict:
    image = {"candidates": [{"url": url or f"{CDN}{code}.jpg?oh=FAKESIG", "width": 320, "height": 240}]}
    base = {"code": code, "pk": pk, "id": f"{pk}_9", "taken_at": 1700000000, "image_versions2": image}
    if video:
        return {**base, "media_type": 2, "product_type": "clips",
                "video_versions": [{"url": f"{CDN}{code}.mp4?oh=FAKESIG", "width": 720, "height": 1280}]}
    return {**base, "media_type": 1, "product_type": "feed"}


def page(nodes: list[dict], has_next: bool, cursor: str | None, viewer: str | None = DS_USER) -> httpx.Response:
    data: dict = {ROOT: {"edges": [{"node": n} for n in nodes],
                         "page_info": {"has_next_page": has_next, "end_cursor": cursor}}}
    if viewer is not None:
        data["xdt_viewer"] = {"user": {"id": viewer}}
    return httpx.Response(200, json={"data": data})


class Server:
    def __init__(self, *graphql: httpx.Response | BaseException) -> None:
        self.graphql = list(graphql)
        self.requests: list[tuple[str, str, str]] = []
        self.variables: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.host, request.url.path))
        assert request.url.host == "www.instagram.com"
        if request.method == "GET" and request.url.path == "/":
            html = (FIXTURES / "bootstrap_viewer_zero.html").read_text(encoding="utf-8")
            return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})
        if request.method == "POST" and request.url.path == "/graphql/query":
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            self.variables.append(json.loads(form["variables"]))
            if not self.graphql:
                raise AssertionError("requisição GraphQL inesperada")
            item = self.graphql.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        raise AssertionError("requisição inesperada")


def run_tool(server: Server, *argv: str, running: bool = False, provider=None, tmp_path: Path):
    code, report = poc.run(
        poc.parse_args(["--profile", "@perfil_teste", *argv]), settings=make_settings(tmp_path),
        session_provider=provider or Provider(), transport=httpx.MockTransport(server.handle), page_delay=(0, 0),
        is_app_running=lambda s: running,
    )
    text = json.dumps(report, ensure_ascii=True)
    for secret in (SID, DS_USER, OTHER_USER, CURSOR_1, CURSOR_2, "FAKESIG", "scontent-fake", "FAKE_DTSG", "FAKE_LSD"):
        assert secret not in text, secret
    return code, report


def gql_count(report: dict) -> int:
    return sum(1 for r in report["requests"] if (r["method"], r["path"]) == GQL)


def test_defaults_are_two_pages_of_one_item():
    args = poc.parse_args(["--profile", "perfil_teste"])
    assert (args.page_size, args.limit) == (1, 2)


def test_two_pages_metadata_items_and_requests(tmp_path):
    server = Server(page([node("AAA111", "31001")], True, CURSOR_1),
                    page([node("BBB222", "31002", video=True)], True, CURSOR_2))
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 0 and report["outcome"] == "ok" and "error" not in report
    assert report["requests"] == [{"method": "GET", "path": "/", "status": 200},
                                  {"method": "POST", "path": "/graphql/query", "status": 200},
                                  {"method": "POST", "path": "/graphql/query", "status": 200}]
    assert report["instagram_requests"] == 3 and report["cdn_requests"] == 0 and report["other_requests"] == 0
    first, second = report["pages"]
    assert first["page_index"] == 1 and first["has_next_page"] is True and first["end_cursor_present"] is True
    assert first["cursor_changed"] is True and first["edges_count"] == 1 and first["new_nodes"] == 1
    assert first["http_status"] == 200 and first["account_confirmation"] == "confirmed"
    assert "after" not in first["variables_keys"]
    assert {"after", "before", "first", "last", "data", "username"} <= set(second["variables_keys"])
    assert second["page_index"] == 2 and second["cursor_changed"] is True and second["duplicate_nodes"] == 0
    # variáveis da página 2 (hipótese Relay): after = cursor da página 1, first = count
    assert server.variables[0]["data"]["count"] == 1 and "after" not in server.variables[0]
    assert server.variables[1]["after"] == CURSOR_1 and server.variables[1]["first"] == 1
    assert server.variables[1]["before"] is None and server.variables[1]["last"] is None
    assert report["items"] == [
        {"shortcode": "AAA111", "media_type": "image", "is_reel": False, "source_media_index": 0,
         "taken_at": "2023-11-14T22:13:20Z", "cdn_url_valid": True, "cdn_domain_suffix": ".cdninstagram.com"},
        {"shortcode": "BBB222", "media_type": "video", "is_reel": True, "source_media_index": 0,
         "taken_at": "2023-11-14T22:13:20Z", "cdn_url_valid": True, "cdn_domain_suffix": ".cdninstagram.com"},
    ]
    assert report["duplicates_across_pages"] == 0
    assert report["stopped_at_limit"] is True and report["account_confirmation"] == "confirmed"
    assert server.graphql == []   # has_next_page=true na página 2, mas nenhuma página extra foi pedida


def test_dedupe_across_pages(tmp_path):
    server = Server(page([node("AAA111", "31001")], True, CURSOR_1),
                    page([node("AAA111", "31001"), node("CCC333", "31003")], False, None))
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 0 and [i["shortcode"] for i in report["items"]] == ["AAA111", "CCC333"]
    assert report["pages"][1]["duplicate_nodes"] == 1 and report["pages"][1]["new_nodes"] == 1
    assert report["duplicates_across_pages"] == 1 and report["stopped_at_limit"] is True


def test_listing_ends_before_limit_is_not_stopped_at_limit(tmp_path):
    server = Server(page([node("AAA111", "31001")], False, None, viewer=OTHER_USER))
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 3 and report["outcome"] == "inconclusive_single_page"
    assert len(report["items"]) == 1 and gql_count(report) == 1
    assert report["stopped_at_limit"] is False and report["account_confirmation"] == "different_account"
    assert report["pages"][0]["end_cursor_present"] is False and report["pages"][0]["cursor_changed"] is False


def test_page_size_and_limit_arguments(tmp_path):
    server = Server(page([node("AAA111", "31001"), node("BBB222", "31002")], True, CURSOR_1, viewer=None),
                    page([node("CCC333", "31003")], False, None))
    code, report = run_tool(server, "--page-size", "2", "--limit", "3", tmp_path=tmp_path)
    assert code == 0 and len(report["items"]) == 3 and gql_count(report) == 2
    assert server.variables[0]["data"]["count"] == 2 and server.variables[1]["first"] == 2
    assert report["account_confirmation"] == "not_confirmed" and report["stopped_at_limit"] is True


def test_item_with_url_outside_cdn_is_flagged(tmp_path):
    server = Server(page([node("AAA111", "31001", url="https://evil.example.com/a.jpg")], False, None))
    _, report = run_tool(server, tmp_path=tmp_path)
    assert report["items"][0]["cdn_url_valid"] is False and report["items"][0]["cdn_domain_suffix"] is None


def _html_403() -> httpx.Response:
    return httpx.Response(403, text="<html>Forbidden</html>", headers={"content-type": "text/html"})


@pytest.mark.parametrize("failure, code", [
    (httpx.Response(429, json={"message": "Please wait a few minutes", "status": "fail"}), "rate_limited"),
    (httpx.Response(302, headers={"location": "https://www.instagram.com/challenge/FAKE/"}), "checkpoint_required"),
    (httpx.Response(400, text=(FIXTURES / "graphql" / "feedback_restriction.json").read_text(encoding="utf-8"),
                    headers={"content-type": "application/json"}), "action_blocked"),
    (_html_403(), "access_refused"),
    (httpx.Response(200, text="isto não é json", headers={"content-type": "text/html"}), "contract_changed"),
    (httpx.Response(200, json={"errors": [{"code": 1357004, "message": "Please reload"}]}), "session_invalid"),
    (RuntimeError("falha FAKE"), "internal_error"),
])
@pytest.mark.parametrize("fail_on_page", [1, 2])
def test_error_stops_immediately_without_retry(tmp_path, failure, code, fail_on_page):
    responses = [page([node("AAA111", "31001")], True, CURSOR_1)] if fail_on_page == 2 else []
    server = Server(*responses, failure)
    exit_code, report = run_tool(server, tmp_path=tmp_path)
    assert exit_code == 1 and report["outcome"] == code and report["error"]["code"] == code
    assert set(report["error"]) == {"code", "message", "stage", "detail"}
    # Para no ato: nenhuma requisição depois da que falhou (sem re-bootstrap, sem nova tentativa).
    assert [r[2] for r in server.requests] == ["/"] + ["/graphql/query"] * fail_on_page
    assert gql_count(report) == fail_on_page and report["cdn_requests"] == 0
    assert report["stopped_at_limit"] is False and len(report["items"]) == fail_on_page - 1
    assert len(report["pages"]) == fail_on_page - 1
    assert "falha FAKE" not in json.dumps(report)


def test_missing_session_stops_without_requests(tmp_path):
    server = Server()
    code, report = run_tool(server, provider=Provider(configured=False), tmp_path=tmp_path)
    assert code == 1 and report["outcome"] == "session_missing" and report["requests"] == []
    assert server.requests == [] and report["account_confirmation"] is None


def test_refuses_when_app_is_running(tmp_path):
    server = Server()
    code, report = run_tool(server, running=True, tmp_path=tmp_path)
    assert code == 2 and report["outcome"] == "app_running" and server.requests == []
    assert "requests" not in report


def test_no_database_or_storage_created(tmp_path):
    server = Server(page([node("AAA111", "31001")], False, None))
    run_tool(server, tmp_path=tmp_path)
    settings = make_settings(tmp_path)
    assert not settings.database_path.exists() and not settings.storage_dir.exists()


def carousel(code: str, pk: str, size: int) -> dict:
    children = [{"pk": f"{pk}{i}", "id": f"{pk}{i}_9", "media_type": 1,
                 "image_versions2": {"candidates": [{"url": f"{CDN}{code}-{i}.jpg?oh=FAKESIG", "width": 320,
                                                     "height": 240}]}} for i in range(size)]
    return {"code": code, "pk": pk, "id": f"{pk}_9", "taken_at": 1700000000, "media_type": 8,
            "product_type": "carousel_container", "carousel_media": children}


def test_first_page_satisfies_limit_is_inconclusive_single_page(tmp_path):
    server = Server(page([carousel("CAR111", "31009", 3)], True, CURSOR_1))
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 3 and report["outcome"] == "inconclusive_single_page" and "error" not in report
    assert [(i["shortcode"], i["source_media_index"]) for i in report["items"]] == [("CAR111", 0), ("CAR111", 1)]
    assert len(report["pages"]) == 1 and gql_count(report) == 1 and report["stopped_at_limit"] is True
    assert [r[2] for r in server.requests] == ["/", "/graphql/query"]   # nenhuma requisição adicional


def no_url_node(code: str, pk: str) -> dict:
    return {"code": code, "pk": pk, "id": f"{pk}_9", "taken_at": 1700000000, "media_type": 1,
            "product_type": "feed", "image_versions2": {"candidates": []}}


def test_hard_cap_of_two_graphql_posts_when_limit_not_reached(tmp_path):
    server = Server(page([no_url_node("NOURL1", "31011")], True, CURSOR_1),
                    page([no_url_node("NOURL2", "31012")], True, CURSOR_2),
                    page([node("AAA111", "31001")], False, None))   # 3a página nunca pode ser pedida
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 3 and report["outcome"] == "inconclusive_limit_not_reached" and "error" not in report
    assert gql_count(report) == 2 and [r[2] for r in server.requests] == ["/", "/graphql/query", "/graphql/query"]
    assert len(server.graphql) == 1 and report["items"] == [] and report["max_pages"] == 2
    assert report["stopped_at_limit"] is False


def test_profile_is_normalized_username_without_url_query(tmp_path):
    server = Server(page([node("AAA111", "31001")], True, CURSOR_1), page([node("BBB222", "31002")], False, None))
    code, report = poc.run(
        poc.parse_args(["--profile", "https://www.instagram.com/perfil_teste/?igsh=FAKEQUERY123"]),
        settings=make_settings(tmp_path), session_provider=Provider(), transport=httpx.MockTransport(server.handle),
        page_delay=(0, 0), is_app_running=lambda s: False,
    )
    assert code == 0 and report["profile"] == "perfil_teste"
    assert "FAKEQUERY123" not in json.dumps(report) and "igsh" not in json.dumps(report)


def test_invalid_profile_is_not_echoed(tmp_path):
    server = Server()
    code, report = poc.run(
        poc.parse_args(["--profile", "https://evil.example.com/x?token=FAKEBAD"]), settings=make_settings(tmp_path),
        session_provider=Provider(), transport=httpx.MockTransport(server.handle), page_delay=(0, 0),
        is_app_running=lambda s: False,
    )
    assert code == 1 and report["profile"] is None and report["outcome"] == "invalid_input"
    assert "FAKEBAD" not in json.dumps(report) and server.requests == []


def test_token_rejected_does_not_log_rebootstrap(tmp_path, caplog):
    caplog.set_level("DEBUG")
    server = Server(httpx.Response(200, json={"errors": [{"code": 1357004, "message": "Please reload"}]}))
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 1 and report["outcome"] == "session_invalid" and report["error"]["detail"] == "web_token_rejected"
    assert "refazendo o bootstrap" not in caplog.text
    assert [r[2] for r in server.requests] == ["/", "/graphql/query"]


def test_keyboard_interrupt_during_listing_reports_json(tmp_path):
    server = Server(page([node("AAA111", "31001")], True, CURSOR_1), KeyboardInterrupt())
    code, report = run_tool(server, tmp_path=tmp_path)
    assert code == 130 and report["outcome"] == "interrupted" and report["error"]["code"] == "interrupted"
    assert len(report["items"]) == 1 and len(report["pages"]) == 1 and gql_count(report) == 2
    json.dumps(report)


def test_keyboard_interrupt_in_main_prints_json(monkeypatch, capsys):
    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(poc, "load_settings", interrupted)
    assert poc.main(["--profile", "perfil_teste"]) == 130
    assert json.loads(capsys.readouterr().out)["outcome"] == "interrupted"
