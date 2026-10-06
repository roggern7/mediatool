"""Paginação (sem parada por improdutividade nem teto de páginas), retomada, ListingRef e refresh_page do provider
web (offline: httpx.MockTransport).

Perfis sintéticos gerados sob demanda pelo cursor (nada de páginas guardadas); valores FAKE.
"""

from __future__ import annotations

import gc
import json
import logging
import threading
import tracemalloc
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest

from app.integrations.instagram.downloader import InstagramError, ListingCheckpoint, ListingRef
from app.integrations.instagram.session import InstagramSession
from app.integrations.instagram.web import budget
from app.integrations.instagram.web.provider import InstagramWebDownloader
from media_fakes import FAKE_SESSION

FIXTURES = Path(__file__).parent / "fixtures" / "instagram_web"
ROOT = "xdt_api__v1__feed__user_timeline_graphql_connection"
CDN = "https://scontent-fake.cdninstagram.com/v/t51.fake"
CURSOR = "FAKE_CUR_"
PK_PREFIX = "77"
SECRET_MARKERS = (CURSOR, "cdninstagram", "SYNP", PK_PREFIX + "000")


class FakeProvider:
    def get(self):
        return InstagramSession({"sessionid": FAKE_SESSION})


def image(code: str, pk: str) -> dict:
    return {"code": code, "pk": pk, "media_type": 1, "product_type": "feed", "taken_at": 1700000000,
            "image_versions2": {"candidates": [{"url": f"{CDN}/{code}.jpg", "width": 640, "height": 640}]}}


def reel(code: str, pk: str) -> dict:
    return {"code": code, "pk": pk, "media_type": 2, "product_type": "clips", "taken_at": 1700000000,
            "video_versions": [{"url": f"{CDN}/{code}.mp4", "width": 720, "height": 1280}],
            "image_versions2": {"candidates": [{"url": f"{CDN}/{code}-t.jpg", "width": 64, "height": 64}]}}


def invalid(code: str, pk: str) -> dict:
    return {"pk": pk, "media_type": 1}   # sem `code`: não normaliza


KINDS = {"image": image, "reel": reel, "invalid": invalid}


class SyntheticInstagram:
    """GET / (bootstrap) + POST /graphql/query com páginas geradas na hora a partir do cursor `after`.

    ``kind(page, i)`` -> "image" | "reel" | "invalid". Guarda só contadores e o último pedido (e, se ``record``, a
    lista de (after, count) dos POSTs)."""

    def __init__(self, total_pages: int, kind=lambda page, i: "image", *, per_page: int = 12,
                 repeat_cursor_at: int | None = None, record: bool = True):
        self.total_pages = total_pages
        self.kind = kind
        self.per_page = per_page
        self.repeat_cursor_at = repeat_cursor_at
        self.record = record
        self.gets = 0
        self.posts = 0
        self.calls: list[tuple[str | None, int]] = []
        self.bootstrap_html = (FIXTURES / "bootstrap_input.html").read_text(encoding="utf-8")
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/":
            self.gets += 1
            return httpx.Response(200, text=self.bootstrap_html, headers={"content-type": "text/html; charset=utf-8"})
        if request.method == "POST" and request.url.path == "/graphql/query":
            self.posts += 1
            variables = json.loads(dict(parse_qsl(request.content.decode()))["variables"])
            after = variables.get("after")
            count = variables["data"]["count"]
            if self.record:
                self.calls.append((after, count))
            page = 1 if after is None else int(after[len(CURSOR):]) + 1
            return httpx.Response(200, text=self.page_json(page, count), headers={"content-type": "application/json"})
        return httpx.Response(404, text="{}", headers={"content-type": "application/json"})

    def page_json(self, page: int, count: int) -> str:
        edges = []
        if page <= self.total_pages:
            for i in range(min(count, self.per_page)):
                code, pk = f"SYNP{page:06d}N{i:02d}", f"{PK_PREFIX}{page:07d}{i:02d}"
                edges.append({"node": KINDS[self.kind(page, i)](code, pk)})
        has_next = page < self.total_pages
        cursor = f"{CURSOR}{page - 1 if page == self.repeat_cursor_at else page}" if has_next else None
        return json.dumps({"data": {ROOT: {"edges": edges,
                                           "page_info": {"end_cursor": cursor, "has_next_page": has_next}}}})


def downloader(site: SyntheticInstagram, env: dict | None = None) -> InstagramWebDownloader:
    return InstagramWebDownloader(FakeProvider(), transport=site.transport, page_delay=(0, 0), env=env)


def assert_no_secrets(text: str) -> None:
    for marker in SECRET_MARKERS:
        assert marker not in text, marker


# --------------------------------------------------------------------------- sem orçamento de páginas


def test_budget_numbers():
    # O orçamento antigo (teto proporcional, páginas improdutivas, rendimento baixo) saiu: só o tamanho de página.
    assert budget.PAGE_SIZE == 12
    for gone in ("max_pages_for", "ListingBudget", "SAFETY_FACTOR", "STOP_FILTER_EXHAUSTED", "STOP_LOW_YIELD"):
        assert not hasattr(budget, gone), gone


def test_old_budget_env_no_longer_stops_the_listing():
    env = {"MEDIA_TOOL_IG_UNPRODUCTIVE_PAGES": "1", "MEDIA_TOOL_IG_UNPRODUCTIVE_PAGES_FILTERED": "1",
           "MEDIA_TOOL_IG_MIN_PAGES_FOR_YIELD": "1", "MEDIA_TOOL_IG_MIN_YIELD": "12"}
    site = SyntheticInstagram(9)   # só fotos
    dl = downloader(site, env)
    assert list(dl.iter_profile_media("perfil_teste", "reels", 10)) == []
    assert site.posts == 9 and dl.last_listing_stats["stop_reason"] == "end_of_profile"


def test_no_page_cap_for_any_limit_or_content():
    seen = {}

    class Recorder:
        last_viewer_id = None
        current_page = None

        def iter_profile_posts(self, username, **kwargs):
            seen[username] = (kwargs["max_pages"], kwargs["max_media"])
            return iter(())

    class Boot:
        def get(self, *, force=False):
            return None

    for content, limit in (("all", 10000), ("reels", 10), ("photos_carousels", 1_000_000), ("all", None)):
        dl = InstagramWebDownloader(FakeProvider(), bootstrap_factory=lambda h: Boot(),
                                    graphql_factory=lambda h, b: Recorder())
        assert list(dl.iter_profile_media(f"perfil_{content}", content, limit)) == []
        assert seen[f"perfil_{content}"] == (None, limit), "sem teto de páginas nem de quantidade"


# --------------------------------------------------------------------------- páginas improdutivas não encerram


def test_ten_reels_in_profile_full_of_photos_goes_to_the_real_end(caplog):
    caplog.set_level("DEBUG")
    site = SyntheticInstagram(40)   # só fotos
    dl = downloader(site)
    assert list(dl.iter_profile_media("perfil_teste", "reels", 10)) == []
    assert site.posts == 40 and site.gets == 1   # antes parava em 6 páginas (filter_exhausted)
    assert dl.last_listing_stats == {"pages": 40, "posts_seen": 478, "items_compatible": 0,
                                     "items_skipped_filter": 478, "duplicates": 0, "stop_reason": "end_of_profile"}
    assert_no_secrets(caplog.text)


def test_sparse_reels_reach_the_requested_quantity():
    site = SyntheticInstagram(500, lambda page, i: "reel" if page % 2 == 0 and i == 0 else "image")
    dl = downloader(site)
    items = list(dl.iter_profile_media("perfil_teste", "reels", 10))
    # Antes: 5 itens em 10 páginas (budget_exhausted). Agora: os 10 pedidos, sem página extra.
    assert len(items) == 10 and site.posts == 20 and dl.last_listing_stats["stop_reason"] == "limit_reached"


def test_low_yield_does_not_stop(caplog):
    caplog.set_level("INFO")
    site = SyntheticInstagram(60, lambda page, i: "reel" if page % 5 == 0 and i == 0 else "image")
    dl = downloader(site)
    items = list(dl.iter_profile_media("perfil_teste", "reels", 100))
    assert len(items) == 12 and site.posts == 60   # antes parava em 20 páginas (low_yield)
    assert dl.last_listing_stats["stop_reason"] == "end_of_profile"
    assert "stop_reason=end_of_profile" in caplog.text and "low_yield" not in caplog.text
    assert_no_secrets(caplog.text)


def test_no_compatible_media_for_all_goes_to_the_real_end():
    site = SyntheticInstagram(30, lambda page, i: "invalid")
    dl = downloader(site)
    assert list(dl.iter_profile_media("perfil_teste", "all", 10)) == []
    assert site.posts == 30 and dl.last_listing_stats["stop_reason"] == "end_of_profile"   # antes: 3 páginas


def test_unproductive_streak_never_stops():
    site = SyntheticInstagram(30, lambda page, i: "reel" if page == 4 and i == 0 else "image")
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "reels", 100))) == 1
    assert site.posts == 30 and dl.last_listing_stats["stop_reason"] == "end_of_profile"


def test_repeated_cursor_is_an_error():
    site = SyntheticInstagram(50, repeat_cursor_at=3)
    dl = downloader(site)
    with pytest.raises(InstagramError) as info:
        list(dl.iter_profile_media("perfil_teste", "all", 100))
    assert info.value.code == "contract_changed" and site.posts == 3
    assert dl.last_listing_stats["stop_reason"] == "error"


@pytest.mark.parametrize("limit", [10, 20, 50, 100])
def test_quantity_is_reached_with_one_item_per_page(limit):
    # 1 item por página (11 nós inválidos): antes parava no teto proporcional; agora entrega a quantidade pedida.
    site = SyntheticInstagram(500, lambda page, i: "image" if i == 0 else "invalid")
    dl = downloader(site)
    items = list(dl.iter_profile_media("perfil_teste", "all", limit))
    assert len(items) == limit == site.posts and dl.last_listing_stats["stop_reason"] == "limit_reached"


@pytest.mark.parametrize("content, kind", [("reels", "reel"), ("videos", "reel"), ("images", "image"),
                                           ("photos_carousels", "image")])
def test_quantity_is_reached_with_filter(content, kind):
    other = "image" if kind == "reel" else "reel"
    site = SyntheticInstagram(500, lambda page, i: kind if page % 2 == 0 and i == 0 else other)
    dl = downloader(site)
    items = list(dl.iter_profile_media("perfil_teste", content, 10))
    assert len(items) == 10 and site.posts == 20
    assert dl.last_listing_stats["stop_reason"] == "limit_reached"


def test_limit_reached_makes_no_extra_page_and_logs_last_page(caplog):
    caplog.set_level("INFO")
    site = SyntheticInstagram(50)
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 20))) == 20
    assert site.posts == 2 and dl.last_listing_stats["stop_reason"] == "limit_reached"
    assert "GraphQL página 2: 12 nós (12 novos)" in caplog.text
    assert_no_secrets(caplog.text)


def test_cancel_stops_without_more_posts():
    site = SyntheticInstagram(50)
    cancel = threading.Event()
    dl = downloader(site)
    received = 0
    for _ in dl.iter_profile_media("perfil_teste", "all", 100, cancel=cancel):
        received += 1
        if received == 13:   # 1o item da página 2
            cancel.set()
    assert received == 13 and site.posts == 2 and dl.last_listing_stats["stop_reason"] == "cancelled"


def test_end_of_profile_is_reported():
    site = SyntheticInstagram(2)
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "all", 100))) == 24
    assert site.posts == 2 and dl.last_listing_stats["stop_reason"] == "end_of_profile"


# --------------------------------------------------------------------------- ListingRef e refresh_page


def test_listing_ref_per_page():
    site = SyntheticInstagram(50)
    items = list(downloader(site).iter_profile_media("perfil_teste", "all", 30))
    refs = [i.listing_ref for i in items]
    assert refs[0] == ListingRef(page_index=1, cursor=None, page_size=12)
    assert refs[12] == ListingRef(page_index=2, cursor=f"{CURSOR}1", page_size=12)   # end_cursor da página 1
    assert refs[24] == ListingRef(page_index=3, cursor=f"{CURSOR}2", page_size=12)
    assert len({id(r) for r in refs[:12]}) == 1
    assert f"{CURSOR}1" not in repr(refs[12]) and f"{CURSOR}1" not in repr(items[12])
    assert site.calls == [(None, 12), (f"{CURSOR}1", 12), (f"{CURSOR}2", 12)]
    small = SyntheticInstagram(50)
    assert list(downloader(small).iter_profile_media("perfil_teste", "all", 10))[0].listing_ref.page_size == 10


@pytest.mark.parametrize("ref, expected_after", [
    (ListingRef(page_index=3, cursor=f"{CURSOR}2", page_size=12), f"{CURSOR}2"),
    (ListingRef(page_index=1, cursor=None, page_size=10), None),
])
def test_refresh_page_is_bootstrap_plus_one_post(ref, expected_after, caplog):
    caplog.set_level("DEBUG")
    site = SyntheticInstagram(50, lambda page, i: "reel" if i % 2 else "image")
    items = downloader(site).refresh_page("perfil_teste", ref)
    assert site.gets == 1 and site.posts == 1 and site.calls == [(expected_after, ref.page_size)]
    page = 1 if expected_after is None else 3
    assert [(i.source_post_id, i.source_media_index) for i in items] == [
        (f"SYNP{page:06d}N{n:02d}", 0) for n in range(ref.page_size)]
    assert {i.media_type for i in items} == {"image", "video"}   # sem filtro de conteúdo
    assert all(i.listing_ref is ref for i in items)
    assert_no_secrets(caplog.text)


def test_refresh_page_validation_and_cancel():
    site = SyntheticInstagram(5)
    dl = downloader(site)
    with pytest.raises(InstagramError) as info:
        dl.refresh_page("perfil_teste", ListingRef(page_index=1, cursor=None, page_size=0))
    assert info.value.code == "invalid_input"
    cancel = threading.Event()
    cancel.set()
    assert dl.refresh_page("perfil_teste", ListingRef(page_index=1, cursor=None, page_size=12), cancel=cancel) == []
    assert site.gets == 0 and site.posts == 0


# --------------------------------------------------------------------------- checkpoint e retomada


def test_checkpoint_after_all_items_of_each_page():
    site = SyntheticInstagram(3)
    received: list = []
    checkpoints: list[tuple[int, ListingCheckpoint]] = []
    dl = downloader(site)
    for item in dl.iter_profile_media("perfil_teste", "all", 100,
                                      on_checkpoint=lambda cp: checkpoints.append((len(received), cp))):
        received.append(item.source_post_id)
    assert [n for n, _ in checkpoints] == [12, 24, 36]
    cps = [cp for _, cp in checkpoints]
    assert [cp.resume_cursor for cp in cps] == [f"{CURSOR}1", f"{CURSOR}2", None]
    assert [cp.next_page_index for cp in cps] == [2, 3, 4]
    assert [cp.has_next for cp in cps] == [True, True, False]
    assert [(cp.pages, cp.posts_seen, cp.items_compatible, cp.unproductive_streak) for cp in cps] == [
        (1, 12, 12, 0), (2, 24, 24, 0), (3, 36, 36, 0)]
    assert CURSOR not in repr(cps[0]) and "redacted" in repr(cps[0])


def test_checkpoint_has_next_false_only_at_the_real_end():
    site = SyntheticInstagram(8)   # só fotos: nenhuma página rende Reel
    cps: list[ListingCheckpoint] = []
    assert list(downloader(site).iter_profile_media("perfil_teste", "reels", 10, on_checkpoint=cps.append)) == []
    assert [cp.unproductive_streak for cp in cps] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert [cp.has_next for cp in cps] == [True] * 7 + [False]


def test_resume_does_not_redo_previous_pages_and_counters_continue():
    site = SyntheticInstagram(5)
    cps: list[ListingCheckpoint] = []
    dl = downloader(site)
    gen = dl.iter_profile_media("perfil_teste", "all", 100, on_checkpoint=cps.append)
    first = [next(gen) for _ in range(25)]   # páginas 1 e 2 inteiras + 1o item da página 3
    gen.close()   # backpressure: fecha no meio da página 3
    assert len(cps) == 2 and site.posts == 3 and dl.last_listing_stats["stop_reason"] == "consumer_closed"
    resume = cps[-1]
    site.calls.clear()
    more: list[ListingCheckpoint] = []
    rest = list(dl.iter_profile_media("perfil_teste", "all", 100, resume=resume, on_checkpoint=more.append))
    assert site.calls[0] == (f"{CURSOR}2", 12) and len(site.calls) == 3   # páginas 3, 4 e 5; nada das 1-2
    codes = [i.source_post_id for i in first[:24]] + [i.source_post_id for i in rest]
    assert codes == [f"SYNP{p:06d}N{n:02d}" for p in range(1, 6) for n in range(12)]   # nada pulado
    assert rest[0].listing_ref == ListingRef(page_index=3, cursor=f"{CURSOR}2", page_size=12)
    assert [(cp.pages, cp.posts_seen, cp.items_compatible, cp.next_page_index) for cp in more] == [
        (3, 36, 36, 4), (4, 48, 48, 5), (5, 60, 60, 6)]
    assert more[-1].has_next is False and dl.last_listing_stats["pages"] == 5


def test_resume_with_long_unproductive_streak_keeps_listing():
    site = SyntheticInstagram(7)
    resume = ListingCheckpoint(resume_cursor=f"{CURSOR}4", next_page_index=5, pages=4, posts_seen=48,
                               items_compatible=0, unproductive_streak=4, has_next=True)
    dl = downloader(site)
    assert list(dl.iter_profile_media("perfil_teste", "reels", 10, resume=resume)) == []
    assert site.calls == [(f"{CURSOR}4", 12), (f"{CURSOR}5", 12), (f"{CURSOR}6", 12)]   # antes parava na 6a página
    assert dl.last_listing_stats["stop_reason"] == "end_of_profile" and dl.last_listing_stats["pages"] == 7


def test_resume_with_low_yield_keeps_listing():
    site = SyntheticInstagram(30, lambda page, i: "reel" if page % 5 == 0 and i == 0 else "image")
    resume = ListingCheckpoint(resume_cursor=f"{CURSOR}19", next_page_index=20, pages=19, posts_seen=228,
                               items_compatible=3, unproductive_streak=4, has_next=True)
    dl = downloader(site)
    assert len(list(dl.iter_profile_media("perfil_teste", "videos", 100, resume=resume))) == 3
    assert site.posts == 11 and dl.last_listing_stats["stop_reason"] == "end_of_profile"   # antes: 1 página


def test_resume_of_finished_listing_makes_no_request():
    site = SyntheticInstagram(5)
    done = ListingCheckpoint(resume_cursor=None, next_page_index=6, pages=5, posts_seen=60, items_compatible=60,
                             unproductive_streak=0, has_next=False)
    dl = downloader(site)
    assert list(dl.iter_profile_media("perfil_teste", "all", 100, resume=done)) == []
    assert site.gets == 0 and site.posts == 0 and dl.last_listing_stats["stop_reason"] == "nothing_to_resume"


# --------------------------------------------------------------------------- memória estável


def _peak_for(pages: int) -> int:
    site = SyntheticInstagram(pages, lambda page, i: "image" if i % 2 else "reel", record=False)
    dl = downloader(site)
    gc.collect()
    tracemalloc.reset_peak()
    base = tracemalloc.get_traced_memory()[0]
    delivered = 0
    for _ in dl.iter_profile_media("perfil_teste", "images", 10000):
        delivered += 1
    peak = tracemalloc.get_traced_memory()[1] - base
    assert site.posts == pages and delivered == pages * 6
    assert dl.last_listing_stats["stop_reason"] == "end_of_profile"
    return peak


def test_long_listing_memory_is_stable():
    """1.000 páginas sintéticas: o pico não cresce com o número de páginas (fora o conjunto de ids/cursores vistos
    do trecho, ~poucos bytes por nó, que o contrato permite)."""
    logging.disable(logging.CRITICAL)   # a captura de log do pytest guardaria um registro por página
    tracemalloc.start()
    try:
        _peak_for(20)   # aquece imports/caches
        small = _peak_for(100)
        large = _peak_for(1000)
    finally:
        tracemalloc.stop()
        logging.disable(logging.NOTSET)
    # Acumular nós/respostas custaria dezenas de KB por página; o conjunto de ids do trecho custa ~1-2 KB/página.
    per_page = (large - small) / 900
    assert per_page < 3000, (small, large, per_page)


# --------------------------------------------------------------------------- "Todos" e N livre na API


def test_limit_all_and_free_n_are_accepted(harness):
    base = {"profile": "perfil_teste", "content_type": "all", "preset_id": None, "process": False}
    response = harness.client.post("/api/imports", json={**base, "limit": None})
    assert response.status_code == 202, response.text
    assert response.json()["requested_limit"] is None and response.json()["listing_end"] is None
    for limit in (1, 10, 37, 100, 12345, 1_000_000):  # perfis distintos: o mesmo perfil com listagem aberta dá 409
        response = harness.client.post("/api/imports", json={**base, "profile": f"perfil_{limit}", "limit": limit})
        assert response.status_code == 202 and response.json()["requested_limit"] == limit
    for bad in (0, -1, 1_000_001, 1.5, "10", True):
        response = harness.client.post("/api/imports", json={**base, "profile": "perfil_ruim", "limit": bad})
        assert response.status_code == 422, bad
    assert harness.client.get("/api/batches").json()["total"] == 7
