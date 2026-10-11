"""Exportação para pasta pela API: lote, grupo, seleção da Biblioteca, colisões, destino, cancelamento e segurança.

Destino sempre em diretório temporário do pytest; o seletor nativo é sempre falso (`ctx.folder_picker`); sem rede.
"""

from __future__ import annotations

import errno
import os
import threading

import pytest
from fastapi.testclient import TestClient

from app.jobs import export_runner
from app.services import export_destination, export_service
from app.services import system_open
from conftest import BASE_URL, wait_until
from export_helpers import (
    export_to,
    files_in,
    original_of,
    pick,
    prepare,
    run_exports,
    seed_batch,
    seed_media,
    sha256_of,
    start_export,
    storage_snapshot,
)
from import_group_fakes import ProfilesDownloader
from media_fakes import MAGIC, remote

# Guardada na importação: o conftest troca `system_open.open_folder` por uma função que falha em todo teste.
REAL_OPEN_FOLDER = system_open.open_folder


def media_rows(harness) -> dict[str, dict]:
    return {f"{row['source_post_id']}:{row['source_media_index']}": dict(row)
            for row in harness.ctx.db.fetchall("SELECT * FROM media")}


def assert_same_content(harness, folder, media: dict, name: str) -> None:
    assert sha256_of(folder / name) == media["original_sha256"]
    assert (folder / name).read_bytes() == harness.ctx.storage.resolve(media["original_path"]).read_bytes()


# --------------------------------------------------------------------------- lote


def test_batch_export_is_flat_complete_and_byte_identical(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    harness.downloader.items = [remote("IMG", profile="alpha"), remote("VID", media_type="video", profile="alpha"),
                                remote("CAR", index=0, profile="alpha"), remote("CAR", index=1, profile="alpha")]
    batch = harness.import_profile(profile="alpha")
    harness.run_all()
    before = storage_snapshot(harness)

    export = export_to(harness, {"kind": "batch", "id": batch["id"]}, dest)

    assert export["status"] == "completed" and export["kind"] == "batch" and export["batch_id"] == batch["id"]
    assert (export["total"], export["copied"], export["processed"]) == (4, 4, 4)
    assert (export["skipped_existing"], export["renamed"], export["missing"], export["failed"]) == (0, 0, 0, 0)
    assert export["dest_path"] == str(dest.resolve()) and export["finished_at"]
    # Plana (sem subpasta), extensões do original, nomes amigáveis e carrossel numerado a partir de 1.
    assert files_in(dest) == ["alpha_2026-01-01_CAR_1.jpg", "alpha_2026-01-01_CAR_2.jpg", "alpha_2026-01-01_IMG.jpg",
                              "alpha_2026-01-01_VID.mp4"]
    assert all(path.is_file() and not path.is_symlink() for path in dest.iterdir())
    rows = media_rows(harness)
    assert_same_content(harness, dest, rows["IMG:0"], "alpha_2026-01-01_IMG.jpg")
    assert_same_content(harness, dest, rows["VID:0"], "alpha_2026-01-01_VID.mp4")
    assert_same_content(harness, dest, rows["CAR:0"], "alpha_2026-01-01_CAR_1.jpg")
    assert_same_content(harness, dest, rows["CAR:1"], "alpha_2026-01-01_CAR_2.jpg")
    # Cópia real (nenhum hardlink) e Biblioteca intacta: nada movido, alterado ou criado no storage.
    for name in files_in(dest):
        assert os.stat(dest / name).st_nlink == 1
    assert storage_snapshot(harness) == before

    shown = harness.batch(batch["id"])["last_export"]
    assert shown["id"] == export["id"] and shown["dest_path"] == str(dest.resolve())
    assert shown["status"] == "completed" and shown["copied"] == 4
    listed = harness.client.get("/api/exports", params={"batch_id": batch["id"]}).json()["items"]
    assert [item["id"] for item in listed] == [export["id"]]


def test_deleting_the_export_folder_does_not_touch_the_library(harness, tmp_path):
    import shutil

    dest = prepare(harness, tmp_path)
    harness.downloader.items = [remote("A1"), remote("A2")]
    batch = harness.import_profile()
    harness.run_all()
    before = storage_snapshot(harness)
    export_to(harness, {"kind": "batch", "id": batch["id"]}, dest)
    shutil.rmtree(dest)
    assert storage_snapshot(harness) == before
    assert harness.client.get("/api/media").json()["total"] == 2


def test_batch_export_includes_media_that_already_existed(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    same_bytes = {"B3": "A2"}   # B3 tem o MESMO conteúdo de A2 (duplicata por sha256)
    harness.downloader.content = lambda item: MAGIC["image"] + (
        f"FAKE-image-{same_bytes.get(item.source_post_id, item.source_post_id)}".encode())
    harness.downloader.items = [remote("A1"), remote("A2")]
    harness.import_profile()
    harness.run_all()
    harness.downloader.items = [remote("A1"), remote("B3"), remote("C4")]
    second = harness.import_profile()
    harness.run_all()
    jobs = {job["source_post_id"]: job for job in harness.batch(second["id"])["jobs"]}
    assert jobs["A1"]["duplicate_reason"] == "source_id" and jobs["B3"]["duplicate_reason"] == "sha256"
    assert harness.client.get("/api/media").json()["total"] == 3   # nada duplicado na Biblioteca
    before = storage_snapshot(harness)

    export = export_to(harness, {"kind": "batch", "id": second["id"]}, dest)

    # Resultado COMPLETO do lote: a mídia que já existia (A1), a existente apontada pela duplicata de conteúdo (A2)
    # e a nova (C4); cada mídia uma vez.
    assert (export["status"], export["total"], export["copied"]) == ("completed", 3, 3)
    assert files_in(dest) == ["perfil_teste_2026-01-01_A1.jpg", "perfil_teste_2026-01-01_A2.jpg",
                              "perfil_teste_2026-01-01_C4.jpg"]
    assert storage_snapshot(harness) == before


def test_export_of_unknown_or_empty_source(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    missing = start_export(harness, {"kind": "batch", "id": 999}, dest, expect=404)
    assert missing["code"] == "batch_not_found"
    assert start_export(harness, {"kind": "group", "id": 999}, dest, expect=404)["code"] == "group_not_found"
    harness.downloader.items = [remote("A1")]
    batch = harness.import_profile()   # ainda listando: nenhum job concluído
    empty = start_export(harness, {"kind": "batch", "id": batch["id"]}, dest, expect=409)
    assert empty["code"] == "export_empty"
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM exports") == 0 and files_in(dest) == []
    assert harness.client.get("/api/exports/999").status_code == 404


# --------------------------------------------------------------------------- grupo


def run_group(harness, group_id: int) -> dict:
    for _ in range(50):
        harness.run_all()
        detail = harness.client.get(f"/api/import-groups/{group_id}").json()
        if detail["group"]["status"] != "active":
            return detail
    raise AssertionError("grupo não terminou")


def test_group_export_puts_every_profile_in_one_folder_once(harness_factory, tmp_path):
    downloader = ProfilesDownloader(profiles={
        "um": [remote("U1", profile="um"), remote("U2", profile="um"), remote("COMUM", profile="um")],
        "dois": [remote("D1", profile="dois"), remote("COMUM", profile="um")],
        "tres": [remote("T1", profile="tres", media_type="video")],
    })
    harness = harness_factory(downloader=downloader)
    dest = prepare(harness, tmp_path)
    created = harness.client.post("/api/import-groups", json={
        "profiles": ["um", "dois", "tres"], "content_type": "all", "limit": 10, "preset_id": None, "process": False})
    assert created.status_code == 202, created.text
    group_id = created.json()["group"]["id"]
    detail = run_group(harness, group_id)
    assert detail["group"]["status"] == "completed" and detail["group"]["last_export"] is None
    assert harness.client.get("/api/media").json()["total"] == 5
    before = storage_snapshot(harness)

    export = export_to(harness, {"kind": "group", "id": group_id}, dest)

    assert (export["status"], export["kind"], export["group_id"]) == ("completed", "group", group_id)
    assert (export["total"], export["copied"]) == (5, 5)   # COMUM (compartilhada por dois perfis) entra uma vez
    names = files_in(dest)
    assert len(names) == 5 and all((dest / name).is_file() for name in names)   # tudo junto, sem pasta por perfil
    assert sum("COMUM" in name for name in names) == 1
    assert {name.split("_")[0] for name in names} == {"um", "dois", "tres"}
    assert storage_snapshot(harness) == before
    detail = harness.client.get(f"/api/import-groups/{group_id}").json()
    assert detail["group"]["last_export"]["id"] == export["id"]
    assert all(child["last_export"] is None for child in detail["children"])
    listed = harness.client.get("/api/import-groups").json()["items"][0]
    assert listed["last_export"]["dest_path"] == str(dest.resolve())

    # Um filho também pode ser exportado sozinho (lote avulso dentro do grupo).
    child = next(c for c in detail["children"] if c["source_profile"] == "dois")
    other = tmp_path / "so-dois"
    other.mkdir()
    alone = export_to(harness, {"kind": "batch", "id": child["id"]}, other)
    assert alone["total"] == 2 and len(files_in(other)) == 2


# --------------------------------------------------------------------------- Biblioteca


@pytest.mark.parametrize("count", [50, pytest.param(5000, marks=pytest.mark.slow)])
def test_library_export_by_ids(harness, tmp_path, count):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, count)
    seed_media(harness, 3, profile="fora", start=90_000)
    before_rows = harness.ctx.db.scalar("SELECT COUNT(*) FROM media")
    export = export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    assert (export["status"], export["kind"], export["total"], export["copied"]) == ("completed", "library", count,
                                                                                    count)
    names = files_in(dest)
    assert len(names) == count and all(name.startswith("semente_2026-02-03_S") for name in names)
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM export_items WHERE export_id = ?", (export["id"],)) == count
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM media") == before_rows
    sample = harness.ctx.media.get(ids[count // 2])
    assert_same_content(harness, dest, sample, f"semente_2026-02-03_{sample['source_post_id']}.jpg")


def test_library_export_ignores_unknown_and_repeated_ids(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 3)
    export = export_to(harness, {"kind": "library", "selection": {"media_ids": [*ids, ids[0], 999_999]}}, dest)
    assert (export["total"], export["copied"]) == (3, 3)
    refused = start_export(harness, {"kind": "library", "selection": {"media_ids": [999_999]}}, dest, expect=409)
    assert refused["code"] == "export_empty"


def test_library_export_by_filter_with_exclusions(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    alpha = seed_media(harness, 30, profile="alpha")
    seed_media(harness, 20, profile="beta", start=1000)
    excluded = alpha[:4]
    created = start_export(harness, {"kind": "library", "selection": {
        "filter": {"view": "all", "media_type": "image", "q": "alpha"}, "exclude_ids": excluded}}, dest)
    assert created["status"] == "pending" and created["total"] == 26
    late = seed_media(harness, 5, profile="alpha", start=500)   # chegou depois do pedido: fica de fora
    run_exports(harness)
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["total"], export["copied"]) == ("completed", 26, 26)
    names = files_in(dest)
    assert len(names) == 26 and all(name.startswith("alpha_") for name in names)
    gone = {f"alpha_2026-02-03_{harness.ctx.media.get(i)['source_post_id']}.jpg" for i in [*excluded, *late]}
    assert not gone & set(names)
    # Os ids escolhidos não foram materializados: só as exclusões ficam guardadas.
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM export_items WHERE export_id = ?", (export["id"],)) == 4

    everything = tmp_path / "tudo"
    everything.mkdir()
    full = export_to(harness, {"kind": "library", "selection": {"filter": {"view": "all"}}}, everything)
    assert full["total"] == 55 and len(files_in(everything)) == 55
    by_profile = tmp_path / "beta"
    by_profile.mkdir()
    beta = export_to(harness, {"kind": "library", "selection": {"filter": {"profile": "@beta"}}}, by_profile)
    assert beta["total"] == 20


def test_library_selection_is_validated(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    seed_media(harness, 1)
    token = pick(harness, dest)["token"]
    for selection in ({}, {"media_ids": []}, {"media_ids": [0]}, {"media_ids": [1], "filter": {}},
                      {"filter": {"view": "x"}}, {"filter": {}, "exclude_ids": ["a"]}, {"media_ids": [1], "path": "C:\\"}):
        response = harness.client.post("/api/exports", json={
            "source": {"kind": "library", "selection": selection}, "destination": {"token": token}})
        assert response.status_code == 422, selection
    # Nenhuma tentativa inválida consumiu o token.
    ok = harness.client.post("/api/exports", json={
        "source": {"kind": "library", "selection": {"media_ids": [1]}}, "destination": {"token": token}})
    assert ok.status_code == 202


# --------------------------------------------------------------------------- nomes e colisões


def test_reexport_to_the_same_folder_is_idempotent(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 12)
    source = {"kind": "library", "selection": {"media_ids": ids}}
    first = export_to(harness, source, dest)
    names = files_in(dest)
    stamps = {name: (dest / name).stat().st_mtime_ns for name in names}
    second = export_to(harness, source, dest)
    assert (first["copied"], first["skipped_existing"]) == (12, 0)
    assert (second["status"], second["copied"], second["skipped_existing"], second["renamed"]) == ("completed", 0,
                                                                                                   12, 0)
    assert second["processed"] == second["total"] == 12
    assert files_in(dest) == names   # nada duplicado
    assert {name: (dest / name).stat().st_mtime_ns for name in names} == stamps   # nada regravado


def test_same_name_with_different_content_is_renamed_never_overwritten(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    taken = dest / "semente_2026-02-03_S00000.jpg"
    taken.write_bytes(b"arquivo do usuario, outro conteudo")
    export = export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    assert (export["status"], export["copied"], export["renamed"], export["skipped_existing"]) == ("completed", 2, 1,
                                                                                                   0)
    assert taken.read_bytes() == b"arquivo do usuario, outro conteudo"   # o pré-existente ficou intacto
    assert files_in(dest) == ["semente_2026-02-03_S00000 (2).jpg", "semente_2026-02-03_S00000.jpg",
                              "semente_2026-02-03_S00001.jpg"]
    assert_same_content(harness, dest, harness.ctx.media.get(ids[0]), "semente_2026-02-03_S00000 (2).jpg")
    # Reexportar: o renomeado é reconhecido como idêntico (nada de " (3)").
    again = export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    assert (again["copied"], again["renamed"], again["skipped_existing"]) == (0, 0, 2)
    assert len(files_in(dest)) == 3 and taken.read_bytes() == b"arquivo do usuario, outro conteudo"


def test_same_size_different_content_is_not_treated_as_identical(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 1)
    original = original_of(harness, ids[0]).read_bytes()
    taken = dest / "semente_2026-02-03_S00000.jpg"
    taken.write_bytes(bytes(reversed(original)))   # mesmo tamanho, outro conteúdo
    export = export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    assert (export["copied"], export["renamed"], export["skipped_existing"]) == (1, 1, 0)
    assert taken.read_bytes() == bytes(reversed(original))


@pytest.mark.skipif(os.name != "nt", reason="sistema de arquivos sem diferença de caixa (Windows)")
def test_names_differing_only_by_case_collide(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 1)
    taken = dest / "SEMENTE_2026-02-03_S00000.JPG"
    taken.write_bytes(b"outro arquivo com o mesmo nome em outra caixa")
    export = export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    assert (export["copied"], export["renamed"]) == (1, 1)
    assert taken.read_bytes() == b"outro arquivo com o mesmo nome em outra caixa"
    assert files_in(dest) == ["SEMENTE_2026-02-03_S00000.JPG", "semente_2026-02-03_S00000 (2).jpg"]


def test_two_media_with_the_same_friendly_name_get_distinct_files(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    # Sem perfil, data nem código, o nome vem do SHA; aqui dois perfis/códigos longos caem no mesmo nome cortado.
    first = seed_media(harness, 1, profile="p" * 150, code="AAA")
    second = seed_media(harness, 1, profile="p" * 150, code="BBB", start=1)
    export = export_to(harness, {"kind": "library", "selection": {"media_ids": [*first, *second]}}, dest)
    assert (export["copied"], export["renamed"]) == (2, 1)
    names = files_in(dest)
    assert len(names) == 2 and all(len(name) <= 120 for name in names)
    assert {sha256_of(dest / name) for name in names} == {
        harness.ctx.media.get(first[0])["original_sha256"], harness.ctx.media.get(second[0])["original_sha256"]}
    again = export_to(harness, {"kind": "library", "selection": {"media_ids": [*first, *second]}}, dest)
    assert (again["copied"], again["skipped_existing"]) == (0, 2) and files_in(dest) == names


def test_hostile_profile_and_code_values_produce_valid_windows_names(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = [
        *seed_media(harness, 1, profile='ru<>:"/\\|?*im', code="a\x01b\tc"),
        *seed_media(harness, 1, profile="..\\..\\fora", code="../../x", start=1),
        *seed_media(harness, 1, profile="CON", code=None, taken_at=None, start=2),
        *seed_media(harness, 1, profile="cafe\u0301", code="CÓDIGO-日本", start=3),
        *seed_media(harness, 1, profile="x" * 300, code="y" * 300, start=4),
        *seed_media(harness, 1, profile=None, code=None, taken_at=None, start=5),
        *seed_media(harness, 1, profile="ponto.", code="fim. ", start=6, ext=".mp4"),
    ]
    before = storage_snapshot(harness)
    export = export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    assert (export["status"], export["copied"], export["failed"]) == ("completed", 7, 0)
    names = files_in(dest)
    assert len(names) == 7
    assert [path for path in dest.rglob("*") if path.is_dir()] == []          # nada de subpasta
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted({"app0", "Documentos", "saida"})  # nada escapou
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(10)} | {f"LPT{i}" for i in range(10)}
    for name in names:
        assert not set(name) & set('<>:"/\\|?*') and all(ord(char) >= 32 for char in name)
        assert len(name) <= 120 and not name.endswith((".", " "))
        assert name.split(".", 1)[0].upper() not in reserved
    assert "café_2026-02-03_CÓDIGO-日本.jpg" in names
    assert "ponto_2026-02-03_fim.mp4" in names
    assert storage_snapshot(harness) == before


# --------------------------------------------------------------------------- destino e token


def test_pick_folder_returns_token_and_cancel(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    seen = []

    def picker(initial):
        seen.append(initial)
        return dest

    harness.ctx.folder_picker = picker
    picked = harness.client.post("/api/exports/pick-folder").json()
    assert picked["cancelled"] is False and picked["path_display"] == str(dest.resolve()) and len(picked["token"]) >= 32
    # Diretório inicial: a pasta padrão de exportações em Documentos, criada sob demanda.
    assert seen == [tmp_path / "Documentos" / "Media Tool" / "Exports"] and seen[0].is_dir()

    harness.ctx.folder_picker = lambda initial: None
    assert harness.client.post("/api/exports/pick-folder").json() == {
        "cancelled": True, "token": None, "path_display": None}

    # Depois de uma exportação, o diálogo abre na última pasta usada.
    ids = seed_media(harness, 1)
    export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    harness.ctx.folder_picker = picker
    harness.client.post("/api/exports/pick-folder")
    assert seen[-1] == dest.resolve()


def test_pick_folder_refuses_forbidden_choices(harness, tmp_path):
    prepare(harness, tmp_path)
    storage = harness.ctx.storage
    cases = {
        storage.originals: "export_dest_forbidden", storage.root: "export_dest_forbidden",
        harness.settings.data_dir: "export_dest_forbidden", harness.settings.root: "export_dest_forbidden",
        tmp_path: "export_dest_forbidden", tmp_path / "nao-existe": "export_dest_missing",
        type(tmp_path)(tmp_path.anchor): "export_dest_drive_root",
    }
    for folder, code in cases.items():
        refused = pick(harness, folder, expect=409 if code == "export_dest_missing" else 422)
        assert refused["code"] == code and "token" not in refused, folder


def test_token_is_single_use_expires_and_unknown_token_is_refused(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 1)
    source = {"kind": "library", "selection": {"media_ids": ids}}
    token = pick(harness, dest)["token"]

    def post(value):
        return harness.client.post("/api/exports", json={"source": source, "destination": {"token": value}})

    assert post(token).status_code == 202
    run_exports(harness)
    reused = post(token)
    assert reused.status_code == 409 and reused.json()["code"] == "export_token_invalid"
    unknown = post("x" * 43)
    assert unknown.status_code == 409 and unknown.json()["code"] == "export_token_invalid"

    now = [0.0]
    harness.ctx.export_tokens.clock = lambda: now[0]
    expiring = pick(harness, dest)["token"]
    now[0] += 601
    expired = post(expiring)
    assert expired.status_code == 409 and expired.json()["code"] == "export_token_invalid"
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM exports") == 1


def test_routes_never_accept_a_path_from_the_client(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 1)
    source = {"kind": "library", "selection": {"media_ids": ids}}
    evil = str(harness.ctx.storage.originals)
    token = pick(harness, dest)["token"]
    for body in (
        {"source": source, "destination": {"path": evil}},
        {"source": source, "destination": {"token": token, "path": evil}},
        {"source": source, "destination": {"default": True, "path": evil}},
        {"source": source, "destination": {"token": token}, "path": evil},
        {"source": source, "destination": {"token": token}, "dest_path": evil},
        {"source": {**source, "path": evil}, "destination": {"token": token}},
        {"source": source, "destination": evil},
        {"source": source, "destination": {"token": evil}},   # caminho no lugar do token: não é um token emitido
    ):
        response = harness.client.post("/api/exports", json=body)
        assert response.status_code in (409, 422), body
        assert evil not in response.text
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM exports") == 0

    # pick-folder ignora qualquer corpo: o caminho vem só do diálogo (falso aqui).
    harness.ctx.folder_picker = lambda initial: dest
    picked = harness.client.post("/api/exports/pick-folder", json={"path": evil, "initial": evil})
    assert picked.status_code == 200 and picked.json()["path_display"] == str(dest.resolve())


def test_destination_is_revalidated_when_the_copy_starts(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    created = start_export(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    dest.rmdir()   # a pasta sumiu entre a escolha e o início da cópia
    run_exports(harness)
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["error_code"], export["copied"]) == ("failed", "export_dest_missing", 0)
    assert export["error_message"]


def test_default_folder_is_built_by_the_server(harness, tmp_path):
    prepare(harness, tmp_path)
    harness.downloader.items = [remote("A1"), remote("A2")]
    batch = harness.import_profile()
    harness.run_all()
    response = harness.client.post("/api/exports", json={
        "source": {"kind": "batch", "id": batch["id"]}, "destination": {"default": True}})
    assert response.status_code == 202, response.text
    run_exports(harness)
    export = harness.client.get(f"/api/exports/{response.json()['id']}").json()
    folder = type(tmp_path)(export["dest_path"])
    base = (tmp_path / "Documentos" / "Media Tool" / "Exports").resolve()
    assert folder.parent == base and folder.name.startswith("Importacao-20") and export["status"] == "completed"
    assert len(files_in(folder)) == 2
    again = harness.client.post("/api/exports", json={
        "source": {"kind": "batch", "id": batch["id"]}, "destination": {"default": True}}).json()
    assert again["dest_path"] != export["dest_path"]   # sufixo -2 no mesmo minuto (ou outro minuto): outra pasta
    # Importação sem mídia: a pasta padrão criada para ela não fica para trás.
    harness.downloader.items = [remote("Z9")]
    listing = harness.import_profile()
    count = len(files_in(base))
    refused = harness.client.post("/api/exports", json={
        "source": {"kind": "batch", "id": listing["id"]}, "destination": {"default": True}})
    assert refused.status_code == 409 and refused.json()["code"] == "export_empty" and len(files_in(base)) == count


# --------------------------------------------------------------------------- cancelamento, falhas e reinício


class WatchedPart:
    """Envolve o `.part` real para agir no meio da cópia (cancelar, simular disco cheio)."""

    def __init__(self, handle, on_write):
        self.handle = handle
        self.on_write = on_write

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handle.close()

    def write(self, chunk):
        self.on_write(chunk)
        return self.handle.write(chunk)


def watch_parts(monkeypatch, on_open=None, on_write=None) -> list:
    opened = []
    real_open = export_runner._open_part

    def fake_open(path):
        opened.append(path)
        if on_open is not None:
            on_open(path, len(opened))
        return WatchedPart(real_open(path), on_write or (lambda chunk: None))

    monkeypatch.setattr(export_runner, "_open_part", fake_open)
    return opened


def test_cancel_during_a_large_file_removes_the_part_and_keeps_the_rest(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    small = seed_media(harness, 3)
    big = seed_media(harness, 1, profile="grande", start=10, size=5 * 1024 * 1024)
    seed_media(harness, 2, profile="depois", start=20)
    ids = harness.ctx.db.fetchall("SELECT id FROM media ORDER BY id")
    before = storage_snapshot(harness)
    created = start_export(harness, {"kind": "library", "selection": {"media_ids": [row["id"] for row in ids]}}, dest)
    chunks = []

    def on_write(chunk):
        chunks.append(len(chunk))
        if len(chunk) == export_runner.CHUNK_SIZE and chunks.count(export_runner.CHUNK_SIZE) == 2:
            assert any(name.startswith(".") and f".{created['id']}." in name and name.endswith(".mtpart")
                       for name in files_in(dest))
            response = harness.client.post(f"/api/exports/{created['id']}/cancel")   # no meio do arquivo grande
            assert response.status_code == 200

    with monkeypatch.context() as scoped:
        watch_parts(scoped, on_write=on_write)
        run_exports(harness)

    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["copied"], export["total"]) == ("cancelled", 3, 6)
    assert export["error_code"] is None and "Biblioteca não foi alterada" in export["error_message"]
    names = files_in(dest)
    assert len(names) == 3 and not any(name.endswith("part") for name in names)   # .part removido; copiados ficam
    for media_id in small:
        media = harness.ctx.media.get(media_id)
        assert_same_content(harness, dest, media, f"semente_2026-02-03_{media['source_post_id']}.jpg")
    assert chunks.count(export_runner.CHUNK_SIZE) == 2   # parou no bloco seguinte, sem copiar o arquivo inteiro
    assert original_of(harness, big[0]).stat().st_size == 5 * 1024 * 1024
    assert storage_snapshot(harness) == before   # Biblioteca intacta

    repeated = harness.client.post(f"/api/exports/{created['id']}/cancel")
    assert repeated.status_code == 409 and repeated.json()["code"] == "export_finished"
    # A exportação seguinte funciona e completa a mesma pasta.
    again = export_to(harness, {"kind": "library", "selection": {"media_ids": [row["id"] for row in ids]}}, dest)
    assert (again["status"], again["copied"], again["skipped_existing"]) == ("completed", 3, 3)
    assert len(files_in(dest)) == 6


def test_cancel_between_files_and_cancel_of_a_queued_export(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 10)
    sources = [{"kind": "batch", "id": seed_batch(harness, ids)} for _ in range(3)]
    running = start_export(harness, sources[0], dest)
    other = tmp_path / "outra"
    other.mkdir()
    queued = start_export(harness, sources[1], other)
    third = tmp_path / "terceira"
    third.mkdir()
    last = start_export(harness, sources[2], third)

    # Na fila: cancela na hora, sem copiar nada.
    cancelled = harness.client.post(f"/api/exports/{queued['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and cancelled["finished_at"]

    def on_open(path, count):
        if count == 5:   # quatro arquivos prontos; o quinto ainda não recebeu nenhum byte
            export_service.cancel_export(harness.ctx, running["id"])

    watch_parts(monkeypatch, on_open=on_open)
    assert harness.ctx.export_runner.run_next() is True
    first = harness.client.get(f"/api/exports/{running['id']}").json()
    assert (first["status"], first["copied"], first["processed"]) == ("cancelled", 4, 4)
    assert len(files_in(dest)) == 4 and not any(name.endswith("part") for name in files_in(dest))

    run_exports(harness)
    assert files_in(other) == []
    done = harness.client.get(f"/api/exports/{last['id']}").json()
    assert (done["status"], done["copied"]) == ("completed", 10) and len(files_in(third)) == 10
    assert harness.client.post("/api/exports/999/cancel").status_code == 404


def test_missing_original_is_counted_and_the_export_goes_on(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 5)
    original_of(harness, ids[2]).unlink()
    harness.ctx.db.execute("UPDATE media SET original_path = '../../fora.jpg' WHERE id = ?", (ids[4],))
    export = export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    assert (export["status"], export["total"], export["copied"], export["missing"], export["failed"]) == (
        "completed", 5, 3, 2, 0)
    assert export["processed"] == 5 and len(files_in(dest)) == 3


def test_disk_full_stops_with_a_clear_error_and_keeps_the_library(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 6)
    before = storage_snapshot(harness)
    created = start_export(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    state = {"files": 0}

    def on_open(path, count):
        state["files"] = count

    def on_write(chunk):
        if state["files"] == 3:
            raise OSError(errno.ENOSPC, "No space left on device")

    watch_parts(monkeypatch, on_open=on_open, on_write=on_write)
    run_exports(harness)
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["error_code"], export["copied"]) == ("failed", "export_disk_full", 2)
    assert "sem espaço" in export["error_message"] and "Biblioteca não foi alterada" in export["error_message"]
    assert len(files_in(dest)) == 2 and not any(name.endswith("part") for name in files_in(dest))
    assert storage_snapshot(harness) == before
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM media") == 6


def test_write_failures_are_counted_and_many_in_a_row_stop_the_export(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 40)
    source = {"kind": "library", "selection": {"media_ids": ids}}
    state = {"files": 0, "fail": {2}}

    def on_open(path, count):
        state["files"] = count
        if count in state["fail"]:
            raise PermissionError(13, "acesso negado")

    watch_parts(monkeypatch, on_open=on_open)
    one = export_to(harness, source, dest)
    assert (one["status"], one["failed"], one["copied"]) == ("completed", 1, 39)

    other = tmp_path / "ruim"
    other.mkdir()
    state.update(files=0, fail=set(range(1, 1000)))
    opened = watch_parts(monkeypatch, on_open=on_open)
    broken = export_to(harness, source, other)
    assert (broken["status"], broken["error_code"]) == ("failed", "export_write_failed")
    assert broken["failed"] == export_runner.MAX_CONSECUTIVE_FAILURES == len(opened) and files_in(other) == []



def test_background_thread_runs_one_export_at_a_time(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 30)
    folders = [dest, tmp_path / "b", tmp_path / "c"]
    for folder in folders[1:]:
        folder.mkdir()
    overlap = []
    real_page = harness.ctx.exports.page

    def page(export, **kwargs):
        overlap.append(harness.ctx.db.scalar("SELECT COUNT(*) FROM exports WHERE status = 'running'"))
        return real_page(export, **kwargs)

    monkeypatch.setattr(harness.ctx.exports, "page", page)
    created = [start_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)}, folder)
               for folder in folders]
    assert [item["status"] for item in created] == ["pending"] * 3
    runner = harness.ctx.export_runner
    runner.start()
    try:
        assert wait_until(lambda: harness.ctx.db.scalar(
            "SELECT COUNT(*) FROM exports WHERE status = 'completed'") == 3, timeout=30)
    finally:
        runner.stop()
    assert overlap and set(overlap) == {1}
    rows = harness.ctx.db.fetchall("SELECT started_at, finished_at FROM exports ORDER BY id")
    assert all(rows[i]["finished_at"] <= rows[i + 1]["started_at"] for i in range(2))   # em fila, na ordem
    assert all(len(files_in(folder)) == 30 for folder in folders)


# --------------------------------------------------------------------------- abrir pasta


def test_open_uses_only_the_path_recorded_in_the_database(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 1)
    export = export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    opened = []
    monkeypatch.setattr(export_service, "open_folder",
                        lambda path, *, allowed_roots: opened.append((path, list(allowed_roots))))
    evil = str(harness.ctx.storage.root)
    response = harness.client.post(f"/api/exports/{export['id']}/open", json={"path": evil, "target": evil})
    assert response.status_code == 200 and response.json() == {"ok": True}
    response = harness.client.post(f"/api/exports/{export['id']}/open", params={"path": evil})
    assert response.status_code == 200
    assert opened == [(dest.resolve(), [dest.resolve()])] * 2

    library = harness.client.post("/api/library/open-folder", json={"path": str(dest)})
    assert library.status_code == 200
    assert opened[-1] == (harness.ctx.storage.originals, [harness.ctx.storage.originals])

    assert harness.client.post("/api/exports/999/open").status_code == 404
    for name in files_in(dest):
        (dest / name).unlink()
    dest.rmdir()
    gone = harness.client.post(f"/api/exports/{export['id']}/open")
    assert gone.status_code == 409 and gone.json()["code"] == "export_folder_missing"
    # Registro adulterado apontando para dentro do storage: revalidado e recusado.
    harness.ctx.db.execute("UPDATE exports SET dest_path = ? WHERE id = ?", (evil, export["id"]))
    tampered = harness.client.post(f"/api/exports/{export['id']}/open")
    assert tampered.status_code == 422 and tampered.json()["code"] == "export_dest_forbidden"
    assert len(opened) == 3


def test_open_folder_helper_only_opens_directories(tmp_path, monkeypatch):
    from app.services import system_open as so

    started = []
    monkeypatch.setattr(so.os, "startfile", started.append, raising=False)
    monkeypatch.setattr(so.os, "name", "nt")
    folder = tmp_path / "Minha pasta, com vírgula"
    folder.mkdir()
    (folder / "a.bat").write_text("echo", encoding="utf-8")
    REAL_OPEN_FOLDER(folder, allowed_roots=[folder])
    assert started == [str(folder.resolve())]
    for target, code in ((folder / "a.bat", "not_found"), (folder / "nao-existe", "not_found"),
                         (tmp_path, "not_allowed")):
        with pytest.raises(so.SystemOpenError) as info:
            REAL_OPEN_FOLDER(target, allowed_roots=[folder])
        assert info.value.code == code
    assert len(started) == 1


# --------------------------------------------------------------------------- proteções de origem


def test_new_mutation_routes_keep_the_local_only_protections(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 1)
    export = export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    token = pick(harness, dest)["token"]
    calls = []
    harness.ctx.folder_picker = lambda initial: calls.append(initial)
    body = {"source": {"kind": "library", "selection": {"media_ids": ids}}, "destination": {"token": token}}
    routes = [
        ("/api/exports/pick-folder", None), ("/api/exports", body), (f"/api/exports/{export['id']}/cancel", None),
        (f"/api/exports/{export['id']}/open", None), ("/api/library/open-folder", None),
    ]
    bare = TestClient(harness.app, base_url=BASE_URL)
    for path, payload in routes:
        missing = bare.post(path, json=payload)
        assert missing.status_code == 403 and missing.json()["code"] == "client_header_required", path
        foreign = bare.post(path, json=payload, headers={"X-Media-Tool-Client": "panel",
                                                         "Origin": "https://evil.example"})
        assert foreign.status_code == 403 and foreign.json()["code"] == "origin_not_allowed", path
    rebind = TestClient(harness.app, base_url="http://evil.example:8765", headers={"X-Media-Tool-Client": "panel"})
    assert rebind.post("/api/exports/pick-folder").status_code == 400
    assert calls == [] and harness.ctx.db.scalar("SELECT COUNT(*) FROM exports") == 1
    # O token não foi consumido por nenhuma tentativa barrada.
    assert harness.client.post("/api/exports", json=body).status_code == 202


def test_only_one_native_dialog_at_a_time(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    entered, release = threading.Event(), threading.Event()

    def slow_picker(initial):
        entered.set()
        assert release.wait(10)
        return dest

    harness.ctx.folder_picker = slow_picker
    results = []
    thread = threading.Thread(target=lambda: results.append(harness.client.post("/api/exports/pick-folder")))
    thread.start()
    try:
        assert entered.wait(10)
        busy = harness.client.post("/api/exports/pick-folder")
        assert busy.status_code == 409 and busy.json()["code"] == "picker_busy"
    finally:
        release.set()
        thread.join(10)
    assert results[0].status_code == 200 and results[0].json()["cancelled"] is False
    # Terminou: o próximo pedido abre normalmente (o bloqueio foi liberado, inclusive depois de erro).
    harness.ctx.folder_picker = lambda initial: harness.ctx.storage.root
    assert harness.client.post("/api/exports/pick-folder").status_code == 422
    harness.ctx.folder_picker = lambda initial: None
    assert harness.client.post("/api/exports/pick-folder").json()["cancelled"] is True


# --------------------------------------------------------------------------- retenção de jobs x resultado do lote


def purge_all_finished(harness) -> int:
    from app.db.connection import utc_now

    return harness.ctx.jobs.purge_finished_before(utc_now(60))


def test_batch_export_is_complete_after_old_jobs_are_purged(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    harness.downloader.items = [remote("A1"), remote("A2"), remote("A3")]
    first = harness.import_profile()
    harness.run_all()
    before_purge = export_to(harness, {"kind": "batch", "id": first["id"]}, dest)
    assert before_purge["total"] == 3 and harness.ctx.db.scalar("SELECT COUNT(*) FROM batch_media") == 0
    # Uma importação posterior com as mesmas mídias: os jobs do primeiro lote deixam de ser "o último de cada mídia"
    # e a retenção pode apagá-los.
    harness.downloader.items = [remote("A1"), remote("A2"), remote("A3"), remote("B9")]
    second = harness.import_profile()
    harness.run_all()
    assert purge_all_finished(harness) == 3
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ?", (first["id"],)) == 0
    pairs = harness.ctx.db.fetchall("SELECT batch_id, media_id FROM batch_media ORDER BY media_id")
    assert [row["batch_id"] for row in pairs] == [first["id"]] * 3

    after = tmp_path / "depois"
    after.mkdir()
    export = export_to(harness, {"kind": "batch", "id": first["id"]}, after)
    assert (export["status"], export["total"], export["copied"]) == ("completed", 3, 3)
    assert files_in(after) == files_in(dest)   # as mesmas mídias de antes do purge
    # O outro lote (jobs ainda presentes) e a união sem repetição: cada mídia uma vez.
    other = tmp_path / "segundo"
    other.mkdir()
    full = export_to(harness, {"kind": "batch", "id": second["id"]}, other)
    assert full["total"] == 4 and len(files_in(other)) == 4
    harness.ctx.db.execute("INSERT OR IGNORE INTO batch_media (batch_id, media_id) SELECT ?, id FROM media",
                           (second["id"],))
    again = export_to(harness, {"kind": "batch", "id": second["id"]}, other)
    assert (again["total"], again["skipped_existing"], again["copied"]) == (4, 4, 0)

    # Mídia excluída da Biblioteca some da relação (ON DELETE CASCADE) e da exportação.
    media_id = pairs[0]["media_id"]
    assert harness.client.delete(f"/api/media/{media_id}", params={"delete_original": "true"}).status_code == 204
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM batch_media WHERE media_id = ?", (media_id,)) == 0
    smaller = tmp_path / "menor"
    smaller.mkdir()
    assert export_to(harness, {"kind": "batch", "id": first["id"]}, smaller)["total"] == 2


def test_group_export_is_complete_after_old_jobs_are_purged(harness_factory, tmp_path):
    downloader = ProfilesDownloader(profiles={
        "um": [remote("U1", profile="um"), remote("U2", profile="um")],
        "dois": [remote("D1", profile="dois"), remote("U1", profile="um")],
        "depois": [remote("U1", profile="um"), remote("U2", profile="um"), remote("D1", profile="dois")],
    })
    harness = harness_factory(downloader=downloader)
    dest = prepare(harness, tmp_path)
    created = harness.client.post("/api/import-groups", json={
        "profiles": ["um", "dois"], "content_type": "all", "limit": 10, "preset_id": None, "process": False})
    group_id = created.json()["group"]["id"]
    run_group(harness, group_id)
    before_purge = export_to(harness, {"kind": "group", "id": group_id}, dest)
    assert before_purge["total"] == 3
    harness.import_profile(profile="depois")   # importação posterior com as mesmas mídias
    harness.run_all()
    assert purge_all_finished(harness) == 4   # todos os jobs do grupo
    assert harness.ctx.db.scalar(
        "SELECT COUNT(*) FROM jobs j JOIN batches b ON b.id = j.batch_id WHERE b.group_id = ?", (group_id,)) == 0
    after = tmp_path / "depois-do-purge"
    after.mkdir()
    export = export_to(harness, {"kind": "group", "id": group_id}, after)
    assert (export["status"], export["total"], export["copied"]) == ("completed", 3, 3)   # U1 uma vez só
    assert files_in(after) == files_in(dest)


def test_migration_0013_backfills_batch_media_over_schema_0012_with_data(tmp_path):
    import shutil

    from app.db.connection import Database
    from app.db.migrate import MIGRATIONS_DIR, apply_migrations, schema_version

    old = tmp_path / "migrations_0012"
    old.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0013":
            shutil.copy(path, old)
    db = Database(tmp_path / "antigo" / "data" / "media-tool.db")
    try:
        apply_migrations(db, old)
        assert schema_version(db) == 12
        now = "2026-01-01T00:00:00.000Z"
        for media_id in (1, 2, 3):
            db.execute(
                "INSERT INTO media (id, source_platform, source_post_id, source_media_index, media_type,"
                " original_path, original_sha256, file_size, created_at, updated_at)"
                " VALUES (?, 'instagram', ?, 0, 'image', ?, ?, 1, ?, ?)",
                (media_id, f"P{media_id}", f"originals/x{media_id}.jpg", str(media_id) * 64, now, now))
        for batch_id in (1, 2):
            db.execute(
                "INSERT INTO batches (id, source, status, found, process, created_at, updated_at)"
                " VALUES (?, 'instagram_import', 'completed', 0, 0, ?, ?)", (batch_id, now, now))
        jobs = [   # (lote, mídia, tipo, status)
            (1, 1, "ingest", "completed"), (1, 2, "ingest", "completed"), (2, 2, "ingest", "completed"),
            (2, 2, "ingest", "completed"),      # repetido: entra uma vez
            (2, 3, "ingest", "failed"),         # não concluído: fora
            (1, 3, "process", "completed"),     # processamento: fora
            (2, None, "ingest", "completed"),   # sem mídia: fora
            (None, 3, "ingest", "completed"),   # sem lote: fora
        ]
        for index, (batch_id, media_id, kind, status) in enumerate(jobs):
            db.execute(
                "INSERT INTO jobs (batch_id, media_id, kind, status, source_post_id, source_media_index, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)", (batch_id, media_id, kind, status, f"J{index}", now, now))
        before = [tuple(row) for row in db.fetchall("SELECT * FROM jobs ORDER BY id")]

        assert apply_migrations(db) == [13, 14, 15, 16]   # 0014/0016 só criam colunas/tabela; 0015 só mexe no preset
        pairs = [(row["batch_id"], row["media_id"]) for row in db.fetchall(
            "SELECT batch_id, media_id FROM batch_media ORDER BY batch_id, media_id")]
        assert pairs == [(1, 1), (1, 2), (2, 2)]
        assert [tuple(row) for row in db.fetchall("SELECT * FROM jobs ORDER BY id")] == before   # nada reescrito
        assert db.scalar("SELECT COUNT(*) FROM exports") == 0
        db.execute("DELETE FROM media WHERE id = 2")
        assert db.scalar("SELECT COUNT(*) FROM batch_media") == 1
    finally:
        db.close_all()


# --------------------------------------------------------------------------- Biblioteca: seleção grande e pasta padrão


def test_select_all_results_exports_by_filter_across_pages(harness, tmp_path):
    """Caminho "Selecionar todos os N resultados" -> Exportar selecionados: o painel manda só o filtro."""
    dest = prepare(harness, tmp_path)
    seed_media(harness, 620, profile="grande")          # mais de uma página de 500 no executor
    seed_media(harness, 15, profile="outro", start=5000)
    body = {"source": {"kind": "library", "selection": {"filter": {"view": "all", "q": "grande"}}},
            "destination": {"token": pick(harness, dest)["token"]}}
    assert len(str(body)) < 400   # nenhum id viaja no pedido
    created = harness.client.post("/api/exports", json=body)
    assert created.status_code == 202 and created.json()["total"] == 620
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM export_items") == 0
    run_exports(harness)
    export = harness.client.get(f"/api/exports/{created.json()['id']}").json()
    assert (export["status"], export["copied"], export["processed"]) == ("completed", 620, 620)
    names = files_in(dest)
    assert len(names) == 620 and all(name.startswith("grande_") for name in names)


def test_library_default_folder_when_the_picker_is_unavailable(harness, tmp_path):
    from app.services.export_destination import PICKER_UNAVAILABLE_MESSAGE, DestinationError

    prepare(harness, tmp_path)
    ids = seed_media(harness, 4)

    def unavailable(initial):
        raise DestinationError("picker_unavailable", PICKER_UNAVAILABLE_MESSAGE, 501)

    harness.ctx.folder_picker = unavailable
    refused = harness.client.post("/api/exports/pick-folder")
    assert refused.status_code == 501 and refused.json()["code"] == "picker_unavailable"
    assert "Usar pasta padrão" in refused.json()["detail"]
    # O seletor continua livre para a próxima tentativa (o bloqueio não ficou preso).
    assert harness.client.post("/api/exports/pick-folder").status_code == 501

    response = harness.client.post("/api/exports", json={
        "source": {"kind": "library", "selection": {"media_ids": ids}}, "destination": {"default": True}})
    assert response.status_code == 202, response.text
    run_exports(harness)
    export = harness.client.get(f"/api/exports/{response.json()['id']}").json()
    folder = type(tmp_path)(export["dest_path"])
    assert folder.parent == (tmp_path / "Documentos" / "Media Tool" / "Exports").resolve()
    assert folder.name.startswith("Biblioteca-20") and (export["status"], export["copied"]) == ("completed", 4)
    assert len(files_in(folder)) == 4


# --------------------------------------------------------------------------- reinício


def test_restart_marks_a_running_export_as_interrupted_and_deletes_nothing(harness_factory, tmp_path):
    harness = harness_factory()
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 3)
    stuck = start_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)}, dest)
    waiting_dir = tmp_path / "fila"
    waiting_dir.mkdir()
    waiting = start_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)}, waiting_dir)
    harness.ctx.db.execute("UPDATE exports SET status = 'running', copied = 1 WHERE id = ?", (stuck["id"],))
    kept = {
        dest / f".semente_2026-02-03_S00001.jpg.{stuck['id']}.a1b2c3d4.mtpart": b"temporario pela metade",
        dest / f"meu trabalho.{stuck['id']}.part": b"arquivo do usuario",
        dest / f"semente_2026-02-03_S00000.jpg.{stuck['id']}.part": b"outro arquivo do usuario",
        dest / "semente_2026-02-03_S00000.jpg": original_of(harness, ids[0]).read_bytes(),
    }
    for path, data in kept.items():
        path.write_bytes(data)

    restarted = harness_factory(root=harness.root)   # mesmo banco e storage: simula o app reiniciado
    prepare(restarted, tmp_path)
    export = restarted.client.get(f"/api/exports/{stuck['id']}").json()
    assert (export["status"], export["error_code"], export["copied"]) == ("failed", "interrupted", 1)
    assert "interrompida" in export["error_message"] and export["finished_at"]
    # Nada é apagado por padrão de nome depois do reinício: nem arquivo do usuário, nem temporário antigo.
    assert {path: path.read_bytes() for path in kept} == kept
    # Não retoma sozinha; a que estava na fila continua na fila e roda normalmente.
    assert restarted.client.get(f"/api/exports/{waiting['id']}").json()["status"] == "pending"
    run_exports(restarted)
    assert restarted.client.get(f"/api/exports/{stuck['id']}").json()["status"] == "failed"
    assert restarted.client.get(f"/api/exports/{waiting['id']}").json()["status"] == "completed"
    assert len(files_in(waiting_dir)) == 3


# --------------------------------------------------------------------------- a exportação só remove o que ela criou


def test_user_files_named_like_temporaries_are_never_deleted(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    created = start_export(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    tokens = iter(["deadbeef"])
    real_token = export_runner.secrets.token_hex
    monkeypatch.setattr(export_runner.secrets, "token_hex", lambda n=None: next(tokens, None) or real_token(n))
    user_files = {
        dest / f"semente_2026-02-03_S00000.jpg.{created['id']}.part": b"arquivo do usuario (nome antigo de .part)",
        dest / f"meu trabalho.{created['id']}.part": b"meu trabalho",
        # Exatamente o nome que o primeiro temporário teria: a exportação sorteia outro, sem apagar este.
        dest / f".semente_2026-02-03_S00000.jpg.{created['id']}.deadbeef.mtpart": b"coincidencia de nome",
    }
    for path, data in user_files.items():
        path.write_bytes(data)
    opened = watch_parts(monkeypatch)
    run_exports(harness)
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["copied"], export["renamed"], export["failed"]) == ("completed", 2, 0, 0)
    assert {path: path.read_bytes() for path in user_files} == user_files
    assert sorted(set(files_in(dest)) - {path.name for path in user_files}) == [
        "semente_2026-02-03_S00000.jpg", "semente_2026-02-03_S00001.jpg"]   # sem " (2)": não houve colisão real
    assert len(opened) == 3   # a primeira tentativa bateu no arquivo do usuário e só trocou de nome
    assert all(path.name.startswith(".") and path.name.endswith(".mtpart") for path in opened)


def test_final_file_created_by_someone_else_before_the_rename_is_kept(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    real_rename = export_runner._rename_no_overwrite
    raced = []

    def rename(src, dst):
        if not raced:   # outro processo cria o arquivo final entre a cópia e o rename
            raced.append(dst)
            dst.write_bytes(b"arquivo de outro programa")
        return real_rename(src, dst)

    monkeypatch.setattr(export_runner, "_rename_no_overwrite", rename)
    export = export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    assert (export["status"], export["copied"], export["renamed"], export["failed"]) == ("completed", 2, 1, 0)
    assert (dest / "semente_2026-02-03_S00000.jpg").read_bytes() == b"arquivo de outro programa"   # intacto
    assert files_in(dest) == ["semente_2026-02-03_S00000 (2).jpg", "semente_2026-02-03_S00000.jpg",
                              "semente_2026-02-03_S00001.jpg"]   # nenhum temporário sobrou
    assert_same_content(harness, dest, harness.ctx.media.get(ids[0]), "semente_2026-02-03_S00000 (2).jpg")


def test_rename_helper_never_overwrites(tmp_path):
    src, dst = tmp_path / "novo.tmp", tmp_path / "final.jpg"
    src.write_bytes(b"novo")
    dst.write_bytes(b"existente")
    with pytest.raises(FileExistsError):
        export_runner._rename_no_overwrite(src, dst)
    assert dst.read_bytes() == b"existente" and src.read_bytes() == b"novo"
    dst.unlink()
    export_runner._rename_no_overwrite(src, dst)
    assert dst.read_bytes() == b"novo" and not src.exists()


def test_destination_swapped_during_the_export_stops_it(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 6)
    created = start_export(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)

    def on_open(path, count):
        if count == 3:   # a pasta validada sai do lugar e outra pasta aparece com o mesmo caminho
            os.rename(dest, tmp_path / "movida")
            dest.mkdir()

    watch_parts(monkeypatch, on_open=on_open)
    run_exports(harness)
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["error_code"]) == ("failed", "export_folder_missing")
    assert export["copied"] == 3 and len(files_in(tmp_path / "movida")) == 2 and len(files_in(dest)) == 1


# --------------------------------------------------------------------------- limites


def test_same_source_cannot_have_two_open_exports(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    first_batch, second_batch = seed_batch(harness, ids), seed_batch(harness, ids)
    first = start_export(harness, {"kind": "batch", "id": first_batch}, dest)
    token = pick(harness, dest)["token"]

    def post(source):
        return harness.client.post("/api/exports", json={"source": source, "destination": {"token": token}})

    again = post({"kind": "batch", "id": first_batch})
    assert again.status_code == 409 and again.json()["code"] == "export_already_running"
    assert again.json()["export_id"] == first["id"] and again.json()["detail"]
    assert post({"kind": "batch", "id": second_batch}).status_code == 202   # outra origem; o token não foi gasto

    library = {"kind": "library", "selection": {"media_ids": ids}}
    open_library = start_export(harness, library, dest)
    other = start_export(harness, {"kind": "library", "selection": {"filter": {"view": "all"}}}, dest, expect=409)
    assert other["code"] == "export_already_running" and other["export_id"] == open_library["id"]
    run_exports(harness)
    assert start_export(harness, {"kind": "batch", "id": first_batch}, dest)["status"] == "pending"   # terminou: pode
    assert start_export(harness, library, dest)["status"] == "pending"


def test_pending_queue_has_a_ceiling(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 1)
    assert export_service.MAX_PENDING_EXPORTS == 20
    monkeypatch.setattr(export_service, "MAX_PENDING_EXPORTS", 3)
    for _ in range(3):
        start_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)}, dest)
    full = start_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)}, dest, expect=409)
    assert full["code"] == "export_queue_full" and harness.ctx.db.scalar("SELECT COUNT(*) FROM exports") == 3
    assert harness.ctx.export_runner.run_next() is True
    assert start_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)}, dest)["status"] == "pending"


def test_empty_default_folder_is_removed_when_the_export_does_not_copy(harness, tmp_path, monkeypatch):
    prepare(harness, tmp_path)
    base = tmp_path / "Documentos" / "Media Tool" / "Exports"
    ids = seed_media(harness, 3)

    def default_export(batch_id, expect=202):
        response = harness.client.post("/api/exports", json={
            "source": {"kind": "batch", "id": batch_id}, "destination": {"default": True}})
        assert response.status_code == expect, response.text
        return response.json()

    # Cancelada ainda na fila: a pasta criada para ela (vazia) some.
    queued = default_export(seed_batch(harness, ids))
    folder = type(tmp_path)(queued["dest_path"])
    assert folder.is_dir()
    harness.client.post(f"/api/exports/{queued['id']}/cancel")
    assert not folder.exists() and base.is_dir()

    # Falhou sem copiar nada (disco cheio no primeiro arquivo): some também.
    failing = default_export(seed_batch(harness, ids))
    with monkeypatch.context() as scoped:
        watch_parts(scoped, on_write=lambda chunk: (_ for _ in ()).throw(OSError(errno.ENOSPC, "cheio")))
        run_exports(harness)
    failed = harness.client.get(f"/api/exports/{failing['id']}").json()
    assert (failed["status"], failed["error_code"]) == ("failed", "export_disk_full")
    assert not type(tmp_path)(failed["dest_path"]).exists()

    # Cancelada depois de copiar algo: a pasta fica (só rmdir de pasta vazia, nunca recursivo).
    partial = default_export(seed_batch(harness, ids))
    with monkeypatch.context() as scoped:
        watch_parts(scoped, on_open=lambda path, count: count == 3 and export_service.cancel_export(
            harness.ctx, partial["id"]))
        run_exports(harness)
    kept = type(tmp_path)(partial["dest_path"])
    assert harness.client.get(f"/api/exports/{partial['id']}").json()["status"] == "cancelled"
    assert kept.is_dir() and len(files_in(kept)) == 2

    # Pasta padrão criada e recusada na validação: não fica para trás.
    before = set(files_in(base))

    def refuse(path, settings, **kwargs):
        raise export_destination.DestinationError("export_dest_not_writable", "Sem permissão.", 409)

    monkeypatch.setattr(export_service, "validate_destination", refuse)
    assert default_export(seed_batch(harness, ids), expect=409)["code"] == "export_dest_not_writable"
    assert set(files_in(base)) == before


def test_empty_selection_does_not_spend_the_folder_token(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 1)
    token = pick(harness, dest)["token"]

    def post(source):
        return harness.client.post("/api/exports", json={"source": source, "destination": {"token": token}})

    empty = post({"kind": "library", "selection": {"media_ids": [999_999]}})
    assert empty.status_code == 409 and empty.json()["code"] == "export_empty"
    assert post({"kind": "batch", "id": 999}).status_code == 404
    ok = post({"kind": "library", "selection": {"media_ids": ids}})   # a mesma pasta escolhida continua valendo
    assert ok.status_code == 202
    run_exports(harness)
    used = post({"kind": "library", "selection": {"media_ids": ids}})
    assert used.status_code == 409 and used.json()["code"] == "export_token_invalid"   # gasto só pela que foi criada


# --------------------------------------------------------------------------- seleção que muda e dados inesperados


def test_media_deleted_during_the_export_is_counted_so_processed_equals_total(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 5)
    created = start_export(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    assert created["total"] == 5
    harness.ctx.db.execute("DELETE FROM media WHERE id IN (?, ?)", (ids[1], ids[3]))   # excluídas depois do pedido
    run_exports(harness)
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["total"], export["copied"], export["missing"]) == ("completed", 5, 3, 2)
    assert export["processed"] == export["total"]


def test_unexpected_media_data_fails_only_that_media(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 4)
    harness.ctx.db.execute("UPDATE media SET source_media_index = 'abc' WHERE id = ?", (ids[1],))
    export = export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, dest)
    assert (export["status"], export["copied"], export["failed"], export["processed"]) == ("completed", 3, 1, 4)
    assert len(files_in(dest)) == 3


def test_export_into_a_long_path_destination(harness, tmp_path):
    import shutil

    prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    top = tmp_path / ("a" * 110)
    long_dest = top / ("b" * 110) / ("c" * 110)
    assert len(str(long_dest)) > 330
    os.makedirs(export_destination.io_path(long_dest))
    try:
        picked = pick(harness, long_dest)
        assert picked["path_display"] == str(long_dest.resolve()) and not picked["path_display"].startswith("\\\\?\\")
        export = export_to(harness, {"kind": "library", "selection": {"media_ids": ids}}, long_dest)
        assert (export["status"], export["copied"]) == ("completed", 2)
        names = sorted(os.listdir(export_destination.io_path(long_dest)))
        assert names == ["semente_2026-02-03_S00000.jpg", "semente_2026-02-03_S00001.jpg"]
    finally:
        shutil.rmtree(export_destination.io_path(top), ignore_errors=True)
