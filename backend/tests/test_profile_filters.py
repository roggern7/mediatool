"""Filtros da importação por perfil pela API e pelo import runner: período de postagem (corte calculado na criação e
persistido), visualizações mínimas pela aba Reels SEM login (429 pausa sem baixar; formato inesperado para o lote),
grupo com vários perfis e importação por link sem filtros. 100% offline, valores FAKE.
"""

from __future__ import annotations

import json
import time
import zlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import import_group_fakes as fakes
import test_graphql_partial_errors as gp
from import_group_fakes import MultiProfileSite, Profile, closed, drive, web_app
from reels_tab_fakes import connection, reels_html, tab_node
from reels_tab_fakes import MirroredSite
from test_graphql_partial_errors import _nothing_sensitive_in_any_log  # noqa: F401 - autouse também neste arquivo
from test_listing_dropped_counters import WebListingFakeDownload, drain, tight_backpressure
from test_web_provider_min_views import CUTOFF, HIGH, LOW, MIN, NEW, OLD, post, reel


def post_single(h, **body):
    return h.client.post("/api/imports", json={"profile": "perfil_um", "content_type": "reels", "limit": 10, **body})


def post_group(h, **body):
    return h.client.post("/api/import-groups", json={"profiles": ["perfil_dois", "perfil_tres"],
                                                     "content_type": "reels", "limit": 10, **body})


def parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def row(h, batch_id: int) -> dict:
    data = dict(h.ctx.db.fetchone("SELECT * FROM batches WHERE id = ?", (batch_id,)))
    data["listing_views"] = json.loads(data["listing_views"]) if data["listing_views"] else None
    return data


def run_until_listed(h, batch_id: int, rounds: int = 12) -> None:
    for _ in range(rounds):
        drain(h, batch_id)
        h.run_imports()
        if row(h, batch_id)["listing_state"] == "done":
            return
    raise AssertionError("a listagem não terminou")


# --------------------------------------------------------------------------- validação e criação


@pytest.mark.parametrize("value", [0, -1, 3651, 1.5, "30", True])
def test_invalid_period_is_422_and_creates_nothing(harness, value):
    for response in (post_single(harness, posted_within_days=value), post_group(harness, posted_within_days=value)):
        assert response.status_code == 422, response.text
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0


def test_period_cutoff_is_computed_once_at_creation_and_shared_by_the_group(harness):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    single = post_single(harness, content_type="all", posted_within_days=30)
    group = post_group(harness, content_type="all", posted_within_days=3650, min_views=None)
    assert single.status_code == 202 and group.status_code == 202
    after = datetime.now(timezone.utc)
    vf = single.json()["views_filter"]
    assert (vf["min_views"], vf["posted_within_days"], vf["out_of_period"]) == (None, 30, 0)
    assert before - timedelta(days=30, seconds=1) <= parse(vf["posted_after"]) <= after - timedelta(days=30)
    cutoffs = {c["views_filter"]["posted_after"] for c in group.json()["children"]}
    assert cutoffs == {group.json()["group"]["views_filter"]["posted_after"]} and len(cutoffs) == 1
    rows = harness.ctx.db.fetchall("SELECT posted_within_days, posted_after FROM batches ORDER BY id")
    assert [r["posted_within_days"] for r in rows] == [30, 3650, 3650]
    assert harness.ctx.db.scalar("SELECT posted_within_days FROM import_groups") == 3650


def test_without_period_nothing_changes(harness):
    single = post_single(harness).json()
    assert single["views_filter"] is None and single["listing_end"] is None
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches WHERE posted_after IS NOT NULL"
                                 " OR posted_within_days IS NOT NULL OR listing_period_end IS NOT NULL") == 0


def test_gallery_dl_method_refuses_the_period(harness):
    assert harness.client.put("/api/settings", json={"instagram_provider": "gallery_dl"}).status_code == 200
    for response in (post_single(harness, posted_within_days=30), post_group(harness, posted_within_days=30)):
        assert response.status_code == 422 and response.json()["code"] == "not_supported_by_provider"
        assert "período de postagem" in response.json()["detail"]
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0


def test_views_filter_is_switched_off_in_the_app_with_a_clear_422(harness):
    from app.services import import_service

    assert import_service.MIN_VIEWS_ENABLED is False
    for response in (post_single(harness, min_views=MIN), post_group(harness, min_views=MIN),
                     post_single(harness, min_views=MIN, posted_within_days=30)):
        assert response.status_code == 422, response.text
        body = response.json()
        assert (body["code"], body["detail"]) == ("min_views_unavailable",
                                                  "O filtro de visualizações ainda não está disponível.")
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM import_groups") == 0
    # Sem `min_views` (ou com null) o período segue aceito.
    assert post_single(harness, min_views=None, posted_within_days=30).status_code == 202


def test_link_import_has_no_filters(harness):
    link = "https://www.instagram.com/reel/SYNLINK01/"
    for extra in ({"min_views": 10}, {"posted_within_days": 30}, {"min_views": 10, "posted_within_days": 7}):
        response = harness.client.post("/api/import-links", json={"links": [link], **extra})
        assert response.status_code == 422
        assert (response.json()["code"], response.json()["detail"]) == (
            "filters_profile_only", "O filtro de período/visualizações vale só para importação por perfil.")
    created = harness.client.post("/api/import-links", json={"links": [link]})
    assert created.status_code == 202, created.text
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches WHERE min_views IS NOT NULL"
                                 " OR posted_after IS NOT NULL") == 0
    batch_id = harness.ctx.db.scalar("SELECT id FROM batches")
    assert harness.batch(batch_id)["views_filter"] is None


# --------------------------------------------------------------------------- runner


def single_batch(h, *, min_views=None, posted_after=None, limit=None, content="reels") -> int:
    return h.ctx.batches.create(source="instagram_import", status="listing", source_profile="perfil_teste",
                                content_type=content, requested_limit=limit, min_views=min_views,
                                posted_within_days=None if posted_after is None else 30, posted_after=posted_after)


def test_period_end_finishes_the_listing_and_the_api_says_so(harness_factory):
    page = [post(1, 0, NEW, []), post(1, 1, NEW, []), post(1, 2, OLD, []), post(1, 3, NEW, [])]
    site = MirroredSite(gp.body(1, page), gp.body(2, [post(2, 0, NEW, [])], has_next=False))
    h = harness_factory(downloader=WebListingFakeDownload(gp.downloader(site)))
    batch_id = single_batch(h, posted_after=CUTOFF)
    run_until_listed(h, batch_id)
    data = row(h, batch_id)
    assert (data["found"], data["listing_end"], data["listing_period_end"], data["error_message"]) == (
        2, "profile_end", 1, None)
    assert site.afters == [None] and site.tab.requests == 0
    out = h.batch(batch_id)
    assert out["listing_end"] == "period_end"
    assert {k: out["views_filter"][k] for k in ("analyzed", "approved", "out_of_period", "pending")} == {
        "analyzed": 3, "approved": 2, "out_of_period": 1, "pending": 0}


def test_cutoff_is_the_stored_one_on_every_resumed_chunk(harness_factory, monkeypatch):
    tight_backpressure(monkeypatch)
    recent = int(time.time()) - 86400
    page = [post(1, i, recent, []) for i in range(8)]
    site = MirroredSite(gp.body(1, page), gp.body(2, [post(2, 0, recent, []), post(2, 1, 1_000_000, [])],
                                                  has_next=False))
    seen: list = []

    class Recorder(WebListingFakeDownload):
        def iter_profile_media(self, *args, **kwargs):
            seen.append(kwargs.get("posted_after"))
            return self.web.iter_profile_media(*args, **kwargs)

    h = harness_factory(downloader=Recorder(gp.downloader(site)))
    created = h.client.post("/api/imports", json={"profile": "perfil_teste", "content_type": "reels", "limit": None,
                                                  "posted_within_days": 7})
    assert created.status_code == 202, created.text
    batch_id = created.json()["id"]
    stored = row(h, batch_id)["posted_after"]
    run_until_listed(h, batch_id, rounds=20)
    assert len(seen) >= 2, "a listagem foi retomada em mais de um trecho"
    assert set(seen) == {stored} and row(h, batch_id)["posted_after"] == stored
    data = row(h, batch_id)
    assert (data["found"], data["listing_period_end"]) == (9, 1)


@pytest.fixture
def views_on(monkeypatch):
    from app.services import import_service

    monkeypatch.setattr(import_service, "MIN_VIEWS_ENABLED", True)


def test_stored_min_views_is_ignored_while_the_filter_is_switched_off(harness_factory, caplog):
    """Lote antigo com `min_views` gravado + interruptor desligado: lista SEM o filtro, sem nenhuma requisição à aba
    Reels, com UM aviso por lote."""
    site = MirroredSite(gp.body(1, [reel(1, i, play_count=LOW) for i in range(3)], has_next=False))
    h = harness_factory(downloader=WebListingFakeDownload(gp.downloader(site)))
    batch_id = single_batch(h, min_views=MIN)
    run_until_listed(h, batch_id)
    assert site.tab.requests == 0
    assert (row(h, batch_id)["found"], row(h, batch_id)["listing_state"]) == (3, "done")
    warnings = [r.getMessage() for r in caplog.records if "filtro de visualizações desativado" in r.getMessage()]
    assert warnings == [f"Lote {batch_id}: filtro de visualizações desativado nesta versão; a listagem segue sem ele"]


@pytest.mark.usefixtures("views_on")
def test_429_on_the_reels_tab_pauses_the_listing_without_downloading(harness_factory):
    site = MirroredSite(gp.body(1, [reel(1, i, play_count=HIGH) for i in range(3)], has_next=False))
    site.tab.responses[0] = httpx.Response(429, text="{}", headers={"content-type": "application/json"})
    h = harness_factory(downloader=WebListingFakeDownload(gp.downloader(site)))
    batch_id = single_batch(h, min_views=MIN)
    h.run_imports()
    data = row(h, batch_id)
    assert (data["status"], data["listing_state"], data["error_code"]) == ("listing", "paused", "rate_limited")
    assert h.ctx.jobs.count_for_batch(batch_id) == 0 and site.tab.requests == 1


@pytest.mark.usefixtures("views_on")
def test_unexpected_reels_tab_shape_stops_the_batch_with_a_clear_reason(harness_factory):
    site = MirroredSite(gp.body(1, [reel(1, i, play_count=HIGH) for i in range(3)], has_next=False))
    site.tab.html = "<html><body>mudou</body></html>"
    h = harness_factory(downloader=WebListingFakeDownload(gp.downloader(site)))
    batch_id = single_batch(h, min_views=MIN)
    h.run_imports()
    out = h.batch(batch_id)
    assert (out["status"], out["error_code"]) == ("failed", "contract_changed")
    assert out["error_message"].startswith("Não foi possível ler as visualizações do perfil")
    assert h.ctx.jobs.count_for_batch(batch_id) == 0


# --------------------------------------------------------------------------- grupo com vários perfis


class ReelsMultiProfileSite(MultiProfileSite):
    """Vários perfis com a aba Reels de cada um (todos os Reels numa página); pk decrescente como no real."""

    def nodes(self, username: str, page: int, count: int) -> list[dict]:
        nodes = super().nodes(username, page, count)
        serial = zlib.crc32(username.encode()) % 900000 + 100000
        for i, node in enumerate(nodes):
            node["pk"] = f"{serial}{999999 - page:06d}{99 - i:02d}"
        return nodes

    def _user_id(self, username: str) -> str:
        return str(zlib.crc32(username.encode()) % 9_000_000 + 1_000_000)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/reels/"):
            username = path.strip("/").split("/")[0]
            profile = self.profiles[username]
            reels = [tab_node(n["code"], n["pk"], n.get("play_count", -1))
                     for page in range(1, profile.pages + 1) for n in self.nodes(username, page, profile.per_page)
                     if n.get("product_type") == "clips"]
            user = {"id": self._user_id(username), "pk": self._user_id(username),
                    "polaris_clips_connection": connection(reels, 1, False)}
            return httpx.Response(200, text=reels_html([], False, user=user),
                                  headers={"content-type": "text/html; charset=utf-8"})
        return super()._respond(request)


def _reel_with(**counts):
    def make(code: str, pk: str) -> dict:
        return {**fakes._reel(code, pk), **counts}
    return make


def test_group_of_five_profiles_counts_per_profile_and_in_aggregate(harness_factory, monkeypatch):
    from app.services import import_service

    monkeypatch.setattr(import_service, "MIN_VIEWS_ENABLED", True)   # backend do filtro testado com ele ligado
    monkeypatch.setitem(fakes.KINDS, "hi", _reel_with(play_count=MIN + 1))
    monkeypatch.setitem(fakes.KINDS, "eq", _reel_with(play_count=MIN))
    monkeypatch.setitem(fakes.KINDS, "lo", _reel_with(play_count=MIN - 1))
    monkeypatch.setitem(fakes.KINDS, "none", _reel_with())
    patterns = {
        "perfil_a": ["hi", "lo", "none"],          # 1 aprovado, 1 abaixo, 1 sem contagem (por página)
        "perfil_b": ["eq", "eq", "lo"],            # 2 aprovados (igual ao mínimo aprova)
        "perfil_c": ["none", "none", "none"],      # 3 sem contagem
        "perfil_d": ["hi", "hi", "hi"],            # 3 aprovados
        "perfil_e": ["lo", "image", "carousel"],   # 1 abaixo; foto e carrossel saem no filtro de conteúdo
    }
    profiles = {name: Profile(pages=2, per_page=3, kind=lambda page, i, kinds=kinds: kinds[i])
                for name, kinds in patterns.items()}
    site = ReelsMultiProfileSite(profiles)
    h = web_app(harness_factory, site)
    created = h.client.post("/api/import-groups", json={"profiles": list(patterns), "content_type": "reels",
                                                        "limit": None, "min_views": MIN})
    assert created.status_code == 202, created.text
    group_id = created.json()["group"]["id"]
    children = [c["id"] for c in created.json()["children"]]
    drive(h, closed(h, *children))
    detail = h.client.get(f"/api/import-groups/{group_id}").json()
    expected = {"perfil_a": (2, 2, 2), "perfil_b": (4, 2, 0), "perfil_c": (0, 0, 6), "perfil_d": (6, 0, 0),
                "perfil_e": (0, 2, 0)}
    for child in detail["children"]:
        approved, below, no_count = expected[child["source_profile"]]
        assert child["views_filter"] == {"min_views": MIN, "posted_within_days": None, "posted_after": None,
                                         "analyzed": approved + below + no_count, "approved": approved,
                                         "below": below, "no_count": no_count, "out_of_period": 0, "pending": 0}
        assert child["progress"]["downloaded"] == approved == child["found"]
    group = detail["group"]["views_filter"]
    assert (group["analyzed"], group["approved"], group["below"], group["no_count"]) == (26, 12, 6, 8)
    assert detail["group"]["progress"]["downloaded"] == 12
    assert str(LOW) not in json.dumps(detail)


def test_group_counts_period_end_apart_from_profile_end(harness):
    created = post_group(harness, content_type="all", posted_within_days=30)
    group_id = created.json()["group"]["id"]
    first, second = [c["id"] for c in created.json()["children"]]
    harness.ctx.db.execute("UPDATE batches SET listing_end = 'profile_end', listing_period_end = 1 WHERE id = ?",
                           (first,))
    harness.ctx.db.execute("UPDATE batches SET listing_end = 'profile_end' WHERE id = ?", (second,))
    group = harness.client.get(f"/api/import-groups/{group_id}").json()["group"]
    assert (group["profile_end_count"], group["period_end_count"]) == (1, 1)
