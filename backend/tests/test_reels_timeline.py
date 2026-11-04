"""web/reels_timeline.py: aba Reels com a sessão (horário pelo pk, id do perfil, paginação; fakes próprios, offline)."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.graphql import InstagramWebGraphQL
from app.integrations.instagram.web.http import WebResponse
from app.integrations.instagram.web.reels_timeline import iter_reels_tab, profile_user_id, reel_taken_at_from_pk

USER_ID = "4000000001"
PROVIDER = "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider"


def pk_at(when: datetime) -> str:
    t_ms = int(when.timestamp() * 1000)
    return str(((t_ms - 1314220021721) << 23) | 123)


def html_resp(text: str, status: int = 200) -> WebResponse:
    return WebResponse(status, None, "text/html; charset=utf-8", 5, [], False, text, "/reels/")


def json_resp(payload: dict) -> WebResponse:
    return WebResponse(200, None, "application/json; charset=utf-8", 5, [], False, json.dumps(payload),
                       "/api/graphql")


def page(nodes: list[dict], has_next: bool, cursor: str | None) -> WebResponse:
    return json_resp({"data": {"fetch__XDTUserDict": {"clips_connection": {
        "edges": [{"node": n} for n in nodes], "page_info": {"end_cursor": cursor, "has_next_page": has_next}}},
        "xdt_viewer": None}})


def node(code: str, **extra) -> dict:
    return {"code": code, "pk": pk_at(datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)), "media_type": 2,
            "product_type": "clips", "clips_tab_pinned_user_ids": [], "is_in_profile_grid": False, **extra}


PROFILE_HTML = f'<html><script>{{"page_id":"profilePage_{USER_ID}","profile_id":"{USER_ID}"}}</script></html>'


@dataclass
class FakeState:
    fb_dtsg: str = "FAKE_DTSG_aaaaaaaa"
    lsd: str = "FAKE_LSD_bbbbbbbb"
    jazoest: str = "FAKE_JAZ_c"
    viewer_id: str | None = "9000000001"
    app_id: str | None = "FAKE_APP_ID_1"


class FakeBootstrap:
    def get(self, *, force: bool = False) -> FakeState:
        return FakeState()

    def invalidate(self) -> None:
        pass


class FakeHttp:
    def __init__(self, document: WebResponse, *posts: WebResponse) -> None:
        self.document = document
        self.queue = list(posts)
        self.documents: list[str] = []
        self.requests: list[tuple[str, dict, dict]] = []

    def get_document(self, path, *, referer=None, max_bytes=None, diagnostic_path=None):
        self.documents.append(path)
        return self.document

    def post_form(self, path, form, headers):
        self.requests.append((path, dict(form), dict(headers)))
        if not self.queue:
            raise AssertionError("requisição inesperada")
        return self.queue.pop(0)

    def csrftoken(self) -> str | None:
        return "FAKE_CSRF"

    def account_id(self) -> str | None:
        return None

    def variables(self, i: int) -> dict:
        return json.loads(self.requests[i][1]["variables"])


def run(http: FakeHttp, **kw) -> list[dict]:
    gql = InstagramWebGraphQL(http, FakeBootstrap(), page_delay=(0, 0))
    return list(iter_reels_tab(gql, http, "perfil_fake", **kw))


def test_taken_at_from_pk_matches_known_instant():
    when = datetime(2026, 11, 3, 18, 30, 15, tzinfo=timezone.utc)
    assert reel_taken_at_from_pk(pk_at(when)) == "2026-11-03T18:30:40Z"   # +25 s
    assert reel_taken_at_from_pk(int(pk_at(when))) == "2026-11-03T18:30:40Z"
    for bad in (None, True, "", "12a", "-5", 1.5, "9" * 25):
        assert reel_taken_at_from_pk(bad) is None


def test_profile_user_id_from_html_and_failures():
    http = FakeHttp(html_resp(PROFILE_HTML))
    assert profile_user_id(http, "perfil_fake") == USER_ID
    assert http.documents == ["/perfil_fake/reels/"]
    with pytest.raises(InstagramWebError) as exc:
        profile_user_id(FakeHttp(html_resp("<html>nada</html>")), "perfil_fake")
    assert exc.value.code == "contract_changed" and exc.value.diagnostic["detail"] == "reels_profile_id_missing"
    with pytest.raises(InstagramWebError) as exc:
        profile_user_id(FakeHttp(html_resp("", status=404)), "perfil_fake")
    assert exc.value.code == "profile_not_found"
    error_route = '<html>{"canonicalRouteName":"comet.igweb.PolarisErrorRoute"}</html>'
    with pytest.raises(InstagramWebError) as exc:
        profile_user_id(FakeHttp(html_resp(error_route)), "perfil_fake")
    assert exc.value.code == "profile_not_found"
    assert USER_ID not in json.dumps(exc.value.diagnostic)


def test_pagination_first_page_then_connection():
    when = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    http = FakeHttp(html_resp(PROFILE_HTML),
                    page([node("A1", clips_tab_pinned_user_ids=[USER_ID], play_count=1500),
                          node("A2", is_in_profile_grid=True)], True, "CUR_1"),
                    page([node("B1", media_type=8, product_type="carousel_container", play_count="12"), node("A2")],
                         False, None))
    items = run(http)
    assert [i["code"] for i in items] == ["A1", "A2", "B1"]
    assert items[0] == {"code": "A1", "taken_at": "2026-10-01T12:00:25Z", "kind": "reel", "pinned": True,
                        "in_grid": False, "source": "reels_tab", "pk": pk_at(when), "play_count": 1500,
                        "media_type": 2}
    assert items[1]["pinned"] is False and items[1]["in_grid"] is True
    assert items[2]["kind"] == "carousel" and items[2]["media_type"] == 8 and items[2]["play_count"] is None
    assert reel_taken_at_from_pk(pk_at(when)) == items[0]["taken_at"]
    (path1, form1, headers1), (path2, form2, _h2) = http.requests
    assert path1 == path2 == "/graphql/query"
    assert form1["doc_id"] == "29109455938671025"
    assert form1["fb_api_req_friendly_name"] == "PolarisProfileReelsTabContentQuery"
    assert headers1["X-Root-Field-Name"] == "fetch__XDTUserDict"
    assert headers1["Referer"] == "https://www.instagram.com/perfil_fake/reels/"
    assert http.variables(0) == {"data": {"include_feed_video": True, "page_size": 12, "target_user_id": USER_ID},
                                 "user_id": USER_ID, PROVIDER: True}
    assert form2["doc_id"] == "28971369649196954"
    assert form2["fb_api_req_friendly_name"] == "PolarisProfileReelsTabContentQuery_connection"
    assert http.variables(1) == {"after": "CUR_1", "data": {"include_feed_video": True, "page_size": 12,
                                                             "target_user_id": USER_ID},
                                 "first": 12, "id": USER_ID, PROVIDER: True}


def test_fallback_first_12_to_3_keeps_3():
    empty_root = json_resp({"data": {"fetch__XDTUserDict": {}}})
    http = FakeHttp(html_resp(PROFILE_HTML), page([node("A1")], True, "CUR_1"), empty_root,
                    page([node("B1")], True, "CUR_2"), page([node("C1")], False, None))
    calls: list[object] = []
    items = run(http, before_page=calls.append)
    assert [i["code"] for i in items] == ["A1", "B1", "C1"]
    assert [http.variables(i).get("first") for i in range(4)] == [None, 12, 3, 3]
    assert http.variables(2)["after"] == "CUR_1" and http.variables(3)["after"] == "CUR_2"
    assert len(calls) == 4   # before_page antes de cada POST, inclusive a nova tentativa


def test_fallback_fails_after_single_retry():
    empty_root = json_resp({"data": {"fetch__XDTUserDict": {}}})
    http = FakeHttp(html_resp(PROFILE_HTML), page([node("A1")], True, "CUR_1"), empty_root, empty_root)
    with pytest.raises(InstagramWebError) as exc:
        run(http)
    assert exc.value.code == "contract_changed" and exc.value.diagnostic["detail"] == "reels_connection_missing"


def test_cancel_and_cursor_anomalies():
    cancel = threading.Event()
    http = FakeHttp(html_resp(PROFILE_HTML), page([node("A1")], True, "CUR_1"))
    gql = InstagramWebGraphQL(http, FakeBootstrap(), page_delay=(0, 0))
    gen = iter_reels_tab(gql, http, "perfil_fake", cancel=cancel)
    assert next(gen)["code"] == "A1"
    cancel.set()
    assert list(gen) == [] and len(http.requests) == 1
    cancel_before = threading.Event()
    cancel_before.set()
    http = FakeHttp(html_resp(PROFILE_HTML))
    assert run(http, cancel=cancel_before) == [] and not http.documents and not http.requests

    for pages, detail in (([page([node("A1")], True, None)], "cursor_missing"),
                          ([page([node("A1")], True, "CUR_1"), page([node("B1")], True, "CUR_1")],
                           "repeated_cursor")):
        with pytest.raises(InstagramWebError) as exc:
            run(FakeHttp(html_resp(PROFILE_HTML), *pages))
        assert exc.value.code == "contract_changed" and exc.value.diagnostic["detail"] == detail
        assert "CUR_1" not in json.dumps(exc.value.diagnostic)


def test_node_wrapped_in_media_as_seen_in_real():
    """Formato real (2026-11-04): edges[].node.media {pk, code, ...}."""
    http = FakeHttp(html_resp(PROFILE_HTML),
                    json_resp({"data": {"fetch__XDTUserDict": {"clips_connection": {
                        "edges": [{"node": {"media": node("W1", play_count=159)}}],
                        "page_info": {"end_cursor": None, "has_next_page": False}}}, "xdt_viewer": None}}))
    [item] = run(http)
    assert item["code"] == "W1" and item["play_count"] == 159 and item["in_grid"] is False


def test_pinned_only_when_pinned_by_profile_owner():
    http = FakeHttp(html_resp(PROFILE_HTML),
                    page([node("C1", clips_tab_pinned_user_ids=["999"]),
                          node("C2", clips_tab_pinned_user_ids=["999", USER_ID])], False, None))
    items = run(http)
    assert [i["pinned"] for i in items] == [False, True]
