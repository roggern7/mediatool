"""Extensão como central de operações (docs/CONTRACTS.md, seções 8.6 e 8.7): tipos de mídia (`media_set`, migration
0023), exportação automática ao concluir e as rotas novas de ``/api/ext/*``. 100% offline, valores FAKE."""

from __future__ import annotations

import json
import zipfile

import pytest

from app.integrations.instagram.downloader import InstagramError
from app.services import auto_export
from export_helpers import prepare, run_exports
from import_links_fakes import OWNER, LinksDownloader, carousel, link, photo, reel
from media_fakes import remote
from test_exports_zip import assert_flat_and_unique, stored_zip
from test_extension_api import EXT_ORIGIN, connect
from test_unified_import import TypedListing, finish, jobs_of, media_by_key, mixed_profile, start, with_thumb

CODE, CODE2, CODE3 = "ABCDE12345", "FGHIJ67890", "KLMNO11111"


def job_keys(h, batch_id: int) -> list[tuple[str, int, str]]:
    return [(j["source_post_id"], j["source_media_index"], j["media_role"]) for j in jobs_of(h, batch_id)]


def mixed_carousel(code: str) -> list:
    """Carrossel foto, vídeo, foto (na ordem), com capa no vídeo."""
    return with_thumb(carousel(code, 3))


# =========================================================================== A. tipos de mídia
@pytest.mark.parametrize("media", ["all", "videos_covers"])
def test_reel_gives_video_and_cover(harness_factory, media):
    downloader = LinksDownloader(posts={CODE: with_thumb(reel(CODE))})
    h = harness_factory(downloader=downloader)
    batch_id = start(h, inputs=[link(CODE, "reel")], media=media)["links_batch_id"]
    finish(h)
    assert job_keys(h, batch_id) == [(CODE, 0, "original"), (CODE, 0, "cover")]
    assert sorted(media_by_key(h)) == [(CODE, 0, "cover"), (CODE, 0, "original")]
    rows = media_by_key(h)
    assert rows[(CODE, 0, "original")]["media_type"] == "video" and rows[(CODE, 0, "cover")]["media_type"] == "image"
    batch = h.batch(batch_id)
    assert (batch["status"], batch["media"], batch["progress"]["downloaded"]) == ("completed", media, 2)


def test_photo_in_all_has_no_cover_and_photos_skip_videos(harness_factory):
    h = harness_factory(downloader=LinksDownloader(posts={CODE: photo(CODE), CODE2: with_thumb(reel(CODE2))}))
    all_id = start(h, inputs=[link(CODE)], media="all")["links_batch_id"]
    finish(h)
    assert job_keys(h, all_id) == [(CODE, 0, "original")]
    photos_id = start(h, inputs=[link(CODE), link(CODE2)], media="photos")["links_batch_id"]
    finish(h)
    batch = h.batch(photos_id)
    assert job_keys(h, photos_id) == [(CODE, 0, "original")]   # o Reel é filtrado
    assert (batch["media"], batch["progress"]["duplicates"], batch["progress"]["filtered"]) == ("photos", 1, 1)


def test_mixed_carousel_all_keeps_order_and_one_failure_does_not_stop_the_others(harness_factory):
    downloader = LinksDownloader(posts={CODE: mixed_carousel(CODE)})
    downloader.download_errors = {f"{CODE}:2": [InstagramError("content_unavailable")]}
    h = harness_factory(downloader=downloader)
    batch_id = start(h, inputs=[link(CODE)], media="all")["links_batch_id"]
    finish(h)
    assert job_keys(h, batch_id) == [(CODE, 0, "original"), (CODE, 1, "original"), (CODE, 1, "cover"),
                                     (CODE, 2, "original")]
    statuses = {(j["source_media_index"], j["media_role"]): j["status"] for j in jobs_of(h, batch_id)}
    assert statuses == {(0, "original"): "completed", (1, "original"): "completed", (1, "cover"): "completed",
                        (2, "original"): "failed"}
    batch = h.batch(batch_id)
    assert (batch["status"], batch["progress"]["downloaded"], batch["progress"]["failed"]) == ("completed", 3, 1)
    headers = connect(h)
    item = next(i for i in h.client.get("/api/ext/activity", headers=headers).json()["items"] if i["id"] == batch_id)
    assert item["state"] == "partial"


@pytest.mark.parametrize("media,expected", [
    ("videos", [("M1", 1, "original"), ("R1", 0, "original")]),
    ("photos", [("M1", 0, "original"), ("F1", 0, "original")]),
    ("covers", [("M1", 1, "cover"), ("R1", 0, "cover")]),
    ("videos_covers", [("M1", 1, "original"), ("M1", 1, "cover"), ("R1", 0, "original"), ("R1", 0, "cover")]),
    ("all", [("M1", 0, "original"), ("M1", 1, "original"), ("M1", 1, "cover"), ("F1", 0, "original"),
             ("R1", 0, "original"), ("R1", 0, "cover")]),
])
def test_profile_media_sets(harness_factory, media, expected):
    downloader = TypedListing(items=mixed_profile())
    h = harness_factory(downloader=downloader)
    batch_id = start(h, inputs=["perfil_teste"], limit=10, media=media)["batch_ids"][0]
    finish(h)
    assert job_keys(h, batch_id) == expected
    assert downloader.kwargs[0] == ({"covers": True} if media in ("covers", "videos_covers", "all") else {})
    batch = h.batch(batch_id)
    assert (batch["status"], batch["media"], batch["progress"]["failed"]) == ("completed", media, 0)


def test_limit_counts_items_not_the_extra_cover_job(harness_factory):
    items = with_thumb([remote(f"V{i}", media_type="video") for i in range(5)])
    h = harness_factory(downloader=TypedListing(items=items))
    batch_id = start(h, inputs=["perfil_teste"], limit=2, media="videos_covers")["batch_ids"][0]
    finish(h)
    assert job_keys(h, batch_id) == [("V0", 0, "original"), ("V0", 0, "cover"), ("V1", 0, "original"),
                                     ("V1", 0, "cover")]


def test_all_after_videos_dedups_the_video_and_adds_the_cover(harness_factory):
    downloader = LinksDownloader(posts={CODE: with_thumb(reel(CODE))})
    h = harness_factory(downloader=downloader)
    start(h, inputs=[link(CODE)], media="videos")
    finish(h)
    again = start(h, inputs=[link(CODE)], media="all")["links_batch_id"]
    finish(h)
    progress = h.batch(again)["progress"]
    assert (progress["downloaded"], progress["duplicates"]) == (1, 1)
    assert sorted(media_by_key(h)) == [(CODE, 0, "cover"), (CODE, 0, "original")]


@pytest.mark.parametrize("media,status", [("photos", 422), ("all", 422), ("videos_covers", 202), ("covers", 202)])
def test_views_filter_by_media_set(harness, media, status):
    body = start(harness, expect=status, inputs=["perfil_teste"], limit=10, media=media, min_views=100)
    if status == 422:
        assert body["code"] == "views_filter_media"


@pytest.mark.parametrize("media", ["videos_covers", "all"])
def test_cover_sets_are_web_only(harness, media):
    assert harness.client.put("/api/settings", json={"instagram_provider": "gallery_dl"}).status_code == 200
    assert start(harness, expect=422, inputs=["perfil_teste"], limit=10, media=media)["code"] == \
        "not_supported_by_provider"
    assert start(harness, expect=422, inputs=[link(CODE)], media=media)["code"] == "not_supported_by_provider"
    start(harness, inputs=[link(CODE)], media="photos")   # sem capa: o gallery-dl atende


def test_mixed_sets_only_process_original_videos(harness_factory):
    h = harness_factory(downloader=LinksDownloader(posts={CODE: mixed_carousel(CODE)}))
    batch_id = start(h, inputs=[link(CODE)], media="all", post_processing="preset")["links_batch_id"]
    finish(h)
    jobs = {(j["source_media_index"], j["media_role"]): j for j in jobs_of(h, batch_id)}
    assert jobs[(1, "original")]["params"] is not None
    assert all(jobs[key]["params"] is None for key in ((0, "original"), (1, "cover"), (2, "original")))
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == 1


# =========================================================================== B. exportação automática
EDIT = {"duration_mode": "15", "speed": 1.1, "sharpen": False, "output_type": "mp4"}


def _exports(h) -> list[dict]:
    return [dict(r) for r in h.ctx.db.fetchall("SELECT * FROM exports ORDER BY id")]


def test_auto_export_of_a_batch_is_one_zip_and_idempotent(harness_factory, tmp_path):
    h = harness_factory(downloader=LinksDownloader(posts={CODE: with_thumb(reel(CODE))}))
    prepare(h, tmp_path)
    batch_id = start(h, inputs=[link(CODE)], media="all", auto_export={"content": "originals"})["links_batch_id"]
    assert h.batch(batch_id)["auto_export"]["status"] == "waiting"
    finish(h)
    exports = _exports(h)
    assert len(exports) == 1 and exports[0]["batch_id"] == batch_id and exports[0]["mode"] == "zip"
    for _ in range(3):   # disparos repetidos não criam outra
        assert auto_export.run_due(h.ctx) == []
        h.run_jobs()
    assert len(_exports(h)) == 1
    run_exports(h)
    auto = h.batch(batch_id)["auto_export"]
    assert auto["status"] == "completed" and auto["zip_ready"] and auto["download_url"].startswith(
        f"/api/exports/{exports[0]['id']}/download?token=")
    with zipfile.ZipFile(stored_zip(h, exports[0]["id"])) as archive:
        names = assert_flat_and_unique(archive)
    assert len(names) == 2 and sum(name.endswith("_capa.jpg") for name in names) == 1


def test_auto_export_of_a_group_is_one_zip_of_the_whole_group(harness_factory, tmp_path):
    h = harness_factory(downloader=TypedListing(items=mixed_profile()))
    prepare(h, tmp_path)
    result = start(h, inputs=["perfil_um", "perfil_dois"], limit=10, media="videos",
                   auto_export={"content": "originals"})
    group_id = result["group_id"]
    finish(h, rounds=6)
    exports = _exports(h)
    assert len(exports) == 1 and exports[0]["group_id"] == group_id and exports[0]["batch_id"] is None
    group = h.client.get(f"/api/import-groups/{group_id}").json()["group"]
    assert group["auto_export"]["export_id"] == exports[0]["id"]
    children = h.ctx.db.fetchall("SELECT auto_export_state FROM batches WHERE group_id = ?", (group_id,))
    assert [row["auto_export_state"] for row in children] == [None, None]


def test_auto_export_waits_for_pending_processing_of_its_media(harness_factory, tmp_path):
    h = harness_factory(downloader=LinksDownloader(posts={CODE: reel(CODE)}))
    prepare(h, tmp_path)
    start(h, inputs=[link(CODE)])
    finish(h)
    media_id = h.ctx.db.scalar("SELECT id FROM media")
    batch_id = start(h, inputs=[link(CODE)], auto_export={"content": "originals"})["links_batch_id"]
    preset_id = h.ctx.presets.first_builtin_id()
    from app.services import import_service
    import_service.process_media(h.ctx, media_id, preset_id)   # processamento pendente da mídia, fora do lote
    h.run_imports()
    assert h.batch(batch_id)["status"] == "completed"
    assert auto_export.run_due(h.ctx) == []
    assert h.batch(batch_id)["auto_export"]["status"] == "waiting"
    h.run_jobs()   # processa
    h.run_jobs()   # manutenção: agora sem processamento pendente
    assert h.batch(batch_id)["auto_export"]["status"] in ("pending", "running", "completed")
    assert len(_exports(h)) == 1


def test_auto_export_with_edit_after_download_waits_for_the_edit(harness_factory, tmp_path):
    h = harness_factory(downloader=LinksDownloader(posts={CODE: reel(CODE)}))
    prepare(h, tmp_path)
    batch_id = start(h, inputs=[link(CODE)], post_processing="edit", edit=EDIT,
                     auto_export={"content": "edited"})["links_batch_id"]
    finish(h)
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM renditions") == 1
    exports = _exports(h)
    assert len(exports) == 1
    run_exports(h)
    assert h.batch(batch_id)["auto_export"]["status"] == "completed"


def test_auto_export_failure_is_recorded_and_visible(harness_factory, tmp_path, monkeypatch):
    from app.core.errors import AppError
    from app.services import export_service

    def refuse(*args, **kwargs):
        raise AppError(422, "export_empty", "Nada para exportar.")

    h = harness_factory(downloader=LinksDownloader(posts={CODE: reel(CODE)}))
    prepare(h, tmp_path)
    # Erro do serviço de exportação (não é "em andamento"/"fila cheia"): fica registrado, sem nova tentativa.
    monkeypatch.setattr(export_service, "create_export", refuse)
    batch_id = start(h, inputs=[link(CODE)], media="covers", auto_export={"content": "originals"})["links_batch_id"]
    finish(h)
    auto = h.batch(batch_id)["auto_export"]
    assert auto["status"] == "failed" and auto["error_message"] and auto["export_id"] is None
    assert _exports(h) == []
    item = next(i for i in h.client.get("/api/ext/activity", headers=connect(h)).json()["items"]
                if i["id"] == batch_id)
    assert item["auto_export"]["status"] == "failed" and item["auto_export"]["error_message"]
    assert auto_export.run_due(h.ctx) == [] and _exports(h) == []


def test_cancelled_batch_never_exports(harness_factory):
    h = harness_factory(downloader=LinksDownloader(posts={CODE: reel(CODE)}))
    batch_id = start(h, inputs=[link(CODE)], auto_export={"content": "originals"})["links_batch_id"]
    assert h.client.post(f"/api/batches/{batch_id}/cancel").status_code in (200, 202, 204)
    finish(h)
    assert h.batch(batch_id)["auto_export"]["status"] == "failed" and _exports(h) == []


class ProfilesAndLinks(TypedListing):
    """Listagem de perfil FAKE (`TypedListing`) que também resolve links (`posts[shortcode]`)."""

    def __init__(self, *, posts, **kwargs):
        super().__init__(**kwargs)
        self.posts = posts

    def resolve_post(self, shortcode, *, cancel=None, path_kind=None):
        return list(self.posts.get(shortcode, []))


@pytest.mark.parametrize("profiles", [["perfil_um"], ["perfil_um", "perfil_dois"]])
def test_mixed_request_profiles_and_links_is_one_zip(harness_factory, tmp_path, profiles):
    """Perfis E links no mesmo start com exportação automática: UM ZIP do pedido (fonte `library` com a união das
    mídias das partes), criado só quando TODAS as partes terminaram; as partes mostram a mesma exportação."""
    h = harness_factory(downloader=ProfilesAndLinks(items=mixed_profile(), posts={CODE: with_thumb(reel(CODE))}))
    prepare(h, tmp_path)
    result = start(h, inputs=[*profiles, link(CODE)], limit=10, media="all", auto_export={"content": "originals"})
    links_id, group_id = result["links_batch_id"], result["group_id"]
    parts = [("batch", links_id), ("group", group_id) if group_id else ("batch", result["batch_ids"][0])]
    request = dict(h.ctx.db.fetchone("SELECT * FROM import_requests"))
    assert request["auto_export_state"] == "waiting"
    pointing = {(kind, row_id) for kind, table in (("batch", "batches"), ("group", "import_groups"))
                for (row_id,) in h.ctx.db.fetchall(f"SELECT id FROM {table} WHERE request_id = ?", (request["id"],))}
    assert pointing == set(parts), "só as partes de nível superior apontam para o pedido (filhos de grupo não)"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches WHERE auto_export_content IS NOT NULL") == 0
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM import_groups WHERE auto_export_content IS NOT NULL") == 0
    # Uma parte encerrada e a outra aberta: nada é exportado ainda.
    h.ctx.db.execute("UPDATE batches SET status = 'completed' WHERE id = ?", (links_id,))
    assert h.ctx.batches.request_auto_export_due() == []
    h.ctx.db.execute("UPDATE batches SET status = 'listing' WHERE id = ?", (links_id,))
    finish(h, rounds=6)
    exports = _exports(h)
    assert len(exports) == 1, exports
    export = exports[0]
    assert (export["kind"], export["mode"], export["batch_id"], export["group_id"]) == ("library", "zip", None, None)
    items = {row[0] for row in h.ctx.db.fetchall("SELECT media_id FROM export_items WHERE export_id = ?",
                                                  (export["id"],))}
    assert items == {row[0] for row in h.ctx.db.fetchall("SELECT id FROM media")} and len(items) > 2
    for _ in range(2):
        assert auto_export.run_due(h.ctx) == []
    assert len(_exports(h)) == 1
    run_exports(h)
    shown = [h.batch(part_id)["auto_export"] if kind == "batch"
             else h.client.get(f"/api/import-groups/{part_id}").json()["group"]["auto_export"]
             for kind, part_id in parts]
    assert all(auto["export_id"] == export["id"] and auto["request_id"] == request["id"] for auto in shown)
    assert all(auto["status"] == "completed" and auto["download_url"] for auto in shown)
    activity = h.client.get("/api/ext/activity", headers=connect(h)).json()["items"]
    assert {i["auto_export"]["export_id"] for i in activity} == {export["id"]} and len(activity) == 2


def test_mixed_request_with_a_cancelled_part_exports_the_other(harness_factory, tmp_path):
    h = harness_factory(downloader=ProfilesAndLinks(items=mixed_profile(), posts={CODE: with_thumb(reel(CODE))}))
    prepare(h, tmp_path)
    result = start(h, inputs=["perfil_um", link(CODE)], limit=10, media="videos",
                   auto_export={"content": "originals"})
    assert h.client.post(f"/api/batches/{result['links_batch_id']}/cancel").status_code in (200, 202, 204)
    finish(h, rounds=6)
    exports = _exports(h)
    assert len(exports) == 1 and exports[0]["kind"] == "library"
    profile_media = {row[0] for row in h.ctx.db.fetchall(
        "SELECT media_id FROM jobs WHERE batch_id = ? AND media_id IS NOT NULL", (result["batch_ids"][0],))}
    items = {row[0] for row in h.ctx.db.fetchall("SELECT media_id FROM export_items WHERE export_id = ?",
                                                  (exports[0]["id"],))}
    assert items == profile_media and items


def test_mixed_request_all_parts_cancelled_never_exports(harness_factory):
    h = harness_factory(downloader=ProfilesAndLinks(items=mixed_profile(), posts={CODE: reel(CODE)}))
    result = start(h, inputs=["perfil_um", link(CODE)], limit=10, auto_export={"content": "originals"})
    for batch_id in result["batch_ids"]:
        assert h.client.post(f"/api/batches/{batch_id}/cancel").status_code in (200, 202, 204)
    finish(h)
    auto = h.batch(result["links_batch_id"])["auto_export"]
    assert auto["status"] == "failed" and auto["error_message"] and _exports(h) == []


@pytest.mark.parametrize("content", ["edited", "both"])
def test_edited_auto_export_needs_post_processing(harness, content):
    error = start(harness, expect=422, inputs=[link(CODE)], auto_export={"content": content})
    assert error["code"] == "auto_export_needs_processing" and "originais" in error["detail"]
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    response = harness.client.post("/api/ext/start", json={"inputs": [link(CODE)], "auto_export": {"content": content}},
                                   headers=connect(harness))
    assert response.status_code == 422 and response.json()["code"] == "auto_export_needs_processing"
    start(harness, inputs=[link(CODE)], auto_export={"content": "originals"})


@pytest.mark.parametrize("media", ["covers", "photos"])
def test_edited_auto_export_refused_for_covers_and_photos(harness, media):
    # Capas e fotos nunca são processadas: "editados" falharia ao concluir mesmo com pós-processamento escolhido.
    edit = {"duration_mode": "15", "speed": 1.05, "sharpen": False, "output_type": "mp4"}
    error = start(harness, expect=422, inputs=[link(CODE)], media=media, post_processing="edit", edit=edit,
                  auto_export={"content": "both"})
    assert error["code"] == "auto_export_needs_processing"
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0


class _Crash(BaseException):
    """Queda do app entre criar a exportação e gravar o id dela (não é Exception: nada a captura)."""


def test_crash_between_export_and_record_never_makes_a_second_zip(harness_factory, tmp_path, monkeypatch):
    h = harness_factory(downloader=LinksDownloader(posts={CODE: reel(CODE)}))
    prepare(h, tmp_path)
    batch_id = start(h, inputs=[link(CODE)], auto_export={"content": "originals"})["links_batch_id"]
    finish(h)
    first = _exports(h)
    assert len(first) == 1
    # Volta ao estado de antes do disparo e repete com a queda injetada logo depois de a exportação ser inserida.
    h.ctx.exports.delete(first[0]["id"])
    h.ctx.db.execute("UPDATE batches SET auto_export_state = 'waiting', auto_export_id = NULL WHERE id = ?",
                     (batch_id,))

    def crash(*_args, **_kwargs):
        raise _Crash()

    with monkeypatch.context() as patch:
        patch.setattr(h.ctx.batches, "finish_auto_export", crash)
        with pytest.raises(_Crash):
            auto_export.run_due(h.ctx)
    assert _exports(h) == [], "a exportação inserida é desfeita junto (mesma transação)"
    assert h.ctx.batches.get(batch_id)["auto_export_state"] == "waiting"
    created = auto_export.run_due(h.ctx)
    assert len(created) == 1 and len(_exports(h)) == 1
    assert h.ctx.batches.get(batch_id)["auto_export_id"] == created[0]
    assert auto_export.run_due(h.ctx) == [] and len(_exports(h)) == 1


# =========================================================================== C. rotas /api/ext/*
ROUTES = [
    ("get", "/api/ext/activity", None),
    ("get", "/api/ext/batches/1/items", None),
    ("get", "/api/ext/groups/1/children", None),
    ("post", "/api/ext/batches/1/cancel", None),
    ("post", "/api/ext/groups/1/cancel", None),
    ("get", "/api/ext/library", None),
    ("get", "/api/ext/library/ids?batch_id=1", None),
    ("get", "/api/ext/media/1/thumbnail", None),
    ("get", "/api/ext/edit/options", None),
    ("post", "/api/ext/edits", {"media_ids": [1], "settings": {"duration_mode": "15", "speed": 1.0,
                                                               "sharpen": False}}),
    ("post", "/api/ext/process", {"media_ids": [1], "preset_id": 1}),
    ("post", "/api/ext/exports", {"source": {"kind": "batch", "id": 1}, "content": "originals"}),
    ("get", "/api/ext/exports/1", None),
    ("post", "/api/ext/post-preview", {"url": link(CODE)}),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_new_route_requires_the_token(harness, method, path, body):
    connect(harness)
    kwargs = {"json": body} if body is not None else {}
    for headers in ({"Origin": EXT_ORIGIN}, {"Origin": EXT_ORIGIN, "Authorization": "Bearer errado"}):
        response = getattr(harness.client, method)(path, headers=headers, **kwargs)
        assert response.status_code == 401, (path, response.text)
        assert response.json()["code"] == "extension_unauthorized"
    other = getattr(harness.client, method)(path, headers={"Origin": "chrome-extension://" + "a" * 32}, **kwargs)
    assert other.status_code == 403


def _seed_library(h):
    """Reel + carrossel misto baixados com "Todas" (vídeos, fotos e capas)."""
    h.downloader = LinksDownloader(posts={CODE: with_thumb(reel(CODE)), CODE2: mixed_carousel(CODE2)})
    batch_id = start(h, inputs=[link(CODE), link(CODE2)], media="all")["links_batch_id"]
    finish(h)
    return batch_id


def test_page_info_reports_what_is_in_the_library(harness):
    batch_id = _seed_library(harness)
    headers = connect(harness)
    post = harness.client.post("/api/ext/page-info", json={"url": link(CODE)}, headers=headers).json()
    assert post["library"]["in_library"] is True
    assert [(i["index"], i["role"], i["media_type"]) for i in post["library"]["items"]] == [
        (0, "original", "video"), (0, "cover", "image")]
    assert post["library"]["source_profile"] == OWNER.lower()
    missing = harness.client.post("/api/ext/page-info", json={"url": link(CODE3)}, headers=headers).json()
    assert missing["library"] == {"in_library": False, "items": [], "source_profile": None}
    profile = harness.client.post("/api/ext/page-info", json={"url": f"https://www.instagram.com/{OWNER}/"},
                                  headers=headers).json()
    assert profile["library"] == {"media_count": 6, "videos": 2, "photos": 2, "covers": 2}
    assert batch_id


def test_post_preview_shape_cache_and_no_media_urls(harness):
    harness.downloader = LinksDownloader(posts={CODE: mixed_carousel(CODE), CODE2: with_thumb(reel(CODE2))})
    headers = connect(harness)
    body = harness.client.post("/api/ext/post-preview", json={"url": link(CODE)}, headers=headers).json()
    assert body["kind"] == "carousel" and body["username"] == OWNER and body["shortcode"] == CODE
    assert [(i["index"], i["media_type"], i["has_cover"]) for i in body["items"]] == [
        (0, "image", False), (1, "video", True), (2, "image", False)]
    assert body["counts"] == {"videos": 1, "photos": 2, "covers": 1}
    assert "http" not in json.dumps(body)
    again = harness.client.post("/api/ext/post-preview", json={"url": link(CODE, "reel")}, headers=headers).json()
    assert again == body and harness.downloader.calls == [CODE]   # cache: uma leitura só
    reel_body = harness.client.post("/api/ext/post-preview", json={"url": link(CODE2)}, headers=headers).json()
    assert reel_body["kind"] == "reel" and reel_body["counts"] == {"videos": 1, "photos": 0, "covers": 1}


def test_post_preview_errors_are_classified_without_retry(harness):
    harness.downloader = LinksDownloader(errors={CODE3: [InstagramError("content_unavailable")]})
    headers = connect(harness)
    response = harness.client.post("/api/ext/post-preview", json={"url": link(CODE3)}, headers=headers)
    assert response.status_code == 404 and response.json()["code"] == "content_unavailable"
    assert harness.downloader.calls == [CODE3]
    bad = harness.client.post("/api/ext/post-preview", json={"url": "https://www.instagram.com/perfil/"},
                              headers=headers)
    assert bad.status_code == 422


def test_start_is_idempotent_by_client_request_id(harness):
    headers = connect(harness)
    body = {"inputs": [link(CODE)], "media": "videos_covers", "client_request_id": "req-1",
            "auto_export": {"content": "both"}, "post_processing": "edit", "edit": EDIT}
    first = harness.client.post("/api/ext/start", json=body, headers=headers)
    second = harness.client.post("/api/ext/start", json=body, headers=headers)
    assert first.status_code == second.status_code == 202
    assert second.json()["links_batch_id"] == first.json()["links_batch_id"] and second.json()["replayed"] is True
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 1
    row = harness.ctx.batches.get(first.json()["links_batch_id"])
    assert (row["media_set"], row["auto_export_content"]) == ("videos_covers", "both")
    other = harness.client.post("/api/ext/start", json={**body, "client_request_id": "req-2"}, headers=headers)
    assert other.status_code == 202 and harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 2
    bad = harness.client.post("/api/ext/start", json={**body, "client_request_id": "x" * 65}, headers=headers)
    assert bad.status_code == 422


def _replay_state(h) -> dict:
    rows = h.ctx.db.fetchall("SELECT key, value FROM app_state WHERE key LIKE 'extension!_start!_requests:%'"
                             " ESCAPE '!'")
    return {row["key"]: json.loads(row["value"]) for row in rows}


def test_start_idempotency_survives_a_restart(harness_factory):
    """O client_request_id fica em app_state: um app novo sobre o mesmo banco devolve o mesmo lote sem criar outro."""
    first_app = harness_factory()
    headers = connect(first_app)
    body = {"inputs": [link(CODE)], "media": "videos", "client_request_id": "req-reinicio"}
    first = first_app.client.post("/api/ext/start", json=body, headers=headers)
    assert first.status_code == 202 and "replayed" not in first.json()
    restarted = harness_factory(root=first_app.root)
    assert restarted.ctx is not first_app.ctx
    again = restarted.client.post("/api/ext/start", json=body, headers=headers)
    assert again.status_code == 202 and again.json()["replayed"] is True
    assert again.json()["links_batch_id"] == first.json()["links_batch_id"]
    assert again.json()["open_url"] == first.json()["open_url"]
    assert restarted.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 1
    # Só ids e a resposta pública: nada do token nem do cabeçalho de autorização no que foi gravado.
    token = headers["Authorization"].split(" ", 1)[1]
    stored = _replay_state(restarted)
    assert len(stored) == 1 and token not in json.dumps(stored)
    (entry,) = next(iter(stored.values())).values()
    assert set(entry) == {"expires_at", "response"}


def test_start_idempotency_expires_and_cleans_up(harness):
    headers = connect(harness)
    body = {"inputs": [link(CODE)], "media": "videos", "client_request_id": "req-expira"}
    first = harness.client.post("/api/ext/start", json=body, headers=headers).json()
    (key, entries), = _replay_state(harness).items()
    # Passados os 10 min (validade no passado), o mesmo id cria de novo e a entrada vencida some.
    entries["req-expira"]["expires_at"] = 1.0
    entries["req-velha"] = {"expires_at": 2.0, "response": {"batch_ids": [1]}}
    harness.ctx.app_settings.set_state(key, json.dumps(entries))
    # Chave de outro dispositivo sem gravação há mais de 10 min: limpa no próximo start.
    harness.ctx.db.execute("INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
                           ("extension_start_requests:00000000000000ff", "{}", "2000-01-01T00:00:00.000Z"))
    second = harness.client.post("/api/ext/start", json=body, headers=headers)
    assert second.status_code == 202 and "replayed" not in second.json()
    assert second.json()["links_batch_id"] != first["links_batch_id"]
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 2
    stored = _replay_state(harness)
    assert list(stored) == [key] and list(stored[key]) == ["req-expira"]
    assert stored[key]["req-expira"]["response"]["links_batch_id"] == second.json()["links_batch_id"]


def test_start_replay_is_per_device(harness):
    first_device = connect(harness)
    second_device = connect(harness)
    body = {"inputs": [link(CODE)], "media": "videos", "client_request_id": "mesmo-id"}
    one = harness.client.post("/api/ext/start", json=body, headers=first_device).json()
    two = harness.client.post("/api/ext/start", json=body, headers=second_device).json()
    assert one["links_batch_id"] != two["links_batch_id"] and "replayed" not in two
    assert len(_replay_state(harness)) == 2


def test_group_children_paginated_with_state_and_message(harness_factory):
    h = harness_factory(downloader=TypedListing(items=mixed_profile()))
    group_a = start(h, inputs=["perfil_um", "perfil_dois", "perfil_tres"], limit=10, media="videos")["group_id"]
    group_b = start(h, inputs=["perfil_quatro", "perfil_cinco"], limit=10, media="videos")["group_id"]
    headers = connect(h)
    db = h.ctx.db
    children_a = [row["id"] for row in db.fetchall(
        "SELECT id FROM batches WHERE group_id = ? ORDER BY group_position", (group_a,))]
    db.execute("UPDATE batches SET status = 'failed', error_message = 'Perfil não encontrado' WHERE id = ?",
               (children_a[1],))
    url = f"/api/ext/groups/{group_a}/children"
    page1 = h.client.get(url, params={"limit": 2}, headers=headers)
    assert page1.status_code == 200, page1.text
    page1 = page1.json()
    assert page1["group_id"] == group_a and page1["total"] == 3 and page1["next_after_position"] is not None
    assert [i["id"] for i in page1["items"]] == children_a[:2]
    first, failed = page1["items"]
    assert first["kind"] == "batch" and first["title"] == "@perfil_um" and first["state"] in ("waiting", "analyzing")
    assert set(first["progress"]) >= {"found", "downloaded", "failed", "pending"}
    assert first["open_url"].endswith(f"/fila?lote={children_a[0]}")
    assert failed["state"] == "failed" and failed["message"] == "Perfil não encontrado."
    page2 = h.client.get(url, params={"limit": 2, "after_position": page1["next_after_position"]},
                         headers=headers).json()
    assert [i["id"] for i in page2["items"]] == children_a[2:] and page2["next_after_position"] is None
    # Os filhos de outro grupo nunca aparecem; grupo inexistente: 404; teto do limite.
    other = h.client.get(f"/api/ext/groups/{group_b}/children", headers=headers).json()
    assert {i["id"] for i in other["items"]}.isdisjoint(children_a) and other["total"] == 2
    assert h.client.get("/api/ext/groups/999999/children", headers=headers).status_code == 404
    assert h.client.get(url, params={"limit": 101}, headers=headers).status_code == 422
    # Cada filho expande pelos itens do lote (rota existente).
    items = h.client.get(f"/api/ext/batches/{children_a[0]}/items", headers=headers)
    assert items.status_code == 200 and isinstance(items.json()["items"], list)


def test_media_read_by_ids_for_the_edit_page(harness):
    _seed_library(harness)
    rows = harness.ctx.db.fetchall("SELECT id, media_type, role FROM media ORDER BY id")
    ids = [int(row["id"]) for row in rows]
    asked = [ids[-1], 999999, ids[0], ids[-1]]
    response = harness.client.get("/api/media", params={"ids": ",".join(map(str, asked))})
    assert response.status_code == 200, response.text
    data = response.json()
    assert [m["id"] for m in data["items"]] == [ids[-1], ids[0]]
    assert data["total"] == 2 and data["next_before_id"] is None
    assert {"media_type", "role"} <= set(data["items"][0])
    for bad in ("1,a", "", "0", ",".join(str(i) for i in range(1, 502))):
        assert harness.client.get("/api/media", params={"ids": bad}).status_code == 422, bad


def test_activity_states_and_counters(harness_factory):
    h = harness_factory(downloader=TypedListing(items=mixed_profile()))
    start(h, inputs=["perfil_um", "perfil_dois"], limit=10, media="videos")
    batch_id = start(h, inputs=["perfil_tres"], limit=10, media="covers", min_views=1)["batch_ids"][0]
    headers = connect(h)
    items = h.client.get("/api/ext/activity", params={"limit": 20}, headers=headers).json()["items"]
    assert [i["kind"] for i in items] == ["batch", "group"]
    assert {i["state"] for i in items} <= {"waiting", "analyzing"}
    finish(h, rounds=6)
    items = h.client.get("/api/ext/activity", headers=headers).json()["items"]
    batch, group = items
    assert batch["id"] == batch_id and batch["media_set"] == "covers" and batch["filters"]["min_views"] == 1
    assert set(batch["progress"]) == {"found", "analyzed", "files", "downloaded", "duplicates", "filtered_period",
                                      "filtered_views", "no_count", "failed", "processed", "processing", "pending"}
    assert group["state"] == "done" and group["progress"]["downloaded"] == 2 and group["progress"]["duplicates"] == 2
    assert batch["open_url"].startswith("http://127.0.0.1:") and group["auto_export"] is None
    assert h.client.get("/api/ext/activity", params={"limit": 51}, headers=headers).status_code == 422


def test_activity_message_for_empty_page_attention_failure_and_pauses(harness_factory):
    from app.jobs.import_runner import EMPTY_FIRST_PAGE_MESSAGE
    h = harness_factory(downloader=TypedListing(items=mixed_profile()))
    group_id = start(h, inputs=["perfil_um", "perfil_dois"], limit=10, media="videos")["group_id"]
    batch_id = start(h, inputs=["perfil_tres"], limit=10, media="videos")["batch_ids"][0]
    headers = connect(h)

    def item(kind, row_id):
        items = h.client.get("/api/ext/activity", headers=headers).json()["items"]
        return next(i for i in items if (i["kind"], i["id"]) == (kind, row_id))

    assert item("batch", batch_id)["message"] is None
    db = h.ctx.db
    db.execute("UPDATE batches SET listing_state = 'needs_attention', error_message = 'Sessão recusada' WHERE id = ?",
               (batch_id,))
    message = item("batch", batch_id)["message"]
    assert message.startswith("A importação parou e precisa de você. Motivo: Sessão recusada.")
    db.execute("UPDATE batches SET listing_state = 'running', paused_until = '2999-01-01T00:00:00.000Z' WHERE id = ?",
               (batch_id,))
    assert item("batch", batch_id)["message"].startswith("Importação pausada depois de várias falhas seguidas.")
    db.execute("UPDATE batches SET paused_until = NULL, listing_state = 'paused' WHERE id = ?", (batch_id,))
    assert item("batch", batch_id)["message"].startswith("A coleta da lista está em pausa.")
    db.execute("UPDATE batches SET listing_state = 'running' WHERE id = ?", (batch_id,))
    h.ctx.worker.pause_state = lambda: ("2999-01-01T00:00:00.000Z", "rate_limited")
    paused = item("batch", batch_id)
    assert paused["state"] in ("waiting", "analyzing", "downloading")
    assert paused["message"].startswith("O acesso automático ao Instagram está pausado até")
    assert "limite de requisições do Instagram" in item("group", group_id)["message"]
    del h.ctx.worker.pause_state
    children = [row["id"] for row in db.fetchall("SELECT id FROM batches WHERE group_id = ? ORDER BY id", (group_id,))]
    db.execute("UPDATE batches SET status = 'completed', listing_end = 'profile_end', listing_empty_first_page = 1,"
               " error_message = ? WHERE id = ?", (EMPTY_FIRST_PAGE_MESSAGE, children[0]))
    db.execute("UPDATE batches SET status = 'failed', error_message = 'Perfil não encontrado' WHERE id = ?",
               (children[1],))
    db.execute("UPDATE batches SET status = 'completed', listing_end = 'profile_end', listing_empty_first_page = 1,"
               " error_message = ? WHERE id = ?", (EMPTY_FIRST_PAGE_MESSAGE, batch_id))
    assert item("batch", batch_id)["message"] == EMPTY_FIRST_PAGE_MESSAGE
    assert item("group", group_id)["message"] == (
        f"@perfil_um: {EMPTY_FIRST_PAGE_MESSAGE}; @perfil_dois: Perfil não encontrado.")


def test_batch_items_labels_and_keyset(harness):
    batch_id = _seed_library(harness)
    headers = connect(harness)
    page = harness.client.get(f"/api/ext/batches/{batch_id}/items", params={"limit": 4}, headers=headers).json()
    assert page["total"] == 6 and len(page["items"]) == 4 and page["next_before_id"]
    rest = harness.client.get(f"/api/ext/batches/{batch_id}/items",
                              params={"limit": 4, "before_id": page["next_before_id"]}, headers=headers).json()
    assert len(rest["items"]) == 2 and rest["next_before_id"] is None
    assert {i["stage_label"] for i in page["items"] + rest["items"]} == {"concluído"}
    assert {i["role"] for i in page["items"] + rest["items"]} == {"original", "cover"}
    assert harness.client.get("/api/ext/batches/999/items", headers=headers).status_code == 404


def test_stage_labels():
    from app.services.extension_ops import stage_label
    cases = [({"status": "pending"}, "aguardando"),
             ({"status": "downloading", "media_type": "video"}, "baixando vídeo"),
             ({"status": "downloading", "media_type": "image"}, "baixando foto"),
             ({"status": "downloading", "media_type": "image", "media_role": "cover"}, "baixando capa"),
             ({"status": "downloading", "stage": "probing"}, "analisando"),
             ({"status": "processing"}, "processando"),
             ({"status": "completed", "is_duplicate": 1}, "duplicado"),
             ({"status": "completed"}, "concluído"), ({"status": "failed"}, "falhou"),
             ({"status": "cancelled"}, "cancelado")]
    for job, label in cases:
        assert stage_label(job) == label


def test_cancel_batch_and_group(harness_factory):
    h = harness_factory(downloader=TypedListing(items=mixed_profile()))
    headers = connect(h)
    batch_id = start(h, inputs=["perfil_um"], limit=10)["batch_ids"][0]
    group_id = start(h, inputs=["perfil_dois", "perfil_tres"], limit=10)["group_id"]
    assert h.client.post(f"/api/ext/batches/{batch_id}/cancel", headers=headers).json() == {"ok": True}
    assert h.client.post(f"/api/ext/groups/{group_id}/cancel", headers=headers).json() == {"ok": True}
    assert h.ctx.batches.get(batch_id)["status"] == "cancelled"
    assert h.client.post(f"/api/ext/batches/{batch_id}/cancel", headers=headers).status_code == 409


def test_library_keyset_filters_and_ids(harness):
    batch_id = _seed_library(harness)
    headers = connect(harness)
    first = harness.client.get("/api/ext/library", params={"limit": 4}, headers=headers).json()
    assert first["total"] == 6 and len(first["items"]) == 4
    second = harness.client.get("/api/ext/library", params={"limit": 4, "before_id": first["next_before_id"]},
                                headers=headers).json()
    assert len(second["items"]) == 2 and second["next_before_id"] is None
    item = first["items"][0]
    assert set(item) == {"id", "media_type", "role", "source_profile", "source_post_id", "index", "taken_at",
                         "created_at", "width", "height", "duration", "has_edited", "renditions", "processing",
                         "has_thumbnail"}
    covers = harness.client.get("/api/ext/library", params={"role": "cover", "batch_id": batch_id},
                                headers=headers).json()
    assert covers["total"] == 2 and {i["role"] for i in covers["items"]} == {"cover"}
    videos = harness.client.get("/api/ext/library", params={"media_type": "video", "role": "original"},
                                headers=headers).json()
    assert videos["total"] == 2
    assert harness.client.get("/api/ext/library", params={"profile": "outro"}, headers=headers).json()["total"] == 0
    assert harness.client.get("/api/ext/library", params={"limit": 51}, headers=headers).status_code == 422
    ids = harness.client.get("/api/ext/library/ids", params={"batch_id": batch_id}, headers=headers).json()
    assert len(ids["ids"]) == 6 and ids["truncated"] is False
    # Tipo e papel de cada id ("selecionar todos do lote" mostra o resumo certo na extensão).
    assert [i["id"] for i in ids["items"]] == ids["ids"]
    kinds = sorted((i["media_type"], i["role"]) for i in ids["items"])
    assert kinds == [("image", "cover")] * 2 + [("image", "original")] * 2 + [("video", "original")] * 2
    photos = harness.client.get("/api/ext/library/ids", params={"batch_id": batch_id, "media_type": "image",
                                                                "role": "original"}, headers=headers).json()
    assert len(photos["ids"]) == 2
    assert harness.client.get("/api/ext/library/ids", headers=headers).status_code == 422
    assert harness.client.get("/api/ext/library/ids", params={"batch_id": 1, "group_id": 1},
                              headers=headers).status_code == 422


def test_thumbnail_of_image_and_video_never_streams_the_video(harness):
    _seed_library(harness)
    headers = connect(harness)
    rows = {(r["media_type"], r["role"]): dict(r) for r in harness.ctx.db.fetchall("SELECT * FROM media")}
    video = rows[("video", "original")]
    response = harness.client.get(f"/api/ext/media/{video['id']}/thumbnail", headers=headers)
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert not response.content.startswith(b"FAKE-video")
    assert harness.ctx.media.get(video["id"])["thumbnail_path"]   # gerada e gravada como a pipeline
    image = rows[("image", "cover")]
    response = harness.client.get(f"/api/ext/media/{image['id']}/thumbnail", headers=headers)
    assert response.status_code == 200 and response.headers["content-type"].startswith("image/")
    harness.processor.ffmpeg_found = False
    other = next(r for r in harness.ctx.db.fetchall("SELECT * FROM media WHERE media_type = 'video'")
                 if not r["thumbnail_path"])
    response = harness.client.get(f"/api/ext/media/{other['id']}/thumbnail", headers=headers)
    assert response.status_code == 404 and response.json()["code"] == "thumbnail_unavailable"
    assert harness.client.get("/api/ext/media/99999/thumbnail", headers=headers).status_code == 404


def test_edit_options_are_the_real_controls(harness):
    body = harness.client.get("/api/ext/edit/options", headers=connect(harness)).json()
    edit = body["edit"]
    assert [m["value"] for m in edit["duration_modes"]] == ["original", "15", "30", "60", "custom"]
    assert edit["custom_seconds_max"] == 7200 and edit["duration_margin"] == 0.03 and edit["output"] == "mp4"
    assert edit["speed"] == {"min": 1.0, "max": 1.5, "step": 0.01,
                             "presets": [1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.5]}
    assert body["presets"] and set(body["presets"][0]) == {"id", "name", "summary"}
    assert body["defaults"]["output_type"] == "mp4" and body["defaults"]["speed"] == 1.0


def test_edits_and_process_only_take_original_videos(harness):
    _seed_library(harness)
    headers = connect(harness)
    all_ids = [int(r["id"]) for r in harness.ctx.db.fetchall("SELECT id FROM media ORDER BY id")]
    videos = [int(r["id"]) for r in harness.ctx.db.fetchall(
        "SELECT id FROM media WHERE media_type = 'video' AND role = 'original' ORDER BY id")]
    settings = {"duration_mode": "custom", "custom_seconds": 12.5, "speed": 1.25, "sharpen": True}
    response = harness.client.post("/api/ext/edits", json={"media_ids": all_ids + [99999], "settings": settings},
                                   headers=headers)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["created"] == len(videos) == 2
    assert {s["reason"] for s in body["skipped"]} == {"cover", "not_video", "not_found"}
    assert len(body["skipped"]) == len(all_ids) - 2 + 1
    batch = harness.ctx.batches.get(body["batch_id"])
    assert batch["preset_name"] == "Edição" and batch["params"]["reencode"] is True
    only_photos = [i for i in all_ids if i not in videos]
    refused = harness.client.post("/api/ext/edits", json={"media_ids": only_photos, "settings": settings},
                                  headers=headers)
    assert refused.status_code == 422 and refused.json()["code"] == "edit_requires_video"
    for bad in ({"media_ids": videos, "settings": {**settings, "output_type": "auto"}},
                {"media_ids": videos, "settings": settings, "trims": []},
                {"media_ids": list(range(1, 502)), "settings": settings}):
        assert harness.client.post("/api/ext/edits", json=bad, headers=headers).status_code == 422
    preset_id = harness.ctx.presets.first_builtin_id()
    processed = harness.client.post("/api/ext/process", json={"media_ids": all_ids, "preset_id": preset_id},
                                    headers=headers)
    assert processed.status_code == 202 and processed.json()["created"] == 2
    assert harness.client.post("/api/ext/process", json={"media_ids": only_photos, "preset_id": preset_id},
                               headers=headers).status_code == 422


def test_exports_one_zip_per_request_with_capa(harness_factory, tmp_path):
    h = harness_factory()
    prepare(h, tmp_path)
    batch_id = _seed_library(h)
    headers = connect(h)
    covers = [int(r["id"]) for r in h.ctx.db.fetchall("SELECT id FROM media WHERE role = 'cover'")]
    created = h.client.post("/api/ext/exports", json={"source": {"kind": "library", "media_ids": covers},
                                                      "content": "originals"}, headers=headers)
    assert created.status_code == 202, created.text
    run_exports(h)
    status = h.client.get(f"/api/ext/exports/{created.json()['id']}", headers=headers).json()
    assert status["status"] == "completed" and status["zip_ready"] and status["total"] == 2
    assert status["download_url"].startswith("http://127.0.0.1:")
    with zipfile.ZipFile(stored_zip(h, created.json()["id"])) as archive:
        assert all(name.endswith("_capa.jpg") for name in assert_flat_and_unique(archive))
    by_batch = h.client.post("/api/ext/exports", json={"source": {"kind": "batch", "id": batch_id}},
                             headers=headers)
    assert by_batch.status_code == 202 and by_batch.json()["id"] != created.json()["id"]
    run_exports(h)
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM exports WHERE mode = 'zip'") == 2
    too_many = h.client.post("/api/ext/exports", json={"source": {"kind": "library",
                                                                  "media_ids": list(range(1, 5002))}},
                             headers=headers)
    assert too_many.status_code == 422
    assert h.client.get("/api/ext/exports/9999", headers=headers).status_code == 404


def test_responses_have_no_sensitive_fields(harness_factory, tmp_path):
    h = harness_factory()
    prepare(h, tmp_path)
    batch_id = _seed_library(h)
    headers = connect(h)
    bodies = [
        h.client.get("/api/ext/status", headers=headers).text,
        h.client.get("/api/ext/activity", headers=headers).text,
        h.client.get(f"/api/ext/batches/{batch_id}/items", headers=headers).text,
        h.client.get("/api/ext/library", headers=headers).text,
        h.client.get("/api/ext/edit/options", headers=headers).text,
        h.client.post("/api/ext/page-info", json={"url": link(CODE)}, headers=headers).text,
    ]
    for text in bodies:
        lowered = text.lower()
        for word in ("sessionid", "original_path", "thumbnail_path", "media_url", "cdninstagram", "storage",
                     "\\\\", "cookie", "secret"):
            assert word not in lowered, (word, text[:200])
    status = h.client.get("/api/ext/status", headers=headers).json()
    assert "library" in status["features"] and status["defaults"]["limit"] == 50


def test_app_start_accepts_the_new_media_and_auto_export(harness):
    body = start(harness, inputs=[link(CODE)], media="videos_covers", auto_export={"content": "edited"},
                 post_processing="edit", edit=EDIT)
    batch = harness.batch(body["links_batch_id"])
    assert batch["media"] == "videos_covers"
    assert batch["auto_export"] == {"content": "edited", "status": "waiting", "export_id": None,
                                    "error_message": None, "zip_ready": False, "download_url": None,
                                    "request_id": None}
    assert start(harness, expect=422, inputs=[link(CODE)], auto_export={"content": "tudo"})["code"]
