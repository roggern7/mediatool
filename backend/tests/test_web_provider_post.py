"""InstagramWebDownloader.resolve_post: documento público da publicação, lido por um cliente ANÔNIMO (sem a sessão
do dono), sobre ``httpx.MockTransport`` (offline; só valores FAKE).

Verificado no Instagram real só para um Reel público (2026-10-10); foto e carrossel aqui são hipótese (formato v1).
"""

from __future__ import annotations

import logging
import threading
from contextlib import nullcontext

import httpx
import pytest

from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.session import InstagramSession
from app.integrations.instagram.web import provider as provider_mod
from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.post_document import MAX_DOCUMENT_BYTES
from app.integrations.instagram.web.provider import InstagramWebDownloader
from app.jobs.errors import safe_diagnostic
from post_document_fixtures import data_sjs_script, envelope, media_node, media_payload, post_html, post_page

CODE = "FAKEREEL001"
OTHER = "FAKEREEL002"
SID = "FAKE-SESSIONID-POSTDOC-0001"
DS_USER = "10000000987"
CSRF = "FAKE-CSRF-POSTDOC-0001"
SESSION_CODES = ("session_missing", "session_invalid", "session_expired", "request_auth_failed",
                 "checkpoint_required", "action_blocked")


class OwnerSession:
    """Sessão do dono configurada (sessionid FAKE). ``gets`` conta quantas vezes ela foi lida."""

    def __init__(self):
        self.gets = 0

    def get(self):
        self.gets += 1
        return InstagramSession({"sessionid": SID, "ds_user_id": DS_USER, "csrftoken": CSRF})


class Gate:
    def __init__(self, events: list, on_wait=None):
        self.events = events
        self.calls: list[tuple[object, int]] = []
        self.slots: list[object] = []
        self.on_wait = on_wait

    def wait_listing_page(self, cancel=None, *, max_per_hour):
        self.calls.append((cancel, max_per_hour))
        self.events.append("wait_page")
        if self.on_wait is not None:
            self.on_wait()

    def slot(self, cancel=None):
        self.slots.append(cancel)
        self.events.append("slot")
        return nullcontext()


def html(body: str, status: int = 200, **headers) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8", **headers})


class Server:
    def __init__(self, *answers, events: list | None = None):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []
        self.events = events if events is not None else []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.events.append("get")
        if not self.answers:
            raise AssertionError("requisição inesperada")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def paths(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]


def build(*answers, gate=None, env=None, events=None):
    server = Server(*answers, events=events)
    owner = OwnerSession()
    dl = InstagramWebDownloader(owner, transport=httpx.MockTransport(server.handle), request_gate=gate, env=env)
    return dl, server, owner


def page(kind: str = "video", code: str = CODE, **kw) -> httpx.Response:
    return html(post_page(media_node(kind, code, **kw)))


@pytest.fixture(autouse=True)
def _logs(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    # Como no app (core/logging): httpx/httpcore registram a URL completa em INFO/DEBUG e ficam em WARNING.
    for noisy in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(noisy), "level", logging.WARNING)
    monkeypatch.setattr(provider_mod, "_rate_limit_disabled_logged", False)
    yield
    for secret in (CODE, OTHER, "cdninstagram", "fbcdn", "FAKE_OH", "dono_fake", "Dono_Fake", SID, DS_USER, CSRF,
                   "Legenda"):
        assert secret not in caplog.text, secret


# --------------------------------------------------------------------------- o Reel observado


def test_reel_is_normalized_from_the_public_document():
    dl, server, _ = build(page())
    [item] = dl.resolve_post(CODE, path_kind="reel")
    assert item.media_type == "video" and item.is_reel is True and item.extension == "mp4"
    assert item.source_url == f"https://www.instagram.com/reel/{CODE}/" and item.source_post_id == CODE
    assert item.source_profile == "dono_fake" and item.listing_ref is None and item.source_media_index == 0
    # As versões de vídeo vêm sem largura/altura: vale a 1a com URL https e o tamanho original da publicação.
    assert (item.width, item.height) == (1080, 1920) and "preel1_101.mp4" in item.media_url
    assert "preel1_640x1136.jpg" in item.thumbnail_url and item.duration is None
    assert item.taken_at == "2023-11-14T22:13:20Z" and item.post_id == "3200000000000000001"
    assert server.paths == [("GET", f"/reel/{CODE}/")], "UM GET, no caminho do tipo do link"


@pytest.mark.parametrize("path_kind", ["p", "reel"])
def test_path_follows_the_link_kind_given_by_the_caller(path_kind):
    dl, server, _ = build(page())
    assert len(dl.resolve_post(CODE, path_kind=path_kind)) == 1
    assert server.paths == [("GET", f"/{path_kind}/{CODE}/")]
    request = server.requests[0]
    assert request.url.host == "www.instagram.com" and request.url.query == b""
    assert request.headers["sec-fetch-site"] == "none" and "referer" not in request.headers


@pytest.mark.parametrize("path_kind", [None, "", "reels", "tv", "P", "/p/", 1])
def test_unknown_path_kind_is_invalid_input_without_request(path_kind):
    dl, server, _ = build()
    with pytest.raises(InstagramError) as info:
        dl.resolve_post(CODE, path_kind=path_kind)
    assert info.value.code == "invalid_input" and server.requests == []


def test_invalid_shortcode_makes_no_request():
    dl, server, _ = build()
    for bad in ("../x", "abc", "", "ABC/DEFGH", None):
        with pytest.raises(InstagramError) as info:
            dl.resolve_post(bad, path_kind="p")
        assert info.value.code == "invalid_input"
    assert server.requests == []


def test_log_has_only_counts_and_fixed_tokens(caplog):
    dl, *_ = build(page("carousel"))
    assert len(dl.resolve_post(CODE, path_kind="p")) == 4
    assert ("Publicação (documento público): outcome=ok scripts=39 envelopes=1 preloader_observado=True "
            "typename=XIGPolarisVideoMedia") in caplog.text
    assert "Publicação resolvida (web): 4 mídias, 0 sem URL, 0 não suportadas" in caplog.text


# --------------------------------------------------------------------------- sem a sessão do dono


def test_anonymous_client_never_sends_the_owner_cookies():
    """Item 5 do "done": com a sessão do dono configurada, o transporte anônimo nunca recebe header Cookie."""
    dl, server, owner = build(page(), page())
    dl.resolve_post(CODE, path_kind="reel")
    dl.resolve_post(CODE, path_kind="p")
    assert owner.gets == 0, "a sessão salva nem é lida"
    assert len(server.requests) == 2
    for request in server.requests:
        assert "cookie" not in request.headers
        sent = " ".join(f"{name}={value}" for name, value in request.headers.items()) + str(request.url)
        for secret in (SID, DS_USER, CSRF, "sessionid", "ds_user_id", "csrftoken"):
            assert secret not in sent, secret
        assert "x-csrftoken" not in request.headers and "x-ig-app-id" not in request.headers


def test_cookies_set_by_the_anonymous_answer_do_not_leak_to_the_next_post():
    first = html(post_page(media_node("video", CODE)), **{"set-cookie": "csrftoken=FAKE-ANON-CSRF-0001; Path=/"})
    dl, server, _ = build(first, page())
    dl.resolve_post(CODE, path_kind="reel")
    dl.resolve_post(CODE, path_kind="reel")
    assert "cookie" not in server.requests[1].headers, "um cliente novo (jar vazio) por publicação"


def test_works_without_any_saved_session():
    class NoSession:
        def get(self):
            raise AssertionError("a sessão não deve ser consultada")

    server = Server(page())
    dl = InstagramWebDownloader(NoSession(), transport=httpx.MockTransport(server.handle))
    assert len(dl.resolve_post(CODE, path_kind="reel")) == 1
    assert InstagramWebDownloader.resolve_post_authenticated is False


# --------------------------------------------------------------------------- mapa de erros do documento


def redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


ERROR_ROWS = [
    # resposta, code, detail, retryable
    (lambda: redirect(f"https://www.instagram.com/accounts/login/?next=/reel/{CODE}/"), "access_refused",
     "redirect_login", False),
    (lambda: redirect("https://www.instagram.com/challenge/FAKE/"), "access_refused", "redirect_challenge", False),
    (lambda: redirect("/accounts/suspended/checkpoint/"), "access_refused", "redirect_checkpoint", False),
    (lambda: redirect("https://www.instagram.com/consent/?flow=fake"), "access_refused", "redirect_consent", False),
    (lambda: redirect("https://www.instagram.com/"), "access_refused", "redirect_home", False),
    (lambda: redirect(f"https://www.instagram.com/reel/{CODE}/", 301), "access_refused", "redirect_other", False),
    (lambda: html("", 429), "rate_limited", "http_429", True),
    (lambda: httpx.Response(400, json={"message": "Please wait a few minutes before you try again.",
                                       "status": "fail"}), "rate_limited", "please_wait", True),
    (lambda: html("<html>login</html>", 401), "access_refused", "http_401", False),
    (lambda: html("<html>Forbidden</html>", 403), "access_refused", "http_403", False),
    (lambda: html("<html>Página não encontrada</html>", 404), "content_unavailable", "post_http_404", False),
    (lambda: html("", 500), "network_error", "http_500", True),
    (lambda: html("", 503), "network_error", "http_503", True),
    (lambda: html("", 400), "contract_changed", "http_400", False),
    (lambda: html("", 410), "contract_changed", "http_410", False),
    (lambda: httpx.Response(200, json={"data": {}}), "contract_changed", "document_not_html", False),
    (lambda: httpx.Response(200, text="texto", headers={"content-type": "text/plain"}), "contract_changed",
     "document_not_html", False),
]


@pytest.mark.parametrize("answer, code, detail, retryable", ERROR_ROWS, ids=[row[2] for row in ERROR_ROWS])
def test_document_error_map(answer, code, detail, retryable):
    dl, server, owner = build(answer())
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    error = info.value
    assert (error.code, error.diagnostic["detail"], error.retryable, error.stage) == (
        code, detail, retryable, "post_document")
    assert error.code not in SESSION_CODES
    assert error.diagnostic["path"] == "/reel/", "token fixo; nunca o path com o shortcode"
    assert "location_path" not in error.diagnostic and "sessionid_deleted" not in error.diagnostic
    safe = safe_diagnostic(error)
    assert f"stage=post_document status={error.diagnostic['status']}" in safe and "path=/reel/" in safe
    assert f"detail={detail}" in safe
    for secret in (CODE, SID):
        assert secret not in safe and secret not in str(error.diagnostic) and secret not in error.message
    assert len(server.requests) == 1 and owner.gets == 0, "sem redirect seguido, sem nova tentativa"


def test_redirect_reports_its_kind_and_is_never_followed():
    dl, server, _ = build(redirect(f"https://www.instagram.com/accounts/login/?next=%2Freel%2F{CODE}%2F"))
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="p")
    assert info.value.diagnostic["redirect"] == "redirect_login" and info.value.diagnostic["path"] == "/p/"
    assert "redirect=redirect_login" in safe_diagnostic(info.value) and "path=/p/" in safe_diagnostic(info.value)
    assert "sessão inválida" in info.value.message and server.paths == [("GET", f"/p/{CODE}/")]


def test_session_cookie_deleted_by_an_anonymous_answer_is_not_a_session_error():
    """Um Set-Cookie apagando `sessionid` numa resposta anônima não diz nada sobre a sessão do dono."""
    answer = html(post_page(media_node("video", CODE)),
                  **{"set-cookie": 'sessionid=""; Max-Age=0; Path=/; Domain=.instagram.com'})
    dl, *_ = build(answer)
    assert len(dl.resolve_post(CODE, path_kind="reel")) == 1
    refused = html("", 403, **{"set-cookie": 'sessionid=""; Max-Age=0; Path=/'})
    dl, *_ = build(refused)
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert info.value.code == "access_refused"


@pytest.mark.parametrize("failure, code", [(httpx.ConnectError("falha FAKE"), "network_error"),
                                           (httpx.ReadTimeout("falha FAKE"), "timeout")])
def test_transport_failures_keep_the_shortcode_out_of_the_diagnostic(failure, code):
    dl, server, _ = build(failure)
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert info.value.code == code and info.value.retryable is True
    assert info.value.diagnostic["path"] == "/reel/" and CODE not in str(info.value.diagnostic)


def test_document_larger_than_the_cap_is_contract_changed():
    big = html("<html>" + "x" * (MAX_DOCUMENT_BYTES + 10) + "</html>")
    dl, server, _ = build(big)
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    error = info.value
    assert (error.code, error.stage, error.retryable) == ("contract_changed", "post_document", False)
    assert error.diagnostic == {"detail": "document_too_large", "method": "GET", "path": "/reel/"}
    assert safe_diagnostic(error) == "stage=post_document detail=document_too_large path=/reel/"


# --------------------------------------------------------------------------- envelope


@pytest.mark.parametrize("body", [
    "<html><body>Entrar</body></html>",                                   # sem nenhum script data-sjs
    post_html([]),                                                        # só ruído
    post_html([data_sjs_script(None, raw="{json quebrado")], noise=2),
    post_html([data_sjs_script({"xig_polaris_media": {"code": CODE}})]),   # fora do caminho observado
], ids=["no_scripts", "noise_only", "broken_json", "outside_the_observed_path"])
def test_document_without_the_envelope_is_a_link_contract_error(body):
    dl, server, _ = build(html(body))
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    error = info.value
    assert (error.code, error.stage, error.retryable) == ("contract_changed", "post_document", False)
    assert error.diagnostic["detail"] == "post_envelope_missing" and error.diagnostic["path"] == "/reel/"
    assert set(error.diagnostic) == {"detail", "path", "scripts", "envelopes"}
    assert safe_diagnostic(error) == "stage=post_document detail=post_envelope_missing path=/reel/"
    assert len(server.requests) == 1


@pytest.mark.parametrize("media", [
    envelope(media_node("video", CODE), gating={"fake_ruling": "FAKE"}),
    envelope(None, code=CODE),
], ids=["gating_not_null", "media_null"])
def test_gated_post_without_saved_session_is_content_unavailable_and_asks_for_the_session(media):
    """Sem sessão salva não há 2a tentativa (os casos com a sessão: test_web_provider_post_session.py)."""
    class NoSession:
        def get(self):
            return None

    server = Server(html(post_html([data_sjs_script(media_payload(media))])))
    dl = InstagramWebDownloader(NoSession(), transport=httpx.MockTransport(server.handle))
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="p")
    error = info.value
    assert (error.code, error.stage, error.retryable) == ("content_unavailable", "post_document", False)
    assert error.diagnostic == {"detail": "post_gated_logged_out", "path": "/p/"}
    assert error.message == ("A publicação exige login ou está restrita, e não há sessão do Instagram salva para "
                             "tentar abri-la. Salve a sessão em Configurações e importe este link de novo.")
    assert len(server.requests) == 1


def test_answer_for_another_post_is_post_mismatch():
    dl, server, _ = build(page(code=OTHER))
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert (info.value.code, info.value.stage, info.value.diagnostic) == (
        "contract_changed", "normalize", {"detail": "post_mismatch"})


def test_inner_node_of_another_post_is_post_mismatch():
    body = post_html([data_sjs_script(media_payload(envelope(media_node("video", OTHER), code=CODE)))])
    dl, *_ = build(html(body))
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert info.value.diagnostic == {"detail": "post_mismatch"}


def test_two_envelopes_resolve_the_requested_one():
    scripts = [data_sjs_script(media_payload(envelope(media_node("photo", OTHER)))),
               data_sjs_script(media_payload(envelope(media_node("video", CODE))))]
    dl, *_ = build(html(post_html(scripts)))
    [item] = dl.resolve_post(CODE, path_kind="reel")
    assert item.source_post_id == CODE and item.media_type == "video"


# --------------------------------------------------------------------------- nó sem mídia (inalterado)


def node_page(node: dict) -> httpx.Response:
    return html(post_page(node, code=CODE))


def test_node_without_code_is_post_shape_unknown():
    node = media_node("video", CODE)
    del node["code"]
    dl, *_ = build(node_page(node))
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert info.value.code == "contract_changed" and info.value.diagnostic["detail"] == "post_shape_unknown"
    assert info.value.stage == "normalize"


def test_only_unsupported_media_type():
    node = {**media_node("video", CODE), "media_type": 99}
    dl, *_ = build(node_page(node))
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert info.value.code == "unsupported_media" and info.value.diagnostic["detail"] == "unsupported_media"


def test_video_without_usable_url_is_content_unavailable():
    node = media_node("video", CODE)
    node["video_versions"] = [{"type": 101, "url": "http://video-fake.fbcdn.net/v/sem_https.mp4"}, {"type": 102}]
    dl, *_ = build(node_page(node))
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE, path_kind="reel")
    assert info.value.code == "content_unavailable" and info.value.diagnostic["detail"] == "post_no_media"


def test_owner_absent_gives_empty_source_profile():
    node = media_node("video", CODE)
    del node["user"]
    dl, *_ = build(node_page(node))
    assert dl.resolve_post(CODE, path_kind="reel")[0].source_profile == ""


# --------------------------------------------------------------------------- formas NÃO observadas (hipótese v1)


def test_photo_hypothesis():
    dl, *_ = build(page("photo"))
    [item] = dl.resolve_post(CODE, path_kind="p")
    assert item.media_type == "image" and not item.is_reel and (item.width, item.height) == (1080, 1350)
    assert item.source_url == f"https://www.instagram.com/p/{CODE}/" and "pimg1_1080x1350.jpg" in item.media_url


def test_carousel_hypothesis_returns_all_items_in_order():
    dl, server, _ = build(page("carousel"))
    items = dl.resolve_post(CODE, path_kind="p")
    assert [(i.source_media_index, i.media_type, i.in_carousel) for i in items] == [
        (0, "image", True), (1, "video", True), (2, "image", True), (3, "video", True)]
    assert all(i.source_post_id == CODE and not i.is_reel for i in items) and len(server.requests) == 1


def test_plain_video_post_is_not_reel():
    dl, *_ = build(page(product_type="feed"))
    [item] = dl.resolve_post(CODE, path_kind="p")
    assert item.media_type == "video" and not item.is_reel
    assert item.source_url == f"https://www.instagram.com/p/{CODE}/"


# --------------------------------------------------------------------------- cancelamento e portão


def test_cancelled_before_start_does_nothing():
    cancel = threading.Event()
    cancel.set()
    events: list = []
    gate = Gate(events)
    dl, server, _ = build(page(), gate=gate, events=events)
    assert dl.resolve_post(CODE, cancel=cancel, path_kind="reel") == []
    assert server.requests == [] and events == []


@pytest.mark.parametrize("env, expected", [(None, 600), ({"INSTAGRAM_PAGE_RATE_LIMIT": "5"}, 5)])
def test_hourly_page_cap_is_applied_once_before_the_get(env, expected):
    events: list = []
    gate = Gate(events)
    cancel = threading.Event()
    dl, server, _ = build(page(), gate=gate, env=env, events=events)
    assert len(dl.resolve_post(CODE, cancel=cancel, path_kind="reel")) == 1
    assert gate.calls == [(cancel, expected)] and events == ["wait_page", "slot", "get"]
    assert gate.slots == [cancel], "a espera pela vaga no portão respeita o cancelamento da operação"


def test_hourly_page_cap_disabled_by_zero():
    events: list = []
    gate = Gate(events)
    dl, *_ = build(page(), gate=gate, env={"INSTAGRAM_PAGE_RATE_LIMIT": "0"}, events=events)
    assert len(dl.resolve_post(CODE, path_kind="reel")) == 1 and gate.calls == [] and events == ["slot", "get"]


def test_cancelled_while_waiting_the_page_cap_makes_no_request():
    cancel = threading.Event()
    events: list = []
    gate = Gate(events, on_wait=cancel.set)
    dl, server, _ = build(page(), gate=gate, events=events)
    assert dl.resolve_post(CODE, cancel=cancel, path_kind="reel") == []
    assert server.requests == [] and events == ["wait_page"]


def test_public_http_factory_is_used_and_closed_even_on_error():
    class FakeHttp:
        cancel = None
        closed = 0

        def get_document(self, path, *, referer=None, max_bytes=None, diagnostic_path=None):
            self.seen = (path, referer, max_bytes, diagnostic_path)
            raise InstagramWebError("network_error", "FAKE", retryable=True, stage="http")

        def close(self):
            self.closed += 1

    http = FakeHttp()
    cancel = threading.Event()
    dl = InstagramWebDownloader(OwnerSession(), public_http_factory=lambda: http)
    with pytest.raises(InstagramWebError):
        dl.resolve_post(CODE, cancel=cancel, path_kind="reel")
    assert http.seen == (f"/reel/{CODE}/", None, MAX_DOCUMENT_BYTES, "/reel/")
    assert http.closed == 1 and http.cancel is cancel
