"""InstagramWebDownloader.resolve_post (post isolado) com fakes de http/bootstrap/graphql (offline; valores FAKE).

A consulta de post NÃO foi verificada no Instagram real: as raízes aqui são fixtures falsas no formato esperado.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest

from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.web import provider as provider_mod
from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.provider import InstagramWebDownloader, WebStateCache
from test_web_provider import FakeBootstrap, FakeHttp, FakeProvider

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web" / "graphql"
POST_ROOT = "xdt_shortcode_media"
CODE = "FAKEPOSTIMG1"


def root_of(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["data"][POST_ROOT]


class CancelHttp(FakeHttp):
    cancel = None   # o provider grava aqui o cancelamento da operação (espera pela vaga no portão)


class PostGraphQL:
    """Só a consulta de post: devolve a raiz (ou levanta o erro) e registra cada chamada."""

    def __init__(self, root=None, *, error: Exception | None = None, events: list | None = None):
        self.root = root
        self.error = error
        self.calls: list[str] = []
        self.events = events if events is not None else []

    def post_by_shortcode(self, shortcode):
        self.calls.append(shortcode)
        self.events.append("post")
        if self.error is not None:
            raise self.error
        return self.root


class Gate:
    def __init__(self, events: list, on_wait=None):
        self.events = events
        self.calls: list[tuple[object, int]] = []
        self.on_wait = on_wait

    def wait_listing_page(self, cancel=None, *, max_per_hour):
        self.calls.append((cancel, max_per_hour))
        self.events.append("wait_page")
        if self.on_wait is not None:
            self.on_wait()


def build(root=None, *, error=None, gate=None, env=None, bootstrap=None, configured=True, events=None, cache=None):
    http = CancelHttp()
    bootstrap = bootstrap or FakeBootstrap()
    graphql = PostGraphQL(root, error=error, events=events)
    dl = InstagramWebDownloader(
        FakeProvider(configured), request_gate=gate, env=env, bootstrap_cache=cache,
        http_factory=lambda session: http, bootstrap_factory=lambda h: bootstrap,
        graphql_factory=lambda h, b: graphql,
    )
    return dl, http, bootstrap, graphql


@pytest.fixture(autouse=True)
def _logs(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(provider_mod, "_rate_limit_disabled_logged", False)
    yield
    for secret in (CODE, "cdninstagram", "fbcdn", "FAKE_OH", "FAKE_DTSG", "FAKE_LSD", "dono_fake", "Dono_Fake"):
        assert secret not in caplog.text, secret


# --------------------------------------------------------------------------- os 4 tipos


def test_photo_post():
    dl, http, bootstrap, graphql = build(root_of("post_image.json"))
    [item] = dl.resolve_post(CODE)
    assert item.media_type == "image" and not item.is_reel and item.source_media_index == 0
    assert item.source_post_id == CODE and item.source_profile == "dono_fake" and item.listing_ref is None
    assert item.source_url == f"https://www.instagram.com/p/{CODE}/" and "pimg1_1080x1350.jpg" in item.media_url
    assert graphql.calls == [CODE] and bootstrap.calls == 1 and http.closed


def test_reel_post():
    dl, *_ = build(root_of("post_video_reel.json"))
    [item] = dl.resolve_post("FAKEPOSTREEL")
    assert item.media_type == "video" and item.is_reel and item.duration == 12.5
    assert item.source_url == "https://www.instagram.com/reel/FAKEPOSTREEL/" and item.listing_ref is None


def test_plain_video_post_is_not_reel():
    dl, *_ = build({**root_of("post_video_reel.json"), "product_type": "feed"})
    [item] = dl.resolve_post("FAKEPOSTREEL")
    assert item.media_type == "video" and not item.is_reel
    assert item.source_url == "https://www.instagram.com/p/FAKEPOSTREEL/"


def test_carousel_returns_all_items_without_content_filter():
    dl, _, _, graphql = build(root_of("post_carousel_mixed.json"))
    items = dl.resolve_post("FAKEPOSTCAR1")
    assert [(i.source_media_index, i.media_type) for i in items] == [
        (0, "image"), (1, "video"), (2, "image"), (3, "video")]
    assert all(i.source_post_id == "FAKEPOSTCAR1" and i.source_profile == "dono_fake" and i.listing_ref is None
               and i.listing_dropped is None for i in items)
    assert graphql.calls == ["FAKEPOSTCAR1"]   # 1 só consulta por link


def test_v1_items_format_is_understood():
    dl, *_ = build(root_of("post_v1_items.json"))
    items = dl.resolve_post("FAKEPOSTV1A1")
    assert [(i.source_media_index, i.media_type) for i in items] == [(0, "image"), (1, "video")]
    assert all(i.source_profile == "dono_fake" for i in items)


def test_owner_absent_gives_empty_source_profile():
    root = root_of("post_image.json")
    del root["owner"]
    dl, *_ = build(root)
    assert dl.resolve_post(CODE)[0].source_profile == ""


def test_log_has_only_counts(caplog):
    dl, *_ = build(root_of("post_carousel_mixed.json"))
    dl.resolve_post("FAKEPOSTCAR1")
    assert "Publicação resolvida (web): 4 mídias, 0 sem URL, 0 não suportadas" in caplog.text
    assert "FAKEPOSTCAR1" not in caplog.text


# --------------------------------------------------------------------------- erros


def test_invalid_shortcode_makes_no_request():
    dl, http, bootstrap, graphql = build(root_of("post_image.json"))
    for bad in ("../x", "abc", "", "ABC/DEFGH", None):
        with pytest.raises(InstagramError) as info:
            dl.resolve_post(bad)
        assert info.value.code == "invalid_input"
    assert bootstrap.calls == 0 and graphql.calls == []


def test_missing_session():
    dl, _, bootstrap, graphql = build(root_of("post_image.json"), configured=False)
    with pytest.raises(InstagramError) as info:
        dl.resolve_post(CODE)
    assert info.value.code == "session_missing" and bootstrap.calls == 0 and graphql.calls == []


@pytest.mark.parametrize("code, retryable", [
    ("content_unavailable", False), ("profile_private", False), ("contract_changed", False),
    ("rate_limited", True), ("checkpoint_required", False), ("request_auth_failed", True),
    ("session_invalid", False), ("action_blocked", False), ("access_refused", False), ("network_error", True),
    ("timeout", True),
])
def test_graphql_errors_go_up_unchanged_and_http_is_closed(code, retryable):
    error = InstagramWebError(code, f"FAKE {code}", retryable=retryable, stage="graphql",
                              diagnostic={"detail": "fake_detail"})
    dl, http, _, graphql = build(error=error)
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE)
    assert info.value is error and graphql.calls == [CODE] and http.closed


def test_bootstrap_error_goes_up_without_post():
    error = InstagramWebError("session_expired", "FAKE", stage="bootstrap")
    dl, http, _, graphql = build(root_of("post_image.json"), bootstrap=FakeBootstrap(error=error))
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE)
    assert info.value is error and graphql.calls == [] and http.closed


def _unsupported(root: dict) -> dict:
    root["__typename"] = "XDTGraphFakeNewKind"
    return root


def test_only_unsupported_media_type():
    dl, *_ = build(_unsupported(root_of("post_image.json")))
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE)
    assert info.value.code == "unsupported_media" and info.value.stage == "normalize"
    assert info.value.diagnostic["detail"] == "unsupported_media" and "publicação" in info.value.message


def test_carousel_with_only_unsupported_children():
    root = root_of("post_carousel_mixed.json")
    for edge in root["edge_sidecar_to_children"]["edges"]:
        _unsupported(edge["node"])
    dl, *_ = build(root)
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post("FAKEPOSTCAR1")
    assert info.value.code == "unsupported_media" and info.value.diagnostic["skipped_unsupported"] == 4


def _no_urls(root: dict) -> dict:
    root.pop("display_resources", None)
    root.pop("display_url", None)
    root.pop("video_url", None)
    return root


def test_items_without_usable_url_is_content_unavailable():
    dl, *_ = build(_no_urls(root_of("post_image.json")))
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE)
    assert info.value.code == "content_unavailable" and info.value.diagnostic["detail"] == "post_no_media"
    assert not info.value.retryable


def test_http_only_urls_are_not_usable():
    root = root_of("post_image.json")
    root["display_url"] = "http://scontent-fake.cdninstagram.com/v/t51.fake/x.jpg"
    root["display_resources"] = []
    dl, *_ = build(root)
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE)
    assert info.value.code == "content_unavailable"


def test_mix_of_unsupported_and_missing_url_is_content_unavailable():
    root = root_of("post_carousel_mixed.json")
    edges = root["edge_sidecar_to_children"]["edges"]
    _unsupported(edges[0]["node"])
    for edge in edges[1:]:
        _no_urls(edge["node"])
    dl, *_ = build(root)
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post("FAKEPOSTCAR1")
    assert info.value.code == "content_unavailable"


def test_empty_v1_items_is_content_unavailable():
    dl, *_ = build({"items": []})
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE)
    assert info.value.code == "content_unavailable"


def test_object_without_post_code_is_contract_changed():
    dl, *_ = build({"fake_unknown_shape": {"a": 1}})
    with pytest.raises(InstagramWebError) as info:
        dl.resolve_post(CODE)
    assert info.value.code == "contract_changed" and info.value.diagnostic["detail"] == "post_shape_unknown"


# --------------------------------------------------------------------------- cancelamento, portão e cache


def test_cancelled_before_start_does_nothing():
    cancel = threading.Event()
    cancel.set()
    dl, http, bootstrap, graphql = build(root_of("post_image.json"))
    assert dl.resolve_post(CODE, cancel=cancel) == []
    assert bootstrap.calls == 0 and graphql.calls == []


def test_cancelled_during_bootstrap_makes_no_post():
    cancel = threading.Event()

    class CancellingBootstrap(FakeBootstrap):
        def get(self, *, force=False):
            cancel.set()
            return super().get(force=force)

    events: list = []
    gate = Gate(events)
    dl, http, bootstrap, graphql = build(root_of("post_image.json"), bootstrap=CancellingBootstrap(), gate=gate,
                                         events=events)
    assert dl.resolve_post(CODE, cancel=cancel) == []
    assert bootstrap.calls == 1 and graphql.calls == [] and gate.calls == [] and http.closed


def test_cancel_reaches_the_http_client_for_the_gate_wait():
    cancel = threading.Event()
    dl, http, *_ = build(root_of("post_image.json"))
    assert len(dl.resolve_post(CODE, cancel=cancel)) == 1
    assert http.cancel is cancel


@pytest.mark.parametrize("env, expected", [(None, 600), ({"INSTAGRAM_PAGE_RATE_LIMIT": "5"}, 5)])
def test_hourly_page_cap_is_applied_once_before_the_post(env, expected):
    events: list = []
    gate = Gate(events)
    cancel = threading.Event()
    dl, *_ = build(root_of("post_image.json"), gate=gate, env=env, events=events)
    assert len(dl.resolve_post(CODE, cancel=cancel)) == 1
    assert gate.calls == [(cancel, expected)] and events == ["wait_page", "post"]


def test_hourly_page_cap_disabled_by_zero_and_gate_without_the_method():
    events: list = []
    gate = Gate(events)
    dl, *_ = build(root_of("post_image.json"), gate=gate, env={"INSTAGRAM_PAGE_RATE_LIMIT": "0"}, events=events)
    assert len(dl.resolve_post(CODE)) == 1 and gate.calls == [] and events == ["post"]
    dl, *_ = build(root_of("post_image.json"), gate=object())
    assert len(dl.resolve_post(CODE)) == 1


def test_cancelled_while_waiting_the_page_cap_makes_no_post():
    cancel = threading.Event()
    events: list = []
    gate = Gate(events, on_wait=cancel.set)
    dl, _, _, graphql = build(root_of("post_image.json"), gate=gate, events=events)
    assert dl.resolve_post(CODE, cancel=cancel) == [] and graphql.calls == [] and events == ["wait_page"]


def test_bootstrap_uses_the_shared_cache():
    bootstrap = FakeBootstrap()
    dl, _, _, graphql = build(root_of("post_image.json"), bootstrap=bootstrap, cache=WebStateCache())
    dl.resolve_post(CODE)
    dl.resolve_post(CODE)
    assert bootstrap.calls == 1 and graphql.calls == [CODE, CODE]
