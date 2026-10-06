"""Revisão independente do provider Instagram Web: prazos, cancelamento, instâncias por operação, re-bootstrap,
paginação e fallback do composite. Tudo OFFLINE (httpx.MockTransport; valores FAKE; sem rede, sem secrets reais)."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest

from app.core.config import INSTAGRAM_PROVIDERS  # noqa: F401  (garante import do módulo de config)
from app.integrations.instagram.composite import CompositeInstagramDownloader
from app.integrations.instagram.downloader import InstagramDownloader, InstagramError, RemoteMedia
from app.integrations.instagram.web.bootstrap import WebBootstrap
from app.integrations.instagram.web.errors import InstagramWebError
from app.integrations.instagram.web.graphql import InstagramWebGraphQL
from app.integrations.instagram.web.http import InstagramWebHttp, WebHttpConfig
from app.integrations.instagram.web.provider import InstagramWebDownloader
from app.integrations.instagram.web.queries import PROFILE_POSTS
from app.services.cdn_downloader import DownloadError
from conftest import make_settings, wait_until
from media_fakes import FAKE_SESSION, MemorySessionProvider
from test_web_provider import FakeProvider, carousel_node, image_node

FIX = Path(__file__).parent / "fixtures" / "instagram_web"
BOOT_HTML = (FIX / "bootstrap_define.html").read_text(encoding="utf-8")
TOKEN_ERROR = (FIX / "graphql" / "error_token_1357004.json").read_text(encoding="utf-8")
ROOT = PROFILE_POSTS.root_field


# --------------------------------------------------------------------------- site falso (MockTransport)


def page_body(nodes: list[dict], has_next: bool, cursor: str | None) -> str:
    return json.dumps({"data": {ROOT: {"edges": [{"node": n} for n in nodes],
                                       "page_info": {"end_cursor": cursor, "has_next_page": has_next}}}})


class Site:
    """Handler do MockTransport: conta requisições por tipo e deixa os testes bloquear/atrasar/reagir."""

    def __init__(self, *, pages: int = 3, per_page: int = 3, prefix: str = "P"):
        self.lock = threading.Lock()
        self.pages = pages
        self.per_page = per_page
        self.prefix = prefix
        self.docs = 0
        self.posts = 0
        self.topsearch = 0
        self.post_forms: list[dict] = []
        self.on_post = None      # (n, form, request) -> httpx.Response | None
        self.on_doc = None       # (n, request) -> httpx.Response | None
        self.on_topsearch = None
        self.set_cookie: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/":
            with self.lock:
                self.docs += 1
                n = self.docs
            if self.on_doc is not None:
                custom = self.on_doc(n, request)
                if custom is not None:
                    return custom
            headers = {"content-type": "text/html; charset=utf-8"}
            if self.set_cookie:
                headers["set-cookie"] = self.set_cookie
            return httpx.Response(200, text=BOOT_HTML, headers=headers)
        if path == "/graphql/query":
            form = {k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode()).items()}
            with self.lock:
                self.posts += 1
                n = self.posts
                self.post_forms.append(form)
            if self.on_post is not None:
                custom = self.on_post(n, form, request)
                if custom is not None:
                    return custom
            after = json.loads(form["variables"]).get("after")
            idx = 0 if after is None else int(after[1:])
            nodes = [image_node(f"{self.prefix}{idx}N{i}", pk=f"{idx}{i}") for i in range(self.per_page)]
            return httpx.Response(200, text=page_body(nodes, idx + 1 < self.pages, f"c{idx + 1}"),
                                  headers={"content-type": "application/json"})
        if path.startswith("/web/search/topsearch"):
            with self.lock:
                self.topsearch += 1
                n = self.topsearch
            if self.on_topsearch is not None:
                custom = self.on_topsearch(n, request)
                if custom is not None:
                    return custom
            body = {"users": [{"user": {"username": "perfil_teste", "pk": "1", "full_name": "Fake"}}]}
            return httpx.Response(200, json=body)
        return httpx.Response(404, text="nope")


def json_response(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=text, headers={"content-type": "application/json"})


class Rig:
    def __init__(self, site: Site, *, timeout: float = 5.0, cdn=None, page_size: int = 3):
        self.site = site
        self.https: list[InstagramWebHttp] = []
        self.bootstraps: list[WebBootstrap] = []
        self.graphqls: list[InstagramWebGraphQL] = []

        def http_factory(session):
            http = InstagramWebHttp(session, WebHttpConfig(timeout=timeout), transport=httpx.MockTransport(site))
            self.https.append(http)
            return http

        def bootstrap_factory(http):
            b = WebBootstrap(http)
            self.bootstraps.append(b)
            return b

        def graphql_factory(http, bootstrap):
            g = InstagramWebGraphQL(http, bootstrap)
            self.graphqls.append(g)
            return g

        self.dl = InstagramWebDownloader(
            FakeProvider(), cdn_downloader=cdn, page_size=page_size, http_factory=http_factory,
            bootstrap_factory=bootstrap_factory, graphql_factory=graphql_factory,
        )

    def all_closed(self) -> bool:
        return bool(self.https) and all(h._client.is_closed for h in self.https)


def media(code: str = "P0N1", index: int = 0) -> RemoteMedia:
    return RemoteMedia(
        source_profile="perfil_teste", source_post_id=code, post_id="1", media_id=None, source_media_index=index,
        media_type="image", is_reel=False, source_url=f"https://www.instagram.com/p/{code}/",
        media_url="https://scontent.cdninstagram.com/v/t51/OLD", thumbnail_url=None, width=1, height=1,
        duration=None, taken_at=None, extension="jpg",
    )


# --------------------------------------------------------------------------- 1. prazos


class DripStream(httpx.SyncByteStream):
    def __init__(self, chunks: int, delay: float):
        self.chunks, self.delay = chunks, delay

    def __iter__(self):
        for _ in range(self.chunks):
            time.sleep(self.delay)
            yield b"x"


def test_http_total_deadline_applies_to_dripping_body():
    """Corpo que goteja (cada chunk dentro do read-timeout) não pode passar do prazo total da requisição."""
    transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=DripStream(40, 0.05)))
    http = InstagramWebHttp(FakeProvider().get(), WebHttpConfig(timeout=0.3), transport=transport)
    started = time.monotonic()
    with pytest.raises(InstagramWebError) as exc:
        http.get_document("/")
    assert exc.value.code == "timeout" and exc.value.retryable
    assert time.monotonic() - started < 1.0  # 40*0.05 = 2 s sem prazo total
    http.close()


def test_http_timeout_and_network_errors_classified_without_retry():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("fake")

    http = InstagramWebHttp(FakeProvider().get(), transport=httpx.MockTransport(handler))
    with pytest.raises(InstagramWebError) as exc:
        http.get_document("/")
    assert exc.value.code == "timeout" and len(calls) == 1  # retries 0

    def boom(request):
        raise httpx.ConnectError("fake")

    http2 = InstagramWebHttp(FakeProvider().get(), transport=httpx.MockTransport(boom))
    with pytest.raises(InstagramWebError) as exc2:
        http2.get_document("/")
    assert exc2.value.code == "network_error"


def test_session_test_api_returns_within_hard_deadline_with_slow_bootstrap_and_graphql(harness_factory):
    """Bootstrap e GraphQL lentos (0,5 s cada) com prazo duro de 0,8 s: a rota responde `timeout` no prazo."""
    site = Site()
    release = threading.Event()

    def slow_doc(n, request):
        release.wait(0.5)

    def slow_post(n, form, request):
        release.wait(0.5)

    site.on_doc, site.on_post = slow_doc, slow_post
    rig = Rig(site)
    harness = harness_factory(downloader=rig.dl, session_test_timeout=0.8)
    started = time.monotonic()
    response = harness.client.post("/api/settings/instagram/session/test")
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert 0.7 <= elapsed < 1.6
    last = response.json()["last_test"]
    assert last["result"] == "timeout" and last["code"] == "timeout"
    # a thread em segundo plano termina e fecha o cliente
    assert wait_until(rig.all_closed, timeout=5.0)
    assert site.docs == 1 and site.posts == 1


def test_session_test_api_deadline_and_lock_while_graphql_blocked(harness_factory):
    site = Site()
    release = threading.Event()
    site.on_post = lambda n, form, request: release.wait(30) and None
    rig = Rig(site)
    harness = harness_factory(downloader=rig.dl, session_test_timeout=0.6)
    started = time.monotonic()
    first = harness.client.post("/api/settings/instagram/session/test")
    assert first.status_code == 200 and time.monotonic() - started < 2.0
    assert first.json()["last_test"]["result"] == "timeout"
    busy = harness.client.post("/api/settings/instagram/session/test")
    assert busy.status_code == 409 and busy.json()["code"] == "session_test_in_progress"
    assert site.docs == 1  # o segundo teste nem começou
    release.set()
    assert wait_until(rig.all_closed, timeout=5.0)
    assert wait_until(lambda: harness.client.post("/api/settings/instagram/session/test").status_code == 200)


def test_profile_preview_api_deadline_and_single_flight(harness_factory):
    site = Site()
    release = threading.Event()
    site.on_topsearch = lambda n, request: release.wait(30) and None
    rig = Rig(site)
    harness = harness_factory(downloader=rig.dl, profile_preview_timeout=0.6)
    started = time.monotonic()
    first = harness.client.post("/api/instagram/profile", json={"profile": "perfil_teste"})
    assert first.status_code == 504 and first.json()["code"] == "timeout"
    assert time.monotonic() - started < 2.0
    busy = harness.client.post("/api/instagram/profile", json={"profile": "perfil_teste"})
    assert busy.status_code == 409 and busy.json()["code"] == "profile_preview_in_progress"
    release.set()
    assert wait_until(rig.all_closed, timeout=5.0)


def test_default_hard_deadlines_cover_worst_case_only_via_outer_deadline():
    """Documenta a conta: sem o prazo externo, o pior caso do teste de sessão excede 40 s.

    Cada requisição tem até `timeout`(15) de prazo total (+ até 15 s de read-timeout de um chunk parado); o teste
    faz bootstrap + GraphQL e, com token recusado, re-bootstrap + GraphQL = 4 requisições. Só `run_with_deadline`
    (40 s/50 s) garante o limite duro.
    """
    cfg = WebHttpConfig()
    worst_requests = 4
    assert cfg.timeout * worst_requests > 40.0


# --------------------------------------------------------------------------- 2. cancelamento da listagem


def test_cancel_after_first_item_stops_requesting_pages():
    site = Site(pages=10, per_page=3)
    rig = Rig(site)
    cancel = threading.Event()
    items = rig.dl.iter_profile_media("perfil_teste", "all", None, cancel=cancel)
    got = [next(items)]
    cancel.set()
    rest = list(items)
    assert rest == [] and len(got) == 1
    assert site.posts == 1 and site.docs == 1
    assert rig.all_closed()


def test_cancel_while_graphql_request_in_flight_yields_nothing_and_no_new_request():
    site = Site(pages=10, per_page=3)
    inflight, go = threading.Event(), threading.Event()

    def blocking(n, form, request):
        inflight.set()
        go.wait(10)

    site.on_post = blocking
    rig = Rig(site)
    cancel = threading.Event()
    out: list = []
    t = threading.Thread(target=lambda: out.extend(rig.dl.iter_profile_media("perfil_teste", "all", None,
                                                                              cancel=cancel)))
    t.start()
    assert inflight.wait(5)
    cancel.set()
    go.set()
    t.join(5)
    assert not t.is_alive()
    assert out == []
    assert site.posts == 1
    assert rig.all_closed()


def test_cancel_before_start_makes_no_request_and_builds_nothing():
    site = Site()
    rig = Rig(site)
    cancel = threading.Event()
    cancel.set()
    assert list(rig.dl.iter_profile_media("perfil_teste", "all", None, cancel=cancel)) == []
    assert site.docs == 0 and site.posts == 0 and rig.https == []


def test_import_runner_cancel_during_listing_marks_batch_cancelled(harness_factory):
    site = Site(pages=40, per_page=2)
    site.on_post = lambda n, form, request: time.sleep(0.05)
    rig = Rig(site, page_size=2)
    harness = harness_factory(downloader=rig.dl)
    batch = harness.import_profile(limit=100)   # "Todos" desabilitado (422 limit_all_disabled)
    runner = harness.ctx.import_runner
    thread = threading.Thread(target=runner.run_next)
    thread.start()
    assert wait_until(lambda: site.posts >= 2, timeout=5.0)
    response = harness.client.post(f"/api/batches/{batch['id']}/cancel")
    assert response.status_code == 200
    thread.join(10)
    assert not thread.is_alive()
    seen = site.posts
    time.sleep(0.4)
    assert site.posts == seen and seen < 40  # nenhuma requisição nova depois do cancel / listagem não completou
    final = harness.batch(batch["id"])
    assert final["status"] == "cancelled"
    assert all(j["status"] == "cancelled" for j in final["jobs"]), final["jobs"]
    assert rig.all_closed()


# --------------------------------------------------------------------------- 3. cancelamento do download


def test_download_cancel_with_blocking_cdn_does_not_relist():
    site = Site()
    cancel = threading.Event()
    started = threading.Event()

    def cdn(url, dest, **kw):
        started.set()
        kw["cancel"].wait(10)
        raise DownloadError("cancelled", "cancelado")

    rig = Rig(site, cdn=cdn)
    box: list = []

    def run():
        try:
            rig.dl.download(media(), Path("."), cancel=cancel)
        except BaseException as exc:  # noqa: BLE001
            box.append(exc)

    t = threading.Thread(target=run)
    t.start()
    assert started.wait(5)
    cancel.set()
    t.join(5)
    assert not t.is_alive()
    assert isinstance(box[0], DownloadError) and box[0].code == "cancelled"
    assert site.docs == 0 and site.posts == 0


def test_download_expired_url_with_cancel_set_does_not_relist():
    site = Site()
    cancel = threading.Event()

    def cdn(url, dest, **kw):
        cancel.set()
        raise DownloadError("forbidden", "expirada")

    rig = Rig(site, cdn=cdn)
    with pytest.raises(DownloadError):
        rig.dl.download(media(), Path("."), cancel=cancel)
    assert site.docs == 0 and site.posts == 0


# --------------------------------------------------------------------------- 4. instâncias por operação


def test_concurrent_operations_do_not_share_state_and_close_clients():
    site = Site(pages=2, per_page=2)
    site.on_doc = lambda n, request: time.sleep(0.05)
    rig = Rig(site, page_size=2)
    barrier = threading.Barrier(2)
    results: dict[int, list] = {}

    def work(i: int):
        barrier.wait()
        results[i] = list(rig.dl.iter_profile_media("perfil_teste", "all", None))

    threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert len(results[0]) == 4 and len(results[1]) == 4
    assert len({id(h) for h in rig.https}) == 2
    assert len({id(h._client.cookies) for h in rig.https}) == 2
    assert len({id(b) for b in rig.bootstraps}) == 2 and len({id(g) for g in rig.graphqls}) == 2
    assert site.docs == 2  # cada operação faz o seu bootstrap (estado não é compartilhado)
    assert rig.all_closed()


def test_cookie_jar_isolated_between_operations():
    site = Site()
    site.set_cookie = "csrftoken=FAKECSRFAAAAAAAA; Path=/"
    rig = Rig(site)
    list(rig.dl.iter_profile_media("perfil_teste", "all", 1))
    site.set_cookie = "csrftoken=FAKECSRFBBBBBBBB; Path=/"
    list(rig.dl.iter_profile_media("perfil_teste", "all", 1))
    assert [h.csrftoken() for h in rig.https] == ["FAKECSRFAAAAAAAA", "FAKECSRFBBBBBBBB"]


def test_main_downloader_factory_builds_new_web_instance_per_call(tmp_path):
    from app.main import build_context

    from app.db.migrate import apply_migrations

    ctx = build_context(make_settings(tmp_path), session_provider=MemorySessionProvider())
    apply_migrations(ctx.db)
    a, b = ctx.downloader_factory(), ctx.downloader_factory()
    assert a is not b and a.primary is not b.primary and a.fallback is not b.fallback
    assert isinstance(a.primary, InstagramWebDownloader)
    ctx.db.close_all()


def test_client_closed_on_success_error_early_stop_and_abandon():
    # sucesso
    rig = Rig(Site(pages=1, per_page=2))
    assert len(list(rig.dl.iter_profile_media("perfil_teste", "all", None))) == 2
    assert rig.all_closed()
    # erro (HTTP 500 no GraphQL)
    site = Site()
    site.on_post = lambda n, form, request: httpx.Response(500, text="boom")
    rig = Rig(site)
    with pytest.raises(InstagramError):
        list(rig.dl.iter_profile_media("perfil_teste", "all", None))
    assert rig.all_closed()
    # parada antecipada por limite
    rig = Rig(Site(pages=5))
    assert len(list(rig.dl.iter_profile_media("perfil_teste", "all", 2))) == 2
    assert rig.all_closed()
    # consumidor fecha o gerador
    rig = Rig(Site(pages=5))
    gen = rig.dl.iter_profile_media("perfil_teste", "all", None)
    next(gen)
    gen.close()
    assert rig.all_closed()
    # consumidor abandona o gerador (GC)
    rig = Rig(Site(pages=5))
    gen = rig.dl.iter_profile_media("perfil_teste", "all", None)
    next(gen)
    del gen
    assert rig.all_closed()


def test_client_closed_for_get_profile_test_session_and_refresh():
    rig = Rig(Site())
    assert rig.dl.get_profile("perfil_teste").username == "perfil_teste"
    assert rig.all_closed()
    site = Site()
    site.on_topsearch = lambda n, r: httpx.Response(200, text="<html>", headers={"content-type": "text/html"})
    rig = Rig(site)
    with pytest.raises(InstagramError):
        rig.dl.get_profile("perfil_teste")
    assert rig.all_closed()
    rig = Rig(Site())
    assert rig.dl.test_session().ok
    assert rig.all_closed()
    site = Site()
    site.on_post = lambda n, form, request: json_response(TOKEN_ERROR)
    rig = Rig(site)
    assert not rig.dl.test_session().ok
    assert rig.all_closed()


def test_no_thread_leak_after_many_operations():
    rig = Rig(Site(pages=1, per_page=1))
    before = threading.active_count()
    for _ in range(20):
        list(rig.dl.iter_profile_media("perfil_teste", "all", None))
        rig.dl.test_session()
    assert threading.active_count() <= before
    assert rig.all_closed() and len(rig.https) == 40


# --------------------------------------------------------------------------- 5. re-bootstrap


def test_token_always_rejected_rebootstraps_exactly_once():
    site = Site()
    site.on_post = lambda n, form, request: json_response(TOKEN_ERROR)
    rig = Rig(site)
    with pytest.raises(InstagramError) as exc:
        list(rig.dl.iter_profile_media("perfil_teste", "all", None))
    assert exc.value.code == "contract_changed"   # 1357004 persistente sem evidência explícita (seção 3)
    assert (site.docs, site.posts) == (2, 2)
    assert rig.all_closed()


def test_token_rejected_once_then_ok_uses_two_documents_and_two_posts():
    site = Site(pages=1)
    site.on_post = lambda n, form, request: json_response(TOKEN_ERROR) if n == 1 else None
    rig = Rig(site)
    assert len(list(rig.dl.iter_profile_media("perfil_teste", "all", None))) == 3
    assert (site.docs, site.posts) == (2, 2)


def test_session_test_with_rejected_token_makes_at_most_four_requests():
    site = Site()
    site.on_post = lambda n, form, request: json_response(TOKEN_ERROR)
    rig = Rig(site)
    result = rig.dl.test_session()
    assert not result.ok and site.docs + site.posts == 4


def test_flapping_token_every_page_is_bounded_by_pages_not_a_loop():
    """O re-bootstrap é limitado a 1 por operação: a segunda recusa de token encerra a listagem."""
    site = Site(pages=6, per_page=1)
    state = {"rejected": set()}

    def flap(n, form, request):
        after = json.loads(form["variables"]).get("after") or "c0"
        if after != "c0" and after not in state["rejected"]:
            state["rejected"].add(after)
            return json_response(TOKEN_ERROR)

    site.on_post = flap
    rig = Rig(site, page_size=1)
    got = []
    with pytest.raises(InstagramWebError) as exc:
        for item in rig.dl.iter_profile_media("perfil_teste", "all", None):
            got.append(item)
    assert exc.value.code == "contract_changed"   # 1357004 persistente sem evidência explícita (seção 3)
    assert len(got) == 2
    assert site.docs == 2  # 1 inicial + 1 re-bootstrap na primeira recusa
    assert site.posts == 4  # p1, p2 recusada, p2 de novo, p3 recusada


def test_expired_url_is_media_url_expired_without_relisting():
    # BATCH_ROBUSTNESS seção 2: o provider não renova mais internamente; a pipeline chama refresh_page.
    site = Site(pages=10, per_page=3)
    calls = []

    def cdn(url, dest, **kw):
        calls.append(url)
        raise DownloadError("forbidden", "expirada")

    rig = Rig(site, cdn=cdn)
    with pytest.raises(InstagramError) as exc:
        rig.dl.download(media("P0N1"), Path("."))
    assert exc.value.code == "media_url_expired" and not exc.value.retryable
    assert site.docs == 0 and site.posts == 0 and len(calls) == 1


# --------------------------------------------------------------------------- 6. paginação


def graphql_for(site: Site) -> tuple[InstagramWebGraphQL, InstagramWebHttp]:
    http = InstagramWebHttp(FakeProvider().get(), transport=httpx.MockTransport(site))
    return InstagramWebGraphQL(http, WebBootstrap(http)), http


def test_repeated_cursor_raises_after_second_request():
    site = Site()
    site.on_post = lambda n, form, request: httpx.Response(
        200, text=page_body([image_node(f"R{n}", pk=f"r{n}")], True, "same"),
        headers={"content-type": "application/json"})
    gql, http = graphql_for(site)
    with pytest.raises(InstagramWebError) as exc:
        list(gql.iter_profile_posts("perfil_teste", max_media=None))
    assert exc.value.code == "contract_changed" and site.posts == 2
    http.close()


def test_cursor_cycle_a_b_a_stops():
    site = Site()
    cursors = ["A", "B", "A", "B", "A"]
    site.on_post = lambda n, form, request: httpx.Response(
        200, text=page_body([image_node(f"C{n}", pk=f"c{n}")], True, cursors[n - 1]),
        headers={"content-type": "application/json"})
    gql, http = graphql_for(site)
    with pytest.raises(InstagramWebError):
        list(gql.iter_profile_posts("perfil_teste", max_media=None))
    assert site.posts == 3
    http.close()


def test_empty_page_with_has_next_stops():
    site = Site()
    site.on_post = lambda n, form, request: httpx.Response(
        200, text=page_body([], True, f"e{n}"), headers={"content-type": "application/json"})
    gql, http = graphql_for(site)
    assert list(gql.iter_profile_posts("perfil_teste", max_media=None)) == []
    assert site.posts == 1
    http.close()


def test_max_pages_bounds_requests_even_with_fresh_cursors_and_duplicate_nodes():
    """Cursores novos só com nós repetidos: para após 2 páginas seguidas sem nó novo (antes de max_pages)."""
    site = Site()
    site.on_post = lambda n, form, request: httpx.Response(
        200, text=page_body([image_node("DUP", pk="dup")], True, f"x{n}"),
        headers={"content-type": "application/json"})
    gql, http = graphql_for(site)
    nodes = list(gql.iter_profile_posts("perfil_teste", max_media=None, max_pages=5))
    assert len(nodes) == 1 and site.posts == 3
    http.close()


def test_limit_one_makes_single_graphql_request_with_count_one():
    site = Site(pages=10)
    rig = Rig(site)
    assert len(list(rig.dl.iter_profile_media("perfil_teste", "all", 1))) == 1
    assert site.posts == 1
    assert json.loads(site.post_forms[0]["variables"])["data"]["count"] == 1


def test_limit_one_with_carousel_first_node_single_request():
    site = Site(pages=10)
    site.on_post = lambda n, form, request: httpx.Response(
        200, text=page_body([carousel_node("CAR1")], True, "c1"), headers={"content-type": "application/json"})
    rig = Rig(site)
    assert len(list(rig.dl.iter_profile_media("perfil_teste", "all", 1))) == 1
    assert site.posts == 1


# --------------------------------------------------------------------------- 7. composite


class CountingDl(InstagramDownloader):
    def __init__(self, error: Exception | None = None, items=()):
        self.error, self.items = error, list(items)
        self.calls = 0

    def test_session(self):
        raise NotImplementedError

    def get_profile(self, username):
        self.calls += 1
        if self.error:
            raise self.error
        return "ok"

    def resolve_post(self, shortcode):
        self.calls += 1
        if self.error:
            raise self.error
        return []

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        self.calls += 1
        if self.error:
            raise self.error
        return Path("x")

    def iter_profile_media(self, username, content, limit, *, cancel=None):
        self.calls += 1
        if self.error:
            raise self.error
        yield from self.items


@pytest.mark.parametrize("code", ["rate_limited", "timeout", "network_error", "session_invalid",
                                  "checkpoint_required", "action_blocked", "access_refused"])
def test_composite_never_falls_back_on_non_contract_errors(code):
    primary, fallback = CountingDl(InstagramError(code)), CountingDl()
    comp = CompositeInstagramDownloader(primary, fallback, "auto")
    with pytest.raises(InstagramError):
        comp.get_profile("x")
    with pytest.raises(InstagramError):
        list(comp.iter_profile_media("x", "all", None))
    assert primary.calls == 2 and fallback.calls == 0


def test_composite_falls_back_once_and_does_not_loop():
    primary, fallback = CountingDl(InstagramError("contract_changed")), CountingDl(InstagramError("contract_changed"))
    comp = CompositeInstagramDownloader(primary, fallback, "auto")
    with pytest.raises(InstagramError):
        comp.get_profile("x")
    with pytest.raises(InstagramError):
        list(comp.iter_profile_media("x", "all", None))
    assert primary.calls == 2 and fallback.calls == 2


def test_composite_no_fallback_after_items_were_yielded():
    class Partial(CountingDl):
        def iter_profile_media(self, username, content, limit, *, cancel=None):
            self.calls += 1
            yield "a"
            raise InstagramError("contract_changed")

    primary, fallback = Partial(), CountingDl(items=["z"])
    comp = CompositeInstagramDownloader(primary, fallback, "auto")
    gen = comp.iter_profile_media("x", "all", None)
    assert next(gen) == "a"
    with pytest.raises(InstagramError):
        next(gen)
    assert fallback.calls == 0


@pytest.mark.parametrize("mode", ["rate_limited", "timeout", "network_error"])
def test_composite_with_real_web_provider_no_fallback_on_429_timeout_network(mode):
    site = Site()

    def doc(n, request):
        if mode == "rate_limited":
            return httpx.Response(429, text="slow down", headers={"content-type": "text/html"})
        if mode == "timeout":
            raise httpx.ReadTimeout("fake")
        raise httpx.ConnectError("fake")

    site.on_doc = doc
    rig = Rig(site)
    fallback = CountingDl(items=["z"])
    comp = CompositeInstagramDownloader(rig.dl, fallback, "auto")
    with pytest.raises(InstagramError) as exc:
        list(comp.iter_profile_media("perfil_teste", "all", None))
    assert exc.value.code == mode
    assert fallback.calls == 0 and site.docs == 1
    assert rig.all_closed()
