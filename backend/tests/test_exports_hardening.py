"""Validação final da exportação: ZIP64, download de arquivo grande, concorrência, falhas e formato plano."""

from __future__ import annotations

import errno
import hashlib
import os
import subprocess
import threading
import tracemalloc
import zipfile

import pytest

from app.jobs import export_runner
from app.services import export_destination, export_service, export_time, export_zip
from export_helpers import (
    export_to,
    files_in,
    original_of,
    pick,
    prepare,
    run_exports,
    seed_batch,
    seed_media,
    start_export,
    storage_snapshot,
)
from import_group_fakes import ProfilesDownloader
from media_fakes import remote
from test_exports import run_group
from test_exports_zip import FullDisk, library, start_zip, stored_zip, temp_files, zip_export

GIB = 1024 ** 3


def library_state(harness) -> tuple:
    """Biblioteca inteira: arquivos do storage (tamanho, mtime, SHA) e as linhas de media/jobs/batches/batch_media."""
    tables = tuple(
        tuple(tuple(row) for row in harness.ctx.db.fetchall(f"SELECT * FROM {table} ORDER BY 1, 2"))
        for table in ("media", "jobs", "batches", "batch_media"))
    return storage_snapshot(harness), tables


# --------------------------------------------------------------------------- ZIP64


def test_every_entry_is_written_with_zip64_local_headers(harness, tmp_path):
    prepare(harness, tmp_path)
    export = zip_export(harness, library(seed_media(harness, 3)))
    raw = stored_zip(harness, export["id"]).read_bytes()
    with zipfile.ZipFile(stored_zip(harness, export["id"])) as archive:
        infos = archive.infolist()
    for info in infos:
        header = raw[info.header_offset:info.header_offset + 30]
        assert header[:4] == b"PK\x03\x04" and int.from_bytes(header[4:6], "little") == 45   # version needed 4.5
        # Com force_zip64 os tamanhos de 32 bits do cabeçalho local ficam 0xFFFFFFFF e os reais vão no extra 0x0001.
        assert header[18:22] == header[22:26] == b"\xff\xff\xff\xff"
        name_len, extra_len = int.from_bytes(header[26:28], "little"), int.from_bytes(header[28:30], "little")
        extra = raw[info.header_offset + 30 + name_len:info.header_offset + 30 + name_len + extra_len]
        assert extra[:2] == b"\x01\x00" and int.from_bytes(extra[4:12], "little") == info.file_size


def test_sizes_above_4_gib_use_64_bit_values_end_to_end(harness, tmp_path):
    # Cabeçalho de uma entrada > 4 GiB: campos de 32 bits saturados e valores reais no extra ZIP64 (sem gravar 4 GiB).
    info = zipfile.ZipInfo("grande.mp4", date_time=(2026, 1, 1, 0, 0, 0))
    info.file_size = info.compress_size = 5 * GIB
    info.header_offset = 6 * GIB
    info.CRC = 0
    header = info.FileHeader(zip64=True)
    assert header[18:22] == header[22:26] == b"\xff\xff\xff\xff"
    extra = header[30 + len("grande.mp4"):]
    assert extra[:2] == b"\x01\x00" and int.from_bytes(extra[4:12], "little") == 5 * GIB
    # Estimativa de espaço, formatação e API com inteiros acima de 2^32.
    assert export_zip.required_bytes(18 * GIB) == 18 * GIB + int(18 * GIB * 0.01) + 64 * 1024 ** 2
    assert export_zip.format_bytes(18 * GIB) == "18,0 GB" and export_zip.format_bytes(3 * 1024 ** 4) == "3,0 TB"
    prepare(harness, tmp_path)
    export = zip_export(harness, library(seed_media(harness, 1)))
    harness.ctx.db.execute("UPDATE exports SET zip_bytes = ? WHERE id = ?", (19_756_849_562, export["id"]))
    assert harness.client.get(f"/api/exports/{export['id']}").json()["zip_bytes"] == 19_756_849_562
    harness.ctx.db.execute("UPDATE media SET file_size = ?", (9 * GIB,))
    assert harness.ctx.exports.sum_bytes(harness.ctx.exports.get(export["id"])) == 9 * GIB


@pytest.mark.slow
def test_more_than_65535_entries_write_the_zip64_end_records(harness, tmp_path):
    prepare(harness, tmp_path)
    total = 66_000
    first = seed_media(harness, 1, profile="muitas")[0]
    path = harness.ctx.media.get(first)["original_path"]
    size = original_of(harness, first).stat().st_size
    with harness.ctx.db.transaction() as conn:   # 65.999 linhas apontando para o mesmo original pequeno (poucos MB)
        conn.executemany(
            "INSERT INTO media (source_platform, source_profile, source_post_id, source_media_index, media_type,"
            " original_path, original_sha256, file_size, taken_at, created_at, updated_at)"
            " VALUES ('instagram', 'muitas', ?, 0, 'image', ?, ?, ?, '2026-02-03T10:00:00Z', 'x', 'x')",
            ((f"M{i:06d}", path, hashlib.sha256(str(i).encode()).hexdigest(), size) for i in range(1, total)))
    created = start_zip(harness, {"kind": "library", "selection": {"filter": {"view": "all"}}})
    assert created["total"] == total
    run_exports(harness)
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["copied"], export["failed"]) == ("completed", total, 0)
    zip_path = stored_zip(harness, export["id"])
    with open(zip_path, "rb") as handle:
        handle.seek(-200, os.SEEK_END)
        tail = handle.read()
    assert b"PK\x06\x06" in tail and b"PK\x06\x07" in tail and tail[-22:-18] == b"PK\x05\x06"   # ZIP64 EOCD + localizador
    assert tail[-12:-10] == b"\xff\xff"   # contagem de 16 bits saturada: a real está no registro ZIP64
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert len(names) == total == len({name.casefold() for name in names})
    assert export["zip_bytes"] == zip_path.stat().st_size < 64 * 1024 ** 2


# --------------------------------------------------------------------------- download


def make_sparse(path, size: int) -> bool:
    """Troca o arquivo por um ESPARSO de `size` bytes (quase nenhum disco ocupado). False se não for possível."""
    if os.name != "nt":
        return False
    import ctypes

    path.write_bytes(b"")
    if subprocess.run(["fsutil", "sparse", "setflag", str(path)], capture_output=True, check=False).returncode != 0:
        return False
    with open(path, "r+b") as handle:
        handle.seek(size - 4)
        handle.write(b"FIM!")
    high = ctypes.c_ulong(0)
    get_size = ctypes.windll.kernel32.GetCompressedFileSizeW
    get_size.restype = ctypes.c_ulong
    allocated = (high.value << 32) + get_size(str(path), ctypes.byref(high))
    allocated = (high.value << 32) + (allocated & 0xFFFFFFFF)
    if allocated > 16 * 1024 ** 2:   # o volume não honrou o esparso: não ocupa disco de verdade no teste
        path.unlink()
        return False
    return True


def test_download_of_a_file_larger_than_4_gib_is_served_by_ranges_without_loading_it(harness, tmp_path):
    prepare(harness, tmp_path)
    export = zip_export(harness, library(seed_media(harness, 1)))
    size = 5 * GIB + 123
    path = stored_zip(harness, export["id"])
    if not make_sparse(path, size):
        harness.ctx.exports.set_expired(export["id"])
        pytest.skip("arquivo esparso indisponível neste volume (o teste não grava GBs reais)")
    try:
        harness.ctx.db.execute("UPDATE exports SET zip_bytes = ? WHERE id = ?", (size, export["id"]))
        url = harness.client.get(f"/api/exports/{export['id']}").json()["download_url"]
        tracemalloc.start()
        try:
            first = harness.client.get(url, headers={"Range": "bytes=0-1048575"})
            middle = harness.client.get(url, headers={"Range": f"bytes={4 * GIB + 7}-{4 * GIB + 1048582}"})
            last = harness.client.get(url, headers={"Range": f"bytes={size - 4}-"})
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert first.status_code == middle.status_code == last.status_code == 206
        assert first.headers["content-range"] == f"bytes 0-1048575/{size}" and len(first.content) == 1048576
        assert middle.headers["content-range"] == f"bytes {4 * GIB + 7}-{4 * GIB + 1048582}/{size}"   # offsets > 32 bits
        assert last.content == b"FIM!" and last.headers["content-range"] == f"bytes {size - 4}-{size - 1}/{size}"
        assert peak < 16 * 1024 ** 2, peak   # nunca o arquivo na RAM
        assert harness.ctx.exports.get(export["id"])["zip_downloaded_at"] is not None   # a faixa final chegou ao fim
        invalid = harness.client.get(url, headers={"Range": f"bytes={size + 10}-"})
        assert invalid.status_code == 416
    finally:
        path.unlink(missing_ok=True)


def test_download_headers_repeated_and_simultaneous_downloads(harness, tmp_path):
    prepare(harness, tmp_path)
    export = zip_export(harness, library(seed_media(harness, 4, size=300_000)))
    before = library_state(harness)
    url, size = export["download_url"], export["zip_bytes"]
    first = harness.client.get(url)
    disposition = first.headers["content-disposition"]
    assert disposition == f'attachment; filename="{export["zip_name"]}"' and disposition.isascii()
    assert "\r" not in disposition and "\n" not in disposition and disposition.count('"') == 2
    assert first.headers["accept-ranges"] == "bytes" and int(first.headers["content-length"]) == size
    assert harness.client.get(url, headers={"Range": f"bytes={size + 5}-{size + 9}"}).status_code == 416
    digest = hashlib.sha256(first.content).hexdigest()
    # "Baixar novamente": 2º e 3º downloads completos, e duas conexões ao mesmo tempo, com a varredura rodando.
    results: list[str] = []

    def download():
        response = harness.client.get(url)
        results.append(hashlib.sha256(response.content).hexdigest() if response.status_code == 200 else "erro")

    threads = [threading.Thread(target=download) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert export_zip.sweep(harness.ctx) == 0   # dentro da validade: a varredura não tira o arquivo
    for thread in threads:
        thread.join(30)
    assert results == [digest, digest] and hashlib.sha256(harness.client.get(url).content).hexdigest() == digest
    assert stored_zip(harness, export["id"]).is_file() and library_state(harness) == before


# --------------------------------------------------------------------------- concorrência


def test_two_simultaneous_requests_create_exactly_one_export(harness, tmp_path):
    prepare(harness, tmp_path)
    batch = {"kind": "batch", "id": seed_batch(harness, seed_media(harness, 3))}
    barrier = threading.Barrier(2)
    responses = []

    def post():
        barrier.wait(10)
        responses.append(harness.client.post("/api/exports", json={"source": batch, "destination": {"zip": True}}))

    threads = [threading.Thread(target=post) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(r.status_code for r in responses) == [202, 409]
    created = next(r.json() for r in responses if r.status_code == 202)
    refused = next(r.json() for r in responses if r.status_code == 409)
    assert refused["code"] == "export_already_running" and refused["export_id"] == created["id"]
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM exports") == 1


def test_exports_do_not_share_temporaries_state_or_cancellation(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 6, size=200_000)
    one, two = ({"kind": "batch", "id": seed_batch(harness, ids)} for _ in range(2))
    zip_a, zip_b = start_zip(harness, one), start_zip(harness, two)
    folder_a = start_export(harness, one, dest)   # mesma origem de zip_a, outro modo
    rows = {row["id"]: row for row in map(harness.ctx.exports.get, (zip_a["id"], zip_b["id"]))}
    assert rows[zip_a["id"]]["zip_path"] != rows[zip_b["id"]]["zip_path"]
    assert rows[zip_a["id"]]["zip_token"] != rows[zip_b["id"]]["zip_token"]

    # A em execução, B na fila: cancelar B não afeta A; depois, cancelar A (em execução) não afeta os demais.
    real_open = export_runner._open_zip_part
    state = {"writes": 0}

    def on_write(size):
        state["writes"] += 1
        if state["writes"] == 3:
            assert harness.client.post(f"/api/exports/{zip_b['id']}/cancel").json()["status"] == "cancelled"

    with monkeypatch.context() as scoped:
        scoped.setattr(export_runner, "_open_zip_part", lambda path: FullDisk(real_open(path), None, on_write))
        assert harness.ctx.export_runner.run_next() is True
    done_a = harness.client.get(f"/api/exports/{zip_a['id']}").json()
    assert (done_a["status"], done_a["copied"]) == ("completed", 6)
    assert temp_files(harness) == [rows[zip_a["id"]]["zip_path"]]
    run_exports(harness)
    done_folder = harness.client.get(f"/api/exports/{folder_a['id']}").json()
    assert (done_folder["status"], done_folder["copied"]) == ("completed", 6) and len(files_in(dest)) == 6

    zip_c, zip_d = start_zip(harness, one), start_zip(harness, two)
    with monkeypatch.context() as scoped:
        scoped.setattr(export_runner, "_open_zip_part", lambda path: FullDisk(real_open(path), 300_000))   # A falha
        assert harness.ctx.export_runner.run_next() is True
    failed = harness.client.get(f"/api/exports/{zip_c['id']}").json()
    assert (failed["status"], failed["error_code"]) == ("failed", "export_disk_full")
    run_exports(harness)
    other = harness.client.get(f"/api/exports/{zip_d['id']}").json()
    assert (other["status"], other["copied"], other["failed"]) == ("completed", 6, 0)   # contadores isolados
    assert stored_zip(harness, zip_a["id"]).is_file() and stored_zip(harness, zip_d["id"]).is_file()
    assert len(temp_files(harness)) == 2


def test_two_sources_exported_to_the_same_folder_do_not_overwrite_each_other(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    shared = seed_media(harness, 2, profile="comum")
    only_a = seed_media(harness, 2, profile="a", start=10)
    only_b = seed_media(harness, 2, profile="b", start=20)
    first = start_export(harness, {"kind": "batch", "id": seed_batch(harness, [*shared, *only_a])}, dest)
    second = start_export(harness, {"kind": "batch", "id": seed_batch(harness, [*shared, *only_b])}, dest)
    user_file = dest / "a_2026-02-03_S00010.jpg"
    user_file.write_bytes(b"arquivo do usuario com o mesmo nome")
    run_exports(harness)
    get = lambda export_id: harness.client.get(f"/api/exports/{export_id}").json()   # noqa: E731
    assert (get(first["id"])["copied"], get(first["id"])["renamed"]) == (4, 1)
    assert (get(second["id"])["copied"], get(second["id"])["skipped_existing"]) == (2, 2)   # as comuns já estavam
    assert user_file.read_bytes() == b"arquivo do usuario com o mesmo nome"
    assert len(files_in(dest)) == 7 and not any(name.endswith("part") for name in files_in(dest))


# --------------------------------------------------------------------------- falhas: Biblioteca intacta + tentar de novo


class Unreadable:
    def __init__(self, handle):
        self.handle, self.reads = handle, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handle.close()

    def read(self, size):
        self.reads += 1
        if self.reads >= 2:
            raise OSError(5, "erro de leitura")
        return self.handle.read(size)


@pytest.mark.parametrize("mode", ["zip", "folder"])
@pytest.mark.parametrize("fault", ["missing_before", "read_error", "enospc", "cancel_pending", "cancel_running"])
def test_failures_leave_the_library_identical_and_retry_works(harness, tmp_path, monkeypatch, mode, fault):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 4, size=2 * 1024 * 1024 + 17)
    harness.downloader.items = [remote("J1"), remote("J2")]   # linhas reais em jobs/batches também
    harness.import_profile()
    harness.run_all()
    source = {"kind": "batch", "id": seed_batch(harness, ids)}
    if fault == "missing_before":
        original_of(harness, ids[1]).unlink()   # original indisponível antes de ser aberto
    before = library_state(harness)

    def start():
        return start_zip(harness, source) if mode == "zip" else start_export(harness, source, dest)

    created = start()
    with monkeypatch.context() as scoped:
        real_zip, real_part = export_runner._open_zip_part, export_runner._open_part
        if fault == "read_error":
            broken = os.fspath(original_of(harness, ids[2]))
            scoped.setattr(export_runner, "open", lambda path, mode_="r", *a, **k: (
                Unreadable(open(path, mode_, *a, **k)) if os.fspath(path) == broken and mode_ == "rb"
                else open(path, mode_, *a, **k)), raising=False)
        elif fault == "enospc":
            scoped.setattr(export_runner, "_open_zip_part", lambda path: FullDisk(real_zip(path), 3 * 1024 * 1024))
            scoped.setattr(export_runner, "_open_part", lambda path: FullDisk(real_part(path), 1024 * 1024))
        elif fault == "cancel_pending":
            assert harness.client.post(f"/api/exports/{created['id']}/cancel").json()["status"] == "cancelled"
        elif fault == "cancel_running":
            cancel = lambda size: export_service.cancel_export(harness.ctx, created["id"]) if (   # noqa: E731
                harness.ctx.exports.get(created["id"])["cancel_requested"] == 0) else None
            scoped.setattr(export_runner, "_open_zip_part", lambda path: FullDisk(real_zip(path), None, cancel))
            scoped.setattr(export_runner, "_open_part", lambda path: FullDisk(real_part(path), None, cancel))
        run_exports(harness)
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    expected = {
        "missing_before": ("completed", None),
        # ZIP: a entrada já começou e não pode ser retirada -> falha inteira. Pasta: só aquela mídia falha, e a
        # exportação conclui com o motivo informado.
        "read_error": ("failed", "export_read_failed") if mode == "zip" else ("completed", "export_read_failed"),
        "enospc": ("failed", "export_disk_full"),
        "cancel_pending": ("cancelled", None),
        "cancel_running": ("cancelled", None),
    }[fault]
    assert (export["status"], export["error_code"]) == expected, export
    if fault == "read_error" and mode == "folder":
        assert "1 arquivo não pôde ser lido do original" in export["error_message"]
    if export["status"] != "completed":
        assert export["error_message"] and "Biblioteca não foi alterada" in export["error_message"]
        assert export["zip_ready"] is False and export["download_url"] is None
    if fault == "missing_before":
        assert export["missing"] == 1 and export["processed"] == export["total"]
    if fault == "read_error" and mode == "folder":
        assert (export["failed"], export["copied"]) == (1, 3)
    # Nenhum temporário incompleto fica para trás nem é confundido com pronto.
    leftovers = [name for name in (*temp_files(harness), *files_in(dest)) if name.endswith("part")]
    assert leftovers == []
    assert library_state(harness) == before   # arquivos e linhas idênticos

    retry = start()   # tentar novamente: a mesma origem volta a aceitar o pedido e conclui
    run_exports(harness)
    again = harness.client.get(f"/api/exports/{retry['id']}").json()
    assert again["status"] == "completed" and again["processed"] == again["total"] == 4
    assert library_state(harness) == before


def test_incomplete_zip_part_is_never_served_as_ready(harness, tmp_path):
    prepare(harness, tmp_path)
    created = start_zip(harness, library(seed_media(harness, 2)))
    row = harness.ctx.exports.get(created["id"])
    folder = export_zip.safe_zip_dir(harness.settings, create=True)
    (folder / (row["zip_path"] + ".part")).write_bytes(b"PK incompleto")
    url = f"/api/exports/{created['id']}/download?token={row['zip_token']}"
    assert harness.client.get(url).json()["code"] == "export_zip_not_ready"
    harness.ctx.db.execute("UPDATE exports SET status = 'completed' WHERE id = ?", (created["id"],))
    gone = harness.client.get(url)   # registro "pronto" sem o .zip final: o .part não é servido no lugar
    assert gone.status_code == 410 and b"incompleto" not in gone.content and temp_files(harness) == []


# --------------------------------------------------------------------------- formato plano (regressão)


def test_exports_are_flat_for_zip_and_folder(harness_factory, tmp_path):
    downloader = ProfilesDownloader(profiles={
        "perfil.um": [remote("U1", profile="perfil.um"), remote("CAR", index=0, profile="perfil.um"),
                      remote("CAR", index=1, profile="perfil.um")],
        "perfil_dois": [remote("D1", profile="perfil_dois", media_type="video")],
    })
    harness = harness_factory(downloader=downloader)
    dest = prepare(harness, tmp_path)
    created = harness.client.post("/api/import-groups", json={
        "profiles": ["perfil.um", "perfil_dois"], "content_type": "all", "limit": 10, "preset_id": None,
        "process": False})
    run_group(harness, created.json()["group"]["id"])
    for index, taken_at in enumerate(("2024-12-31T23:10:00Z", "2025-06-15T12:00:00Z", "2026-10-06T08:00:00Z")):
        seed_media(harness, 1, profile="2026", code=f"originals{index}", taken_at=taken_at, start=100 + index)
    seed_media(harness, 1, profile="p" * 150, code="AAA", start=200)
    seed_media(harness, 1, profile="p" * 150, code="BBB", start=201)   # mesmo nome depois do corte: colisão
    before = storage_snapshot(harness)
    source = {"kind": "library", "selection": {"filter": {"view": "all"}}}
    taken = dest / "perfil.um_2026-01-01_U1.jpg"
    taken.write_bytes(b"arquivo diferente que ja estava na pasta")

    folder = export_to(harness, source, dest)
    zipped = zip_export(harness, source)
    with zipfile.ZipFile(stored_zip(harness, zipped["id"])) as archive:
        entries = archive.namelist()
        assert not any(info.is_dir() for info in archive.infolist())
    assert folder["total"] == zipped["total"] == zipped["copied"] == 9 and folder["copied"] == 9
    # Via scandir com o prefixo de caminho longo: o teste não depende do tamanho do diretório temporário.
    with os.scandir(export_destination.io_path(dest)) as scan:
        on_disk = list(scan)
    assert len(on_disk) == 10 and all(entry.is_file(follow_symlinks=False) for entry in on_disk)   # profundidade 1
    for name in (*entries, *files_in(dest)):
        assert "/" not in name and "\\" not in name and os.path.basename(name) == name
        assert not name.startswith(("2026/", "originals/", "perfil.um/", "storage/"))
    assert len(entries) == 9 == len({name.casefold() for name in entries})    # ZIP: nenhuma entrada duplicada
    assert len(files_in(dest)) == 10 and taken.read_bytes() == b"arquivo diferente que ja estava na pasta"
    assert "perfil.um_2026-01-01_U1 (2).jpg" in files_in(dest) and (folder["renamed"], zipped["renamed"]) == (2, 1)
    assert storage_snapshot(harness) == before


# --------------------------------------------------------------------------- revisão final


def test_interrupted_download_is_not_marked_as_downloaded(harness, tmp_path):
    import anyio

    from app.db.connection import parse_utc, utc_now

    prepare(harness, tmp_path)
    export = zip_export(harness, library(seed_media(harness, 4, size=600_000)))
    path, token = export["download_url"].split("?token=")
    received = {"bytes": 0, "blocks": 0}

    async def run() -> None:
        drop = anyio.Event()
        first = {"done": False}

        async def receive():
            if not first["done"]:
                first["done"] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await drop.wait()
            return {"type": "http.disconnect"}   # o navegador fechou a conexão no meio

        async def send(message):
            if message["type"] == "http.response.body":
                received["bytes"] += len(message.get("body", b""))
                received["blocks"] += 1
                if received["blocks"] == 2:
                    drop.set()
                    await anyio.sleep(0.05)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
                 "path": path, "raw_path": path.encode(), "root_path": "", "query_string": f"token={token}".encode(),
                 "headers": [(b"host", b"127.0.0.1:8765")], "client": ("127.0.0.1", 50000),
                 "server": ("127.0.0.1", 8765), "app": harness.app}
        await harness.app(scope, receive, send)

    anyio.run(run)
    row = harness.ctx.exports.get(export["id"])
    assert 0 < received["bytes"] < export["zip_bytes"]            # o envio parou ao detectar a desconexão
    assert row["zip_downloaded_at"] is None                        # interrompido NÃO conta como baixado
    assert (parse_utc(row["zip_expires_at"]) - parse_utc(utc_now())).total_seconds() > 23 * 3600   # 24 h mantidas
    assert stored_zip(harness, export["id"]).is_file()
    again = harness.client.get(export["download_url"])             # um novo download funciona e, completo, marca
    assert again.status_code == 200 and len(again.content) == export["zip_bytes"]
    assert harness.ctx.exports.get(export["id"])["zip_downloaded_at"] is not None


def test_folder_export_where_nothing_could_be_written_fails_with_a_reason(harness, tmp_path, monkeypatch):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 6)
    source = {"kind": "batch", "id": seed_batch(harness, ids)}
    real_open = export_runner._open_part
    state = {"calls": 0, "deny": lambda count: True}

    def denied(path):
        state["calls"] += 1
        if state["deny"](state["calls"]):
            raise PermissionError(13, "acesso negado")
        return real_open(path)

    monkeypatch.setattr(export_runner, "_open_part", denied)
    nothing = export_to(harness, source, dest)
    assert (nothing["status"], nothing["error_code"], nothing["failed"], nothing["copied"]) == (
        "failed", "export_write_failed", 6, 0)
    assert nothing["error_message"] == (
        "Nenhum arquivo pôde ser gravado na pasta de destino: acesso negado. A Biblioteca não foi alterada.")
    state.update(calls=0, deny=lambda count: count in (2, 4, 5))
    partial = export_to(harness, source, dest)
    assert (partial["status"], partial["error_code"], partial["copied"], partial["failed"]) == (
        "completed", "export_write_failed", 3, 3)
    assert partial["error_message"].startswith("3 arquivos não puderam ser gravados na pasta de destino: acesso negado.")
    state["deny"] = lambda count: False
    retry = export_to(harness, source, dest)   # tentar de novo: copia só o que faltou
    assert (retry["status"], retry["error_code"], retry["copied"], retry["skipped_existing"]) == (
        "completed", None, 3, 3)


def test_extreme_dates_never_break_names_or_zip_entries(harness, tmp_path):
    for value in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"):
        assert export_time.local_date_text(value, None) is None
        assert export_time.local_date_text(value, export_time.system_zone()) is None
        stamp = export_runner._zip_time({"taken_at": value}, tmp_path, export_time.system_zone())
        assert 1980 <= stamp[0] <= 2107
    dest = prepare(harness, tmp_path)
    ids = [*seed_media(harness, 1, profile="ano1", code="A", taken_at="0001-01-01T00:00:00+14:00"),
           *seed_media(harness, 1, profile="ano9999", code="B", taken_at="9999-12-31T23:59:59-14:00", start=1)]
    folder = export_to(harness, library(ids), dest)
    zipped = zip_export(harness, library(ids))
    assert (folder["status"], folder["copied"], zipped["status"], zipped["copied"]) == ("completed", 2, "completed", 2)
    assert files_in(dest) == ["ano1_A.jpg", "ano9999_B.jpg"]   # nome sem data


def test_exported_file_mtime_is_the_publication_time(harness, tmp_path):
    from datetime import UTC, datetime

    dest = prepare(harness, tmp_path)
    ids = [*seed_media(harness, 1, profile="com", code="D", taken_at="2018-12-31T01:30:00Z"),
           *seed_media(harness, 1, profile="sem", code="N", taken_at=None, start=1)]
    export_to(harness, library(ids), dest)
    assert (dest / "com_2018-12-31_D.jpg").stat().st_mtime == datetime(2018, 12, 31, 1, 30, tzinfo=UTC).timestamp()
    assert abs((dest / "sem_N.jpg").stat().st_mtime - original_of(harness, ids[1]).stat().st_mtime) < 2


@pytest.mark.skipif(export_time.iana_zone("Asia/Tokyo") is None, reason="base IANA (tzdata) ausente")
def test_configured_timezone_reaches_the_real_export(harness_factory, tmp_path):
    harness = harness_factory(timezone="Asia/Tokyo")   # como MEDIA_TOOL_TIMEZONE=Asia/Tokyo; resolve_zone real
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 1, profile="toquio", code="T", taken_at="2026-03-04T23:59:00Z")
    export_to(harness, library(ids), dest)
    zipped = zip_export(harness, library(ids))
    with zipfile.ZipFile(stored_zip(harness, zipped["id"])) as archive:
        info = archive.infolist()[0]
    assert files_in(dest) == ["toquio_2026-03-05_T.jpg"] == [info.filename]
    assert info.date_time == (2026, 3, 5, 8, 59, 0)


# --------------------------------------------------------------------------- ZIP64 pelo código do app, acima de 4 GiB


class CountingSink:
    """Destino que só conta: guarda as escritas pequenas (cabeçalhos, diretório central) por posição e descarta os
    blocos de dados. Aceita `seek`, como o arquivo real, para o `zipfile` regravar o cabeçalho local ao fechar a
    entrada. Nenhum byte vai para o disco."""

    def __init__(self) -> None:
        self.pos = 0
        self.size = 0
        self.small: dict[int, bytes] = {}

    def write(self, data) -> int:
        length = len(data)
        if length <= 64 * 1024:
            self.small[self.pos] = bytes(data)
        self.pos += length
        self.size = max(self.size, self.pos)
        return length

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset if whence == 0 else (self.pos + offset if whence == 1 else self.size + offset)
        return self.pos

    def seekable(self) -> bool:
        return True

    def flush(self) -> None:
        pass

    def tail(self) -> bytes:
        return b"".join(self.small[key] for key in sorted(self.small) if key >= self.size - 8 * 1024 ** 2)


@pytest.mark.slow
def test_app_writes_an_entry_above_4_gib_and_entries_after_it_as_zip64(harness, tmp_path):
    """Passa pelo `_zip_one` do app com um original ESPARSO de 4 GiB + 5 MiB e um destino que só conta."""
    prepare(harness, tmp_path)
    ids = seed_media(harness, 3)
    big = 4 * 1024 ** 3 + 5 * 1024 ** 2
    if not make_sparse(original_of(harness, ids[1]), big):
        pytest.skip("o volume de teste não suporta arquivo esparso")
    runner = harness.ctx.export_runner
    sink, used = CountingSink(), set()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        results = [runner._zip_one(archive, used, harness.ctx.media.get(media_id), False) for media_id in ids]
        infos = list(archive.filelist)
    assert results == ["copied", "copied", "copied"] and sink.size > big
    assert [info.file_size for info in infos][1] == big and infos[2].header_offset > 0xFFFFFFFF
    for info in infos:   # cabeçalho local regravado ao fechar a entrada: versão 4.5 e tamanhos de 32 bits saturados
        header = sink.small[info.header_offset]
        assert header[:4] == b"PK\x03\x04" and int.from_bytes(header[4:6], "little") == 45
        assert header[18:22] == header[22:26] == b"\xff\xff\xff\xff"
        name_len = int.from_bytes(header[26:28], "little")
        extra = header[30 + name_len:]
        assert extra[:2] == b"\x01\x00" and int.from_bytes(extra[4:12], "little") == info.file_size
    tail = sink.tail()
    assert b"PK\x06\x06" in tail and b"PK\x06\x07" in tail, "registro final ZIP64 e localizador"
    central = tail[tail.index(b"PK\x01\x02"):]
    entries = central.split(b"PK\x01\x02")[1:4]
    assert len(entries) == 3
    # Entrada grande: tamanhos de 32 bits saturados; entrada gravada depois de 4 GiB: offset de 32 bits saturado.
    assert entries[1][16:20] == entries[1][20:24] == b"\xff\xff\xff\xff"
    assert entries[2][38:42] == b"\xff\xff\xff\xff"
