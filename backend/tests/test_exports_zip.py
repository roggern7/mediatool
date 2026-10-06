"""Baixar ZIP: arquivo plano gerado pelo servidor, download com token, espaço, cancelamento e limpeza.

Tudo em diretórios temporários do pytest; sem rede; nenhum diálogo ou Explorer.
"""

from __future__ import annotations

import errno
import io
import os
import re
import tracemalloc
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.db.connection import parse_utc, utc_now
from app.jobs import export_runner
from app.services import export_service, export_zip
from conftest import BASE_URL
from export_helpers import (
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
from test_exports import run_group

ZIP = {"zip": True}


def start_zip(harness, source: dict, *, expect: int = 202) -> dict:
    response = harness.client.post("/api/exports", json={"source": source, "destination": ZIP})
    assert response.status_code == expect, response.text
    return response.json()


def zip_export(harness, source: dict) -> dict:
    created = start_zip(harness, source)
    run_exports(harness)
    return harness.client.get(f"/api/exports/{created['id']}").json()


def zip_folder(harness):
    return export_zip.zip_dir(harness.settings)


def stored_zip(harness, export_id: int):
    return export_zip.zip_file(harness.settings, harness.ctx.exports.get(export_id))


def temp_files(harness) -> list[str]:
    folder = zip_folder(harness)
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


def library(ids) -> dict:
    return {"kind": "library", "selection": {"media_ids": list(ids)}}


def assert_flat_and_unique(archive: zipfile.ZipFile) -> list[str]:
    names = archive.namelist()
    assert all("/" not in name and "\\" not in name for name in names)        # raiz plana, sem diretórios
    assert not any(info.is_dir() for info in archive.infolist())
    assert len({name.casefold() for name in names}) == len(names)             # nenhuma entrada duplicada
    return sorted(names)


# --------------------------------------------------------------------------- ZIP pequeno, download


def test_small_batch_zip_is_flat_byte_identical_and_downloadable(harness, tmp_path):
    prepare(harness, tmp_path)
    harness.downloader.items = [remote("IMG", profile="alpha"), remote("VID", media_type="video", profile="alpha"),
                                remote("CAR", index=0, profile="alpha"), remote("CAR", index=1, profile="alpha")]
    batch = harness.import_profile(profile="alpha")
    harness.run_all()
    before = storage_snapshot(harness)

    created = start_zip(harness, {"kind": "batch", "id": batch["id"]})
    assert (created["mode"], created["status"], created["total"]) == ("zip", "pending", 4)
    assert created["zip_ready"] is False and created["download_url"] is None and created["zip_bytes"] is None
    run_exports(harness)
    export = harness.client.get(f"/api/exports/{created['id']}").json()

    assert (export["status"], export["mode"], export["copied"], export["processed"]) == ("completed", "zip", 4, 4)
    assert re.fullmatch(r"importacao-\d{4}-\d{2}-\d{2}-\d{4}\.zip", export["zip_name"])
    assert export["dest_path"] == export["zip_name"] and str(harness.settings.data_dir) not in str(export)
    assert export["zip_ready"] is True and export["zip_bytes"] == stored_zip(harness, export["id"]).stat().st_size
    assert export["download_url"].startswith(f"/api/exports/{export['id']}/download?token=")
    hours = (parse_utc(export["zip_expires_at"]) - parse_utc(utc_now())).total_seconds() / 3600
    assert 23.9 < hours <= 24.0
    assert temp_files(harness) == [stored_zip(harness, export["id"]).name]   # o .part virou .zip

    response = harness.client.get(export["download_url"])
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert int(response.headers["content-length"]) == export["zip_bytes"] == len(response.content)
    assert response.headers["content-disposition"] == f'attachment; filename="{export["zip_name"]}"'
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert assert_flat_and_unique(archive) == [
            "alpha_2026-01-01_CAR_1.jpg", "alpha_2026-01-01_CAR_2.jpg", "alpha_2026-01-01_IMG.jpg",
            "alpha_2026-01-01_VID.mp4"]
        rows = {f"{row['source_post_id']}:{row['source_media_index']}": dict(row)
                for row in harness.ctx.db.fetchall("SELECT * FROM media")}
        for key, name in (("IMG:0", "alpha_2026-01-01_IMG.jpg"), ("VID:0", "alpha_2026-01-01_VID.mp4"),
                          ("CAR:0", "alpha_2026-01-01_CAR_1.jpg"), ("CAR:1", "alpha_2026-01-01_CAR_2.jpg")):
            assert archive.read(name) == harness.ctx.storage.resolve(rows[key]["original_path"]).read_bytes()
            info = archive.getinfo(name)
            assert info.compress_type == zipfile.ZIP_STORED and info.date_time == (2026, 1, 1, 0, 0, 0)
        assert archive.testzip() is None
    assert storage_snapshot(harness) == before   # originais intactos; nada criado no storage
    # O ZIP não conta como "pasta exportada": Abrir pasta continua olhando só exportações de pasta.
    assert harness.batch(batch["id"])["last_export"] is None
    opened = harness.client.post(f"/api/exports/{export['id']}/open")
    assert opened.status_code == 409 and opened.json()["code"] == "export_not_folder"


def test_download_requires_the_token_and_supports_range(harness, tmp_path):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 3)
    created = start_zip(harness, library(ids))
    token = harness.ctx.exports.get(created["id"])["zip_token"]
    base = f"/api/exports/{created['id']}/download"
    early = harness.client.get(base, params={"token": token})
    assert early.status_code == 409 and early.json()["code"] == "export_zip_not_ready"
    run_exports(harness)
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    for response in (harness.client.get(base), harness.client.get(base, params={"token": "x" * 43}),
                     harness.client.get(base, params={"token": ""}),
                     harness.client.get(f"/api/exports/999/download", params={"token": token})):
        assert response.status_code == 404 and response.json()["code"] == "export_not_found"
    other_host = TestClient(harness.app, base_url="http://evil.example:8765")
    assert other_host.get(export["download_url"]).status_code == 400   # validação de Host continua valendo
    assert harness.ctx.exports.get(created["id"])["zip_downloaded_at"] is None

    partial = harness.client.get(export["download_url"], headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206 and len(partial.content) == 10
    assert partial.headers["content-range"] == f"bytes 0-9/{export['zip_bytes']}"
    full = harness.client.get(export["download_url"])
    assert full.status_code == 200 and full.content[:10] == partial.content
    # Depois do download: disponível por mais 1 h no máximo (e "Baixar novamente" segue funcionando).
    row = harness.ctx.exports.get(created["id"])
    minutes = (parse_utc(row["zip_expires_at"]) - parse_utc(utc_now())).total_seconds() / 60
    assert row["zip_downloaded_at"] and 59 < minutes <= 60
    assert harness.client.get(export["download_url"]).status_code == 200


# --------------------------------------------------------------------------- o que entra


def test_zip_includes_media_that_already_existed_in_the_library(harness, tmp_path):
    prepare(harness, tmp_path)
    same_bytes = {"B3": "A2"}
    harness.downloader.content = lambda item: MAGIC["image"] + (
        f"FAKE-image-{same_bytes.get(item.source_post_id, item.source_post_id)}".encode())
    harness.downloader.items = [remote("A1"), remote("A2")]
    harness.import_profile()
    harness.run_all()
    harness.downloader.items = [remote("A1"), remote("B3"), remote("C4")]
    second = harness.import_profile()
    harness.run_all()
    export = zip_export(harness, {"kind": "batch", "id": second["id"]})
    assert (export["status"], export["total"], export["copied"]) == ("completed", 3, 3)
    with zipfile.ZipFile(stored_zip(harness, export["id"])) as archive:
        assert assert_flat_and_unique(archive) == [
            "perfil_teste_2026-01-01_A1.jpg", "perfil_teste_2026-01-01_A2.jpg", "perfil_teste_2026-01-01_C4.jpg"]


def test_group_zip_puts_every_profile_in_the_root_once(harness_factory, tmp_path):
    downloader = ProfilesDownloader(profiles={
        "um": [remote("U1", profile="um"), remote("COMUM", profile="um")],
        "dois": [remote("D1", profile="dois"), remote("COMUM", profile="um")],
        "tres": [remote("T1", profile="tres", media_type="video")],
    })
    harness = harness_factory(downloader=downloader)
    prepare(harness, tmp_path)
    created = harness.client.post("/api/import-groups", json={
        "profiles": ["um", "dois", "tres"], "content_type": "all", "limit": 10, "preset_id": None, "process": False})
    group_id = created.json()["group"]["id"]
    run_group(harness, group_id)
    export = zip_export(harness, {"kind": "group", "id": group_id})
    assert (export["status"], export["total"], export["copied"]) == ("completed", 4, 4)
    with zipfile.ZipFile(stored_zip(harness, export["id"])) as archive:
        names = assert_flat_and_unique(archive)
    assert len(names) == 4 and sum("COMUM" in name for name in names) == 1
    assert {name.split("_")[0] for name in names} == {"um", "dois", "tres"}
    assert harness.client.get(f"/api/import-groups/{group_id}").json()["group"]["last_export"] is None


def test_zip_of_an_old_batch_uses_the_durable_batch_media_relation(harness, tmp_path):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 6)
    export = zip_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)})   # sem nenhum job (pós-retenção)
    assert (export["status"], export["total"], export["copied"]) == ("completed", 6, 6)


# --------------------------------------------------------------------------- nomes, colisões, ZIP64, memória


def test_zip_names_collisions_unicode_and_zip64_fields(harness, tmp_path):
    prepare(harness, tmp_path)
    ids = [
        *seed_media(harness, 1, profile="Caixa", code="AbC"),
        *seed_media(harness, 1, profile="caixa", code="aBc", start=1),           # só a caixa muda
        *seed_media(harness, 1, profile="p" * 150, code="AAA", start=2),
        *seed_media(harness, 1, profile="p" * 150, code="BBB", start=3),          # mesmo nome depois do corte
        *seed_media(harness, 1, profile="cafe\u0301", code="CÓDIGO-日本", start=4),
        *seed_media(harness, 1, profile='ru<>:"/\\|?*im', code="x", start=5),
        *seed_media(harness, 1, profile="CON", code=None, taken_at=None, start=6),
        *seed_media(harness, 1, profile=None, code=None, taken_at=None, start=7, ext=".mp4"),
    ]
    export = zip_export(harness, library(ids))
    assert (export["status"], export["copied"], export["renamed"], export["failed"]) == ("completed", 8, 2, 0)
    path = stored_zip(harness, export["id"])
    with zipfile.ZipFile(path) as archive:
        names = assert_flat_and_unique(archive)
        assert len(names) == 8 and "Caixa_2026-02-03_AbC.jpg" in names and "caixa_2026-02-03_aBc (2).jpg" in names
        assert "café_2026-02-03_CÓDIGO-日本.jpg" in names and sum(name.endswith(".mp4") for name in names) == 1
        for name in names:
            assert not set(name) & set('<>:"/\\|?*') and len(name) <= 120 and not name.endswith((".", " "))
        unicode_info = archive.getinfo("café_2026-02-03_CÓDIGO-日本.jpg")
        assert unicode_info.flag_bits & 0x800                                      # nome em UTF-8
        assert {sha256_of(original_of(harness, i)) for i in ids} == {
            __import__("hashlib").sha256(archive.read(name)).hexdigest() for name in names}
        first = archive.infolist()[0]
    raw = path.read_bytes()
    assert raw[first.header_offset:first.header_offset + 4] == b"PK\x03\x04"
    assert int.from_bytes(raw[first.header_offset + 4:first.header_offset + 6], "little") == 45   # entrada ZIP64


def big_zip(harness, tmp_path, monkeypatch, total: int) -> None:
    prepare(harness, tmp_path)
    seed_media(harness, total)
    created = start_zip(harness, {"kind": "library", "selection": {"filter": {"view": "all"}}})
    assert created["total"] == total and harness.ctx.db.scalar("SELECT COUNT(*) FROM export_items") == 0
    runner = harness.ctx.export_runner
    samples: list[tuple[int, int]] = []
    pages: list[int] = []
    real_flush, real_page = runner._flush, harness.ctx.exports.page

    def flush(export_id, counts):
        real_flush(export_id, counts)
        samples.append((counts["copied"], tracemalloc.get_traced_memory()[0]))

    def page(export, **kwargs):
        rows = real_page(export, **kwargs)
        pages.append(len(rows))
        return rows

    monkeypatch.setattr(runner, "_flush", flush)
    monkeypatch.setattr(runner, "progress_writes", 0)
    monkeypatch.setattr(export_runner, "PROGRESS_EVERY_FILES", max(50, total // 40))
    monkeypatch.setattr(harness.ctx.exports, "page", page)
    tracemalloc.start()
    try:
        run_exports(harness)
    finally:
        tracemalloc.stop()
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["copied"], export["processed"]) == ("completed", total, total)
    assert max(pages) <= export_runner.PAGE_SIZE and sum(pages) == total      # paginado por keyset
    progress = [copied for copied, _ in samples]
    assert progress == sorted(set(progress)) and len(samples) >= 5
    # Memória: só os metadados do ZIP (uma ficha pequena por entrada) crescem; nunca o conteúdo dos arquivos.
    growth = max(used for _, used in samples) - samples[0][1]
    assert growth < 1024 * 1024 + total * 1200, growth
    with zipfile.ZipFile(stored_zip(harness, export["id"])) as archive:
        assert len(assert_flat_and_unique(archive)) == total
    assert export["zip_bytes"] > total * 20


def test_zip_with_thousands_of_entries_from_a_filter_selection(harness, tmp_path, monkeypatch):
    big_zip(harness, tmp_path, monkeypatch, 1500)


@pytest.mark.slow
def test_zip_with_ten_thousand_entries(harness, tmp_path, monkeypatch):
    big_zip(harness, tmp_path, monkeypatch, 10_000)


def test_ids_selection_has_no_arbitrary_count_limit(harness, tmp_path):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 5)
    created = start_zip(harness, library([*ids, *range(1_000_000, 1_060_000)]))   # 60.005 ids num pedido (< 1 MB)
    assert created["total"] == 5
    run_exports(harness)
    assert harness.client.get(f"/api/exports/{created['id']}").json()["copied"] == 5


def test_missing_original_is_left_out_of_the_zip(harness, tmp_path):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 4)
    original_of(harness, ids[1]).unlink()
    export = zip_export(harness, library(ids))
    assert (export["status"], export["copied"], export["missing"], export["processed"]) == ("completed", 3, 1, 4)
    with zipfile.ZipFile(stored_zip(harness, export["id"])) as archive:
        assert len(archive.namelist()) == 3


# --------------------------------------------------------------------------- espaço


def test_no_space_is_refused_before_starting(harness, tmp_path, monkeypatch):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 3, size=200_000)
    monkeypatch.setattr(export_zip, "reserve_bytes", lambda settings: 0)
    monkeypatch.setattr(export_zip, "free_bytes", lambda path: 10 * 1024 * 1024)
    refused = start_zip(harness, library(ids), expect=507)
    assert refused["code"] == "export_no_space"
    assert "necessários 64," in refused["detail"] and "há 10,0 MB" in refused["detail"]
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM exports") == 0 and temp_files(harness) == []
    # A reserva dos downloads é respeitada: espaço livre que só cobre a reserva não basta.
    monkeypatch.setattr(export_zip, "reserve_bytes", lambda settings: 5 * 1024**3)
    monkeypatch.setattr(export_zip, "free_bytes", lambda path: 5 * 1024**3 + 1024**2)
    assert "mantém 5,0 GB livres" in start_zip(harness, library(ids), expect=507)["detail"]
    monkeypatch.setattr(export_zip, "free_bytes", lambda path: 5 * 1024**3 + 100 * 1024**2)
    assert start_zip(harness, library(ids))["status"] == "pending"
    assert export_zip.required_bytes(100 * 1024**3) == 101 * 1024**3 + 64 * 1024**2


class FullDisk:
    """Envolve o `.zip.part` real: depois de `limit` bytes, toda escrita falha com ENOSPC."""

    def __init__(self, handle, limit, on_write=None):
        self.handle, self.limit, self.on_write, self.written = handle, limit, on_write, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handle.close()

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def write(self, data):
        if self.on_write is not None:
            self.on_write(len(data))
        if self.limit is not None and self.written + len(data) > self.limit:
            raise OSError(errno.ENOSPC, "No space left on device")
        self.written += len(data)
        return self.handle.write(data)


def test_disk_full_while_writing_removes_the_part(harness, tmp_path, monkeypatch):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 6, size=100_000)
    before = storage_snapshot(harness)
    real_open = export_runner._open_zip_part
    monkeypatch.setattr(export_runner, "_open_zip_part", lambda path: FullDisk(real_open(path), 250_000))
    export = zip_export(harness, library(ids))
    assert (export["status"], export["error_code"]) == ("failed", "export_disk_full")
    assert "sem espaço" in export["error_message"] and export["zip_ready"] is False
    assert export["download_url"] is None and temp_files(harness) == []
    assert storage_snapshot(harness) == before


# --------------------------------------------------------------------------- cancelamento e fila


def test_cancel_before_and_during_a_large_file_removes_the_part(harness, tmp_path, monkeypatch):
    prepare(harness, tmp_path)
    ids = [*seed_media(harness, 3), *seed_media(harness, 1, profile="grande", start=10, size=5 * 1024 * 1024),
           *seed_media(harness, 2, profile="depois", start=20)]
    before = storage_snapshot(harness)
    queued = start_zip(harness, {"kind": "batch", "id": seed_batch(harness, ids)})
    cancelled = harness.client.post(f"/api/exports/{queued['id']}/cancel").json()   # ainda na fila
    assert cancelled["status"] == "cancelled" and temp_files(harness) == []

    created = start_zip(harness, library(ids))
    real_open = export_runner._open_zip_part
    big_chunks = []

    def on_write(size):
        if size == export_runner.CHUNK_SIZE:
            big_chunks.append(size)
            if len(big_chunks) == 2:   # no meio do arquivo grande
                assert temp_files(harness) == [stored_zip(harness, created["id"]).name + ".part"]
                assert harness.client.post(f"/api/exports/{created['id']}/cancel").status_code == 200

    with monkeypatch.context() as scoped:
        scoped.setattr(export_runner, "_open_zip_part", lambda path: FullDisk(real_open(path), None, on_write))
        run_exports(harness)
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["copied"], export["zip_ready"]) == ("cancelled", 3, False)
    assert len(big_chunks) == 2 and temp_files(harness) == [] and storage_snapshot(harness) == before
    again = zip_export(harness, library(ids))   # a seguinte funciona
    assert (again["status"], again["copied"]) == ("completed", 6)


def test_zip_and_folder_exports_share_the_queue_one_at_a_time(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 4)
    batch = {"kind": "batch", "id": seed_batch(harness, ids)}
    to_zip = start_zip(harness, batch)
    to_folder = start_export(harness, batch, dest)   # mesma origem, outro modo: convivem
    twice = start_zip(harness, batch, expect=409)
    assert twice["code"] == "export_already_running" and twice["export_id"] == to_zip["id"]
    assert harness.ctx.export_runner.run_next() is True
    states = {row["id"]: row["status"] for row in harness.ctx.db.fetchall("SELECT id, status FROM exports")}
    assert states == {to_zip["id"]: "completed", to_folder["id"]: "pending"}
    run_exports(harness)
    assert harness.client.get(f"/api/exports/{to_folder['id']}").json()["status"] == "completed"
    assert harness.batch(batch["id"])["last_export"]["id"] == to_folder["id"]
    assert harness.batch(batch["id"])["last_export"]["mode"] == "folder"
    # Mutação protegida como as outras; pedido de ZIP não aceita caminho.
    bare = TestClient(harness.app, base_url=BASE_URL)
    assert bare.post("/api/exports", json={"source": batch, "destination": ZIP}).status_code == 403
    for destination in ({"zip": True, "path": "C:\\x"}, {"zip": False}, {"zip": True, "token": "x" * 40}):
        assert harness.client.post("/api/exports", json={"source": batch, "destination": destination}
                                   ).status_code == 422


# --------------------------------------------------------------------------- limpeza


def test_expired_zip_is_removed_and_the_record_expires(harness, tmp_path):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    before = storage_snapshot(harness)
    export = zip_export(harness, library(ids))
    path = stored_zip(harness, export["id"])
    assert export_zip.sweep(harness.ctx) == 0 and path.is_file()   # dentro do prazo: fica
    harness.ctx.db.execute("UPDATE exports SET zip_expires_at = ? WHERE id = ?", (utc_now(-1), export["id"]))
    assert export_zip.sweep(harness.ctx) == 1 and not path.exists()
    expired = harness.client.get(f"/api/exports/{export['id']}").json()
    assert (expired["status"], expired["zip_ready"], expired["download_url"]) == ("expired", False, None)
    gone = harness.client.get(export["download_url"])
    assert gone.status_code == 410 and gone.json()["code"] == "export_zip_expired"
    assert storage_snapshot(harness) == before
    # Arquivo sumiu por fora antes do prazo: o download responde "expirado", sem erro interno.
    other = zip_export(harness, library(ids))
    stored_zip(harness, other["id"]).unlink()
    assert harness.client.get(other["download_url"]).status_code == 410
    assert harness.client.get(f"/api/exports/{other['id']}").json()["status"] == "expired"


def test_startup_cleans_interrupted_expired_and_orphan_zip_files(harness_factory, tmp_path):
    harness = harness_factory()
    prepare(harness, tmp_path)
    ids = seed_media(harness, 3)
    before = storage_snapshot(harness)
    ready = zip_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)})
    old = zip_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)})
    harness.ctx.db.execute("UPDATE exports SET zip_expires_at = ? WHERE id = ?", (utc_now(-1), old["id"]))
    stuck = start_zip(harness, {"kind": "batch", "id": seed_batch(harness, ids)})
    waiting = start_zip(harness, {"kind": "batch", "id": seed_batch(harness, ids)})
    harness.ctx.db.execute("UPDATE exports SET status = 'running' WHERE id = ?", (stuck["id"],))
    folder = zip_folder(harness)
    stuck_part = folder / (stored_zip(harness, stuck["id"]).name + ".part")
    stuck_part.write_bytes(b"zip pela metade")
    orphan = folder / "export-0123456789abcdef.zip"
    orphan.write_bytes(b"sem registro")

    restarted = harness_factory(root=harness.root)
    prepare(restarted, tmp_path)
    get = lambda export_id: restarted.client.get(f"/api/exports/{export_id}").json()   # noqa: E731
    assert (get(stuck["id"])["status"], get(stuck["id"])["error_code"]) == ("failed", "interrupted")
    assert get(old["id"])["status"] == "expired" and get(ready["id"])["zip_ready"] is True
    assert temp_files(restarted) == [stored_zip(restarted, ready["id"]).name]   # só o ZIP pronto e válido ficou
    assert restarted.client.get(get(ready["id"])["download_url"]).status_code == 200
    run_exports(restarted)
    assert get(waiting["id"])["status"] == "completed"   # a que estava na fila roda normalmente
    assert storage_snapshot(restarted) == before         # nada em storage/originals foi tocado


# --------------------------------------------------------------------------- revisão: limpeza segura e coerente


def expire(harness, export_id: int) -> None:
    harness.ctx.db.execute("UPDATE exports SET zip_expires_at = ? WHERE id = ?", (utc_now(-1), export_id))


def test_expired_zip_in_use_stays_eligible_until_it_can_be_removed(harness, tmp_path, monkeypatch):
    prepare(harness, tmp_path)
    export = zip_export(harness, library(seed_media(harness, 2)))
    path = stored_zip(harness, export["id"])
    expire(harness, export["id"])
    real_unlink = os.unlink
    with open(path, "rb") as in_use, monkeypatch.context() as scoped:   # download em andamento segura o arquivo
        def busy(target, *args, **kwargs):
            if os.fspath(target) == os.fspath(path):
                raise PermissionError(32, "arquivo em uso")
            return real_unlink(target, *args, **kwargs)

        scoped.setattr(export_zip.os, "unlink", busy)
        assert export_zip.sweep(harness.ctx) == 0
        assert in_use.read(2) == b"PK" and path.is_file()
        shown = harness.client.get(f"/api/exports/{export['id']}").json()
        assert (shown["status"], shown["zip_ready"]) == ("completed", True)   # registro coerente com o arquivo
        assert export_zip.sweep(harness.ctx) == 0                             # continua elegível, sem virar órfão
    assert export_zip.sweep(harness.ctx) == 1 and not path.exists()           # liberou: a varredura seguinte remove
    assert harness.client.get(f"/api/exports/{export['id']}").json()["status"] == "expired"
    assert temp_files(harness) == []


def test_sweep_only_touches_server_generated_regular_files(harness, tmp_path):
    prepare(harness, tmp_path)
    ready = zip_export(harness, library(seed_media(harness, 2)))
    folder = zip_folder(harness)
    notes = folder / "minhas-notas.txt"
    notes.write_text("do usuario", encoding="utf-8")
    lookalike = folder / "export-nao-e-hexadecimal.zip"
    lookalike.write_bytes(b"x")
    (folder / "export-00000000000000aa.zip").mkdir()                   # pasta com nome do padrão: não é arquivo
    orphan = folder / "export-0123456789abcdef.zip"
    orphan.write_bytes(b"orfao do servidor")
    orphan_part = folder / "export-fedcba9876543210.zip.part"
    orphan_part.write_bytes(b"orfao parcial")
    assert export_zip.sweep(harness.ctx) == 2                           # a varredura periódica também tira órfãos
    assert not orphan.exists() and not orphan_part.exists()
    assert notes.read_text(encoding="utf-8") == "do usuario" and lookalike.exists()
    assert (folder / "export-00000000000000aa.zip").is_dir() and stored_zip(harness, ready["id"]).is_file()

    # Registro adulterado apontando para um arquivo que NÃO tem o nome gerado pelo servidor (mesmo dentro da pasta):
    # não é servido nem apagado.
    harness.ctx.db.execute("UPDATE exports SET zip_path = 'minhas-notas.txt' WHERE id = ?", (ready["id"],))
    assert export_zip.zip_file(harness.settings, harness.ctx.exports.get(ready["id"])) is None
    served = harness.client.get(ready["download_url"])
    assert served.status_code == 410 and b"do usuario" not in served.content
    for evil in ("minhas-notas.txt", "..\\media-tool.db", "../media-tool.db", "export-0123456789abcdef.zip\\..\\x"):
        row = {"zip_path": evil}
        assert export_zip.zip_file(harness.settings, row) is None
        assert export_zip.zip_file(harness.settings, row, part=True) is None
        assert export_zip.servable_zip(harness.settings, row) is None
        assert export_zip.remove_zip_files(harness.settings, row) is True
    assert notes.exists() and harness.settings.database_path.is_file()


@pytest.mark.skipif(os.name != "nt", reason="junção do Windows")
def test_junction_in_place_of_the_temp_folder_is_never_followed(harness, tmp_path):
    import _winapi

    prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    ready = zip_export(harness, library(ids))
    folder = zip_folder(harness)
    stored_zip(harness, ready["id"]).unlink()
    folder.rmdir()
    outside = tmp_path / "fora"
    outside.mkdir()
    document = outside / "documento.txt"
    document.write_text("documento do usuario", encoding="utf-8")
    victim = outside / "export-0123456789abcdef.zip"
    victim.write_bytes(b"parece do servidor")
    _winapi.CreateJunction(str(outside), str(folder))
    expire(harness, ready["id"])
    assert export_zip.safe_zip_dir(harness.settings) is None
    assert export_zip.sweep(harness.ctx) == 0 and export_zip.sweep(harness.ctx, startup=True) == 0
    assert document.exists() and victim.exists()
    assert harness.ctx.exports.get(ready["id"])["status"] == "completed"   # nada limpo: o registro não mente
    refused = start_zip(harness, {"kind": "batch", "id": seed_batch(harness, ids)}, expect=500)
    assert refused["code"] == "internal_error" and sorted(p.name for p in outside.iterdir()) == [
        "documento.txt", "export-0123456789abcdef.zip"]
    os.rmdir(folder)   # remove só a junção


# --------------------------------------------------------------------------- revisão: espaço com fila


def test_queue_is_counted_and_space_is_rechecked_when_the_zip_starts(harness, tmp_path, monkeypatch):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 2, size=100_000)
    needed = export_zip.required_bytes(200_000)
    monkeypatch.setattr(export_zip, "reserve_bytes", lambda settings: 0)
    monkeypatch.setattr(export_zip, "free_bytes", lambda path: needed + 1000)       # cabe UM ZIP
    first = start_zip(harness, {"kind": "batch", "id": seed_batch(harness, ids)})
    second = start_zip(harness, {"kind": "batch", "id": seed_batch(harness, ids)}, expect=507)
    assert second["code"] == "export_no_space" and "reservados para ZIPs na fila" in second["detail"]
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM exports") == 1
    # O disco encheu enquanto a exportação esperava: rechecagem no início, falha limpa, nenhum .part criado.
    monkeypatch.setattr(export_zip, "free_bytes", lambda path: 0)
    opened = []
    real_open = export_runner._open_zip_part
    monkeypatch.setattr(export_runner, "_open_zip_part", lambda path: opened.append(path) or real_open(path))
    run_exports(harness)
    failed = harness.client.get(f"/api/exports/{first['id']}").json()
    assert (failed["status"], failed["error_code"], failed["copied"]) == ("failed", "export_no_space", 0)
    assert "necessários" in failed["error_message"] and opened == [] and temp_files(harness) == []
    monkeypatch.setattr(export_zip, "free_bytes", lambda path: needed + 1000)
    assert zip_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)})["status"] == "completed"


def test_tight_disk_removes_only_zips_that_were_already_downloaded(harness, tmp_path, monkeypatch):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    downloaded = zip_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)})
    untouched = zip_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)})
    assert harness.client.get(downloaded["download_url"]).status_code == 200
    assert export_zip.sweep(harness.ctx) == 0                                # disco folgado: os dois ficam
    monkeypatch.setattr(export_zip.disk, "disk_ok", lambda *args, **kwargs: (False, 1, 2))
    assert export_zip.sweep(harness.ctx) == 1
    get = lambda export_id: harness.client.get(f"/api/exports/{export_id}").json()   # noqa: E731
    assert get(downloaded["id"])["status"] == "expired" and get(untouched["id"])["zip_ready"] is True
    assert temp_files(harness) == [stored_zip(harness, untouched["id"]).name]


# --------------------------------------------------------------------------- revisão: datas, memória, Range, vazio


def test_dates_outside_the_zip_range_do_not_break_the_zip(harness, tmp_path, monkeypatch):
    prepare(harness, tmp_path)
    ids = [*seed_media(harness, 1, profile="futuro", taken_at="2200-01-01T00:00:00Z"),
           *seed_media(harness, 1, profile="antigo", taken_at="1970-01-01T00:00:00Z", start=1),
           *seed_media(harness, 1, profile="lixo", taken_at="data-invalida", start=2),
           *seed_media(harness, 1, profile="normal", start=3)]
    export = zip_export(harness, library(ids))
    assert (export["status"], export["copied"], export["failed"]) == ("completed", 4, 0)
    with zipfile.ZipFile(stored_zip(harness, export["id"])) as archive:
        dates = {info.filename.split("_")[0]: info.date_time for info in archive.infolist()}
        assert archive.testzip() is None
    assert dates["futuro"] == (2107, 12, 31, 23, 59, 58) and dates["antigo"] == (1980, 1, 1, 0, 0, 0)
    assert dates["normal"] == (2026, 2, 3, 10, 0, 0) and 1980 <= dates["lixo"][0] <= 2107

    # Cabeçalho que não pode ser montado para UMA mídia: ela conta como falha e o ZIP segue.
    real_time = export_runner._zip_time
    monkeypatch.setattr(export_runner, "_zip_time", lambda media, src: (
        (2200, 1, 1, 0, 0, 0) if media["source_profile"] == "futuro" else real_time(media, src)))
    again = zip_export(harness, library(ids))
    assert (again["status"], again["copied"], again["failed"], again["processed"]) == ("completed", 3, 1, 4)
    with zipfile.ZipFile(stored_zip(harness, again["id"])) as archive:
        assert len(assert_flat_and_unique(archive)) == 3 and archive.testzip() is None


def test_large_original_is_streamed_in_blocks_not_read_whole(harness, tmp_path):
    prepare(harness, tmp_path)
    size = 64 * 1024 * 1024
    ids = seed_media(harness, 1, profile="grande", size=size)
    created = start_zip(harness, library(ids))
    tracemalloc.start()
    try:
        run_exports(harness)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["copied"]) == ("completed", 1) and export["zip_bytes"] > size
    assert peak < 8 * 1024 * 1024, peak   # blocos de 1 MiB: o pico fica muito abaixo dos 64 MiB do arquivo


def test_only_a_response_that_reaches_the_last_byte_counts_as_downloaded(harness, tmp_path):
    prepare(harness, tmp_path)
    export = zip_export(harness, library(seed_media(harness, 2)))
    url, size = export["download_url"], export["zip_bytes"]
    downloaded_at = lambda: harness.ctx.exports.get(export["id"])["zip_downloaded_at"]   # noqa: E731
    for partial in ("bytes=0-0", f"bytes=0-{size - 2}", "bytes=10-20", "bytes=0-0,5-9", "itens=0-"):
        response = harness.client.get(url, headers={"Range": partial})
        assert response.status_code in (200, 206, 400, 416), partial
        if partial != "itens=0-" and "," not in partial:
            assert response.status_code == 206
        assert downloaded_at() is None, partial                       # parcial: o prazo de 24 h não encurta
    shown = harness.client.get(f"/api/exports/{export['id']}").json()
    assert (parse_utc(shown["zip_expires_at"]) - parse_utc(utc_now())).total_seconds() > 23 * 3600
    tail = harness.client.get(url, headers={"Range": "bytes=5-"})       # retomada que vai até o último byte
    assert tail.status_code == 206 and len(tail.content) == size - 5 and downloaded_at() is not None
    minutes = (parse_utc(harness.ctx.exports.get(export["id"])["zip_expires_at"])
               - parse_utc(utc_now())).total_seconds() / 60
    assert 59 < minutes <= 60
    for reaches_end in (None, "bytes=0-", f"bytes=3-{size - 1}", "bytes=-10", f"bytes=0-{size + 50}"):
        assert export_service._range_reaches_end(reaches_end, size) is True, reaches_end


def test_zip_without_any_available_original_fails_clearly(harness, tmp_path):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 3)
    for media_id in ids:
        original_of(harness, media_id).unlink()
    export = zip_export(harness, library(ids))
    assert (export["status"], export["error_code"], export["missing"]) == ("failed", "export_empty", 3)
    assert "Nenhum arquivo original disponível" in export["error_message"]
    assert export["zip_ready"] is False and export["download_url"] is None and temp_files(harness) == []


def test_read_error_in_the_middle_of_an_original_explains_and_cleans_up(harness, tmp_path, monkeypatch):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 3, size=3 * 1024 * 1024)
    broken = original_of(harness, ids[1])

    class Unreadable:
        def __init__(self, handle):
            self.handle, self.reads = handle, 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def read(self, size):
            self.reads += 1
            if self.reads == 2:
                raise OSError(5, "erro de leitura")
            return self.handle.read(size)

    def fake_open(path, mode="r", *args, **kwargs):
        handle = open(path, mode, *args, **kwargs)
        return Unreadable(handle) if os.fspath(path) == os.fspath(broken) and mode == "rb" else handle

    monkeypatch.setattr(export_runner, "open", fake_open, raising=False)
    export = zip_export(harness, library(ids))
    assert (export["status"], export["error_code"]) == ("failed", "export_read_failed")
    assert "não pôde ser lido" in export["error_message"] and "Tente de novo" in export["error_message"]
    assert temp_files(harness) == []


def test_exports_can_be_listed_by_kind_and_not_found_message(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    from_batch = zip_export(harness, {"kind": "batch", "id": seed_batch(harness, ids)})
    from_library = zip_export(harness, library(ids))
    folder = start_export(harness, library(ids), dest)
    listed = harness.client.get("/api/exports", params={"kind": "library", "limit": 5}).json()["items"]
    assert [item["id"] for item in listed] == [folder["id"], from_library["id"]]
    assert listed[1]["zip_ready"] is True and listed[1]["download_url"] == from_library["download_url"]
    assert [item["id"] for item in harness.client.get("/api/exports", params={"kind": "batch"}).json()["items"]] == [
        from_batch["id"]]
    assert harness.client.get("/api/exports", params={"kind": "outro"}).status_code == 422
    missing = harness.client.get("/api/exports/999")
    assert missing.status_code == 404 and missing.json()["detail"] == "Exportação não encontrada."
