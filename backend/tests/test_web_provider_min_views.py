"""Filtros da importação por perfil no provider web: visualizações mínimas (`min_views`, contagem da aba Reels SEM
login, web/reels_tab.py) e período de postagem (`posted_after`).

Offline (httpx.MockTransport, pilha real http + bootstrap + GraphQL + provider; aba Reels FAKE em reels_tab_fakes.py
com a estrutura verificada em 2026-10-29); só valores FAKE. Nenhuma contagem individual, código, cursor, token ou URL
pode ir para o log.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import httpx
import pytest

import test_graphql_partial_errors as gp
from app.integrations.instagram.downloader import InstagramError
from reels_tab_fakes import CSRF, DOC_ID, FRIENDLY, LSD, REELS_CURSOR, USER_ID, MirroredSite, ReelsTab, attach, tab_node
from test_graphql_partial_errors import _nothing_sensitive_in_any_log  # noqa: F401 - autouse também neste arquivo

# Contagens FAKE com dígitos inconfundíveis: nenhuma pode aparecer no log.
HIGH, EQUAL, LOW = 7_654_321, 5_000_000, 4_999_999
MIN = 5_000_000

CUTOFF = "2027-01-15T08:00:00Z"
CUTOFF_TS = int(datetime(2027, 1, 15, 8, tzinfo=timezone.utc).timestamp())
NEW, OLD = CUTOFF_TS + 3600, CUTOFF_TS - 3600


def reel(page: int, i: int, **counts) -> dict:
    """Reel FAKE (media_type 2, product_type clips). ``play_count`` aqui vira a contagem da aba Reels (MirroredSite);
    a listagem real não traz contagem e o provider ignora o campo nela."""
    node = gp.image(page, i)
    node.update(media_type=2, product_type="clips",
                video_versions=[{"url": f"{gp.SIGNED_URL}&v={page}{i}", "width": 720, "height": 1280}], **counts)
    return node


def views(approved: int = 0, below: int = 0, no_count: int = 0, out_of_period: int = 0) -> dict:
    return {"approved": approved, "below": below, "no_count": no_count, "out_of_period": out_of_period}


def listing(site: gp.Site, content: str = "reels", limit: int | None = 100, **kwargs):
    dl = gp.downloader(site)
    checkpoints = []
    items = list(dl.iter_profile_media("perfil_teste", content, limit, on_checkpoint=checkpoints.append, **kwargs))
    return dl, items, checkpoints


# --------------------------------------------------------------------------- visualizações mínimas


def test_views_come_from_the_anonymous_reels_tab_and_equal_to_the_minimum_approves(caplog):
    page = [reel(1, 0, play_count=HIGH), reel(1, 1, play_count=EQUAL), reel(1, 2, play_count=LOW), reel(1, 3),
            reel(1, 4, like_count=99_999_999), reel(1, 5, play_count="9999999"), gp.image(1, 6),
            reel(1, 7, ig_play_count=HIGH), reel(1, 8, play_count=True)]
    site = MirroredSite(gp.body(1, page, has_next=False))
    dl, items, [checkpoint] = listing(site, min_views=MIN)
    assert gp.codes(items) == ["SYNP01N00", "SYNP01N01"]
    # A imagem já saiu no filtro de conteúdo ("reels"); `ig_play_count`, string e bool não são contagem.
    assert checkpoint.page_views == views(2, 1, 5)
    assert [item.listing_views for item in items] == [views(1), views(2)]
    stats = dl.last_listing_stats
    assert (stats["views_approved"], stats["views_below"], stats["views_no_count"]) == (2, 1, 5)
    assert stats["items_compatible"] == 2, "só os aprovados contam como entregues"
    assert not hasattr(items[0], "view_count"), "a contagem não viaja no item"
    tab = site.tab
    assert (tab.gets, tab.posts) == (1, []), "uma página da aba bastou"
    cookie = tab.headers[0].get("cookie") or ""
    assert "sessionid" not in cookie and gp.FAKE_SESSION not in cookie, "a aba Reels é aberta SEM a sessão"
    for number in (HIGH, EQUAL, LOW, 99_999_999):
        assert str(number) not in caplog.text and f"{number:,}" not in caplog.text
    assert "views_approved=2 views_below=1 views_no_count=5" in caplog.text


def test_reels_tab_pages_are_fetched_on_demand_with_the_verified_request():
    page = [reel(1, i, play_count=HIGH) for i in range(14)]
    site = MirroredSite(gp.body(1, page, has_next=False), per_page=12)
    dl = gp.downloader(site)
    got = list(dl.iter_profile_media("perfil_teste", "reels", 1, min_views=MIN))
    assert len(got) == 1 and (site.tab.gets, len(site.tab.posts)) == (1, 0), "nada além do necessário"

    site = MirroredSite(gp.body(1, page, has_next=False), per_page=12)
    _, items, _ = listing(site, min_views=MIN)
    assert len(items) == 14 and site.tab.gets == 1 and len(site.tab.posts) == 1
    form, headers = site.tab.posts[0], site.tab.headers[1]
    assert (form["doc_id"], form["fb_api_req_friendly_name"], form["lsd"]) == (DOC_ID, FRIENDLY, LSD)
    assert (form["fb_api_caller_class"], form["server_timestamps"]) == ("RelayModern", "true")
    assert json.loads(form["variables"]) == {"after": f"{REELS_CURSOR}1", "first": 12, "id": USER_ID}
    assert headers["x-fb-lsd"] == LSD and headers["x-csrftoken"] == CSRF and headers["x-fb-friendly-name"] == FRIENDLY
    assert headers["x-ig-app-id"] == "936619743392459" and headers["x-asbd-id"] == "359341"
    assert headers["referer"].endswith("/perfil_teste/reels/") and "sessionid" not in (headers.get("cookie") or "")


def test_item_outside_the_reels_tab_is_no_count_by_pk_without_paging_to_the_end():
    feed_video = reel(1, 2, play_count=HIGH)
    feed_video["product_type"] = "feed"   # vídeo do feed: não está na aba Reels
    page = [reel(1, 0, play_count=HIGH), reel(1, 1, play_count=HIGH), feed_video] + [
        reel(1, i, play_count=HIGH) for i in range(3, 9)]
    site = MirroredSite(gp.body(1, page, has_next=False), per_page=2)
    _, items, [checkpoint] = listing(site, content="videos", min_views=MIN)
    assert "SYNP01N02" not in gp.codes(items) and len(items) == 8
    assert checkpoint.page_views == views(8, 0, 1)
    # Páginas da aba: 4 (8 Reels, 2 por página); o vídeo do feed não fez a busca ir além da posição dele.
    assert site.tab.gets + len(site.tab.posts) == 4


def test_old_pinned_reel_in_the_tab_does_not_end_the_search():
    nodes = [reel(1, i, play_count=HIGH) for i in range(3)]
    site = gp.Site(gp.body(1, nodes, has_next=False))
    pks = [9_000_000 - i for i in range(3)]
    for node, pk in zip(nodes, pks):
        node["pk"] = str(pk)
    tab = ReelsTab([[tab_node("ANTIGOFIX", 5, HIGH, pinned=True), tab_node(nodes[0]["code"], pks[0], HIGH)],
                    [tab_node(nodes[1]["code"], pks[1], HIGH), tab_node(nodes[2]["code"], pks[2], LOW)]])
    attach(site, tab)
    _, items, [checkpoint] = listing(site, min_views=MIN)
    assert gp.codes(items) == [nodes[0]["code"], nodes[1]["code"]] and checkpoint.page_views == views(2, 1, 0)


def test_page_size_falls_back_to_four_once_when_the_server_refuses_twelve():
    page = [reel(1, i, play_count=HIGH) for i in range(3)]
    site = MirroredSite(gp.body(1, page, has_next=False), per_page=1)
    site.tab.responses[1] = httpx.Response(200, text=json.dumps({"data": {"node": None}, "errors": [{"message": "x"}]}),
                                           headers={"content-type": "application/json"})
    _, items, _ = listing(site, min_views=MIN)
    assert len(items) == 3
    assert [json.loads(form["variables"])["first"] for form in site.tab.posts] == [12, 4, 4]


@pytest.mark.parametrize("index", [0, 1])
def test_429_on_the_reels_tab_raises_without_delivering_unfiltered_items(index):
    page = [reel(1, i, play_count=HIGH) for i in range(3)]
    site = MirroredSite(gp.body(1, page, has_next=False), per_page=1)
    site.tab.responses[index] = httpx.Response(429, text="{}", headers={"content-type": "application/json"})
    got = []
    with pytest.raises(InstagramError) as info:
        for item in gp.downloader(site).iter_profile_media("perfil_teste", "reels", 100, min_views=MIN):
            got.append(item)
    assert info.value.code == "rate_limited" and info.value.retryable
    assert len(got) == index, "só os já aprovados antes do erro; nenhum sem filtro"
    assert site.tab.requests == index + 1, "sem nova tentativa imediata"


@pytest.mark.parametrize("html", ["<html><body>nada</body></html>",
                                  '<html><script type="application/json" data-sjs>{"require":[]}</script></html>'])
def test_unexpected_reels_tab_shape_is_a_clear_error(html):
    site = MirroredSite(gp.body(1, [reel(1, 0, play_count=HIGH)], has_next=False))
    site.tab.html = html
    with pytest.raises(InstagramError) as info:
        listing(site, min_views=MIN)
    assert info.value.code == "contract_changed"
    assert info.value.message.startswith("Não foi possível ler as visualizações do perfil")


def test_redirect_on_the_reels_tab_is_access_refused_never_a_session_error():
    site = MirroredSite(gp.body(1, [reel(1, 0, play_count=HIGH)], has_next=False))
    site.tab.responses[0] = httpx.Response(302, headers={"location": "/accounts/login/"})
    with pytest.raises(InstagramError) as info:
        listing(site, min_views=MIN)
    assert info.value.code == "access_refused"


def test_non_video_items_are_no_count_with_the_filter_on():
    site = MirroredSite(gp.body(1, [reel(1, 0, play_count=HIGH), gp.image(1, 1)], has_next=False))
    _, items, [checkpoint] = listing(site, content="all", min_views=1)
    assert gp.codes(items) == ["SYNP01N00"] and checkpoint.page_views == views(1, 0, 1)


def test_limit_counts_only_approved_and_keeps_paginating():
    site = MirroredSite(
        gp.body(1, [reel(1, i, play_count=LOW) for i in range(5)] + [reel(1, 5, play_count=HIGH)]),
        gp.body(2, [reel(2, 0, play_count=LOW), reel(2, 1, play_count=HIGH), reel(2, 2, play_count=HIGH)],
                has_next=False))
    dl, items, checkpoints = listing(site, limit=2, min_views=MIN)
    assert gp.codes(items) == ["SYNP01N05", "SYNP02N01"]
    assert site.afters == [None, gp.cursor(1)]
    assert [c.page_views for c in checkpoints] == [views(1, 5)]
    assert items[1].listing_views == views(1, 1)
    assert dl.last_listing_stats["stop_reason"] == "limit_reached"


def test_cancel_in_the_middle_of_the_page_stops_without_more_items():
    cancel = threading.Event()
    site = MirroredSite(gp.body(1, [reel(1, i, play_count=HIGH) for i in range(4)], has_next=False))
    dl = gp.downloader(site)
    got = []
    for item in dl.iter_profile_media("perfil_teste", "reels", 100, cancel=cancel, min_views=MIN):
        got.append(item)
        cancel.set()
    assert len(got) == 1 and dl.last_listing_stats["stop_reason"] == "cancelled"


def test_error_in_the_middle_of_the_page_carries_the_open_page_verdicts():
    site = MirroredSite(gp.body(1, [reel(1, 0, play_count=HIGH)]),
                        gp.body(2, [reel(2, 0, play_count=LOW), reel(2, 1)], end_cursor=None,
                                errors=[{"message": "partial"}]))
    got = []
    with pytest.raises(InstagramError) as info:
        for item in gp.downloader(site).iter_profile_media("perfil_teste", "reels", 100, min_views=MIN):
            got.append(item)
    assert gp.codes(got) == ["SYNP01N00"]
    assert info.value.listing_views == views(0, 1, 1)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "500000"])
def test_invalid_min_views_is_refused_before_any_request(bad):
    site = MirroredSite(gp.body(1, [reel(1, 0, play_count=HIGH)], has_next=False))
    with pytest.raises(InstagramError) as info:
        list(gp.downloader(site).iter_profile_media("perfil_teste", "reels", 10, min_views=bad))
    assert info.value.code == "invalid_input" and site.afters == [] and site.gets == 0 and site.tab.requests == 0


def test_filters_off_is_identical_to_the_listing_without_the_arguments(caplog):
    pages = (gp.body(1, [reel(1, 0, play_count=HIGH), gp.image(1, 1)]),
             gp.body(2, [reel(2, 0, play_count=LOW), gp.image(2, 1)], has_next=False))
    runs = []
    for kwargs in ({}, {"min_views": None, "posted_after": None}):
        caplog.clear()
        site = MirroredSite(*json.loads(json.dumps(pages)))
        dl, items, checkpoints = listing(site, content="all", **kwargs)
        assert site.tab.requests == 0, "sem filtro de visualizações a aba Reels nunca é aberta"
        runs.append((site.afters, [(i.source_post_id, i.media_url, i.listing_views) for i in items],
                     [(repr(c), c.page_counts, c.page_views, c.period_end) for c in checkpoints],
                     dict(dl.last_listing_stats), [r.getMessage() for r in caplog.records if "filtros" in r.getMessage()]))
    assert runs[0] == runs[1]
    _, items, checkpoints, stats, lines = runs[0]
    assert len(items) == 4 and all(v is None for *_, v in items)
    assert all(pv is None and not end for *_, pv, end in checkpoints)
    assert not any(key.startswith("views_") for key in stats) and lines == []


# --------------------------------------------------------------------------- período de postagem


def post(page: int, i: int, taken_at: int | None, pinned: object = "absent") -> dict:
    node = reel(page, i, play_count=HIGH)
    if taken_at is None:
        del node["taken_at"]
    else:
        node["taken_at"] = taken_at
    if pinned != "absent":
        node["timeline_pinned_user_ids"] = pinned
    return node


def test_first_old_unpinned_post_ends_the_listing_without_asking_more_pages():
    page = [post(1, 0, NEW, []), post(1, 1, NEW, []), post(1, 2, OLD, []), post(1, 3, NEW, [])]
    site = MirroredSite(gp.body(1, page), gp.body(2, [post(2, 0, NEW, [])], has_next=False))
    dl, items, [checkpoint] = listing(site, posted_after=CUTOFF)
    assert gp.codes(items) == ["SYNP01N00", "SYNP01N01"]
    assert site.afters == [None], "a página 2 nunca é pedida"
    assert (checkpoint.has_next, checkpoint.period_end, checkpoint.page_views) == (False, True, views(2, 0, 0, 1))
    assert dl.last_listing_stats["stop_reason"] == "period_end" and site.tab.requests == 0


def test_old_pinned_posts_are_skipped_without_ending():
    page = [post(1, 0, OLD, ["1"]), post(1, 1, OLD, ["1"]), post(1, 2, NEW, []), post(1, 3, OLD, [])]
    site = MirroredSite(gp.body(1, page), gp.body(2, [post(2, 0, NEW, [])], has_next=False))
    _, items, [checkpoint] = listing(site, posted_after=CUTOFF)
    assert gp.codes(items) == ["SYNP01N02"] and checkpoint.period_end
    assert checkpoint.page_views == views(1, 0, 0, 3)


def test_without_the_pinned_field_it_ends_only_after_four_old_posts_in_a_row():
    top = [post(1, i, OLD) for i in range(3)]           # 3 antigos no topo (podem ser fixados)
    middle = [post(1, 3, NEW), post(1, 4, None), post(1, 5, OLD), post(1, 6, NEW)]
    tail = [post(1, 7 + i, OLD) for i in range(4)] + [post(1, 11, NEW)]
    site = MirroredSite(gp.body(1, top + middle + tail), gp.body(2, [post(2, 0, NEW)], has_next=False))
    _, items, [checkpoint] = listing(site, posted_after=CUTOFF)
    assert gp.codes(items) == ["SYNP01N03", "SYNP01N06"]
    assert site.afters == [None] and checkpoint.period_end
    # Sem data conta como fora do período e não mexe na sequência.
    assert checkpoint.page_views == views(2, 0, 0, 9)


def test_period_and_views_together_only_look_up_views_inside_the_period():
    page = [post(1, 0, NEW, []), reel(1, 1, play_count=LOW, taken_at=NEW), post(1, 2, OLD, [])]
    site = MirroredSite(gp.body(1, page), gp.body(2, [post(2, 0, NEW, [])], has_next=False))
    _, items, [checkpoint] = listing(site, min_views=MIN, posted_after=CUTOFF)
    assert gp.codes(items) == ["SYNP01N00"] and checkpoint.page_views == views(1, 1, 0, 1)


@pytest.mark.parametrize("taken", [str(NEW), float(NEW) + 0.5, str(OLD), float(OLD)])
def test_taken_at_accepts_the_same_formats_as_normalize(taken):
    page = [post(1, 0, NEW, []), post(1, 1, NEW, [])]
    page[1]["taken_at"] = taken
    site = MirroredSite(gp.body(1, page, has_next=False))
    _, items, [checkpoint] = listing(site, posted_after=CUTOFF)
    inside = int(float(taken)) >= CUTOFF_TS
    assert len(items) == (2 if inside else 1)
    assert checkpoint.period_end is (not inside)
    assert items[0].taken_at is not None


def test_taken_at_that_normalize_refuses_is_out_of_period_without_ending():
    page = [post(1, 0, NEW, []), post(1, 1, NEW, []), post(1, 2, NEW, [])]
    page[0]["taken_at"] = "ontem"
    page[1]["taken_at"] = {"x": 1}
    site = MirroredSite(gp.body(1, page, has_next=False))
    _, items, [checkpoint] = listing(site, posted_after=CUTOFF)
    assert gp.codes(items) == ["SYNP01N02"] and not checkpoint.period_end
    assert checkpoint.page_views == views(1, 0, 0, 2)


@pytest.mark.parametrize("bad", ["2027-01-15", "ontem", 1800000000])
def test_invalid_cutoff_is_refused_before_any_request(bad):
    site = MirroredSite(gp.body(1, [post(1, 0, NEW)], has_next=False))
    with pytest.raises(InstagramError) as info:
        list(gp.downloader(site).iter_profile_media("perfil_teste", "reels", 10, posted_after=bad))
    assert info.value.code == "invalid_input" and site.gets == 0
