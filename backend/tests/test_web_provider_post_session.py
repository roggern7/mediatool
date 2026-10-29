"""InstagramWebDownloader.resolve_post, 2a tentativa de um link restrito: o MESMO documento da publicação aberto com a
sessão do dono, pelo cliente autenticado da importação por perfil, e só quando o documento público vem restrito.
Sobre ``httpx.MockTransport`` (offline; só valores FAKE): o servidor separa as requisições com e sem Cookie."""

from __future__ import annotations

import logging
import threading
from contextlib import nullcontext

import httpx
import pytest

from app.integrations.instagram.session import InstagramSession
from app.integrations.instagram.web import provider as provider_mod
from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.provider import InstagramWebDownloader
from app.jobs.errors import safe_diagnostic
from app.services.instagram_gate import InstagramPaused
from dash_fixtures import simple_mpd
from post_document_fixtures import (
    data_sjs_script,
    envelope,
    media_node,
    media_payload,
    post_html,
    post_page,
    session_media_node,
    session_page,
)

CODE = "FAKEGATED01"
OTHER = "FAKEOTHER02"
SID = "FAKE-SESSIONID-GATED-0001"
DS_USER = "10000000555"
CSRF = "FAKE-CSRF-GATED-0001"
SECRETS = (CODE, OTHER, SID, DS_USER, CSRF, "cdninstagram", "fbcdn", "FAKE_OH", "dono_fake", "Dono_Fake")
MSG_WITH_SESSION = ("A publicação continua indisponível mesmo com a sessão: pode ser de conta privada que você não "
                    "segue, removida ou restrita por região/idade.")


class OwnerSession:
    def __init__(self, cookies: dict | None = None):
        self.gets = 0
        self.cookies = {"sessionid": SID, "ds_user_id": DS_USER, "csrftoken": CSRF} if cookies is None else cookies

    def get(self):
        self.gets += 1
        return InstagramSession(dict(self.cookies)) if self.cookies else None


class Gate:
    def __init__(self, events: list, *, scope=None, on_wait=None):
        self.events = events
        self.scope = scope
        self.on_wait = on_wait
        self.waits = 0

    def wait_listing_page(self, cancel=None, *, max_per_hour):
        self.waits += 1
        self.events.append("wait_page")
        if self.on_wait is not None:
            self.on_wait(self.waits)

    def slot(self, cancel=None):
        self.events.append("slot")
        return nullcontext()

    def paused_scope(self):
        return self.scope


def html(body: str, status: int = 200, **headers) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **headers})


def gated_page() -> httpx.Response:
    return html(post_html([data_sjs_script(media_payload(envelope(None, code=CODE)))]))


def ok_page(kind: str = "video") -> httpx.Response:
    return html(post_page(media_node(kind, CODE)))


def logged_page(node=None, **kw) -> httpx.Response:
    """Documento aberto com a sessão (``xig_fetch_media``), como observado em 2026-10-29."""
    return html(session_page(session_media_node(CODE) if node is None else node, **kw))


def logged_null() -> httpx.Response:
    return html(session_page(None))


class Server:
    """``public`` = respostas sem Cookie (documento público); ``logged`` = respostas com Cookie (sessão)."""

    def __init__(self, public: list, logged: list, events: list | None = None, on_request=None):
        self.public = list(public)
        self.logged = list(logged)
        self.requests: list[httpx.Request] = []
        self.events = events if events is not None else []
        self.on_request = on_request

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.events.append("get")
        if self.on_request is not None:
            self.on_request(request)
        queue = self.logged if "cookie" in request.headers else self.public
        if not queue:
            raise AssertionError("requisição inesperada")
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def with_cookie(self) -> list[bool]:
        return ["cookie" in r.headers for r in self.requests]


def build(public: list, logged: list | None = None, *, owner=None, gate=None, events=None, on_request=None):
    server = Server(public, logged or [], events=events, on_request=on_request)
    owner = owner or OwnerSession()
    dl = InstagramWebDownloader(owner, transport=httpx.MockTransport(server.handle), request_gate=gate)
    return dl, server, owner


@pytest.fixture(autouse=True)
def _logs(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    for noisy in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(noisy), "level", logging.WARNING)
    monkeypatch.setattr(provider_mod, "_rate_limit_disabled_logged", False)
    yield
    text = caplog.text
    for secret in SECRETS:
        assert secret not in text, secret
    for word in ("cookie", "sessionid", "csrftoken", "token", "ds_user_id"):
        assert word not in text.lower(), word


# --------------------------------------------------------------------------- público: sessão intocada


def test_public_post_never_touches_the_session_or_the_authenticated_client():
    def no_session_client(session):
        raise AssertionError("o cliente autenticado não deve ser criado para uma publicação pública")

    server = Server([ok_page()], [])
    owner = OwnerSession()
    dl = InstagramWebDownloader(owner, transport=httpx.MockTransport(server.handle), http_factory=no_session_client)
    assert len(dl.resolve_post(CODE, path_kind="reel")) == 1
    assert owner.gets == 0 and server.with_cookie == [False]


# --------------------------------------------------------------------------- restrita -> documento com a sessão


def test_gated_then_session_document_ok_returns_the_media(caplog):
    events: list = []
    gate = Gate(events)
    dl, server, owner = build([gated_page()], [logged_page(feed=[session_media_node(CODE),
                                                                session_media_node(OTHER)])],
                              gate=gate, events=events)
    [item] = dl.resolve_post(CODE, path_kind="reel")
    assert item.source_post_id == CODE and item.media_type == "video" and item.is_reel
    assert (item.width, item.height) == (1080, 1920) and "preel1_102.mp4" in item.media_url, "a maior versão"
    assert [(r.method, r.url.path) for r in server.requests] == [("GET", f"/reel/{CODE}/"), ("GET", f"/reels/{CODE}/")]
    assert server.with_cookie == [False, True] and owner.gets == 1
    logged = server.requests[1]
    cookie = logged.headers["cookie"]
    assert f"sessionid={SID}" in cookie and f"csrftoken={CSRF}" in cookie
    # Os headers de navegador do documento (os mesmos do bootstrap da importação por perfil); nunca os de API.
    assert logged.headers["sec-fetch-mode"] == "navigate" and logged.headers["sec-fetch-dest"] == "document"
    assert logged.headers["accept"].startswith("text/html") and "referer" not in logged.headers
    for name in ("x-csrftoken", "x-ig-app-id", "x-requested-with", "content-type"):
        assert name not in logged.headers, name
    assert events == ["wait_page", "slot", "get", "wait_page", "slot", "get"], "teto e portão nas duas"
    assert "Publicação (documento com a sessão): outcome=ok" in caplog.text
    assert "via=session_document" in caplog.text


def test_gated_then_still_gated_with_the_session():
    dl, server, _ = build([gated_page()], [logged_null()])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="p")
    error = info.value
    assert (error.code, error.stage, error.retryable) == ("content_unavailable", "session_document", False)
    assert error.diagnostic == {"detail": "post_gated_with_session", "path": "/p/", "via": "session_document"}
    assert error.message == MSG_WITH_SESSION
    assert error.session_off is True, "falha de formato desliga a sessão no lote"
    assert len(server.requests) == 2


def test_session_document_in_another_format_fails_classified():
    """Documento logado sem ``xig_fetch_media`` (inclusive o envelope do público): erro do link, sem quebrar, e a
    sessão é desligada no lote."""
    dl, server, _ = build([gated_page()], [gated_page()])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    error = info.value
    assert (error.code, error.stage) == ("contract_changed", "session_document")
    assert error.diagnostic["detail"] == "post_envelope_missing" and error.diagnostic["via"] == "session_document"
    assert error.session_off is True


def test_session_document_404_is_content_unavailable():
    dl, server, _ = build([gated_page()], [html("<html>nada</html>", 404)])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert (info.value.code, info.value.diagnostic["detail"]) == ("content_unavailable", "post_http_404")
    assert getattr(info.value, "session_off", False) is False


def redirect(location: str) -> httpx.Response:
    return httpx.Response(302, headers={"location": location})


REFUSALS = [
    (lambda: redirect(f"https://www.instagram.com/accounts/login/?next=/reel/{CODE}/"), "request_auth_failed",
     "redirect_login"),
    (lambda: redirect("https://www.instagram.com/challenge/FAKE/"), "checkpoint_required", "redirect_challenge"),
    (lambda: redirect("/accounts/suspended/checkpoint/"), "checkpoint_required", "redirect_checkpoint"),
    (lambda: redirect(f"https://www.instagram.com/p/{CODE}/"), "access_refused", "redirect_other"),
    (lambda: redirect(f"https://www.instagram.com/reel/{CODE}/"), "access_refused", "redirect_other"),
    (lambda: html("", 401), "request_auth_failed", "http_401"),
    (lambda: html("<html>Forbidden</html>", 403), "access_refused", "http_403"),
    (lambda: html("", 429), "rate_limited", "http_429"),
    (lambda: httpx.Response(200, json={"message": "checkpoint_required", "checkpoint_url": "/challenge/"}),
     "checkpoint_required", "checkpoint_marker"),
    (lambda: html(post_page(media_node("video", CODE)),
                  **{"set-cookie": 'sessionid=""; Max-Age=0; Path=/; Domain=.instagram.com'}),
     "session_invalid", "sessionid_deleted"),
    (lambda: html("", 503), "network_error", "http_503"),
    (lambda: httpx.Response(200, text="texto", headers={"content-type": "text/plain"}), "contract_changed",
     "document_not_html"),
    (lambda: httpx.Response(200, json={"data": {"xig_fetch_media": None}}), "contract_changed", "document_not_html"),
    (lambda: httpx.ConnectError("falha FAKE"), "network_error", None),
]


@pytest.mark.parametrize("answer, code, detail", REFUSALS, ids=[f"{row[1]}-{row[2]}" for row in REFUSALS])
def test_session_document_refusal_is_classified_without_retry(answer, code, detail):
    dl, server, _ = build([gated_page()], [answer()])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    error = info.value
    assert (error.code, error.stage) == (code, "session_document")
    assert error.diagnostic.get("detail") == detail
    assert error.diagnostic["via"] == "session_document" and error.diagnostic["path"] == "/reels/"
    assert error.session_off is True
    assert "location_path" not in error.diagnostic and CODE not in str(error.diagnostic)
    assert SID not in str(error.diagnostic) and SID not in error.message
    assert "stage=session_document" in safe_diagnostic(error)
    assert len(server.requests) == 2, "UMA tentativa com a sessão, sem retry nem redirect seguido"
    assert server.requests[1].url.path == f"/reels/{CODE}/"


# --------------------------------------------------------------------------- pré-condições


def test_session_off_for_the_batch_makes_no_second_request():
    dl, server, owner = build([gated_page()], [ok_page()])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel", use_session=False)
    error = info.value
    assert (error.code, error.stage) == ("content_unavailable", "post_document")
    assert error.diagnostic == {"detail": "post_gated_session_off", "path": "/reel/"}
    assert "recusou a tentativa com a sessão salva" in error.message
    assert owner.gets == 0 and server.with_cookie == [False]


@pytest.mark.parametrize("cookies", [{}, {"csrftoken": CSRF}], ids=["no_session", "no_sessionid"])
def test_without_saved_session_there_is_no_second_request(cookies):
    def no_session_client(session):
        raise AssertionError("sem sessão não há cliente autenticado")

    server = Server([gated_page()], [])
    dl = InstagramWebDownloader(OwnerSession(cookies), transport=httpx.MockTransport(server.handle),
                                http_factory=no_session_client)
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert info.value.diagnostic == {"detail": "post_gated_logged_out", "path": "/reel/"}
    assert "Salve a sessão em Configurações" in info.value.message and server.with_cookie == [False]


@pytest.mark.parametrize("scope", ["instagram", "session"])
def test_paused_instagram_blocks_the_session_attempt(scope):
    created: list = []
    events: list = []
    server = Server([gated_page()], [ok_page()], events=events)
    dl = InstagramWebDownloader(OwnerSession(), transport=httpx.MockTransport(server.handle),
                                request_gate=Gate(events, scope=scope),
                                http_factory=lambda session: created.append(session))
    with pytest.raises(InstagramPaused) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert info.value.scope == scope and created == [] and server.with_cookie == [False]


# --------------------------------------------------------------------------- cancelamento


def test_cancelled_during_the_public_get_skips_the_session_attempt():
    cancel = threading.Event()
    dl, server, owner = build([gated_page()], [logged_page()], on_request=lambda request: cancel.set())
    assert dl.resolve_post(CODE, cancel=cancel, path_kind="reel") == []
    assert server.with_cookie == [False] and owner.gets == 0, "cancelado: a sessão salva nem é lida"


def test_cancelled_while_waiting_the_page_cap_for_the_session_get():
    cancel = threading.Event()
    events: list = []
    gate = Gate(events, on_wait=lambda n: cancel.set() if n == 2 else None)
    dl, server, _ = build([gated_page()], [logged_page()], gate=gate, events=events)
    assert dl.resolve_post(CODE, cancel=cancel, path_kind="reel") == []
    assert server.with_cookie == [False] and events == ["wait_page", "slot", "get", "wait_page"]


def test_session_client_gets_the_cancel_event_and_is_closed():
    class FakeHttp:
        cancel = None
        closed = 0

        def get_document(self, path, *, referer=None, max_bytes=None, diagnostic_path=None):
            self.seen = (path, referer, diagnostic_path)
            raise InstagramWebError("timeout", "FAKE", retryable=True, stage="http")

        def close(self):
            self.closed += 1

    logged = FakeHttp()
    server = Server([gated_page()], [])
    cancel = threading.Event()
    dl = InstagramWebDownloader(OwnerSession(), transport=httpx.MockTransport(server.handle),
                                http_factory=lambda session: logged)
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, cancel=cancel, path_kind="p")
    assert (info.value.code, info.value.stage, info.value.session_off) == ("timeout", "session_document", True)
    assert logged.seen == (f"/p/{CODE}/", None, "/p/") and logged.cancel is cancel and logged.closed == 1


# --------------------------------------------------------------------------- documento logado (observado 2026-10-29)


def test_feed_of_reels_never_becomes_media():
    """Só o feed (mesmo código e outro), sem ``xig_fetch_media``: nada vira mídia, nem como fallback."""
    feed_only = html(post_html([data_sjs_script(__import__("post_document_fixtures").feed_payload(
        [session_media_node(CODE), session_media_node(OTHER)]))]))
    dl, server, _ = build([gated_page()], [feed_only])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert (info.value.code, info.value.diagnostic["detail"]) == ("contract_changed", "post_envelope_missing")


def test_session_node_of_another_post_is_mismatch():
    dl, *_ = build([gated_page()], [logged_page(session_media_node(OTHER))])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert info.value.diagnostic == {"detail": "post_mismatch", "via": "session_document"}
    assert info.value.session_off is True


def test_session_node_with_dash_manifest_is_read(caplog):
    """DASH só vale quando supera o maior MP4 declarado (regra existente): MP4 até 720x1280, DASH 1080x1920."""
    node = session_media_node(CODE)
    for version, (w, h) in zip(node["video_versions"], ((720, 1280), (640, 1136), (480, 852)), strict=True):
        version["width"], version["height"] = w, h
    node["video_dash_manifest"] = simple_mpd(CODE)
    dl, *_ = build([gated_page()], [logged_page(node)])
    [item] = dl.resolve_post(CODE, path_kind="reel")
    assert item.dash_video_url and item.dash_audio_url and (item.dash_width, item.dash_height) == (1080, 1920)
    assert "preel1_101.mp4" in item.media_url, "o MP4 direto de reserva é o maior (720x1280)"
    assert "dash=ok:1 dash_escolhido=1 via=session_document" in caplog.text


@pytest.mark.parametrize("target", ["reels", "reel"])
def test_p_link_follows_one_redirect_to_the_same_post(target):
    events: list = []
    gate = Gate(events)
    answers = [redirect(f"https://www.instagram.com/{target}/{CODE}/?igsh=FAKE"), logged_page()]
    dl, server, _ = build([gated_page()], answers, gate=gate, events=events)
    [item] = dl.resolve_post(CODE, path_kind="p")
    assert item.source_post_id == CODE
    assert [r.url.path for r in server.requests] == [f"/p/{CODE}/", f"/p/{CODE}/", f"/{target}/{CODE}/"]
    assert server.with_cookie == [False, True, True]
    assert events == ["wait_page", "slot", "get"] * 3, "o 2o GET logado também passa pelo teto e pelo portão"


def test_p_link_second_redirect_is_not_followed():
    answers = [redirect(f"/reels/{CODE}/"), redirect(f"/reel/{CODE}/")]
    dl, server, _ = build([gated_page()], answers)
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="p")
    assert (info.value.code, info.value.diagnostic["detail"]) == ("access_refused", "redirect_other")
    assert info.value.diagnostic["path"] == "/reels/" and len(server.requests) == 3


@pytest.mark.parametrize("location, code, detail", [
    (f"/reels/{OTHER}/", "access_refused", "redirect_other"),
    (f"/reels/{CODE.lower()}/", "access_refused", "redirect_other"),
    ("/accounts/login/?next=/p/", "request_auth_failed", "redirect_login"),
    ("/challenge/FAKE/", "checkpoint_required", "redirect_challenge"),
])
def test_p_link_other_redirects_are_classified_without_following(location, code, detail):
    dl, server, _ = build([gated_page()], [redirect(location)])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="p")
    assert (info.value.code, info.value.diagnostic["detail"]) == (code, detail)
    assert info.value.session_off is True and len(server.requests) == 2
    assert OTHER not in str(info.value.diagnostic) and "location_path" not in info.value.diagnostic


def test_redirect_deleting_the_sessionid_is_not_followed():
    answer = httpx.Response(302, headers={"location": f"/reels/{CODE}/",
                                          "set-cookie": 'sessionid=""; Max-Age=0; Path=/; Domain=.instagram.com'})
    dl, server, _ = build([gated_page()], [answer])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="p")
    assert info.value.code == "session_invalid" and len(server.requests) == 2


def test_cancelled_while_waiting_the_page_cap_of_the_followed_redirect():
    cancel = threading.Event()
    events: list = []
    gate = Gate(events, on_wait=lambda n: cancel.set() if n == 3 else None)
    dl, server, _ = build([gated_page()], [redirect(f"/reels/{CODE}/"), logged_page()], gate=gate, events=events)
    assert dl.resolve_post(CODE, cancel=cancel, path_kind="p") == []
    assert len(server.requests) == 2


@pytest.mark.parametrize("location", [
    "https://evil.example/reels/{code}/", "http://www.instagram.com/reels/{code}/", "//evil.example/reels/{code}/",
    "https://www.instagram.com.evil.example/reels/{code}/",
])
def test_p_link_redirect_to_another_host_is_not_followed(location):
    dl, server, _ = build([gated_page()], [redirect(location.format(code=CODE))])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="p")
    assert (info.value.code, info.value.diagnostic["detail"]) == ("access_refused", "redirect_other")
    assert len(server.requests) == 2 and "evil" not in str(info.value.diagnostic)


def test_p_link_redirect_on_instagram_com_without_www_is_followed():
    dl, server, _ = build([gated_page()], [redirect(f"https://instagram.com/reels/{CODE}/"), logged_page()])
    assert len(dl.resolve_post(CODE, path_kind="p")) == 1 and len(server.requests) == 3


def test_reel_format_failure_reports_the_requested_path():
    dl, *_ = build([gated_page()], [gated_page()])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert info.value.diagnostic["path"] == "/reels/" and "path=/reels/" in safe_diagnostic(info.value)


def test_followed_redirect_failure_reports_the_final_path():
    dl, *_ = build([gated_page()], [redirect(f"/reels/{CODE}/"), logged_null()])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="p")
    assert info.value.diagnostic["detail"] == "post_gated_with_session" and info.value.diagnostic["path"] == "/reels/"


def test_session_off_error_is_marked_to_skip_the_breaker():
    dl, *_ = build([gated_page()], [])
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel", use_session=False)
    assert info.value.session_off is True
