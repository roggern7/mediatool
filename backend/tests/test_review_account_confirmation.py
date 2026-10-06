"""Revisão independente: confirmação semântica da conta, log 'entregues' e on_page (offline, MockTransport, FAKE)."""

from __future__ import annotations

import logging
import re

import httpx
import pytest

from app.integrations.instagram.web.bootstrap import WebBootstrap
from app.integrations.instagram.web.graphql import InstagramWebGraphQL
from app.integrations.instagram.web.http import InstagramWebHttp, WebHttpConfig
from tests.test_review_viewer_semantics import (
    CONN, DS_USER, OTHER_USER, SECRETS, Provider, Server, html, make, node,
)

CURSOR_1 = "FAKECURSORAAA111"
CURSOR_2 = "FAKECURSORBBB222"


def listing(viewer: object = "absent", nodes=None, has_next=False, cursor=None) -> dict:
    nodes = nodes if nodes is not None else [node()]
    data: dict = {CONN: {"edges": [{"node": n, "cursor": None} for n in nodes],
                         "page_info": {"has_next_page": has_next, "end_cursor": cursor}}}
    if viewer != "absent":
        data["xdt_viewer"] = viewer
    return {"data": data}


def served(viewer):
    return lambda req: httpx.Response(200, json=listing(viewer))


def post_node(code: str, pk: str) -> dict:
    n = node(code)
    n["pk"] = pk
    n["id"] = f"{pk}_9"
    return n


# 1 -----------------------------------------------------------------------------------------------------------
CASES = [
    ({"user": {"id": DS_USER}}, "confirmed"),
    ({"user": {"id": OTHER_USER}}, "different_account"),
    ("absent", "not_confirmed"),
    (None, "not_confirmed"),
    ({"user": None}, "not_confirmed"),
    ({"user": {"id": "0"}}, "not_confirmed"),
    ({"user": {"id": None}}, "not_confirmed"),
    ({"user": {"id": int(DS_USER)}}, "confirmed"),
    ({"user": {"id": f"  {DS_USER} "}}, "confirmed"),
]
IDS = ["match", "other", "absent", "null", "user_null", "zero", "id_null", "numeric", "spaces"]


@pytest.mark.parametrize("viewer,expected", CASES, ids=IDS)
def test_last_account_confirmation(viewer, expected):
    dl, _ = make(html("0"), served(viewer))
    assert len(list(dl.iter_profile_media("instagram", "all", 1))) == 1
    assert dl.last_account_confirmation == expected


# 6 -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("viewer,expected", CASES, ids=IDS)
def test_test_session_coherent(viewer, expected):
    dl, _ = make(html("0"), served(viewer))
    r = dl.test_session()
    assert r.session_recognized == {"confirmed": True, "different_account": False, "not_confirmed": None}[expected], r
    assert r.result == "valid"


# 2 -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("viewer", [{"user": {"id": DS_USER}}, {"user": {"id": OTHER_USER}}, "absent"])
def test_no_extra_requests(viewer):
    dl, server = make(html("0"), served(viewer))
    seen: list[httpx.Request] = []
    inner = server.handle

    def counting(request):
        seen.append(request)
        return inner(request)

    dl = type(dl)(Provider(), transport=httpx.MockTransport(counting), page_delay=(0, 0))
    list(dl.iter_profile_media("instagram", "all", 1))
    assert [r.method for r in seen] == ["GET", "POST"], [(r.method, str(r.url.path)) for r in seen]


# 3 -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("viewer", [{"user": {"id": DS_USER}}, {"user": {"id": OTHER_USER}}])
def test_ids_never_leak(viewer, caplog):
    dl, _ = make(html("0"), served(viewer))
    with caplog.at_level(logging.DEBUG):
        list(dl.iter_profile_media("instagram", "all", 1))
        r = dl.test_session()
    blob = "\n".join(rec.getMessage() + " " + str(rec.args) for rec in caplog.records)
    state = repr(dl) + str(dl) + repr(dl.last_account_confirmation) + r.message + repr(r) + (r.detail or "")
    for ident in (DS_USER, OTHER_USER, *SECRETS):
        assert ident not in blob, ident
        assert ident not in state, ident
    public = {k: v for k, v in vars(dl).items() if not k.startswith("_")}
    assert DS_USER not in repr(public) and OTHER_USER not in repr(public)


# 4 -----------------------------------------------------------------------------------------------------------
def _delivered(caplog) -> list[int]:
    out = []
    for rec in caplog.records:
        m = re.search(r"(\d+) entregues", rec.getMessage())
        if "Listagem web de" in rec.getMessage() and m:
            out.append(int(m.group(1)))
    return out


def test_log_delivered_counts_when_consumer_stops_at_limit(caplog):
    nodes = [post_node(f"C{i}", f"310000000000000010{i}") for i in range(3)]
    dl, _ = make(html("0"), lambda req: httpx.Response(200, json=listing({"user": {"id": DS_USER}}, nodes)))
    with caplog.at_level(logging.INFO):
        items = list(dl.iter_profile_media("instagram", "all", 1))
    assert len(items) == 1
    assert _delivered(caplog) == [1]


def test_log_delivered_full_consumption(caplog):
    nodes = [post_node(f"C{i}", f"310000000000000020{i}") for i in range(2)]
    dl, _ = make(html("0"), lambda req: httpx.Response(200, json=listing({"user": {"id": DS_USER}}, nodes)))
    with caplog.at_level(logging.INFO):
        items = list(dl.iter_profile_media("instagram", "all", 10))
    assert len(items) == 2
    assert _delivered(caplog) == [2]


# 5 -----------------------------------------------------------------------------------------------------------
def test_on_page_metadata():
    p1 = [post_node("P1", "3100000000000000301")]
    p2 = [post_node("P1", "3100000000000000301"), post_node("P2", "3100000000000000302")]
    pages = iter([listing({"user": {"id": DS_USER}}, p1, True, CURSOR_1),
                  listing({"user": {"id": DS_USER}}, p2, False, None)])
    server = Server(html("0"), lambda req: httpx.Response(200, json=next(pages)))
    http = InstagramWebHttp(Provider().get(), WebHttpConfig(), transport=httpx.MockTransport(server.handle))
    bootstrap = WebBootstrap(http)
    bootstrap.get()
    gql = InstagramWebGraphQL(http, bootstrap, page_delay=(0, 0))
    metas: list[dict] = []
    nodes = list(gql.iter_profile_posts("instagram", max_media=2, page_size=1, max_pages=2, on_page=metas.append))
    assert len(metas) == 2, metas
    assert len(nodes) == 2
    m1, m2 = metas
    assert m1["has_next_page"] is True and m1["end_cursor_present"] is True
    assert (m1["page_index"], m2["page_index"]) == (1, 2)
    assert m1["http_status"] == 200 and m1["edges_count"] == 1
    assert "after" not in m1["variables_keys"]
    assert "after" in m2["variables_keys"]
    assert m1["cursor_changed"] is True
    assert m1["new_nodes"] == 1 and m1["duplicate_nodes"] == 0
    assert m2["new_nodes"] == 1 and m2["duplicate_nodes"] == 1
    blob = repr(metas)
    for secret in (CURSOR_1, CURSOR_2, DS_USER, OTHER_USER, *SECRETS):
        assert secret not in blob
