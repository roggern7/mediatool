"""InstagramWebDownloader com fakes de http/bootstrap/graphql (offline; valores FAKE)."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integrations.instagram import downloader as downloader_mod
from app.integrations.instagram.downloader import InstagramError, RemoteMedia
from app.integrations.instagram.session import InstagramSession
from app.integrations.instagram.web import provider as provider_mod
from app.integrations.instagram.web.bootstrap import WebState
from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.http import WebResponse
from app.integrations.instagram.web.provider import InstagramWebDownloader
from app.services.cdn_downloader import DownloadError
from media_fakes import FAKE_SESSION

CDN = "https://scontent.cdninstagram.com/v/t51/FAKE"
JARGON = ("bootstrap", "GraphQL", "REST", "gallery-dl", "fb_dtsg", "lsd", "doc_id")


def web_state(viewer_id: str | None = "10000000001") -> WebState:
    return WebState("FAKE_DTSG_0001", "FAKE_LSD_0001", "2", viewer_id, None, 0.0)


# --------------------------------------------------------------------------- fakes


ACCOUNT_ID = "70000000001"   # ds_user_id FAKE da sessão de teste (igual ao xdt_viewer de viewer_match.json)


class FakeProvider:
    def __init__(self, configured: bool = True, cookies: dict | None = None):
        self.configured = configured
        self.cookies = cookies or {"sessionid": FAKE_SESSION}

    def get(self):
        return InstagramSession(dict(self.cookies)) if self.configured else None


def response(status: int = 200, text: str = "{}", *, content_type: str = "application/json",
             location: str | None = None) -> WebResponse:
    return WebResponse(status=status, location_path=location, content_type=content_type, elapsed_ms=5,
                       set_cookie_names=[], sessionid_deleted=False, text=text, path="/web/search/topsearch/")


class FakeHttp:
    def __init__(self, api_response: WebResponse | None = None, account: str | None = None):
        self.api_response = api_response or response()
        self.account = account
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def account_id(self):
        return self.account

    def get_api(self, path, params=None, *, referer=None):
        self.calls.append((path, dict(params or {})))
        return self.api_response

    def close(self):
        self.closed = True


class FakeBootstrap:
    def __init__(self, state=None, error: Exception | None = None):
        self.state = state if state is not None else web_state()
        self.error = error
        self.calls = 0

    def get(self, *, force=False):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.state


class FakeGraphQL:
    """Entrega páginas sob demanda, como o real: a página N só é pedida quando o consumidor quer mais nós.

    Emula current_page/on_page/on_page_end/start_cursor do graphql real (cursores FAKE_CURSOR_<n>)."""

    def __init__(self, pages: list[list[dict]] | None = None, *, error_on_page: int | None = None,
                 error: Exception | None = None, viewer_id: str | None = None):
        self.pages = pages or []
        self.last_viewer_id = viewer_id
        self.error_on_page = error_on_page
        self.error = error
        self.requests: list[dict] = []
        self.current_page = None

    def iter_profile_posts(self, username, *, max_media, page_size=12, max_pages=500, cancel=None, on_page=None,
                           start_cursor=None, start_page=1, on_page_end=None):
        from app.integrations.instagram.web.graphql import PageState

        after = start_cursor
        last = start_page + max_pages - 1
        for number in range(start_page, last + 1):
            if cancel is not None and cancel.is_set():
                return
            self.requests.append({"username": username, "page": number, "max_media": max_media,
                                  "page_size": page_size, "max_pages": max_pages, "after": after})
            if self.error is not None and (self.error_on_page is None or self.error_on_page == number):
                raise self.error
            if number > len(self.pages):
                return
            has_next = number < len(self.pages)
            count = max(1, min(page_size, max_media)) if number == 1 and after is None and max_media else page_size
            end = f"FAKE_CURSOR_{number}" if has_next else None
            self.current_page = PageState(number, after, count, end, has_next, 0)
            if on_page is not None:
                on_page({"page_index": number, "duplicate_nodes": 0})
            yield from self.pages[number - 1]
            reason = "end_of_profile" if not has_next else ("budget_exhausted" if number == last else None)
            if on_page_end is not None and not on_page_end(reason):
                return
            if reason is not None:
                return
            after = end


def build(pages=None, *, http=None, bootstrap=None, graphql=None, configured=True, cdn=None):
    http = http or FakeHttp()
    bootstrap = bootstrap or FakeBootstrap()
    graphql = graphql or FakeGraphQL(pages)
    dl = InstagramWebDownloader(
        FakeProvider(configured),
        cdn_downloader=cdn,
        http_factory=lambda session: http,
        bootstrap_factory=lambda h: bootstrap,
        graphql_factory=lambda h, b: graphql,
    )
    return dl, http, bootstrap, graphql


def candidates(url: str, w: int = 640, h: int = 640) -> dict:
    return {"candidates": [{"url": url + "-small.jpg", "width": 150, "height": 150},
                           {"url": url + ".jpg", "width": w, "height": h}]}


def image_node(code: str, pk: str = "1") -> dict:
    return {"code": code, "pk": pk, "media_type": 1, "taken_at": 1700000000, "product_type": "feed",
            "image_versions2": candidates(f"{CDN}/{code}")}


def video_node(code: str, pk: str = "2", clips: bool = True) -> dict:
    return {"code": code, "pk": pk, "media_type": 2, "taken_at": 1700000000,
            "product_type": "clips" if clips else "feed", "video_duration": 12.5,
            "video_versions": [{"url": f"{CDN}/{code}.mp4", "width": 720, "height": 1280}],
            "image_versions2": candidates(f"{CDN}/{code}-thumb")}


def carousel_node(code: str, pk: str = "3") -> dict:
    children = [
        {"pk": f"{pk}1", "media_type": 1, "image_versions2": candidates(f"{CDN}/{code}-0")},
        {"pk": f"{pk}2", "media_type": 2, "video_versions": [{"url": f"{CDN}/{code}-1.mp4", "width": 1, "height": 1}],
         "image_versions2": candidates(f"{CDN}/{code}-1t")},
        {"pk": f"{pk}3", "media_type": 1, "image_versions2": candidates(f"{CDN}/{code}-2")},
    ]
    return {"code": code, "pk": pk, "media_type": 8, "taken_at": 1700000000, "product_type": "carousel_container",
            "carousel_media": children}


def images(prefix: str, count: int) -> list[dict]:
    return [image_node(f"{prefix}{i}", pk=f"{prefix}{i}") for i in range(count)]


# --------------------------------------------------------------------------- listagem


def test_empty_profile_lists_nothing_and_closes_http():
    dl, http, bootstrap, graphql = build([[]])
    assert list(dl.iter_profile_media("perfil_teste", "all", None)) == []
    assert bootstrap.calls == 1 and len(graphql.requests) == 1 and http.closed


def test_image_video_and_carousel_are_normalized():
    dl, *_ = build([[image_node("IMG1"), video_node("VID1"), carousel_node("CAR1")]])
    items = list(dl.iter_profile_media("perfil_teste", "all", None))
    assert [(i.source_post_id, i.source_media_index, i.media_type) for i in items] == [
        ("IMG1", 0, "image"), ("VID1", 0, "video"),
        ("CAR1", 0, "image"), ("CAR1", 1, "video"), ("CAR1", 2, "image"),
    ]
    image, video = items[0], items[1]
    assert image.media_url == f"{CDN}/IMG1.jpg" and image.source_url == "https://www.instagram.com/p/IMG1/"
    assert image.source_profile == "perfil_teste" and image.taken_at.endswith("Z")
    assert video.is_reel and video.source_url == "https://www.instagram.com/reel/VID1/"
    assert video.duration == 12.5 and video.thumbnail_url == f"{CDN}/VID1-thumb.jpg"


@pytest.mark.parametrize("content, expected", [
    ("reels", ["VID1"]),
    ("videos", ["VID1", "CAR1"]),
    ("images", ["IMG1", "CAR1", "CAR1"]),
])
def test_content_filter(content, expected):
    dl, *_ = build([[image_node("IMG1"), video_node("VID1"), carousel_node("CAR1")]])
    assert [i.source_post_id for i in dl.iter_profile_media("perfil_teste", content, None)] == expected


def test_limit_one_requests_a_single_page():
    dl, http, _, graphql = build([images("a", 4), images("b", 4)])
    items = list(dl.iter_profile_media("perfil_teste", "all", 1))
    assert len(items) == 1 and len(graphql.requests) == 1
    assert graphql.requests[0]["max_media"] == 1 and http.closed


def test_limit_ten_counts_media_and_stops_without_extra_pages():
    dl, _, _, graphql = build([images("a", 4), images("b", 4), images("c", 4), images("d", 4)])
    items = list(dl.iter_profile_media("perfil_teste", "all", 10))
    assert len(items) == 10 and len(graphql.requests) == 3


def test_limit_counts_carousel_media_not_posts():
    dl, _, _, graphql = build([[carousel_node("CAR1"), carousel_node("CAR2", pk="9")], [image_node("X")]])
    items = list(dl.iter_profile_media("perfil_teste", "all", 4))
    assert [(i.source_post_id, i.source_media_index) for i in items] == [
        ("CAR1", 0), ("CAR1", 1), ("CAR1", 2), ("CAR2", 0)]
    assert len(graphql.requests) == 1


def test_limit_none_reads_everything_up_to_safety_cap(monkeypatch):
    dl, _, _, graphql = build([images("a", 3), images("b", 3)])
    assert len(list(dl.iter_profile_media("perfil_teste", "all", None))) == 6
    assert graphql.requests[0]["max_media"] == downloader_mod.MAX_ITEMS == 10000

    monkeypatch.setattr(provider_mod, "MAX_ITEMS", 4)
    dl, _, _, graphql = build([images("a", 3), images("b", 3), images("c", 3)])
    assert len(list(dl.iter_profile_media("perfil_teste", "all", None))) == 4
    assert len(graphql.requests) == 2


def test_cancel_stops_listing_between_requests():
    cancel = threading.Event()
    dl, http, _, graphql = build([images("a", 2), images("b", 2), images("c", 2)])
    received = []
    for item in dl.iter_profile_media("perfil_teste", "all", None, cancel=cancel):
        received.append(item)
        cancel.set()
    assert len(received) == 1 and len(graphql.requests) == 1 and http.closed


def test_cancel_before_start_makes_no_request():
    cancel = threading.Event()
    cancel.set()
    dl, _, bootstrap, graphql = build([images("a", 2)])
    assert list(dl.iter_profile_media("perfil_teste", "all", None, cancel=cancel)) == []
    assert bootstrap.calls == 0 and graphql.requests == []


def test_listing_errors_propagate_with_code():
    error = InstagramWebError("rate_limited", "429 FAKE", retryable=True, stage="graphql")
    dl, http, _, _ = build(graphql=FakeGraphQL([images("a", 1)], error_on_page=1, error=error))
    with pytest.raises(InstagramError) as info:
        list(dl.iter_profile_media("perfil_teste", "all", None))
    assert info.value.code == "rate_limited" and info.value.retryable and http.closed


def unsupported_node(code: str) -> dict:
    return {"code": code, "pk": code, "media_type": 99, "taken_at": 1700000000}


def test_listing_with_only_unsupported_media_raises_unsupported_media():
    dl, http, _, _ = build([[unsupported_node("UNK1"), unsupported_node("UNK2")]])
    with pytest.raises(InstagramError) as info:
        list(dl.iter_profile_media("perfil_teste", "all", None))
    assert info.value.code == "unsupported_media" and not info.value.retryable and http.closed
    assert not [word for word in JARGON if word in info.value.message]


def test_unsupported_media_is_skipped_when_something_else_is_delivered():
    dl, *_ = build([[unsupported_node("UNK1"), image_node("IMG1")]])
    assert [i.source_post_id for i in dl.iter_profile_media("perfil_teste", "all", None)] == ["IMG1"]
    dl, *_ = build([[]])
    assert list(dl.iter_profile_media("perfil_teste", "all", None)) == []


def test_listing_input_validation_and_missing_session():
    dl, *_ = build([[]])
    for args in (("perfil teste", "all", None), ("perfil_teste", "stories", None), ("perfil_teste", "all", 0)):
        with pytest.raises(InstagramError) as info:
            dl.iter_profile_media(*args)
        assert info.value.code == "invalid_input"
    dl, *_ = build([[]], configured=False)
    with pytest.raises(InstagramError) as info:
        list(dl.iter_profile_media("perfil_teste", "all", None))
    assert info.value.code == "session_missing"


# --------------------------------------------------------------------------- perfil


def _topsearch(*users: dict) -> WebResponse:
    import json

    return response(text=json.dumps({"users": [{"position": i, "user": u} for i, u in enumerate(users)],
                                     "status": "ok"}))


def test_get_profile_uses_topsearch_exact_match():
    http = FakeHttp(_topsearch(
        {"pk": "11", "username": "perfil_teste2", "full_name": "Outro"},
        {"pk": "12", "username": "Perfil_Teste", "full_name": "Perfil Teste", "is_private": False,
         "is_verified": True, "profile_pic_url": "https://scontent.cdninstagram.com/FAKE.jpg"},
    ))
    dl, *_ = build(http=http)
    info = dl.get_profile("@perfil_teste")
    assert http.calls == [("/web/search/topsearch/", {"query": "perfil_teste"})]
    assert (info.username, info.user_id, info.full_name, info.is_private, info.is_verified) == (
        "Perfil_Teste", "12", "Perfil Teste", False, True)
    assert http.closed


def test_get_profile_not_found_and_errors():
    dl, *_ = build(http=FakeHttp(_topsearch({"pk": "1", "username": "outro"})))
    with pytest.raises(InstagramError) as info:
        dl.get_profile("perfil_teste")
    assert info.value.code == "profile_not_found"

    dl, *_ = build(http=FakeHttp(response(429, "{}")))
    with pytest.raises(InstagramError) as info:
        dl.get_profile("perfil_teste")
    assert info.value.code == "rate_limited"

    dl, *_ = build(http=FakeHttp(response(200, '{"status": "ok"}')))
    with pytest.raises(InstagramError) as info:
        dl.get_profile("perfil_teste")
    assert info.value.code == "contract_changed" and info.value.retryable is False


# --------------------------------------------------------------------------- download


class FakeCdn:
    def __init__(self, failures: dict[str, str] | None = None):
        self.failures = failures or {}
        self.urls: list[str] = []

    def __call__(self, url, dest_dir, *, max_bytes, cancel=None, progress=None):
        self.urls.append(url)
        if url in self.failures:
            raise DownloadError(self.failures[url], "FAKE")
        path = Path(dest_dir) / "cdn-fake.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xd8\xffFAKE")
        return SimpleNamespace(path=path)


def _stale(code: str, index: int = 0, media_id: str | None = None, media_type: str = "image") -> RemoteMedia:
    return RemoteMedia(source_profile="perfil_teste", source_post_id=code, post_id=None, media_id=media_id,
                       source_media_index=index, media_type=media_type, is_reel=False,
                       source_url=f"https://www.instagram.com/p/{code}/", media_url=f"{CDN}/expired-{code}.jpg",
                       thumbnail_url=None, width=None, height=None, duration=None, taken_at=None, extension="jpg")


def test_download_uses_cdn_without_listing(tmp_path):
    cdn = FakeCdn()
    dl, _, bootstrap, graphql = build([[image_node("IMG1")]], cdn=cdn)
    path = dl.download(_stale("IMG1"), tmp_path)
    assert path.is_file() and cdn.urls == [f"{CDN}/expired-IMG1.jpg"]
    assert bootstrap.calls == 0 and graphql.requests == []


@pytest.mark.parametrize("cdn_code, detail", [("forbidden", "http_403"), ("not_found", "http_404")])
def test_expired_url_is_media_url_expired_without_internal_refresh(tmp_path, cdn_code, detail, caplog):
    caplog.set_level("DEBUG")
    cdn = FakeCdn({f"{CDN}/expired-CAR1.jpg": cdn_code})
    dl, _, bootstrap, graphql = build([[carousel_node("CAR1")]], cdn=cdn)
    with pytest.raises(InstagramError) as info:
        dl.download(_stale("CAR1", index=2, media_id="33"), tmp_path)
    assert info.value.code == "media_url_expired" and not info.value.retryable
    assert info.value.diagnostic["detail"] == detail
    assert cdn.urls == [f"{CDN}/expired-CAR1.jpg"] and bootstrap.calls == 0 and graphql.requests == []
    assert CDN not in caplog.text


def test_download_cancel_is_preserved(tmp_path):
    cdn = FakeCdn({f"{CDN}/expired-IMG1.jpg": "cancelled"})
    dl, *_ = build(cdn=cdn)
    with pytest.raises(DownloadError) as info:
        dl.download(_stale("IMG1"), tmp_path, cancel=threading.Event())
    assert info.value.code == "cancelled"


def test_non_expired_cdn_errors_do_not_refresh(tmp_path):
    cdn = FakeCdn({f"{CDN}/expired-IMG1.jpg": "network_error"})
    dl, _, bootstrap, _ = build([[image_node("IMG1")]], cdn=cdn)
    with pytest.raises(InstagramError) as info:
        dl.download(_stale("IMG1"), tmp_path)
    assert info.value.code == "download_failed" and info.value.retryable and bootstrap.calls == 0


def test_resolve_post_is_explicitly_unavailable():
    dl, _, bootstrap, graphql = build([[image_node("IMG1")]])
    with pytest.raises(InstagramError) as info:
        dl.resolve_post("IMG1abc")
    assert info.value.code == "content_unavailable" and bootstrap.calls == 0 and graphql.requests == []
    with pytest.raises(InstagramError) as info:
        dl.resolve_post("../x")
    assert info.value.code == "invalid_input"


# --------------------------------------------------------------------------- teste de sessão


def _assert_web_fields(result, *, present, bootstrap, graphql, recognized, listing):
    assert (result.session_present, result.bootstrap, result.graphql, result.session_recognized,
            result.listing_api) == (present, bootstrap, graphql, recognized, listing)
    assert isinstance(result.duration_ms, int)
    assert FAKE_SESSION not in result.message and "10000000001" not in result.message
    assert not [word for word in JARGON if word in result.message]


def test_session_test_valid_uses_count_one_without_download(tmp_path):
    cdn = FakeCdn()
    dl, http, bootstrap, graphql = build(
        http=FakeHttp(account=ACCOUNT_ID), cdn=cdn,
        graphql=FakeGraphQL([[image_node("IMG1")], [image_node("IMG2")]], viewer_id=ACCOUNT_ID))
    result = dl.test_session()
    assert result.ok and result.result == "valid" and result.code == "ok"
    _assert_web_fields(result, present=True, bootstrap="ok", graphql="ok", recognized=True, listing="ok")
    assert graphql.requests == [{"username": "instagram", "page": 1, "max_media": 1, "page_size": 1,
                                 "max_pages": 1, "after": None}]
    assert bootstrap.calls == 1 and cdn.urls == [] and http.closed
    assert result.message == provider_mod.VALID_SESSION_MESSAGE and result.username is None


def test_session_test_missing_session():
    dl, _, bootstrap, _ = build([[]], configured=False)
    result = dl.test_session()
    assert result.result == "session_missing" and bootstrap.calls == 0
    _assert_web_fields(result, present=False, bootstrap="unknown", graphql="unknown", recognized=None,
                       listing="unknown")


@pytest.mark.parametrize("code, result_name, recognized", [
    ("session_invalid", "invalid", False),
    ("session_expired", "expired", False),
    ("checkpoint_required", "checkpoint_required", None),
    ("rate_limited", "rate_limited", None),
    ("contract_changed", "instagram_error", None),
    ("network_error", "network_error", None),
    ("timeout", "timeout", None),
])
def test_session_test_bootstrap_failures(code, result_name, recognized):
    bootstrap = FakeBootstrap(error=InstagramWebError(code, "Motivo FAKE.", stage="bootstrap",
                                                      diagnostic={"status": 200}))
    dl, http, _, graphql = build([[]], bootstrap=bootstrap)
    result = dl.test_session()
    assert not result.ok and result.result == result_name and result.code == code
    _assert_web_fields(result, present=True, bootstrap="failed", graphql="unknown", recognized=recognized,
                       listing="rate_limited" if code == "rate_limited" else "unknown")
    assert graphql.requests == [] and http.closed and result.http_status == 200
    assert result.message.startswith("Sessão presente")
    if code not in ("session_invalid", "session_expired"):
        assert "não reconhecida" not in result.message and "inválida" not in result.message


def test_session_test_html_viewer_zero_does_not_abort_and_confirms_by_response():
    # O HTML inicial com viewer "0" não decide nada: a conta é confirmada pela resposta da consulta.
    dl, _, _, graphql = build(bootstrap=FakeBootstrap(web_state(None)), http=FakeHttp(account=ACCOUNT_ID),
                              graphql=FakeGraphQL([[image_node("IMG1")]], viewer_id=ACCOUNT_ID))
    result = dl.test_session()
    assert result.ok and result.result == "valid" and len(graphql.requests) == 1
    _assert_web_fields(result, present=True, bootstrap="ok", graphql="ok", recognized=True, listing="ok")


@pytest.mark.parametrize("account, viewer", [(ACCOUNT_ID, None), (None, ACCOUNT_ID), (None, None)])
def test_session_test_listing_ok_but_account_not_confirmed(account, viewer):
    dl, *_ = build(http=FakeHttp(account=account), graphql=FakeGraphQL([[image_node("IMG1")]], viewer_id=viewer))
    result = dl.test_session()
    assert result.ok and result.result == "valid" and result.code == "ok"
    assert result.message == provider_mod.UNCONFIRMED_SESSION_MESSAGE
    _assert_web_fields(result, present=True, bootstrap="ok", graphql="ok", recognized=None, listing="ok")


def test_session_test_listing_ok_with_other_account():
    dl, *_ = build(http=FakeHttp(account=ACCOUNT_ID),
                   graphql=FakeGraphQL([[image_node("IMG1")]], viewer_id="70000000999"))
    result = dl.test_session()
    assert result.result == "valid" and result.message.startswith("A sessão salva pertence a outra conta")
    assert ACCOUNT_ID not in result.message and "70000000999" not in result.message
    _assert_web_fields(result, present=True, bootstrap="ok", graphql="ok", recognized=False, listing="ok")


def test_session_test_listing_shape_failure_keeps_graphql_ok():
    error = InstagramWebError("contract_changed", "Motivo FAKE.", stage="graphql", diagnostic={"detail": "edges_not_list"})
    result = build(graphql=FakeGraphQL(error=error))[0].test_session()
    assert result.result == "instagram_error"
    _assert_web_fields(result, present=True, bootstrap="ok", graphql="ok", recognized=None, listing="unknown")


@pytest.mark.parametrize("code, result_name, listing", [
    ("contract_changed", "instagram_error", "unknown"),
    ("rate_limited", "rate_limited", "rate_limited"),
    ("access_refused", "api_refused", "refused"),
    ("action_blocked", "restricted", "refused"),
    ("checkpoint_required", "checkpoint_required", "unknown"),
    ("network_error", "network_error", "unknown"),
])
def test_session_test_graphql_failures_leave_session_unconfirmed(code, result_name, listing):
    error = InstagramWebError(code, "Motivo FAKE.", stage="graphql", diagnostic={"detail": "repeated_cursor"})
    dl, *_ = build(graphql=FakeGraphQL(error=error))
    result = dl.test_session()
    assert not result.ok and result.result == result_name and result.code == code
    _assert_web_fields(result, present=True, bootstrap="ok", graphql="failed", recognized=None, listing=listing)
    assert "reconhecida" not in result.message
    assert "inválida" not in result.message and result.detail == "repeated_cursor"


def test_session_test_graphql_auth_refusal_is_not_recognized():
    error = InstagramWebError("session_invalid", "Motivo FAKE.", stage="graphql")
    dl, *_ = build(graphql=FakeGraphQL(error=error))
    result = dl.test_session()
    assert result.result == "invalid"
    _assert_web_fields(result, present=True, bootstrap="ok", graphql="failed", recognized=False, listing="unknown")


def test_session_test_never_raises_on_unexpected_errors():
    dl, *_ = build(graphql=FakeGraphQL(error=RuntimeError("boom")))
    result = dl.test_session()
    assert result.result == "instagram_error" and result.code == "internal_error"
    assert result.bootstrap == "ok" and result.graphql == "failed"


def test_reprs_do_not_leak_tokens():
    dl, *_ = build([[]])
    assert FAKE_SESSION not in repr(dl) and "redacted" in repr(dl)


# --------------------------------------------------------------------------- pilha real (http/bootstrap/graphql)

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web"


class MockInstagram:
    """Transporte httpx em memória: documento "/", POST /graphql/query paginado e topsearch."""

    def __init__(self, pages=("page1_has_next", "page2_last"), bootstrap="bootstrap_input.html"):
        import httpx

        self.pages = [(FIXTURES / "graphql" / f"{name}.json").read_text(encoding="utf-8") for name in pages]
        self.bootstrap = bootstrap
        self.requests: list[tuple[str, str, str]] = []
        self.forms: list[dict] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        import httpx

        self.requests.append((request.method, request.url.host, request.url.path))
        if request.url.path == "/" and request.method == "GET":
            html = (FIXTURES / self.bootstrap).read_text(encoding="utf-8")
            return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})
        if request.url.path == "/graphql/query" and request.method == "POST":
            from urllib.parse import parse_qsl

            self.forms.append(dict(parse_qsl(request.content.decode())))
            body = self.pages[min(sum(1 for r in self.requests if r[2] == "/graphql/query"), len(self.pages)) - 1]
            return httpx.Response(200, text=body, headers={"content-type": "application/json"})
        return httpx.Response(404, text="{}", headers={"content-type": "application/json"})


def real_stack(mock: MockInstagram, cdn=None, cookies=None) -> InstagramWebDownloader:
    # Pilha padrão (http, bootstrap, GraphQL); só o intervalo entre páginas é zerado para o teste não esperar.
    return InstagramWebDownloader(FakeProvider(cookies=cookies), transport=mock.transport, cdn_downloader=cdn,
                                  page_delay=(0, 0))


@pytest.mark.parametrize("page, recognized, message", [
    ("viewer_match", True, provider_mod.VALID_SESSION_MESSAGE),
    ("viewer_other", False, provider_mod.OTHER_ACCOUNT_MESSAGE),
    ("viewer_absent", None, provider_mod.UNCONFIRMED_SESSION_MESSAGE),
])
def test_real_stack_session_test_html_viewer_zero_confirms_by_xdt_viewer(page, recognized, message, caplog):
    caplog.set_level("DEBUG")
    mock = MockInstagram(pages=(page,), bootstrap="bootstrap_viewer_zero.html")
    result = real_stack(mock, cookies={"sessionid": FAKE_SESSION, "ds_user_id": ACCOUNT_ID}).test_session()
    assert result.ok and result.result == "valid" and result.message == message
    _assert_web_fields(result, present=True, bootstrap="ok", graphql="ok", recognized=recognized, listing="ok")
    assert [r[2] for r in mock.requests] == ["/", "/graphql/query"]
    assert mock.forms[0]["av"] == ACCOUNT_ID   # viewer do HTML "0" -> ds_user_id da sessão
    for value in (ACCOUNT_ID, "70000000999", "FAKE_DTSG_ZERO_0007", "FAKE_LSD_ZERO_0007", FAKE_SESSION):
        assert value not in caplog.text and value not in result.message


def test_real_stack_import_with_html_viewer_zero_lists_items():
    mock = MockInstagram(bootstrap="bootstrap_viewer_zero.html")
    items = list(real_stack(mock).iter_profile_media("perfil_teste", "all", None))
    assert items and [r[2] for r in mock.requests] == ["/", "/graphql/query", "/graphql/query"]
    assert mock.forms[0]["av"] == "0"   # sem viewer real no HTML e sem ds_user_id nos cookies


def test_real_stack_lists_all_pages_without_duplicates():
    mock = MockInstagram()
    items = list(real_stack(mock).iter_profile_media("perfil_teste", "all", None))
    codes = [(i.source_post_id, i.source_media_index) for i in items]
    assert len(codes) == len(set(codes)) and codes[0] == ("FAKEIMG0001", 0)
    assert {c for c, _ in codes} == {"FAKEIMG0001", "FAKEREEL001", "FAKECAR0001", "FAKEIMG0002"}
    assert [r[2] for r in mock.requests] == ["/", "/graphql/query", "/graphql/query"]
    assert {r[1] for r in mock.requests} == {"www.instagram.com"}


def test_real_stack_limit_one_makes_one_graphql_request():
    mock = MockInstagram()
    assert len(list(real_stack(mock).iter_profile_media("perfil_teste", "all", 1))) == 1
    assert [r[2] for r in mock.requests] == ["/", "/graphql/query"]


def test_real_stack_session_test_and_refresh(tmp_path, caplog):
    mock = MockInstagram()
    result = real_stack(mock).test_session()
    assert result.ok and (result.bootstrap, result.graphql, result.listing_api) == ("ok", "ok", "ok")
    assert [r[2] for r in mock.requests] == ["/", "/graphql/query"]
    for text in (result.message, repr(result), caplog.text):
        assert "FAKE_DTSG" not in text and "FAKE_LSD" not in text and FAKE_SESSION not in text

    fresh = list(real_stack(MockInstagram()).iter_profile_media("perfil_teste", "all", None))[-1]
    assert fresh.listing_ref is not None and fresh.listing_ref.page_index == 2
    mock = MockInstagram(pages=("page2_last",))
    page = real_stack(mock).refresh_page("perfil_teste", fresh.listing_ref)
    assert [r[2] for r in mock.requests] == ["/", "/graphql/query"]
    assert any((i.source_post_id, i.source_media_index) == (fresh.source_post_id, fresh.source_media_index)
               for i in page)
    assert all(i.listing_ref == fresh.listing_ref for i in page)


def test_page_delay_defaults_to_graphql_and_is_injectable():
    from app.integrations.instagram.web.graphql import DEFAULT_PAGE_DELAY

    session = InstagramSession({"sessionid": FAKE_SESSION})
    for kwargs, expected in (({}, tuple(DEFAULT_PAGE_DELAY)), ({"page_delay": (0, 0)}, (0.0, 0.0))):
        ops = InstagramWebDownloader(FakeProvider(), **kwargs)._build_ops(session)
        try:
            assert ops.graphql._page_delay == expected
        finally:
            ops.http.close()


def test_supported_items_emptied_by_content_filter_are_not_unsupported_media():
    dl, *_ = build([[unsupported_node("UNK1"), image_node("IMG1")]])
    assert list(dl.iter_profile_media("perfil_teste", "reels", None)) == []


@pytest.mark.parametrize("account, viewer", [(" 70000000001 ", "70000000001"), ("70000000001", 70000000001),
                                             ("70000000001\n", " 70000000001")])
def test_session_test_normalizes_ids_before_comparing(account, viewer):
    dl, *_ = build(http=FakeHttp(account=account), graphql=FakeGraphQL([[image_node("IMG1")]], viewer_id=viewer))
    result = dl.test_session()
    assert result.result == "valid" and result.message == provider_mod.VALID_SESSION_MESSAGE
    _assert_web_fields(result, present=True, bootstrap="ok", graphql="ok", recognized=True, listing="ok")


def test_real_stack_partial_login_error_with_data_is_valid():
    # Sonda: 200 com edges + xdt_viewer igual ao ds_user_id + errors "requires login" -> valid, sem re-bootstrap.
    import json

    mock = MockInstagram(pages=("viewer_match",), bootstrap="bootstrap_viewer_zero.html")
    payload = json.loads(mock.pages[0])
    payload["errors"] = [{"message": "This query requires login", "severity": "WARNING"}]
    mock.pages = [json.dumps(payload)]
    result = real_stack(mock, cookies={"sessionid": FAKE_SESSION, "ds_user_id": ACCOUNT_ID}).test_session()
    assert result.ok and result.result == "valid" and result.message == provider_mod.VALID_SESSION_MESSAGE
    _assert_web_fields(result, present=True, bootstrap="ok", graphql="ok", recognized=True, listing="ok")
    assert [r[2] for r in mock.requests] == ["/", "/graphql/query"]


# --------------------------------------------------------------------------- contador de entregues e conta da listagem


def _delivered_log(caplog) -> str:
    return next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Listagem web de"))


def test_delivered_counter_counts_items_when_consumer_stops_at_limit(caplog):
    caplog.set_level("INFO")
    dl, *_ = build([[image_node("IMG1"), image_node("IMG2", pk="2")]])
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 1))) == 1
    assert "1 posts, 1 mídias normalizadas, 1 entregues" in _delivered_log(caplog)


def test_delivered_counter_counts_item_when_consumer_closes_generator(caplog):
    caplog.set_level("INFO")
    dl, *_ = build([[image_node("IMG1"), image_node("IMG2", pk="2")]])
    it = dl.iter_profile_media("perfil_teste", "all", 5)
    next(it)
    it.close()
    assert "1 entregues" in _delivered_log(caplog)


@pytest.mark.parametrize("viewer, expected", [(ACCOUNT_ID, "confirmed"), ("70000000999", "different_account"),
                                              (None, "not_confirmed"), ("0", "not_confirmed")])
def test_last_account_confirmation_after_listing(viewer, expected, caplog):
    caplog.set_level("INFO")
    dl, *_ = build(http=FakeHttp(account=ACCOUNT_ID), graphql=FakeGraphQL([[image_node("IMG1")]], viewer_id=viewer))
    assert dl.last_account_confirmation is None
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 1))) == 1
    assert dl.last_account_confirmation == expected
    assert _delivered_log(caplog).endswith(f"conta {expected}")
    assert ACCOUNT_ID not in caplog.text and "70000000999" not in caplog.text


@pytest.mark.parametrize("page, expected, recognized", [("viewer_match", "confirmed", True),
                                                        ("viewer_other", "different_account", False),
                                                        ("viewer_absent", "not_confirmed", None)])
def test_real_stack_listing_account_confirmation_matches_session_test(page, expected, recognized, caplog):
    caplog.set_level("DEBUG")
    cookies = {"sessionid": FAKE_SESSION, "ds_user_id": ACCOUNT_ID}
    mock = MockInstagram(pages=(page,), bootstrap="bootstrap_viewer_zero.html")
    dl = real_stack(mock, cookies=cookies)
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 1))) == 1
    assert dl.last_account_confirmation == expected
    assert [r[2] for r in mock.requests] == ["/", "/graphql/query"]   # sem chamada extra
    mock = MockInstagram(pages=(page,), bootstrap="bootstrap_viewer_zero.html")
    assert real_stack(mock, cookies=cookies).test_session().session_recognized is recognized
    assert ACCOUNT_ID not in caplog.text and "70000000999" not in caplog.text
