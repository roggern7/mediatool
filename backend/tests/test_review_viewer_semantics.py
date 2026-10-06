"""Revisão independente: semântica do viewer no provider Instagram Web (offline, MockTransport, valores FAKE).

Evidência real: o HTML inicial logado traz "USER_ID":"0"; a PolarisProfilePostsQuery responde 200 com edges e
data.xdt_viewer.user.id. Caixa-preta: InstagramWebDownloader.test_session()/iter_profile_media(), WebBootstrap.get()
e o formulário POST capturado.
"""

from __future__ import annotations

import json
import logging
import urllib.parse

import httpx
import pytest

from app.integrations.instagram.session import InstagramSession, InstagramSessionProvider, SessionStatus
from app.integrations.instagram.web.bootstrap import WebBootstrap
from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.http import InstagramWebHttp, WebHttpConfig
from app.integrations.instagram.web.provider import InstagramWebDownloader

DS_USER = "10000000777"
OTHER_USER = "10000000999"
REAL_HTML_USER = "10000000555"
SID = "FAKE-SESSIONID-VIEWER-0001"
CSRF = "FAKECSRFVIEWER0001"
DTSG = "FAKE_DTSG_VIEWER_0001"
LSD = "FAKE_LSD_VIEWER_0001"
SECRETS = (SID, CSRF, DTSG, LSD)
CDN = "https://scontent-fake.cdninstagram.com/v/t51.fake/"
CONN = "xdt_api__v1__feed__user_timeline_graphql_connection"


def html(user_id: str | None = "0", *, dtsg: bool = True, lsd: bool = True) -> str:
    define = []
    if dtsg:
        define.append(f'["DTSGInitialData",[],{{"token":"{DTSG}"}},258]')
    if lsd:
        define.append(f'["LSD",[],{{"token":"{LSD}"}},323]')
    user = f'"USER_ID":"{user_id}",' if user_id is not None else ""
    define.append(f'["CurrentUserInitialData",[],{{"ACCOUNT_ID":"0",{user}"NAME":"","APP_ID":"936619743392459"}},270]')
    body = '{"require":[["ScheduledServerJS","handle",null,[{"__bbox":{"define":[' + ",".join(define) + "]}}]]]}"
    return f'<!DOCTYPE html><html><body><script type="application/json">{body}</script></body></html>'


def node(code: str = "AAA111") -> dict:
    return {"code": code, "pk": "3100000000000000001", "id": "3100000000000000001_9", "taken_at": 1700000000,
            "media_type": 1, "product_type": "feed", "carousel_media": None,
            "image_versions2": {"candidates": [{"url": f"{CDN}{code}.jpg?oh=FAKE", "width": 320, "height": 240}]}}


def listing(viewer: object = "absent") -> dict:
    data: dict = {CONN: {"edges": [{"node": node(), "cursor": None}],
                         "page_info": {"has_next_page": False, "end_cursor": None}}}
    if viewer != "absent":
        data["xdt_viewer"] = viewer
    return {"data": data}


class Provider(InstagramSessionProvider):
    def status(self) -> SessionStatus:
        return SessionStatus(configured=True, source="file")

    def get(self):
        return InstagramSession({"sessionid": SID, "csrftoken": CSRF, "ds_user_id": DS_USER})

    def save(self, sessionid, extra=None) -> None: ...

    def clear(self) -> None: ...


class Server:
    def __init__(self, page_html: str, graphql=None) -> None:
        self.html = page_html
        self.graphql = graphql if graphql is not None else (lambda req: httpx.Response(200, json=listing({"user": {"id": DS_USER}})))
        self.forms: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.instagram.com"
        if request.method == "GET":
            return httpx.Response(200, text=self.html, headers={"content-type": "text/html; charset=utf-8"})
        self.forms.append({k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode()).items()})
        return self.graphql(request)

    def downloader(self) -> InstagramWebDownloader:
        return InstagramWebDownloader(Provider(), transport=httpx.MockTransport(self.handle), page_delay=(0, 0))


def make(page_html: str, graphql=None) -> tuple[InstagramWebDownloader, Server]:
    server = Server(page_html, graphql)
    return server.downloader(), server


def bootstrap_get(page_html: str):
    server = Server(page_html)
    http = InstagramWebHttp(Provider().get(), WebHttpConfig(), transport=httpx.MockTransport(server.handle))
    return WebBootstrap(http).get()


def no_leak(*texts: str) -> None:
    blob = "\n".join(texts)
    for secret in SECRETS:
        assert secret not in blob


# 1 / 2 / 3 ---------------------------------------------------------------------------------------------------


def test_bootstrap_user_id_zero_does_not_fail():
    state = bootstrap_get(html(user_id="0"))
    assert state is not None


def test_bootstrap_without_any_viewer_marker_does_not_fail():
    assert bootstrap_get(html(user_id=None)) is not None


@pytest.mark.parametrize("kwargs", [{"dtsg": False}, {"lsd": False}, {"dtsg": False, "lsd": False}])
def test_bootstrap_missing_token_is_contract_changed(kwargs):
    for uid in (None, "0", REAL_HTML_USER):
        with pytest.raises(InstagramWebError) as info:
            bootstrap_get(html(user_id=uid, **kwargs))
        assert info.value.code == "contract_changed", (uid, kwargs, info.value.code)
        no_leak(str(info.value), repr(info.value))


# 4 -----------------------------------------------------------------------------------------------------------


def test_form_av_is_ds_user_id_when_html_user_id_zero():
    dl, server = make(html(user_id="0"))
    list(dl.iter_profile_media("instagram", "all", 1))
    assert server.forms and all(f["av"] == DS_USER for f in server.forms), [f.get("av") for f in server.forms]


def test_form_av_is_ds_user_id_when_html_has_no_viewer_marker():
    dl, server = make(html(user_id=None))
    list(dl.iter_profile_media("instagram", "all", 1))
    assert server.forms and server.forms[0]["av"] == DS_USER


def test_form_av_uses_real_html_user_id_when_not_zero():
    dl, server = make(html(user_id=REAL_HTML_USER))
    list(dl.iter_profile_media("instagram", "all", 1))
    assert server.forms and server.forms[0]["av"] == REAL_HTML_USER


# 5 - 8 -------------------------------------------------------------------------------------------------------


def test_session_valid_when_xdt_viewer_matches():
    dl, _ = make(html("0"))
    r = dl.test_session()
    assert (r.result, r.ok, r.session_recognized, r.listing_api, r.bootstrap, r.graphql) == (
        "valid", True, True, "ok", "ok", "ok"), r


@pytest.mark.parametrize("viewer", ["absent", None, {}, {"user": None}], ids=["absent", "null", "empty", "user_null"])
def test_session_without_viewer_evidence_is_unknown_not_invalid(viewer):
    dl, _ = make(html("0"), lambda req: httpx.Response(200, json=listing(viewer)))
    r = dl.test_session()
    assert r.listing_api == "ok", r
    assert r.session_recognized is None, r
    assert r.result not in ("invalid", "expired"), r
    assert r.bootstrap == "ok" and r.graphql == "ok", r


def test_session_other_account_viewer():
    dl, _ = make(html("0"), lambda req: httpx.Response(200, json=listing({"user": {"id": OTHER_USER}})))
    r = dl.test_session()
    assert r.session_recognized is False, r
    assert r.listing_api == "ok", r
    assert "outra conta" in r.message.lower(), r.message
    assert OTHER_USER not in r.message and DS_USER not in r.message


def _login_required(req):
    return httpx.Response(401, json={"message": "login_required", "require_login": True, "status": "fail"})


def _redirect_login(req):
    return httpx.Response(302, headers={"location": "https://www.instagram.com/accounts/login/?next=/graphql/query"})


def _cookie_deleted(req):
    return httpx.Response(200, json=listing(), headers={
        "set-cookie": 'sessionid=""; Max-Age=0; expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/; Domain=.instagram.com'})


@pytest.mark.parametrize("graphql", [_cookie_deleted], ids=["sessionid_deleted"])
def test_session_reliable_invalid_evidence(graphql):
    dl, _ = make(html("0"), graphql)
    r = dl.test_session()
    assert r.session_recognized is False, r
    assert r.result in ("invalid", "expired"), r
    assert r.ok is False


@pytest.mark.parametrize("graphql", [_login_required, _redirect_login], ids=["401_require_login", "302_login"])
def test_single_auth_refusal_is_not_session_invalid(graphql):
    # Seção 7: um request recusado por autenticação (401/require_login, redirect da consulta para login) é
    # request_auth_failed; não prova sessão inválida.
    dl, _ = make(html("0"), graphql)
    r = dl.test_session()
    assert r.ok is False and r.code == "request_auth_failed", r
    assert r.session_recognized is not False and r.result not in ("invalid", "expired"), r


# 9 -----------------------------------------------------------------------------------------------------------


def test_iter_profile_media_with_user_id_zero_returns_one_media():
    dl, _ = make(html("0"))
    items = list(dl.iter_profile_media("instagram", "all", 1))
    assert len(items) == 1
    assert items[0].source_post_id == "AAA111"


# 10 ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("graphql", [None, _login_required, _redirect_login, _cookie_deleted], ids=["ok", "401", "302", "cookie"])
def test_no_fake_secret_leaks(graphql, caplog):
    dl, _ = make(html("0"), graphql)
    with caplog.at_level(logging.DEBUG):
        r = dl.test_session()
        items = []
        try:
            items = list(dl.iter_profile_media("instagram", "all", 1))
            exc_text = ""
        except Exception as exc:  # noqa: BLE001
            exc_text = f"{exc} {exc!r}"
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    no_leak(blob, repr(r), str(r), r.message, r.detail or "", exc_text, repr(items), json.dumps(r.__dict__, default=str))
