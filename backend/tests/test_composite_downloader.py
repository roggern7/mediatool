"""CompositeInstagramDownloader (estratégias e fallback), wiring padrão e API de configurações."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import load_settings
from app.integrations.instagram.composite import FALLBACK_CODES, CompositeInstagramDownloader
from app.integrations.instagram.downloader import InstagramError, SessionTestResult
from app.integrations.instagram.gallerydl import GalleryDlInstagramDownloader
from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.provider import InstagramWebDownloader
from app.main import build_context
from conftest import make_settings
from media_fakes import FAKE_SESSION, FakeInstagramDownloader, MemorySessionProvider, remote

NO_FALLBACK_CODES = ("rate_limited", "checkpoint_required", "action_blocked", "session_invalid", "session_expired",
                     "network_error", "timeout", "access_refused", "profile_private", "profile_not_found")


class Recording(FakeInstagramDownloader):
    """Fake que registra chamadas e pode falhar com um erro em todas as operações."""

    def __init__(self, name: str, *, error: InstagramError | None = None, items=None, error_after=None):
        super().__init__(items=list(items or []))
        self.name = name
        self.error = error
        self.error_after = error_after
        self.calls: list[str] = []

    def test_session(self):
        self.calls.append("test_session")
        return SessionTestResult(ok=True, code="ok", message=self.name, username=None)

    def get_profile(self, username):
        self.calls.append("get_profile")
        if self.error is not None:
            raise self.error
        return super().get_profile(username)

    def iter_profile_media(self, username, content, limit, *, cancel=None, resume=None, on_checkpoint=None):
        self.calls.append("iter_profile_media")
        produced = 0
        for item in self.items:
            if self.error is not None and self.error_after is not None and produced >= self.error_after:
                raise self.error
            produced += 1
            yield item
        if self.error is not None:
            raise self.error

    def resolve_post(self, shortcode, **kwargs):
        self.calls.append("resolve_post")
        self.resolve_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return []

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        self.calls.append("download")
        if self.error is not None:
            raise self.error
        return super().download(item, dest_dir, cancel=cancel, progress=progress)


def web_error(code: str) -> InstagramWebError:
    return InstagramWebError(code, f"FAKE {code}", stage="graphql")


def run_all_ops(dl: CompositeInstagramDownloader, tmp_path: Path) -> list[object]:
    outcomes: list[object] = []
    for op in (
        lambda: dl.get_profile("perfil_teste"),
        lambda: list(dl.iter_profile_media("perfil_teste", "all", 10)),
        lambda: dl.download(remote("ABCDE1"), tmp_path),
        lambda: dl.resolve_post("ABCDE1"),
    ):
        try:
            outcomes.append(op())
        except InstagramError as exc:
            outcomes.append(exc.code)
    return outcomes


# --------------------------------------------------------------------------- estratégias


def test_gallery_dl_strategy_never_touches_web(tmp_path):
    web = Recording("web", error=web_error("contract_changed"))
    gallery = Recording("gallery", items=[remote("ABCDE1")])
    dl = CompositeInstagramDownloader(web, gallery, strategy="gallery_dl")
    run_all_ops(dl, tmp_path)
    assert dl.test_session().message == "gallery"
    assert web.calls == []
    assert gallery.calls == ["get_profile", "iter_profile_media", "download", "resolve_post", "test_session"]


def test_web_strategy_never_falls_back(tmp_path):
    web = Recording("web", error=web_error("contract_changed"))
    gallery = Recording("gallery", items=[remote("ABCDE1")])
    dl = CompositeInstagramDownloader(web, gallery, strategy="web")
    assert run_all_ops(dl, tmp_path) == ["contract_changed"] * 4
    assert dl.test_session().message == "web"
    assert gallery.calls == []


@pytest.mark.parametrize("strategy, used", [("web", "web"), ("gallery_dl", "gallery"), ("auto", "web")])
def test_resolve_post_forwards_cancel_only_when_given(strategy, used):
    import threading

    providers = {"web": Recording("web"), "gallery": Recording("gallery")}
    dl = CompositeInstagramDownloader(providers["web"], providers["gallery"], strategy=strategy)
    cancel = threading.Event()
    assert dl.resolve_post("ABCDE1", cancel=cancel) == []
    assert providers[used].resolve_kwargs == {"cancel": cancel}
    # sem cancel nada é repassado (providers antigos, sem o parâmetro, continuam funcionando)
    assert dl.resolve_post("ABCDE1") == [] and providers[used].resolve_kwargs == {}
    assert providers[used].calls == ["resolve_post"] * 2
    assert [p.calls for name, p in providers.items() if name != used] == [[]]


@pytest.mark.parametrize("code", sorted(FALLBACK_CODES))
def test_auto_resolve_post_fallback_keeps_cancel(code):
    import threading

    web, gallery = Recording("web", error=web_error(code)), Recording("gallery")
    cancel = threading.Event()
    assert CompositeInstagramDownloader(web, gallery, strategy="auto").resolve_post("ABCDE1", cancel=cancel) == []
    assert web.resolve_kwargs == gallery.resolve_kwargs == {"cancel": cancel}


@pytest.mark.parametrize("code", ["content_unavailable", "profile_private", "rate_limited", "invalid_input"])
def test_auto_resolve_post_link_errors_do_not_fall_back(code):
    web, gallery = Recording("web", error=web_error(code)), Recording("gallery")
    with pytest.raises(InstagramError) as info:
        CompositeInstagramDownloader(web, gallery, strategy="auto").resolve_post("ABCDE1")
    assert info.value.code == code and gallery.calls == []


@pytest.mark.parametrize("code", sorted(FALLBACK_CODES))
def test_auto_falls_back_once_on_contract_codes(tmp_path, code, caplog):
    web = Recording("web", error=web_error(code))
    gallery = Recording("gallery", items=[remote("ABCDE1")])
    dl = CompositeInstagramDownloader(web, gallery, strategy="auto")
    profile, items, path, posts = run_all_ops(dl, tmp_path)
    assert profile.username == "perfil_teste" and len(items) == 1 and Path(path).is_file() and posts == []
    assert web.calls == ["get_profile", "iter_profile_media", "download", "resolve_post"]
    assert gallery.calls == ["get_profile", "iter_profile_media", "download", "resolve_post"]
    assert sum("fallback" in r.getMessage() for r in caplog.records) == 4


@pytest.mark.parametrize("code", NO_FALLBACK_CODES)
def test_auto_does_not_fall_back_on_other_codes(tmp_path, code):
    web = Recording("web", error=web_error(code))
    gallery = Recording("gallery", items=[remote("ABCDE1")])
    dl = CompositeInstagramDownloader(web, gallery, strategy="auto")
    assert run_all_ops(dl, tmp_path) == [code] * 4
    assert gallery.calls == []


def test_auto_fallback_happens_only_once_per_operation(tmp_path):
    web = Recording("web", error=web_error("contract_changed"))
    gallery = Recording("gallery", error=InstagramError("contract_changed"))
    dl = CompositeInstagramDownloader(web, gallery, strategy="auto")
    with pytest.raises(InstagramError) as info:
        dl.get_profile("perfil_teste")
    assert info.value.code == "contract_changed" and web.calls == ["get_profile"] and gallery.calls == ["get_profile"]


def test_auto_listing_does_not_fall_back_after_items_were_delivered():
    web = Recording("web", error=web_error("contract_changed"), items=[remote("ABCDE1"), remote("ABCDE2")],
                    error_after=1)
    gallery = Recording("gallery", items=[remote("ZZZZZ1")])
    dl = CompositeInstagramDownloader(web, gallery, strategy="auto")
    received = []
    with pytest.raises(InstagramError) as info:
        for item in dl.iter_profile_media("perfil_teste", "all", None):
            received.append(item.source_post_id)
    assert info.value.code == "contract_changed" and received == ["ABCDE1"] and gallery.calls == []


def test_auto_session_test_reports_the_web_result_only():
    web = Recording("web")
    gallery = Recording("gallery")
    dl = CompositeInstagramDownloader(web, gallery, strategy="auto")
    assert dl.test_session().message == "web" and gallery.calls == []


def test_default_and_unknown_strategy_is_web():
    assert CompositeInstagramDownloader(Recording("w"), Recording("g")).strategy == "web"
    assert CompositeInstagramDownloader(Recording("w"), Recording("g"), strategy="outra").strategy == "web"


# --------------------------------------------------------------------------- pausas e pipeline


def _composite_harness(harness_factory, code: str, strategy: str = "auto"):
    web = Recording("web", error=web_error(code))
    gallery = Recording("gallery", items=[remote("ABCDE1")])
    harness = harness_factory()
    harness.downloader = CompositeInstagramDownloader(web, gallery, strategy=strategy)
    return harness, web, gallery


def test_contract_changed_listing_is_not_paused_nor_retried(harness_factory):
    harness, _, gallery = _composite_harness(harness_factory, "contract_changed", strategy="web")
    batch = harness.import_profile()
    harness.run_imports()
    current = harness.batch(batch["id"])
    assert current["status"] == "failed" and current["error_code"] == "contract_changed"
    assert not harness.ctx.worker.downloads_paused() and gallery.calls == []


@pytest.mark.parametrize("code, paused", [("rate_limited", True), ("action_blocked", True),
                                          ("access_refused", False)])
def test_existing_pauses_still_apply_through_composite(harness_factory, code, paused):
    harness, _, gallery = _composite_harness(harness_factory, code)
    batch = harness.import_profile()
    harness.run_imports()
    assert harness.batch(batch["id"])["error_code"] == code
    assert harness.ctx.worker.downloads_paused() is paused and gallery.calls == []


def test_auto_fallback_imports_through_gallery_dl(harness_factory):
    harness, _, gallery = _composite_harness(harness_factory, "contract_changed")
    batch = harness.import_profile()
    harness.run_all()
    assert harness.batch(batch["id"])["found"] == 1
    assert gallery.calls == ["iter_profile_media", "download"]


# --------------------------------------------------------------------------- wiring padrão


def _context(tmp_path, **overrides):
    return build_context(make_settings(tmp_path, **overrides), session_provider=MemorySessionProvider(),
                         processor=None)


def test_default_factory_is_composite_web_with_gallery_fallback(tmp_path):
    ctx = _context(tmp_path)
    from app.db.migrate import apply_migrations

    apply_migrations(ctx.db)
    first, second = ctx.downloader_factory(), ctx.downloader_factory()
    assert isinstance(first, CompositeInstagramDownloader) and first is not second
    assert isinstance(first.primary, InstagramWebDownloader)
    assert isinstance(first.fallback, GalleryDlInstagramDownloader)
    assert first.primary is not second.primary
    assert first.primary._provider is first.fallback._provider is ctx.session_provider
    assert first.strategy == "web"

    ctx.app_settings.set("instagram_provider", "gallery_dl")
    ctx.app_settings.set("instagram_web_doc_ids", {"PolarisProfilePostsQuery": "12345678"})
    third = ctx.downloader_factory()
    assert third.strategy == "gallery_dl" and third.primary._overrides == {"PolarisProfilePostsQuery": "12345678"}
    ctx.db.close_all()


def test_default_factory_passes_settings_env_to_doc_id_resolution(tmp_path):
    from app.db.migrate import apply_migrations
    from app.integrations.instagram.session import InstagramSession
    from app.integrations.instagram.web.queries import PROFILE_POSTS

    ctx = _context(tmp_path, env={"MEDIA_TOOL_IG_DOC_POLARISPROFILEPOSTSQUERY": "99999999"})
    apply_migrations(ctx.db)
    web = ctx.downloader_factory().primary
    ops = web._build_ops(InstagramSession({"sessionid": FAKE_SESSION}))
    try:
        assert ops.graphql._spec(PROFILE_POSTS).doc_id == "99999999"
    finally:
        ops.http.close()
    ctx.app_settings.set("instagram_web_doc_ids", {"PolarisProfilePostsQuery": "12345678"})
    ops = ctx.downloader_factory().primary._build_ops(InstagramSession({"sessionid": FAKE_SESSION}))
    try:
        assert ops.graphql._spec(PROFILE_POSTS).doc_id == "12345678"  # setting > env
    finally:
        ops.http.close()
    ctx.db.close_all()


def test_env_default_strategy(tmp_path):
    assert load_settings({"MEDIA_TOOL_INSTAGRAM_PROVIDER": "Web"}, root=tmp_path).instagram_provider == "web"
    assert load_settings({"MEDIA_TOOL_INSTAGRAM_PROVIDER": "Auto"}, root=tmp_path).instagram_provider == "auto"
    assert load_settings({"MEDIA_TOOL_INSTAGRAM_PROVIDER": "xyz"}, root=tmp_path).instagram_provider == "web"
    assert load_settings({}, root=tmp_path).instagram_provider == "web"
    ctx = _context(tmp_path, instagram_provider="gallery_dl")
    from app.db.migrate import apply_migrations

    apply_migrations(ctx.db)
    assert ctx.downloader_factory().strategy == "gallery_dl"
    ctx.app_settings.set("instagram_provider", "web")
    assert ctx.downloader_factory().strategy == "web"
    ctx.db.close_all()


# --------------------------------------------------------------------------- API


def test_settings_api_exposes_and_validates_provider(harness):
    client = harness.client
    body = client.get("/api/settings").json()
    assert body["instagram_provider"] == "web" and body["instagram_web_doc_ids"] == {}

    for value in ("web", "gallery_dl", "auto"):
        response = client.put("/api/settings", json={"instagram_provider": value})
        assert response.status_code == 200 and response.json()["instagram_provider"] == value
    assert client.put("/api/settings", json={"instagram_provider": "instaloader"}).status_code == 422
    assert client.put("/api/settings", json={"instagram_provider": 1}).status_code == 422
    client.put("/api/settings", json={"instagram_provider": "auto"})
    assert client.put("/api/settings", json={"instagram_provider": None}).json()["instagram_provider"] == "web"


def test_settings_api_validates_doc_id_overrides(harness):
    client = harness.client
    ok = client.put("/api/settings", json={"instagram_web_doc_ids": {"PolarisProfilePostsQuery": "1234567890"}})
    assert ok.status_code == 200
    assert ok.json()["instagram_web_doc_ids"] == {"PolarisProfilePostsQuery": "1234567890"}
    for bad in ({"PolarisProfilePostsQuery": "12ab5"}, {"PolarisProfilePostsQuery": "123"},
                {"PolarisProfilePostsQuery": "\u0661\u0662\u0663\u0664\u0665\u0666"},
                {"PolarisProfilePostsQuery": " 123456"}, {"x y": "123456"}, {"OutraQuery": "123456"},
                {"polarisprofilepostsquery": "123456"},
                {"PolarisProfilePostsQuery": "123456", "POLARISPROFILEPOSTSQUERY": "123456"},
                {"PolarisProfilePostsQuery": 123456}, ["123456"]):
        assert client.put("/api/settings", json={"instagram_web_doc_ids": bad}).status_code == 422, bad
    unknown = client.put("/api/settings", json={"instagram_web_doc_ids": {"OutraQuery": "123456"}})
    assert "consulta desconhecida" in unknown.text and "PolarisProfilePostsQuery" in unknown.text
    assert client.put("/api/settings", json={"instagram_web_doc_ids": None}).json()["instagram_web_doc_ids"] == {}


def test_last_test_exposes_web_stages_without_secrets(harness):
    harness.client.put("/api/settings/instagram/session", json={"sessionid": FAKE_SESSION})
    harness.downloader.session_result = SessionTestResult(
        ok=False, code="contract_changed", message="Sessão presente e reconhecida; GraphQL falhou.", username=None,
        result="instagram_error", session_recognized=True, listing_api="unknown", session_present=True,
        bootstrap="ok", graphql="failed", detail="repeated_cursor",
    )
    tested = harness.client.post("/api/settings/instagram/session/test")
    last = tested.json()["last_test"]
    assert (last["session_present"], last["bootstrap"], last["graphql"]) == (True, "ok", "failed")
    assert last["code"] == "contract_changed" and last["result"] == "instagram_error"
    assert not harness.ctx.worker.downloads_paused()
    settings = harness.client.get("/api/settings")
    assert settings.json()["instagram"]["last_test"]["graphql"] == "failed"
    for response in (tested, settings):
        assert FAKE_SESSION not in response.text and "fb_dtsg" not in response.text
    stored = json.loads(harness.ctx.app_settings.get_state("instagram_session_last_test"))
    assert stored["bootstrap"] == "ok" and FAKE_SESSION not in json.dumps(stored)


def test_last_test_rejects_unknown_stage_values(harness):
    harness.ctx.app_settings.set_state("instagram_session_last_test", json.dumps(
        {"ok": False, "code": "contract_changed", "message": "x", "session_present": "sim", "bootstrap": "talvez",
         "graphql": "ok", "tested_at": "2026-10-01T00:00:00.000Z"}))
    last = harness.client.get("/api/settings").json()["instagram"]["last_test"]
    assert (last["session_present"], last["bootstrap"], last["graphql"]) == (None, None, "ok")


# --------------------------------------------------------------------------- refresh_page, retomada e métricas


def _fake_ref():
    from app.integrations.instagram.downloader import ListingRef

    return ListingRef(page_index=2, cursor="FAKE_CURSOR_1", page_size=12)


@pytest.mark.parametrize("strategy, used", [("web", "web"), ("auto", "web"), ("gallery_dl", "gallery")])
def test_refresh_page_is_delegated_by_strategy(strategy, used):
    calls = []

    class Refreshing(Recording):
        def refresh_page(self, profile, ref, *, cancel=None):
            calls.append((self.name, profile, ref.page_index))
            return []

    dl = CompositeInstagramDownloader(Refreshing("web"), Refreshing("gallery"), strategy)
    assert dl.refresh_page("perfil_teste", _fake_ref()) == [] and calls == [(used, "perfil_teste", 2)]


def test_interface_default_refresh_page_is_content_unavailable():
    from app.integrations.instagram.downloader import InstagramDownloader

    with pytest.raises(InstagramError) as info:   # gallery-dl herda o padrão da interface
        InstagramDownloader.refresh_page(GalleryDlInstagramDownloader.__new__(GalleryDlInstagramDownloader),
                                         "perfil_teste", _fake_ref())
    assert info.value.code == "content_unavailable"


def test_resume_and_checkpoint_are_forwarded_only_when_given():
    from app.integrations.instagram.downloader import ListingCheckpoint

    seen = []

    class Web(Recording):
        def iter_profile_media(self, username, content, limit, *, cancel=None, **kwargs):
            seen.append(sorted(kwargs))
            self.last_listing_stats = {"pages": 1, "stop_reason": "end_of_profile"}
            return iter(())

    primary = Web("web")
    dl = CompositeInstagramDownloader(primary, Recording("gallery"), "web")
    list(dl.iter_profile_media("perfil_teste", "all", 10))
    cp = ListingCheckpoint(resume_cursor="FAKE_CURSOR_3", next_page_index=4, pages=3, posts_seen=30,
                           items_compatible=5, unproductive_streak=1, has_next=True)
    list(dl.iter_profile_media("perfil_teste", "all", 10, resume=cp, on_checkpoint=lambda c: None))
    assert seen == [[], ["on_checkpoint", "resume"]]
    assert dl.last_listing_stats == {"pages": 1, "stop_reason": "end_of_profile"}
    assert "FAKE_CURSOR" not in repr(cp)
    gallery = CompositeInstagramDownloader(primary, Recording("gallery"), "gallery_dl")
    list(gallery.iter_profile_media("perfil_teste", "all", 10, resume=cp))   # fake antigo aceita/ignora
    assert gallery.last_listing_stats is None


# --------------------------------------------------------------------------- "Todos" e photos_carousels só no web


@pytest.mark.parametrize("content, limit", [("all", None), ("reels", None), ("photos_carousels", 10),
                                            ("photos_carousels", None), ("all", 5001), ("reels", 1_000_000)])
def test_gallery_dl_strategy_refuses_all_and_photos_carousels_clearly(content, limit):
    web = Recording("web", items=[remote("ABCDE1")])
    gallery = Recording("gallery", items=[remote("ABCDE1")])
    dl = CompositeInstagramDownloader(web, gallery, strategy="gallery_dl")
    with pytest.raises(InstagramError) as info:
        list(dl.iter_profile_media("perfil_teste", content, limit))
    assert info.value.code == "invalid_input" and "método de importação web" in info.value.message
    assert gallery.calls == [] and web.calls == [], "nada de truncar em 5.000 fingindo fim de perfil"
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 10))) == 1, "os demais casos seguem no legado"


@pytest.mark.parametrize("content, limit", [("all", None), ("photos_carousels", 10), ("all", 5001)])
@pytest.mark.parametrize("code", sorted(FALLBACK_CODES))
def test_auto_never_falls_back_to_gallery_dl_for_all_or_photos_carousels(content, limit, code):
    web = Recording("web", error=web_error(code))
    gallery = Recording("gallery", items=[remote("ABCDE1")])
    dl = CompositeInstagramDownloader(web, gallery, strategy="auto")
    with pytest.raises(InstagramError) as info:
        list(dl.iter_profile_media("perfil_teste", content, limit))
    assert info.value.code == code, "o erro do web fica visível"
    assert gallery.calls == []


def test_legacy_cap_constant_matches_the_gallery_dl_adapter():
    from app.integrations.instagram.downloader import LEGACY_MAX_ITEMS
    from app.integrations.instagram.gallerydl import GALLERY_DL_MAX_ITEMS

    assert LEGACY_MAX_ITEMS == GALLERY_DL_MAX_ITEMS == 5000
    dl = CompositeInstagramDownloader(Recording("web", items=[remote("ABCDE1")]),
                                      Recording("gallery", items=[remote("ABCDE1")]), strategy="gallery_dl")
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 5000))) == 1, "até 5.000 o legado atende"


def test_no_listing_anomaly_detail_triggers_the_fallback():
    from app.integrations.instagram.composite import NO_FALLBACK_DETAILS
    from app.integrations.instagram.downloader import LISTING_ANOMALY_DETAILS
    from app.integrations.instagram.web.errors import InstagramWebError

    assert LISTING_ANOMALY_DETAILS <= NO_FALLBACK_DETAILS
    for detail in sorted(LISTING_ANOMALY_DETAILS):
        web = Recording("web", items=[])
        gallery = Recording("gallery", items=[remote("ABCDE1")])

        def broken(username, content, limit, *, cancel=None, _detail=detail, **kwargs):
            raise InstagramWebError("contract_changed", "fim anômalo FAKE", stage="graphql",
                                    diagnostic={"detail": _detail})
            yield   # gerador: o erro sai antes de qualquer item, como numa página só com itens filtrados

        web.iter_profile_media = broken
        dl = CompositeInstagramDownloader(web, gallery, strategy="auto")
        with pytest.raises(InstagramError) as info:
            list(dl.iter_profile_media("perfil_teste", "reels", 5))
        assert info.value.code == "contract_changed" and info.value.diagnostic["detail"] == detail
        assert gallery.calls == [], detail
