"""Importação por link: validação da lista colada, prévia, criação do lote, API dos links e campos novos do lote
(docs/CONTRACTS.md, seção 8.3). 100% offline, valores FAKE."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import apply_migrations, schema_version
from app.db.repositories.batches import BatchRepository
from app.services.import_service import MAX_BATCH_LINKS, parse_link_entries
from import_links_fakes import create_links, link, link_rows, links_app, photo
from media_fakes import MemorySessionProvider, remote

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "app" / "db" / "migrations"
CODE = "ABCDE12345"
P_URL, REEL_URL = link(CODE), link(CODE, "reel")


def preview(h, **body) -> dict:
    response = h.client.post("/api/import-links/preview", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- lista de links


@pytest.mark.parametrize("entry, url", [
    (f"https://www.instagram.com/p/{CODE}/", P_URL),
    (f"https://www.instagram.com/p/{CODE}", P_URL),
    (f"http://instagram.com/p/{CODE}/", P_URL),
    (f"instagram.com/p/{CODE}/", P_URL),
    (f"www.instagram.com/reel/{CODE}/?igsh=FAKE123#topo", REEL_URL),
    (f"https://instagram.com/reel/{CODE}/", REEL_URL),
    (f"https://www.instagram.com/reels/{CODE}/", REEL_URL),
    (f"https://www.instagram.com/usuario_teste/p/{CODE}/", P_URL),
    (f"https://www.instagram.com/usuario_teste/reel/{CODE}/?utm_source=ig_web", REEL_URL),
    (f"  https://www.instagram.com/p/{CODE}/  ", P_URL),
])
def test_link_forms_accepted(entry, url):
    assert parse_link_entries([entry]) == ([{"shortcode": CODE, "url": url}], [], 0)


@pytest.mark.parametrize("entry", [
    CODE, f"/p/{CODE}/", "https://www.instagram.com/usuario_teste/", "https://www.instagram.com/",
    f"https://example.com/p/{CODE}/", f"https://instagram.com.example.com/p/{CODE}/",
    "https://www.instagram.com/p/", "https://www.instagram.com/p/abc/", f"https://www.instagram.com/tv/{CODE}/",
    "https://www.instagram.com/stories/usuario_teste/123/", f"ftp://instagram.com/p/{CODE}/",
    f"https://www.instagram.com/p/{CODE}/extra/", f"https://www.instagram.com/explore/p/{CODE}/",
    "https://www.instagram.com/p/c%C3%B3digo/", "@usuario_teste",
])
def test_link_forms_rejected(entry):
    valid, invalid, duplicates = parse_link_entries([entry])
    assert valid == [] and duplicates == 0, entry
    assert len(invalid) == 1 and invalid[0]["entry"] == entry and invalid[0]["reason"]


def test_preview_splits_dedupes_and_reports_invalid_without_side_effects(harness_factory):
    h, dl = links_app(harness_factory, {})
    text = (f"https://www.instagram.com/p/AAAAA11111/\r\n\r\n   \n  instagram.com/reel/BBBBB22222/ , "
            f"https://www.instagram.com/p/CCCCC33333/;https://www.instagram.com/p/DDDDD44444/\n"
            f"https://www.instagram.com/p/EEEEE55555/ https://www.instagram.com/reels/FFFFF66666/\t"
            f"https://www.instagram.com/reel/AAAAA11111/\n"          # mesmo shortcode em /reel/: repetido
            f"https://www.instagram.com/p/BBBBB22222/?x=1\n"          # mesmo shortcode em /p/: repetido
            f"nao-e-link\nhttps://www.instagram.com/usuario_teste/\nnao-e-link\n")
    body = preview(h, text=text)
    assert body["valid"] == [
        {"shortcode": "AAAAA11111", "url": link("AAAAA11111")},
        {"shortcode": "BBBBB22222", "url": link("BBBBB22222", "reel")},
        {"shortcode": "CCCCC33333", "url": link("CCCCC33333")},
        {"shortcode": "DDDDD44444", "url": link("DDDDD44444")},
        {"shortcode": "EEEEE55555", "url": link("EEEEE55555")},
        {"shortcode": "FFFFF66666", "url": link("FFFFF66666", "reel")},
    ]
    assert [entry["entry"] for entry in body["invalid"]] == ["nao-e-link", "https://www.instagram.com/usuario_teste/"]
    assert all(entry["reason"] for entry in body["invalid"])
    assert body["duplicates"] == 3, "dois shortcodes repetidos e um inválido repetido"
    # `entries` e `text` juntos; sem efeitos: nada criado e nenhuma chamada ao provider.
    both = preview(h, entries=[link("AAAAA11111"), ""], text=link("AAAAA11111", "reel"))
    assert [v["shortcode"] for v in both["valid"]] == ["AAAAA11111"] and both["duplicates"] == 1
    assert preview(h) == {"valid": [], "invalid": [], "duplicates": 0}
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0 and dl.calls == []


def test_preview_and_create_enforce_the_sanity_cap_and_truncate_the_echo(harness_factory):
    h, _ = links_app(harness_factory, {})
    many = [link(f"CODE{i:05d}") for i in range(MAX_BATCH_LINKS + 1)]
    assert len(preview(h, entries=many[:MAX_BATCH_LINKS])["valid"]) == MAX_BATCH_LINKS == 500
    for path, body in (("/api/import-links/preview", {"text": "\n".join(many)}),
                       ("/api/import-links", {"links": many})):
        response = h.client.post(path, json=body)
        assert response.status_code == 422 and response.json()["code"] == "too_many_links", path
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    echoed = preview(h, entries=["x" * 300])["invalid"][0]["entry"]
    assert echoed == "x" * 200


# --------------------------------------------------------------------------- criação


def test_create_makes_one_listing_batch_with_the_links_in_pasted_order(harness_factory):
    h, dl = links_app(harness_factory, {})
    created = create_links(h, [link("BBBBB22222", "reel"), f"instagram.com/p/AAAAA11111/?x=1\n{link('CCCCC33333')}",
                               link("BBBBB22222")])
    assert (created["source"], created["import_mode"], created["status"], created["source_profile"],
            created["content_type"], created["requested_limit"], created["process"], created["preset_id"]) == (
        "instagram_import", "links", "listing", None, "all", None, False, None)
    assert created["links"] == {"total": 3, "pending": 3, "resolved": 0, "failed": 0}
    assert created["duplicates"] == 1 and created["found"] == 0
    assert (created["group_id"], created["group_state"], created["listing_end"]) == (None, None, None)
    assert link_rows(h, created["id"]) == [
        {"position": 1, "url": link("BBBBB22222", "reel"), "shortcode": "BBBBB22222", "status": "pending",
         "error_code": None, "error_message": None, "items_found": None, "profile": None},
        {"position": 2, "url": link("AAAAA11111"), "shortcode": "AAAAA11111", "status": "pending",
         "error_code": None, "error_message": None, "items_found": None, "profile": None},
        {"position": 3, "url": link("CCCCC33333"), "shortcode": "CCCCC33333", "status": "pending",
         "error_code": None, "error_message": None, "items_found": None, "profile": None},
    ]
    assert dl.calls == [], "a criação não fala com o Instagram"
    # O lote aparece no histórico e no detalhe; `duplicates` só existe na resposta da criação.
    listed = h.client.get("/api/batches").json()
    assert listed["total"] == 1 and listed["items"][0]["import_mode"] == "links"
    assert listed["items"][0]["links"]["total"] == 3 and "duplicates" not in listed["items"][0]
    assert "duplicates" not in h.batch(created["id"])


def test_create_rejects_invalid_entries_with_the_list_and_creates_nothing(harness_factory):
    h, _ = links_app(harness_factory, {})
    body = create_links(h, [link("AAAAA11111"), "nao-e-link", link("AAAAA11111", "reel"), "outro lixo"], expect=422)
    assert body["code"] == "invalid_links" and body["detail"]
    assert [entry["entry"] for entry in body["invalid"]] == ["nao-e-link", "outro", "lixo"]
    assert body["valid"] == [{"shortcode": "AAAAA11111", "url": link("AAAAA11111")}] and body["duplicates"] == 1
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batch_links") == 0
    created = create_links(h, [entry["url"] for entry in body["valid"]])
    assert created["links"]["total"] == 1


def test_create_errors_and_their_order(harness_factory):
    no_session, _ = links_app(harness_factory, {}, session_provider=MemorySessionProvider(configured=False))
    good = [link("AAAAA11111")]
    # Lista inválida ou vazia vem antes da sessão; sessão antes do preset.
    assert create_links(no_session, ["nao-e-link"], expect=422)["code"] == "invalid_links"
    assert create_links(no_session, ["  ", "\n"], expect=422)["code"] == "no_links"
    missing = create_links(no_session, good, expect=409, process=True, preset_id=999)
    assert missing["code"] == "session_missing" and "sessionid" in missing["detail"]

    h, dl = links_app(harness_factory, {})
    assert create_links(h, good, expect=404, process=True, preset_id=999)["code"] == "preset_not_found"
    for bad in ({"links": []}, {"links": good, "extra": True}, {"links": good, "process": "sim"},
                {"links": good, "preset_id": 0}, {"links": "texto"}, {}):
        assert h.client.post("/api/import-links", json=bad).status_code == 422, bad
    assert h.client.post("/api/import-links", json={"links": good},
                         headers={"X-Media-Tool-Client": ""}).status_code == 403
    # Instagram pausado: 409 antes de conferir o preset; nada criado.
    assert h.ctx.worker.pause_for("rate_limited") is True
    assert create_links(h, good, expect=409, process=True, preset_id=999)["code"] == "instagram_paused"
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM batches") == 0 and dl.calls == []


def test_create_with_processing_snapshots_the_preset_and_has_no_provider_restriction(harness_factory):
    h, _ = links_app(harness_factory, {"AAAAA11111": photo("AAAAA11111")})
    assert h.client.put("/api/settings", json={"instagram_provider": "gallery_dl"}).status_code == 200
    presets = h.client.get("/api/presets").json()
    created = create_links(h, [link("AAAAA11111")], process=True, preset_id=presets[1]["id"])
    assert created["process"] is True and created["preset_name"] == presets[1]["name"]
    h.run_all()
    done = h.batch(created["id"])
    assert done["status"] == "completed" and done["progress"]["processed"] == 1
    assert h.ctx.jobs.for_batch(created["id"])[0]["params"] == presets[1]["params"]


# --------------------------------------------------------------------------- API dos links e campos do lote


def test_links_endpoint_paginates_and_other_batches_have_no_links(harness_factory):
    h, _ = links_app(harness_factory, {})
    created = create_links(h, [link(f"CODE{i:05d}") for i in range(5)])
    page = h.client.get(f"/api/batches/{created['id']}/links", params={"limit": 2, "offset": 3}).json()
    assert page["total"] == 5 and [item["position"] for item in page["items"]] == [4, 5]
    assert len(link_rows(h, created["id"])) == 5
    for params in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        assert h.client.get(f"/api/batches/{created['id']}/links", params=params).status_code == 422
    missing = h.client.get("/api/batches/999/links")
    assert missing.status_code == 404 and missing.json()["code"] == "batch_not_found"

    # Lote por perfil: `import_mode` "profile", sem `links`, lista de links vazia.
    h.downloader.items = [remote("PERFIL1")]
    profile_batch = h.import_profile()
    assert (profile_batch["import_mode"], profile_batch["links"]) == ("profile", None)
    assert h.client.get(f"/api/batches/{profile_batch['id']}/links").json() == {"items": [], "total": 0}
    # Lote de Biblioteca: `import_mode` null.
    h.ctx.db.execute("UPDATE batches SET status = 'cancelled' WHERE id = ?", (created["id"],))
    h.run_all()
    media_id = h.client.get("/api/media").json()["items"][0]["id"]
    preset_id = h.client.get("/api/presets").json()[0]["id"]
    library = h.client.post("/api/media/process", json={"media_ids": [media_id], "preset_id": preset_id}).json()
    assert (library["source"], library["import_mode"], library["links"]) == ("library", None, None)
    by_id = {item["id"]: item for item in h.client.get("/api/batches").json()["items"]}
    assert [by_id[i]["import_mode"] for i in (created["id"], profile_batch["id"], library["id"])] == [
        "links", "profile", None]


def test_restart_listing_is_refused_for_link_batches(harness_factory):
    h, _ = links_app(harness_factory, {})
    created = create_links(h, [link("AAAAA11111")])
    h.ctx.batches.update_listing(created["id"], listing_state="needs_attention")
    for body in ({"confirm": True}, {}):
        response = h.client.post(f"/api/batches/{created['id']}/restart-listing", json=body)
        assert response.status_code == 409 and response.json()["code"] == "listing_not_resumable"
        assert "Retomar" in response.json()["detail"]
    assert h.client.post(f"/api/batches/{created['id']}/resume-listing").json()["listing_state"] == "pending"


# --------------------------------------------------------------------------- migration


def test_migration_0014_over_schema_0013_with_data_keeps_old_batches(tmp_path, harness_factory):
    root = tmp_path / "antigo"
    old = tmp_path / "migrations_0013"
    old.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0014":
            shutil.copy(path, old)
    db = Database(root / "data" / "media-tool.db")
    try:
        apply_migrations(db, old)
        assert schema_version(db) == 13
        batch_id = BatchRepository(db).create(source="instagram_import", status="listing",
                                              source_profile="perfil_antigo", content_type="posts", requested_limit=50)
        db.execute("UPDATE batches SET status = 'completed', listing_state = 'done', found = 7 WHERE id = ?",
                   (batch_id,))
        before = dict(db.fetchone("SELECT * FROM batches WHERE id = ?", (batch_id,)))
        assert apply_migrations(db) == [14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30]   # 0015 só mexe no preset; 0016-0020 só colunas anuláveis
        after = dict(db.fetchone("SELECT * FROM batches WHERE id = ?", (batch_id,)))
        assert {k: after[k] for k in before} == before, "nenhum dado antigo reescrito"
        assert after["import_mode"] is None and db.scalar("SELECT COUNT(*) FROM batch_links") == 0
    finally:
        db.close_all()

    h = harness_factory(root=root)   # o app abre o banco migrado e lê o lote antigo igual
    batch = h.client.get("/api/batches").json()["items"][0]
    assert (batch["id"], batch["source_profile"], batch["found"], batch["status"]) == (
        batch_id, "perfil_antigo", 7, "completed")
    assert (batch["import_mode"], batch["links"]) == ("profile", None)
    with pytest.raises(sqlite3.IntegrityError):
        h.ctx.db.execute("UPDATE batches SET import_mode = 'outro' WHERE id = ?", (batch_id,))
