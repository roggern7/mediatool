"""Importação unificada (docs/CONTRACTS.md, seção 8.6): classificação, criação (perfis e/ou links), tipo de mídia
(vídeos, todas, capas), capas como mídia própria (migration 0021), máximo de visualizações, contadores
`duplicates`/`filtered`, parser do documento /p/ logado e nome `_capa` na exportação. 100% offline, valores FAKE."""

from __future__ import annotations

import json
import logging
import sqlite3
import zipfile
from dataclasses import replace

import pytest

import test_graphql_partial_errors as gp
from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.web.normalize import filter_by_content
from app.integrations.instagram.web.post_document import SESSION_POST_FIELD, parse_session_post_document
from app.services import import_service
from app.services.export_names import export_stem
from export_helpers import prepare, seed_batch
from import_links_fakes import OWNER, LinksDownloader, carousel, link, photo, reel
from media_fakes import FakeInstagramDownloader, remote
from reels_tab_fakes import MirroredSite
from test_exports_zip import assert_flat_and_unique, library, stored_zip, zip_export
from test_graphql_partial_errors import _nothing_sensitive_in_any_log  # noqa: F401 - autouse também aqui
from test_listing_dropped_counters import WebListingFakeDownload
from test_web_provider_min_views import listing
from test_web_provider_min_views import reel as tab_reel

CODE, CODE2, CODE3 = "ABCDE12345", "FGHIJ67890", "KLMNO11111"
THUMB = "https://scontent.cdninstagram.com/v/CAPA-{code}.jpg?oh=SIGFAKE"


def start(h, expect: int = 202, **body) -> dict:
    body.setdefault("inputs", [])
    response = h.client.post("/api/imports/start", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def with_thumb(items):
    return [replace(item, thumbnail_url=THUMB.format(code=item.source_post_id)) for item in items]


def jobs_of(h, batch_id: int) -> list[dict]:
    return [dict(row) for row in h.ctx.db.fetchall(
        "SELECT * FROM jobs WHERE batch_id = ? ORDER BY id", (batch_id,))]


def finish(h, rounds: int = 4) -> None:
    for _ in range(rounds):
        h.run_imports()
        h.run_jobs()


class KwargsDownloader(FakeInstagramDownloader):
    """Listagem FAKE que aceita (e registra) os kwargs novos (max_views, covers)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.kwargs: list[dict] = []

    def iter_profile_media(self, username, content, limit, *, cancel=None, resume=None, on_checkpoint=None,
                           **extra):
        self.kwargs.append(extra)
        return super().iter_profile_media(username, content, limit, cancel=cancel, resume=resume,
                                          on_checkpoint=on_checkpoint)


# --------------------------------------------------------------------------- classificação
def test_classify_mixes_profiles_links_invalid_and_duplicates(harness):
    text = (f"https://www.instagram.com/p/{CODE}/ @perfil_um, perfil_um; https://instagram.com/perfil_dois/\n"
            f"https://www.instagram.com/reel/{CODE}/ https://www.instagram.com/tv/{CODE2}/ "
            "https://www.instagram.com/explore/tags/x/\n!!")
    response = harness.client.post("/api/imports/classify", json={"text": text})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profiles"] == ["perfil_um", "perfil_dois"]
    assert body["links"] == [{"shortcode": CODE, "url": link(CODE), "kind": "p"},
                             {"shortcode": CODE2, "url": link(CODE2), "kind": "p"}]
    assert [entry["entry"] for entry in body["invalid"]] == ["https://www.instagram.com/explore/tags/x/", "!!"]
    assert body["duplicates"] == 2   # perfil_um repetido e o mesmo código em /reel/
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0


@pytest.mark.parametrize("text, profiles, links, invalid", [
    # Espaço entre palavras soltas: a linha inteira é inválida (nunca vira os perfis "lixo" e "com").
    ("lixo com espaço", [], [], ["lixo com espaço"]),
    ("perfil_um outro_perfil", [], [], ["perfil_um outro_perfil"]),
    # Mistura de URL/@ com palavra solta: inválida inteira.
    (f"https://www.instagram.com/p/{CODE}/ perfil_um", [], [], [f"https://www.instagram.com/p/{CODE}/ perfil_um"]),
    # Todos os pedaços são URL do Instagram ou @usuario: o espaço separa.
    (f"https://www.instagram.com/p/{CODE}/   @perfil_um https://instagram.com/perfil_dois/", ["perfil_um",
                                                                                           "perfil_dois"],
     [CODE], []),
    # Quebra de linha, vírgula e ponto e vírgula continuam separando; espaços nas pontas são aparados.
    ("  perfil_um  \nperfil_dois, perfil_tres ;perfil_quatro", ["perfil_um", "perfil_dois", "perfil_tres",
                                                                "perfil_quatro"], [], []),
])
def test_classify_space_only_splits_instagram_urls_and_handles(harness, text, profiles, links, invalid):
    body = harness.client.post("/api/imports/classify", json={"text": text}).json()
    assert body["profiles"] == profiles
    assert [entry["shortcode"] for entry in body["links"]] == links
    assert [entry["entry"] for entry in body["invalid"]] == invalid
    assert all(entry["reason"] == import_service.SPACED_ENTRY_REASON for entry in body["invalid"])


def test_start_with_spaced_garbage_creates_nothing(harness):
    error = start(harness, 422, inputs=["lixo com espaço"], limit=10)
    assert error["code"] == "no_valid_inputs"
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0


def test_classify_requires_exactly_one_source(harness):
    assert harness.client.post("/api/imports/classify", json={}).status_code == 422
    assert harness.client.post("/api/imports/classify", json={"inputs": ["a"], "text": "b"}).status_code == 422


# --------------------------------------------------------------------------- criação
def test_one_profile_and_links_create_a_single_batch_and_a_links_batch(harness):
    body = start(harness, inputs=["@perfil_um", link(CODE), link(CODE2, "reel"), "lixo/lixo/lixo"], limit=10)
    assert body["group_id"] is None and body["profiles"] == 1 and body["links"] == 2
    assert len(body["batch_ids"]) == 2 and body["links_batch_id"] == body["batch_ids"][1]
    assert [entry["entry"] for entry in body["invalid"]] == ["lixo/lixo/lixo"]
    profile_batch = harness.batch(body["batch_ids"][0])
    links_batch = harness.batch(body["links_batch_id"])
    assert (profile_batch["source_profile"], profile_batch["content_type"], profile_batch["requested_limit"],
            profile_batch["media"]) == ("perfil_um", "videos", 10, "videos")
    assert (links_batch["import_mode"], links_batch["content_type"], links_batch["media"]) == ("links", "videos",
                                                                                               "videos")
    assert links_batch["progress"]["duplicates"] == 0 and links_batch["progress"]["filtered"] == 0


def test_two_profiles_create_a_group_with_filters_only_on_profiles(harness):
    body = start(harness, inputs=["perfil_um", "perfil_dois", link(CODE)], limit=50, media="covers",
                 min_views=1_000, max_views=500_000, posted_within_days=30)
    assert body["group_id"] is not None and body["batch_ids"] == [body["links_batch_id"]]
    group = harness.client.get(f"/api/import-groups/{body['group_id']}").json()
    assert group["group"]["media"] == "covers" and group["group"]["profiles"]["total"] == 2
    child = group["children"][0]
    assert (child["views_filter"]["min_views"], child["views_filter"]["max_views"],
            child["views_filter"]["posted_within_days"]) == (1_000, 500_000, 30)
    links_row = harness.ctx.batches.get(body["links_batch_id"])
    assert (links_row["media_role"], links_row["min_views"], links_row["max_views"], links_row["posted_after"]) == (
        "cover", None, None, None)


def test_all_requires_confirmation(harness):
    error = start(harness, 422, inputs=["perfil_um"], limit=None)
    assert error["code"] == "confirm_all_required"
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    body = start(harness, inputs=["perfil_um"], limit=None, confirm_all=True)
    assert harness.batch(body["batch_ids"][0])["requested_limit"] is None
    # Só links: "todas" não se aplica e não pede confirmação.
    assert start(harness, inputs=[link(CODE)], limit=None)["links"] == 1


@pytest.mark.parametrize("body, code", [
    ({"inputs": ["!!", "https://www.instagram.com/explore/tags/x/"]}, "no_valid_inputs"),
    ({"inputs": ["perfil_um"], "limit": 5, "media": "all", "min_views": 10}, "views_filter_media"),
    ({"inputs": ["perfil_um"], "limit": 5, "min_views": 10, "max_views": 5}, "validation_error"),
    ({"inputs": ["perfil_um"], "limit": 5, "post_processing": "edit"}, "validation_error"),
    ({"inputs": ["perfil_um"], "text": "x", "limit": 5}, "validation_error"),
])
def test_invalid_requests_create_nothing(harness, body, code):
    assert start(harness, 422, **body)["code"] == code
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0


def test_views_filter_is_switched_on_in_both_places():
    from pathlib import Path

    assert import_service.MIN_VIEWS_ENABLED is True
    ts = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "viewsFilter.ts"
    assert "export const VIEWS_FILTER_ENABLED = true;" in ts.read_text(encoding="utf-8")


def test_session_missing_creates_nothing(harness_factory):
    from media_fakes import MemorySessionProvider

    h = harness_factory(session_provider=MemorySessionProvider(configured=False))
    assert start(h, 409, inputs=["perfil_um", link(CODE)], limit=5)["code"] == "session_missing"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0


# --------------------------------------------------------------------------- tipo de mídia nos links
def test_links_only_with_all_media_and_views_ignore_the_profile_filters(harness):
    body = start(harness, inputs=[link(CODE)], media="all", min_views=10, posted_within_days=7)
    assert body["links"] == 1 and body["notes"] == [import_service.LINK_FILTERS_IGNORED_NOTE]
    row = harness.ctx.batches.get(body["links_batch_id"])
    assert (row["content_type"], row["min_views"], row["max_views"], row["posted_after"]) == ("all", None, None, None)
    # Com perfil na entrada a combinação continua recusada (e nada é criado).
    assert start(harness, 422, inputs=["perfil_um", link(CODE2)], limit=5, media="all",
                 min_views=10)["code"] == "views_filter_media"
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 1
    # Sem filtro: sem aviso.
    assert start(harness, inputs=[link(CODE3)])["notes"] == []


@pytest.mark.parametrize("profiles", [["perfil_um"], ["perfil_um", "perfil_dois"]])
def test_failure_creating_the_links_batch_undoes_the_profile_batches(harness, monkeypatch, profiles):
    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("falha injetada")

    monkeypatch.setattr(harness.ctx.batches, "add_links", broken)
    with pytest.raises(sqlite3.OperationalError):
        import_service.start_unified(harness.ctx, import_service_request(inputs=[*profiles, link(CODE)], limit=10))
    db = harness.ctx.db
    assert db.scalar("SELECT COUNT(*) FROM batches") == 0
    assert db.scalar("SELECT COUNT(*) FROM import_groups") == 0
    assert db.scalar("SELECT COUNT(*) FROM batch_links") == 0
    # O banco segue utilizável (a transação foi desfeita, não ficou aberta).
    monkeypatch.undo()
    assert start(harness, inputs=[*profiles, link(CODE)], limit=10)["links"] == 1


def import_service_request(**body):
    from app.schemas.batches import UnifiedImportRequest
    return UnifiedImportRequest(**body)


def test_links_videos_keep_only_videos_and_count_the_rest_as_filtered(harness_factory):
    posts = {CODE: carousel(CODE, 3), CODE2: photo(CODE2), CODE3: reel(CODE3)}
    h = harness_factory(downloader=LinksDownloader(posts=posts))
    batch_id = start(h, inputs=[link(CODE), link(CODE2), link(CODE3)])["links_batch_id"]
    finish(h)
    jobs = jobs_of(h, batch_id)
    assert [(j["source_post_id"], j["source_media_index"]) for j in jobs] == [(CODE, 1), (CODE3, 0)]
    batch = h.batch(batch_id)
    assert (batch["status"], batch["progress"]["downloaded"], batch["progress"]["filtered"]) == ("completed", 2, 3)


def test_links_all_keep_the_whole_carousel(harness_factory):
    h = harness_factory(downloader=LinksDownloader(posts={CODE: carousel(CODE, 3)}))
    batch_id = start(h, inputs=[link(CODE)], media="all")["links_batch_id"]
    finish(h)
    assert [j["source_media_index"] for j in jobs_of(h, batch_id)] == [0, 1, 2]
    assert h.batch(batch_id)["progress"]["filtered"] == 0


# --------------------------------------------------------------------------- capas
def test_cover_is_its_own_media_and_never_collides_with_the_video(harness_factory):
    posts = {CODE: with_thumb(reel(CODE)), CODE2: with_thumb(carousel(CODE2, 2)), CODE3: photo(CODE3)}
    downloader = LinksDownloader(posts=posts)
    h = harness_factory(downloader=downloader)
    covers_id = start(h, inputs=[link(CODE), link(CODE2), link(CODE3)], media="covers")["links_batch_id"]
    finish(h)
    covers = [dict(r) for r in h.ctx.db.fetchall("SELECT * FROM media WHERE role = 'cover' ORDER BY id")]
    assert [(m["source_post_id"], m["source_media_index"], m["media_type"]) for m in covers] == [
        (CODE, 0, "image"), (CODE2, 1, "image")]
    # Só a capa foi baixada (URL da capa), nunca o vídeo; a foto ficou de fora (filtrada).
    assert downloader.download_urls == {f"{CODE}:0": [THUMB.format(code=CODE)],
                                        f"{CODE2}:1": [THUMB.format(code=CODE2)]}
    batch = h.batch(covers_id)
    assert (batch["media"], batch["progress"]["downloaded"], batch["progress"]["filtered"]) == ("covers", 2, 2)
    assert all(j["media_role"] == "cover" and j["params"] is None for j in jobs_of(h, covers_id))
    # O vídeo depois da capa: baixado normalmente (não é duplicata da capa).
    videos_id = start(h, inputs=[link(CODE)])["links_batch_id"]
    finish(h)
    assert h.batch(videos_id)["progress"]["downloaded"] == 1
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media WHERE source_post_id = ?", (CODE,)) == 2
    # A capa de novo: já existe (dedup por origem COM o papel).
    again = start(h, inputs=[link(CODE)], media="covers")["links_batch_id"]
    finish(h)
    progress = h.batch(again)["progress"]
    assert (progress["downloaded"], progress["duplicates"]) == (0, 1)
    # Biblioteca: selo e filtro.
    listed = h.client.get("/api/media", params={"role": "cover"}).json()
    assert listed["total"] == 2 and {item["role"] for item in listed["items"]} == {"cover"}
    assert h.client.get("/api/media", params={"role": "original"}).json()["total"] == 1
    assert h.client.get("/api/media").json()["total"] == 3


def test_cover_after_the_video_is_still_downloaded(harness_factory):
    h = harness_factory(downloader=LinksDownloader(posts={CODE: with_thumb(reel(CODE))}))
    start(h, inputs=[link(CODE)])
    finish(h)
    batch_id = start(h, inputs=[link(CODE)], media="covers")["links_batch_id"]
    finish(h)
    assert h.batch(batch_id)["progress"]["downloaded"] == 1
    assert {r["role"] for r in h.ctx.db.fetchall("SELECT role FROM media")} == {"original", "cover"}


class CoverRefresh(LinksDownloader):
    """A 2a consulta da publicação devolve a capa com URL nova."""

    def resolve_post(self, shortcode, *, cancel=None, path_kind=None):
        items = super().resolve_post(shortcode, cancel=cancel, path_kind=path_kind)
        if self.calls.count(shortcode) > 1:
            items = [replace(item, thumbnail_url=item.thumbnail_url + "&novo=1") for item in items]
        return items


def test_expired_cover_url_is_renewed_by_the_existing_mechanism(harness_factory):
    downloader = CoverRefresh(posts={CODE: with_thumb(reel(CODE))})
    downloader.expired = {THUMB.format(code=CODE)}
    h = harness_factory(downloader=downloader)
    batch_id = start(h, inputs=[link(CODE)], media="covers")["links_batch_id"]
    finish(h, rounds=6)
    assert downloader.download_urls[f"{CODE}:0"] == [THUMB.format(code=CODE), THUMB.format(code=CODE) + "&novo=1"]
    assert h.batch(batch_id)["progress"]["downloaded"] == 1


def test_profile_covers_download_only_the_cover_and_pass_covers_to_the_listing(harness_factory):
    items = with_thumb([remote("C1", media_type="video"), remote("C2", media_type="video")])
    downloader = KwargsDownloader(items=items)
    h = harness_factory(downloader=downloader)
    batch_id = start(h, inputs=["perfil_teste"], limit=10, media="covers")["batch_ids"][0]
    finish(h)
    assert downloader.kwargs[0] == {"covers": True}
    assert sorted(downloader.downloads) == ["C1:0", "C2:0"]
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media WHERE role = 'cover' AND media_type = 'image'") == 2
    assert h.batch(batch_id)["progress"]["downloaded"] == 2
    # Capas (imagens) ganham largura/altura pelo ffprobe no download.
    assert [tuple(r) for r in h.ctx.db.fetchall("SELECT width, height FROM media WHERE role = 'cover'")] == [
        (64, 48), (64, 48)]


def test_media_source_index_includes_the_role(harness):
    db = harness.ctx.db
    now = "2026-01-01T00:00:00.000Z"
    sql = ("INSERT INTO media (source_platform, source_post_id, source_media_index, media_type, original_path,"
           " original_sha256, file_size, created_at, updated_at, role) VALUES ('instagram', ?, 0, 'image', 'x', ?,"
           " 1, ?, ?, ?)")
    db.execute(sql, (CODE, "a" * 64, now, now, "original"))
    db.execute(sql, (CODE, "b" * 64, now, now, "cover"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, (CODE, "c" * 64, now, now, "cover"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, (CODE2, "a" * 64, now, now, "cover"))   # sha256 continua único
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, (CODE2, "d" * 64, now, now, "capa"))


# --------------------------------------------------------------------------- visualizações (mínimo e máximo)
def test_stored_min_and_max_views_go_to_the_listing_on_every_chunk(harness_factory):
    downloader = KwargsDownloader(items=[remote("C1", media_type="video")])
    h = harness_factory(downloader=downloader)
    batch_id = h.ctx.batches.create(source="instagram_import", status="listing", source_profile="perfil_teste",
                                    content_type="videos", requested_limit=5, min_views=10, max_views=900)
    h.run_imports()
    assert downloader.kwargs[0] == {"min_views": 10, "max_views": 900}
    # Reinício sobre os mesmos dados: o lote reaberto lista de novo com os valores GRAVADOS.
    h.ctx.db.execute("UPDATE batches SET status = 'listing', listing_state = 'pending' WHERE id = ?", (batch_id,))
    again_dl = KwargsDownloader(items=[remote("C1", media_type="video")])
    again = harness_factory(root=h.root, downloader=again_dl)
    again.run_imports()
    assert again_dl.kwargs and again_dl.kwargs[0] == {"min_views": 10, "max_views": 900}


def test_provider_max_views_marks_above_and_keeps_the_minimum():
    high, mid, low = 7_654_321, 5_000_000, 4_999_999
    page = [tab_reel(1, 0, play_count=high), tab_reel(1, 1, play_count=mid), tab_reel(1, 2, play_count=low),
            tab_reel(1, 3)]
    site = MirroredSite(gp.body(1, page, has_next=False))
    _dl, items, [checkpoint] = listing(site, min_views=low, max_views=mid)
    assert len(items) == 2
    assert checkpoint.page_views == {"approved": 2, "below": 0, "no_count": 1, "out_of_period": 0, "above": 1}
    with pytest.raises(InstagramError):
        listing(site, min_views=10, max_views=5)


# --------------------------------------------------------------------------- documento /p/ logado
def _scheduled(field: str, value) -> str:
    relay = ["RelayPrefetchedStreamCache", "next", [],
             ["adp_PolarisPostRootQueryRelayPreloader_X", {"__bbox": {"result": {"data": {field: value}}}}]]
    return json.dumps({"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [relay]}}]]]})


def _html(*scripts: str) -> str:
    return "<html><body>" + "".join(
        f'<script type="application/json" data-sjs>{s}</script>' for s in scripts) + "</body></html>"


def test_logged_post_document_web_info_envelope_is_read():
    node = {"code": CODE, "media_type": 8, "carousel_media_count": 2, "carousel_media": [{"media_type": 1}],
            "taken_at": 1_700_000_000, "user": {"username": OWNER}}
    other = {"code": CODE2, "media_type": 1, "carousel_media": None}
    root, parse = parse_session_post_document(_html(_scheduled(SESSION_POST_FIELD, {"items": [other, node]})), CODE)
    assert root == node and parse.outcome == "ok"
    root, parse = parse_session_post_document(_html(_scheduled(SESSION_POST_FIELD, {"items": [other]})), CODE)
    assert root is None and parse.outcome == "mismatch"
    root, parse = parse_session_post_document(_html(_scheduled(SESSION_POST_FIELD, None)), CODE)
    assert root is None and parse.outcome == "gated"
    # O envelope dos Reels (xig_fetch_media) continua valendo.
    root, parse = parse_session_post_document(_html(_scheduled("xig_fetch_media", node)), CODE)
    assert root == node and parse.outcome == "ok"


# --------------------------------------------------------------------------- exportação
def test_cover_export_name_has_the_capa_suffix():
    base = {"id": 7, "source_platform": "instagram", "source_profile": "perfil", "source_post_id": CODE,
            "source_media_index": 0, "taken_at": None}
    assert export_stem(base) == f"perfil_{CODE}"
    assert export_stem({**base, "role": "cover"}) == f"perfil_{CODE}_capa"
    assert export_stem({**base, "role": "cover", "source_media_index": 1}) == f"perfil_{CODE}_2_capa"


# --------------------------------------------------------------------------- capas por perfil, dedup e exportação
class TypedListing(KwargsDownloader):
    """Listagem FAKE com o contrato do provider web para o tipo de mídia: só entrega o que passa em
    `filter_by_content` (a mesma função do provider) e conta o resto em `page_counts.expected.content_filter` do
    checkpoint da página. Aceita (e registra) os kwargs novos (`covers`, `max_views`)."""

    def iter_profile_media(self, username, content, limit, *, cancel=None, resume=None, on_checkpoint=None,
                           **extra):
        self.kwargs.append(extra)
        dropped = 0

        def checkpoint(cp):
            nonlocal dropped
            counts = {"expected": {"content_filter": dropped}} if dropped else None
            dropped = 0
            if on_checkpoint is not None:
                on_checkpoint(replace(cp, page_counts=counts))

        kept = 0
        for item in FakeInstagramDownloader.iter_profile_media(self, username, content, None, cancel=cancel,
                                                               resume=resume, on_checkpoint=checkpoint):
            if not filter_by_content([item], content):
                dropped += 1
                continue
            if limit is not None and kept >= limit:
                return
            kept += 1
            yield item


def mixed_profile() -> list:
    """Carrossel misto (foto + vídeo), foto avulsa e Reel, todos com capa (`thumbnail_url`)."""
    return with_thumb([remote("M1", 0), remote("M1", 1, media_type="video"), remote("F1"),
                       remote("R1", media_type="video")])


def media_by_key(h) -> dict[tuple[str, int, str], dict]:
    return {(row["source_post_id"], row["source_media_index"], row["role"]): dict(row)
            for row in h.ctx.db.fetchall("SELECT * FROM media")}


def test_profile_covers_with_mixed_carousel_and_photo_keep_only_video_covers(harness_factory):
    downloader = TypedListing(items=mixed_profile())
    h = harness_factory(downloader=downloader)
    batch_id = start(h, inputs=["perfil_teste"], limit=10, media="covers")["batch_ids"][0]
    finish(h)
    assert downloader.kwargs[0] == {"covers": True}
    jobs = jobs_of(h, batch_id)
    assert [(j["source_post_id"], j["source_media_index"], j["media_role"], j["media_type"]) for j in jobs] == [
        ("M1", 1, "cover", "image"), ("R1", 0, "cover", "image")]
    # Só as capas dos vídeos foram baixadas (nunca o vídeo nem as fotos).
    assert sorted(downloader.downloads) == ["M1:1", "R1:0"]
    assert sorted(media_by_key(h)) == [("M1", 1, "cover"), ("R1", 0, "cover")]
    batch = h.batch(batch_id)
    assert (batch["status"], batch["media"]) == ("completed", "covers")
    progress = batch["progress"]
    assert (progress["found"], progress["downloaded"], progress["filtered"], progress["duplicates"]) == (2, 2, 2, 0)


def test_profile_video_and_cover_coexist_and_reimported_covers_are_duplicates(harness_factory):
    downloader = TypedListing(items=mixed_profile())
    h = harness_factory(downloader=downloader)
    videos_id = start(h, inputs=["perfil_teste"], limit=10)["batch_ids"][0]
    finish(h)
    covers_id = start(h, inputs=["perfil_teste"], limit=10, media="covers")["batch_ids"][0]
    finish(h)
    rows = media_by_key(h)
    assert sorted(rows) == [("M1", 1, "cover"), ("M1", 1, "original"), ("R1", 0, "cover"), ("R1", 0, "original")]
    assert {row["media_type"] for (_, _, role), row in rows.items() if role == "original"} == {"video"}
    assert {row["media_type"] for (_, _, role), row in rows.items() if role == "cover"} == {"image"}
    for batch_id in (videos_id, covers_id):
        progress = h.batch(batch_id)["progress"]
        assert (progress["downloaded"], progress["duplicates"], progress["filtered"]) == (2, 0, 2)
    # Capas de novo: as duas já existem (dedup por origem COM o papel), nenhum arquivo novo.
    downloads = len(downloader.downloads)
    again_id = start(h, inputs=["perfil_teste"], limit=10, media="covers")["batch_ids"][0]
    finish(h)
    again = h.batch(again_id)
    assert again["status"] == "completed"
    assert (again["progress"]["downloaded"], again["progress"]["duplicates"], again["progress"]["filtered"]) == (
        0, 2, 2)
    assert len(downloader.downloads) == downloads and len(media_by_key(h)) == 4
    assert {j["media_id"] for j in jobs_of(h, again_id)} == {rows[("M1", 1, "cover")]["id"],
                                                               rows[("R1", 0, "cover")]["id"]}


def test_batch_zip_with_videos_and_covers_is_one_flat_zip_with_capa_beside_the_video(harness_factory, tmp_path):
    h = harness_factory(downloader=TypedListing(items=mixed_profile()))
    prepare(h, tmp_path)
    start(h, inputs=["perfil_teste"], limit=10)
    finish(h)
    start(h, inputs=["perfil_teste"], limit=10, media="covers")
    finish(h)
    rows = media_by_key(h)
    # Lote com vídeos E capas (relação durável batch_media, que é o que a exportação de lote lê).
    batch_id = seed_batch(h, [row["id"] for row in rows.values()], profile="perfil_teste")
    export = zip_export(h, {"kind": "batch", "id": batch_id})
    assert (export["status"], export["total"], export["copied"]) == ("completed", 4, 4)
    with zipfile.ZipFile(stored_zip(h, export["id"])) as archive:
        assert assert_flat_and_unique(archive) == [
            "perfil_teste_2026-01-01_M1_2.mp4", "perfil_teste_2026-01-01_M1_2_capa.jpg",
            "perfil_teste_2026-01-01_R1.mp4", "perfil_teste_2026-01-01_R1_capa.jpg"]
        for (code, index, role), row in rows.items():
            stem = f"perfil_teste_2026-01-01_{code}" + ("_2" if index else "")
            name = stem + ("_capa.jpg" if role == "cover" else ".mp4")
            assert archive.read(name) == h.ctx.storage.resolve(row["original_path"]).read_bytes()


def test_library_zip_of_many_covers_by_ids_and_by_cover_filter(harness_factory, tmp_path):
    items = with_thumb([remote(f"V{i:02d}", media_type="video") for i in range(30)])
    h = harness_factory(downloader=TypedListing(items=items))
    prepare(h, tmp_path)
    start(h, inputs=["perfil_teste"], limit=50)                     # os vídeos (ficam fora do filtro de capas)
    finish(h, rounds=8)
    start(h, inputs=["perfil_teste"], limit=50, media="covers")
    finish(h, rounds=8)
    covers = [dict(r) for r in h.ctx.db.fetchall("SELECT * FROM media WHERE role = 'cover' ORDER BY id")]
    assert len(covers) == 30 and h.ctx.db.scalar("SELECT COUNT(*) FROM media WHERE role = 'original'") == 30
    expected = sorted(f"perfil_teste_2026-01-01_V{i:02d}_capa.jpg" for i in range(30))
    by_ids = zip_export(h, library(row["id"] for row in covers))
    by_filter = zip_export(h, {"kind": "library", "selection": {"filter": {"role": "cover"}}})
    for export in (by_ids, by_filter):
        assert (export["status"], export["total"], export["copied"]) == ("completed", 30, 30)
        with zipfile.ZipFile(stored_zip(h, export["id"])) as archive:
            assert assert_flat_and_unique(archive) == expected


# --------------------------------------------------------------------------- 1a página vazia
def test_empty_first_page_ends_with_its_own_reason_and_message(harness_factory):
    from app.jobs.import_runner import EMPTY_FIRST_PAGE_MESSAGE
    site = MirroredSite(gp.body(1, [], has_next=False))
    # Sem a etapa "aba Reels" (migration 0027; testes próprios em test_post_times_flow.py): só a grade.
    h = harness_factory(downloader=WebListingFakeDownload(gp.downloader(site)))
    batch_id = start(h, inputs=["perfil_teste"], limit=50)["batch_ids"][0]
    finish(h)
    batch = h.batch(batch_id)
    assert (batch["status"], batch["found"], batch["listing_end"]) == ("completed", 0, "empty_first_page")
    assert batch["error_message"] == EMPTY_FIRST_PAGE_MESSAGE and batch["error_code"] is None
    # A marca fica no lote (a coluna da 0012 continua profile_end; a API apresenta empty_first_page).
    assert h.ctx.db.scalar("SELECT listing_empty_first_page FROM batches WHERE id = ?", (batch_id,)) == 1


def test_profile_with_posts_still_ends_as_profile_end(harness_factory):
    site = MirroredSite(gp.body(1, [tab_reel(1, 0)], has_next=False))
    h = harness_factory(downloader=WebListingFakeDownload(gp.downloader(site)))
    batch_id = start(h, inputs=["perfil_teste"], limit=50)["batch_ids"][0]
    finish(h)
    batch = h.batch(batch_id)
    assert (batch["found"], batch["listing_end"]) == (1, "profile_end")
    assert h.ctx.db.scalar("SELECT listing_empty_first_page FROM batches WHERE id = ?", (batch_id,)) is None


# --------------------------------------------------------------------------- capa maior pela publicação (lote por perfil)
BIG = "https://scontent.cdninstagram.com/v/CAPA-GRANDE-{code}.jpg?oh=SIGFAKE"


class CoverPostDownloader(KwargsDownloader):
    """Listagem com a capa menor; `resolve_post` devolve a capa maior (ou o erro configurado). Registra as consultas
    e a URL de cada download."""

    def __init__(self, *args, post_error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.post_error = post_error
        self.resolved: list[str] = []
        self.urls: dict[str, str] = {}

    def resolve_post(self, shortcode, *, cancel=None, path_kind=None):
        self.resolved.append(shortcode)
        if self.post_error is not None:
            raise self.post_error
        return [replace(item, thumbnail_url=BIG.format(code=shortcode)) for item in self.items
                if item.source_post_id == shortcode]

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        self.urls[item.source_post_id] = item.media_url
        return super().download(item, dest_dir, cancel=cancel, progress=progress)


def test_profile_cover_uses_the_largest_cover_from_the_post(harness_factory, caplog):
    caplog.set_level(logging.INFO)
    downloader = CoverPostDownloader(items=with_thumb([remote("C1", media_type="video"),
                                                       remote("C2", media_type="video")]))
    h = harness_factory(downloader=downloader)
    batch_id = start(h, inputs=["perfil_teste"], limit=10, media="covers")["batch_ids"][0]
    finish(h)
    assert sorted(downloader.resolved) == ["C1", "C2"]   # UMA leitura por capa
    assert downloader.urls == {"C1": BIG.format(code="C1"), "C2": BIG.format(code="C2")}
    assert h.batch(batch_id)["progress"]["downloaded"] == 2
    assert "CAPA-GRANDE" not in caplog.text and "CAPA-C1" not in caplog.text   # log sem URL


def test_profile_cover_falls_back_to_the_listing_cover_and_pauses_on_rate_limit(harness_factory, caplog):
    caplog.set_level(logging.INFO)
    error = InstagramError("rate_limited", "Muitas requisições.", retryable=True)
    downloader = CoverPostDownloader(items=with_thumb([remote("C1", media_type="video")]), post_error=error)
    h = harness_factory(downloader=downloader)
    paused: list = []
    original = h.ctx.worker.pause_for
    h.ctx.worker.pause_for = lambda code: paused.append(code) or original(code)
    batch_id = start(h, inputs=["perfil_teste"], limit=10, media="covers")["batch_ids"][0]
    finish(h)
    assert downloader.resolved == ["C1"]   # sem insistir
    assert downloader.urls == {"C1": THUMB.format(code="C1")}   # a capa da listagem
    assert paused == ["rate_limited"]
    assert h.batch(batch_id)["progress"]["downloaded"] == 1
    lines = [r.getMessage() for r in caplog.records if "capa da listagem" in r.getMessage()]
    assert lines and all("http" not in line for line in lines)


def test_links_cover_batch_makes_no_extra_post_request(harness_factory):
    downloader = LinksDownloader(posts={CODE: with_thumb(reel(CODE))})
    h = harness_factory(downloader=downloader)
    start(h, inputs=[link(CODE)], media="covers")
    finish(h)
    assert downloader.calls == [CODE]   # só a resolução do link
