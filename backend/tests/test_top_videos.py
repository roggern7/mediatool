"""Top vídeos de um perfil (web/top_reels.py, services/top_videos.py, api/top_videos.py): aba Reels SEM login, 100%
offline com os fakes da aba Reels (reels_tab_fakes.py) e do retrato de perfil (profile_fakes.py)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import httpx
import pytest

from app.integrations.instagram.web.http import InstagramWebHttp
from app.integrations.instagram.web.reels_timeline import reel_taken_at_from_pk
from app.integrations.instagram.web.top_reels import TopReel, rank, scan_top_reels
from app.services.top_videos import TopVideosService
from conftest import wait_until
from profile_fakes import ERROR_ROUTE, profile_html, user_envelope
from reels_tab_fakes import CSRF, LSD, REELS_CURSOR, ReelsTab, tab_node
from test_graphql_partial_errors import assert_safe

PK = 3_600_000_000_000_000_000   # pk realista (2025): a data derivada cai entre 2010 e 2100
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 60


def code(page: int, i: int) -> str:
    return f"TOPP{page:02d}N{i:02d}"


def pages_of(*views_by_page: list[int], pinned_first: int | None = None) -> list[list[dict]]:
    """Páginas da aba com ``pk`` decrescente; ``pinned_first`` = views de um Reel fixado no topo da 1a página."""
    pages, pk = [], PK
    for p, views in enumerate(views_by_page, start=1):
        page = []
        for i, count in enumerate(views):
            pk -= 1_000_000
            page.append(tab_node(code(p, i), pk, count))
        pages.append(page)
    if pinned_first is not None:
        pages[0].insert(0, tab_node("TOPPINNED0", PK - 10**15, pinned_first, pinned=True))
    return pages


def handler_of(tab: ReelsTab):
    def handle(request: httpx.Request) -> httpx.Response:
        answer = tab.handle(request)
        return answer if answer is not None else httpx.Response(404)
    return handle


def scan(tab: ReelsTab, *, top: int = 10, scan_limit: int = 120, before=lambda: True):
    http = InstagramWebHttp.anonymous(transport=httpx.MockTransport(handler_of(tab)))
    try:
        return scan_top_reels(http, "perfil_teste", top=top, scan_limit=scan_limit, before_request=before)
    finally:
        http.close()


@pytest.fixture(autouse=True)
def _nothing_sensitive_in_any_log(caplog):
    caplog.set_level("DEBUG")
    yield
    assert_safe(caplog.text)
    for secret in (LSD, CSRF, REELS_CURSOR, "fake-thumb.jpg", "8080808080"):
        assert secret not in caplog.text


# ---- leitura e ranking ----------------------------------------------------------------------------------------------


def test_ranks_by_views_across_pages_with_pinned_marked_and_date_from_pk():
    tab = ReelsTab(pages_of([10, 500, 30], [9000, 20], [70], pinned_first=1000))
    result = scan(tab, top=4)
    assert [item.code for item in result.items] == [code(2, 0), "TOPPINNED0", code(1, 1), code(3, 0)]
    assert [item.views for item in result.items] == [9000, 1000, 500, 70]
    assert [item.pinned for item in result.items] == [False, True, False, False]
    assert result.items[0].posted_at == reel_taken_at_from_pk(tab.pages[1][0]["pk"])
    assert result.items[0].thumbnail and result.items[0].thumbnail.startswith("https://")
    assert (result.scanned, result.pages, result.cancelled) == (7, 3, False)
    assert tab.gets == 1 and len(tab.posts) == 2
    assert [json.loads(form["variables"])["first"] for form in tab.posts] == [12, 12]


def test_scan_limit_stops_paging_and_counts_only_the_limit():
    tab = ReelsTab(pages_of([1] * 12, [2] * 12, [99999] * 12))
    result = scan(tab, scan_limit=13)
    assert result.scanned == 13 and result.pages == 2 and len(tab.posts) == 1
    assert all(item.views in (1, 2) for item in result.items)


def test_end_of_tab_before_the_limit_completes_with_what_exists():
    result = scan(ReelsTab(pages_of([5, 6])), scan_limit=600)
    assert (result.scanned, result.pages) == (2, 1)
    assert [item.views for item in result.items] == [6, 5]


def test_reels_without_count_or_with_hidden_count_stay_out_of_the_ranking():
    page = [tab_node("TOPNOCOUNT", PK - 1), tab_node("TOPHIDDEN0", PK - 2, 5000), tab_node("TOPCOUNTED", PK - 3, 7)]
    page[1]["like_and_view_counts_disabled"] = True
    result = scan(ReelsTab([page]))
    assert [(item.code, item.views) for item in result.items] == [("TOPCOUNTED", 7)]
    assert result.scanned == 3


def test_tie_keeps_the_newest_first_and_top_cuts():
    reels = [TopReel(code=f"C{i:04d}", views=v, posted_at=None, pinned=False, index=i) for i, v in
             enumerate([5, 9, 5, 9])]
    assert [reel.code for reel in rank(reels, 3)] == ["C0001", "C0003", "C0000"]


def test_cancel_before_a_request_returns_what_was_read():
    calls = []

    def before():
        calls.append(1)
        return len(calls) < 2

    tab = ReelsTab(pages_of([3, 4], [100]))
    result = scan(tab, before=before)
    assert result.cancelled and result.scanned == 2 and tab.posts == []


# ---- erros ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("response", [
    httpx.Response(404, text="<html></html>", headers={"content-type": "text/html"}),
    httpx.Response(200, text="<html><body>" + ERROR_ROUTE + "</body></html>", headers={"content-type": "text/html"}),
])
def test_missing_profile_is_profile_not_found(response):
    with pytest.raises(Exception) as info:
        scan(ReelsTab([[]], responses={0: response}))
    assert info.value.code == "profile_not_found"


def test_private_profile_is_profile_private():
    html = profile_html(user_envelope("perfil_teste", is_private=True), connection=None)
    with pytest.raises(Exception) as info:
        scan(ReelsTab([[]], html=html))
    assert info.value.code == "profile_private"


@pytest.mark.parametrize("index", [0, 1])
def test_429_is_rate_limited_without_a_new_attempt(index):
    tab = ReelsTab(pages_of([1], [2]), responses={index: httpx.Response(429, text="{}", headers={
        "content-type": "application/json"})})
    with pytest.raises(Exception) as info:
        scan(tab)
    assert info.value.code == "rate_limited" and tab.requests == index + 1


def test_unknown_shape_is_contract_changed_with_its_own_message():
    with pytest.raises(Exception) as info:
        scan(ReelsTab([[]], html='<html><script type="application/json" data-sjs>{"require":[]}</script></html>'))
    assert info.value.code == "contract_changed"
    assert "listar os vídeos com mais visualizações" in info.value.message


def test_redirect_to_login_is_access_refused():
    tab = ReelsTab([[]], responses={0: httpx.Response(302, headers={"location": "/accounts/login/"})})
    with pytest.raises(Exception) as info:
        scan(tab)
    assert info.value.code == "access_refused"


# ---- rotas ---------------------------------------------------------------------------------------------------------


def install(harness, tab: ReelsTab, thumbs: dict | None = None) -> list[str]:
    fetched: list[str] = []

    def thumb(url: str) -> tuple[bytes, str]:
        fetched.append(url)
        if thumbs is None:
            raise RuntimeError("cdn fora")
        return thumbs.get(url, (JPEG, "image/jpeg"))

    harness.ctx.top_videos = TopVideosService(
        harness.ctx, thumb_fetcher=thumb,
        http_factory=lambda: InstagramWebHttp.anonymous(transport=httpx.MockTransport(handler_of(tab)),
                                                        gate=harness.ctx.instagram_gate))
    return fetched


def finished(harness, search_id: str) -> dict:
    def done() -> bool:
        return harness.client.get(f"/api/top-videos/{search_id}").json()["status"] != "running"
    assert wait_until(done)
    return harness.client.get(f"/api/top-videos/{search_id}").json()


def test_route_runs_in_background_and_returns_the_ranking(harness):
    tab = ReelsTab(pages_of([10, 500], [9000]))
    fetched = install(harness, tab, thumbs={})
    response = harness.client.post("/api/top-videos", json={"profile": "https://www.instagram.com/Perfil_Teste/",
                                                            "top": 2, "scan_limit": 12})
    assert response.status_code == 202, response.text
    body = finished(harness, response.json()["id"])
    assert body["status"] == "completed" and body["profile"] == "perfil_teste"
    assert (body["scanned"], body["pages"]) == (3, 2)
    assert [(item["code"], item["views"]) for item in body["items"]] == [(code(2, 0), 9000), (code(1, 1), 500)]
    first = body["items"][0]
    assert first["url"] == f"https://www.instagram.com/reel/{code(2, 0)}/" and first["pinned"] is False
    assert datetime.fromisoformat(first["posted_at"].replace("Z", "+00:00")).tzinfo == timezone.utc
    assert first["thumbnail_url"] == f"/api/top-videos/{body['id']}/thumb/{code(2, 0)}"
    assert "cdninstagram" not in json.dumps(body), "a URL da CDN nunca sai do servidor"

    thumb = harness.client.get(first["thumbnail_url"])
    assert thumb.status_code == 200 and thumb.content == JPEG and thumb.headers["content-type"] == "image/jpeg"
    assert harness.client.get(first["thumbnail_url"]).status_code == 200
    assert len(fetched) == 1, "a 2a leitura vem do cache"
    assert harness.client.get(f"/api/top-videos/{body['id']}/thumb/OUTROCODE").status_code == 404


def test_thumbnail_failure_and_missing_url(harness):
    page = [tab_node("TOPWITHURL", PK - 1, 5), tab_node("TOPNOURL00", PK - 2, 4)]
    del page[1]["display_uri"]
    install(harness, ReelsTab([page]), thumbs=None)
    search_id = harness.client.post("/api/top-videos", json={"profile": "@perfil_teste"}).json()["id"]
    body = finished(harness, search_id)
    assert body["items"][1]["thumbnail_url"] is None
    assert harness.client.get(f"/api/top-videos/{search_id}/thumb/TOPNOURL00").json()["code"] == "thumbnail_not_found"
    failed = harness.client.get(body["items"][0]["thumbnail_url"])
    assert failed.status_code == 502 and failed.json()["code"] == "thumbnail_unavailable"


def test_thumbnail_that_is_not_an_image_is_refused(harness):
    install(harness, ReelsTab([[tab_node("TOPWITHURL", PK - 1, 5)]]), thumbs={
        "https://scontent-fake.cdninstagram.com/v/fake-thumb.jpg": (b"<html>", "text/html")})
    body = finished(harness, harness.client.post("/api/top-videos", json={"profile": "perfil_teste"}).json()["id"])
    assert harness.client.get(body["items"][0]["thumbnail_url"]).status_code == 502


@pytest.mark.parametrize("payload", [{"profile": "perfil_teste", "top": 0}, {"profile": "perfil_teste", "top": 51},
                                     {"profile": "perfil_teste", "scan_limit": 11},
                                     {"profile": "perfil_teste", "scan_limit": 601},
                                     {"profile": "https://www.instagram.com/p/ABCDEF/"}, {"profile": ""}])
def test_invalid_input_is_refused_before_any_request(harness, payload):
    tab = ReelsTab(pages_of([1]))
    install(harness, tab)
    assert harness.client.post("/api/top-videos", json=payload).status_code == 422
    assert tab.requests == 0


def test_unknown_id_is_404(harness):
    assert harness.client.get("/api/top-videos/" + "0" * 32).status_code == 404
    assert harness.client.get("/api/top-videos/nao-e-id").status_code == 422


def test_paused_instagram_refuses_the_search_without_requests(harness):
    tab = ReelsTab(pages_of([1]))
    install(harness, tab)
    harness.ctx.worker.pause_downloads(600, reason="rate_limited")
    response = harness.client.post("/api/top-videos", json={"profile": "perfil_teste"})
    assert response.status_code == 409 and response.json()["code"] == "instagram_paused"
    assert tab.requests == 0


def test_429_fails_the_search_and_pauses_instagram(harness):
    tab = ReelsTab(pages_of([1], [2]), responses={1: httpx.Response(429, text="{}", headers={
        "content-type": "application/json"})})
    install(harness, tab)
    body = finished(harness, harness.client.post("/api/top-videos", json={"profile": "perfil_teste"}).json()["id"])
    assert body["status"] == "failed" and body["error_code"] == "rate_limited" and body["error_message"]
    assert [item["views"] for item in body["items"]] == [1], "o que foi lido antes do 429 continua na lista"
    assert (body["scanned"], body["pages"]) == (1, 1)
    assert harness.ctx.worker.pause_state()[0] is not None, "429 aplica a pausa global, como na importação"
    again = harness.client.post("/api/top-videos", json={"profile": "perfil_teste"})
    assert again.status_code == 409 and again.json()["code"] == "instagram_paused"


def test_pause_published_during_the_search_fails_it(harness):
    tab = ReelsTab(pages_of([1], [2]))
    install(harness, tab)
    original = tab.handle

    def handle(request):
        answer = original(request)
        harness.ctx.instagram_gate.publish_pause("instagram")   # chega depois da 1a página
        return answer

    tab.handle = handle
    body = finished(harness, harness.client.post("/api/top-videos", json={"profile": "perfil_teste"}).json()["id"])
    assert body["status"] == "failed" and body["error_code"] == "instagram_paused"
    assert tab.requests == 1
    assert [item["views"] for item in body["items"]] == [1] and (body["scanned"], body["pages"]) == (1, 1)


def test_broken_page_in_the_middle_keeps_the_partial_ranking(harness):
    tab = ReelsTab(pages_of([5, 50], [7], [9]), responses={2: httpx.Response(
        200, text=json.dumps({"data": {"node": {"polaris_clips_connection": {"edges": "x"}}}}),
        headers={"content-type": "application/json"})})
    install(harness, tab)
    body = finished(harness, harness.client.post("/api/top-videos", json={"profile": "perfil_teste"}).json()["id"])
    assert body["status"] == "failed" and body["error_code"] == "contract_changed"
    assert [item["views"] for item in body["items"]] == [50, 7, 5]
    assert (body["scanned"], body["pages"]) == (3, 2)
    assert harness.ctx.worker.pause_state()[0] is None, "formato desconhecido não pausa o Instagram"


def test_missing_profile_fails_with_its_code(harness):
    install(harness, ReelsTab([[]], responses={0: httpx.Response(404, text="<html></html>", headers={
        "content-type": "text/html"})}))
    body = finished(harness, harness.client.post("/api/top-videos", json={"profile": "perfil_teste"}).json()["id"])
    assert body["status"] == "failed" and body["error_code"] == "profile_not_found" and body["error_message"]


def test_cancel_stops_before_the_next_page_and_one_search_at_a_time(harness):
    tab = ReelsTab(pages_of([1, 2], [3], [4]))
    install(harness, tab)
    release = threading.Event()
    original = tab.handle

    def handle(request):
        if request.method == "POST":
            release.wait(5)   # a 2a página fica presa até o cancelamento
        return original(request)

    tab.handle = handle
    search_id = harness.client.post("/api/top-videos", json={"profile": "perfil_teste"}).json()["id"]
    assert wait_until(lambda: harness.client.get(f"/api/top-videos/{search_id}").json()["scanned"] == 2)
    busy = harness.client.post("/api/top-videos", json={"profile": "outro_perfil"})
    assert busy.status_code == 409 and busy.json()["code"] == "top_videos_in_progress"
    assert harness.client.post(f"/api/top-videos/{search_id}/cancel").status_code == 200
    release.set()
    body = finished(harness, search_id)
    assert body["status"] == "cancelled" and len(tab.posts) == 1, "nenhuma página nova depois do cancelamento"
    assert [item["views"] for item in body["items"]] == [3, 2, 1], "o que já foi lido (e a página em voo) fica"
    assert harness.client.post("/api/top-videos", json={"profile": "outro_perfil"}).status_code == 202


def test_only_the_most_recent_searches_are_kept(harness):
    from app.services import top_videos

    install(harness, ReelsTab(pages_of([1])))
    ids = []
    for _ in range(top_videos.MAX_SEARCHES + 1):
        ids.append(harness.client.post("/api/top-videos", json={"profile": "perfil_teste"}).json()["id"])
        finished(harness, ids[-1])
    assert harness.client.get(f"/api/top-videos/{ids[0]}").status_code == 404
    assert harness.client.get(f"/api/top-videos/{ids[-1]}").status_code == 200
