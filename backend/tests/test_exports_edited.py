"""Exportação das versões editadas/processadas (`content`: originals | edited | both) pelo fluxo de exportação.

Sem `content` tudo continua como antes (mesmo corpo, mesmos nomes, mesmas contagens). Destinos em pastas temporárias
do pytest; seletor de pasta sempre falso; sem rede.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile

from app.db.connection import utc_now
from app.jobs import export_runner
from app.services import export_zip
from app.services.export_names import MAX_NAME_LENGTH
from export_helpers import (
    EDIT_PARAMS,
    export_to,
    files_in,
    pick,
    prepare,
    run_exports,
    seed_batch,
    seed_edit_batch,
    seed_media,
    seed_rendition,
    sha256_of,
    storage_snapshot,
)
from media_fakes import MAGIC, remote

ZIP = {"zip": True}
STEM = "semente_2026-02-03_S{i:05d}"
EDIT_TAG = "_editado_15s_1.5x"
OTHER_PARAMS = {**EDIT_PARAMS, "speed": 1.0, "max_duration": 30.03, "sharpen": True}   # _editado_30s_nitido


def post_export(harness, source: dict, destination: dict, content: str | None = None, *, expect: int = 202) -> dict:
    body = {"source": source, "destination": destination}
    if content is not None:
        body["content"] = content
    response = harness.client.post("/api/exports", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def finished(harness, created: dict) -> dict:
    run_exports(harness)
    return harness.client.get(f"/api/exports/{created['id']}").json()


def zip_entries(harness, export: dict) -> dict[str, bytes]:
    response = harness.client.get(export["download_url"])
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == len({name.casefold() for name in names})   # nenhum nome repetido
        assert all("/" not in name and "\\" not in name for name in names)   # plano
        return {name: archive.read(name) for name in names}


def folder_export(harness, source: dict, folder, content: str | None = None) -> dict:
    token = pick(harness, folder)["token"]
    return finished(harness, post_export(harness, source, {"token": token}, content))


def stored_bytes(harness, relative: str) -> bytes:
    return harness.ctx.storage.resolve(relative).read_bytes()


def edit_batch_with_two_outputs(harness):
    """3 mídias num lote de Edição: as 2 primeiras com versão, a 3a falhou (fora do lote)."""
    ids = seed_media(harness, 3)
    renditions = [seed_rendition(harness, media_id, params=EDIT_PARAMS) for media_id in ids[:2]]
    batch_id = seed_edit_batch(harness, [(ids[0], renditions[0]), (ids[1], renditions[1]), (ids[2], None)],
                               params=EDIT_PARAMS)
    return ids, renditions, batch_id


# --------------------------------------------------------------------------- padrão inalterado


def test_default_export_unchanged_without_content(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    harness.downloader.items = [remote("IMG", profile="alpha"), remote("VID", media_type="video", profile="alpha"),
                                remote("CAR", index=0, profile="alpha"), remote("CAR", index=1, profile="alpha")]
    batch = harness.import_profile(profile="alpha")
    harness.run_all()
    media = {row["source_post_id"] + str(row["source_media_index"]): dict(row)
             for row in harness.ctx.db.fetchall("SELECT * FROM media")}
    # Versões existentes não mudam a exportação de originais.
    seed_rendition(harness, media["IMG0"]["id"], params=EDIT_PARAMS)
    expected = ["alpha_2026-01-01_CAR_1.jpg", "alpha_2026-01-01_CAR_2.jpg", "alpha_2026-01-01_IMG.jpg",
                "alpha_2026-01-01_VID.mp4"]

    outputs = []
    for label, content in (("sem", None), ("explicito", "originals")):
        folder = tmp_path / label
        folder.mkdir()
        export = folder_export(harness, {"kind": "batch", "id": batch["id"]}, folder, content)
        assert (export["status"], export["total"], export["copied"], export["content"]) == (
            "completed", 4, 4, "originals")
        assert files_in(folder) == expected
        outputs.append({name: (folder / name).read_bytes() for name in files_in(folder)})
        assert harness.ctx.db.scalar("SELECT selection FROM exports WHERE id = ?", (export["id"],)) is None
    assert outputs[0] == outputs[1]
    by_name = {"alpha_2026-01-01_IMG.jpg": media["IMG0"], "alpha_2026-01-01_VID.mp4": media["VID0"]}
    for name, row in by_name.items():
        assert outputs[0][name] == stored_bytes(harness, row["original_path"])

    # Biblioteca e ZIP sem `content`: `selection` sem a chave nova, nome do ZIP e entradas como antes.
    ids = [row["id"] for row in media.values()]
    library = finished(harness, post_export(harness, {"kind": "library", "selection": {"media_ids": ids}}, ZIP))
    assert (library["status"], library["total"], library["copied"], library["content"]) == (
        "completed", 4, 4, "originals")
    assert re.fullmatch(r"biblioteca-\d{4}-\d{2}-\d{2}-\d{4}\.zip", library["zip_name"])
    assert harness.ctx.exports.get(library["id"])["selection"] == {"mode": "ids", "count": 4}
    assert sorted(zip_entries(harness, library)) == expected
    zipped = finished(harness, post_export(harness, {"kind": "batch", "id": batch["id"]}, ZIP))
    assert re.fullmatch(r"importacao-\d{4}-\d{2}-\d{2}-\d{4}\.zip", zipped["zip_name"])
    assert zip_entries(harness, zipped) == outputs[0]


def test_default_group_export_unchanged_without_content(harness, tmp_path):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 3)
    group = harness.ctx.batches.create_group(content_type="all", requested_limit=None, preset_id=None,
                                             preset_name=None, params=None, process=False)
    child = harness.ctx.batches.create(source="instagram_import", status="completed", source_profile="semente",
                                       group_id=group, group_position=0)
    with harness.ctx.db.transaction() as conn:
        conn.executemany("INSERT INTO batch_media (batch_id, media_id) VALUES (?, ?)",
                         [(child, media_id) for media_id in ids])
    seed_rendition(harness, ids[0], params=EDIT_PARAMS)
    export = finished(harness, post_export(harness, {"kind": "group", "id": group}, ZIP))
    assert (export["total"], export["copied"], export["content"]) == (3, 3, "originals")
    assert sorted(zip_entries(harness, export)) == [STEM.format(i=i) + ".jpg" for i in range(3)]
    assert harness.ctx.db.scalar("SELECT selection FROM exports WHERE id = ?", (export["id"],)) is None
    # Grupo de download apenas: nenhuma versão é do grupo.
    empty = post_export(harness, {"kind": "group", "id": group}, ZIP, "edited", expect=409)
    assert empty["code"] == "export_empty" and "versões editadas" in empty["detail"]


# --------------------------------------------------------------------------- lote de Edição


def test_zip_edited_of_edit_batch_and_download(harness, tmp_path):
    prepare(harness, tmp_path)
    ids, renditions, batch_id = edit_batch_with_two_outputs(harness)
    before = storage_snapshot(harness)
    # Sem `content`, um lote de Edição também exporta (os originais das mídias processadas).
    plain = finished(harness, post_export(harness, {"kind": "batch", "id": batch_id}, ZIP))
    assert (plain["total"], plain["copied"]) == (2, 2)
    assert re.fullmatch(r"edicao-\d{4}-\d{2}-\d{2}-\d{4}\.zip", plain["zip_name"])   # lote de Edição, não importação

    created = post_export(harness, {"kind": "batch", "id": batch_id}, ZIP, "edited")
    assert (created["total"], created["content"]) == (2, "edited")
    assert harness.ctx.exports.get(created["id"])["selection"] == {"content": "edited"}
    export = finished(harness, created)
    assert (export["status"], export["total"], export["copied"], export["processed"]) == ("completed", 2, 2, 2)
    assert re.fullmatch(r"editados-\d{4}-\d{2}-\d{2}-\d{4}\.zip", export["zip_name"])
    response = harness.client.get(export["download_url"])
    assert response.headers["content-disposition"] == f'attachment; filename="{export["zip_name"]}"'
    entries = zip_entries(harness, export)
    assert sorted(entries) == [STEM.format(i=0) + EDIT_TAG + ".mp4", STEM.format(i=1) + EDIT_TAG + ".mp4"]
    for i, rendition_id in enumerate(renditions):
        rendition = harness.ctx.media.get_rendition(rendition_id)
        assert entries[STEM.format(i=i) + EDIT_TAG + ".mp4"] == stored_bytes(harness, rendition["processed_path"])
    originals = {harness.ctx.media.get(media_id)["original_sha256"] for media_id in ids}

    assert not {hashlib.sha256(data).hexdigest() for data in entries.values()} & originals
    assert storage_snapshot(harness) == before


def test_both_has_every_file_once_and_no_repeated_names(harness, tmp_path):
    prepare(harness, tmp_path)
    ids, renditions, batch_id = edit_batch_with_two_outputs(harness)
    export = finished(harness, post_export(harness, {"kind": "batch", "id": batch_id}, ZIP, "both"))
    assert (export["status"], export["total"], export["copied"]) == ("completed", 4, 4)
    entries = zip_entries(harness, export)
    assert sorted(entries) == sorted([STEM.format(i=0) + ".jpg", STEM.format(i=1) + ".jpg",
                                      STEM.format(i=0) + EDIT_TAG + ".mp4", STEM.format(i=1) + EDIT_TAG + ".mp4"])

    expected = {harness.ctx.media.get(media_id)["original_sha256"] for media_id in ids[:2]} | {
        harness.ctx.media.get_rendition(rendition_id)["processed_sha256"] for rendition_id in renditions}
    digests = [hashlib.sha256(data).hexdigest() for data in entries.values()]
    assert sorted(digests) == sorted(expected) and len(set(digests)) == 4   # cada arquivo do storage uma vez


def test_batch_edited_picks_only_that_batch_and_library_takes_all(harness, tmp_path):
    prepare(harness, tmp_path)
    (media_id,) = seed_media(harness, 1)
    mine = seed_rendition(harness, media_id, params=EDIT_PARAMS)
    seed_rendition(harness, media_id, params=OTHER_PARAMS)
    preset = harness.client.get("/api/presets").json()[1]
    seed_rendition(harness, media_id, params=preset["params"], preset_id=preset["id"], preset_name=preset["name"])
    first = seed_edit_batch(harness, [(media_id, mine)], params=EDIT_PARAMS)
    # Outro lote com os mesmos params que REAPROVEITOU a versão (job `skipped` apontando para a do primeiro).
    reused = seed_edit_batch(harness, [(media_id, mine)], params=EDIT_PARAMS, stage="skipped")

    for batch_id in (first, reused):
        export = finished(harness, post_export(harness, {"kind": "batch", "id": batch_id}, ZIP, "edited"))
        assert (export["total"], export["copied"]) == (1, 1)
        assert sorted(zip_entries(harness, export)) == [STEM.format(i=0) + EDIT_TAG + ".mp4"]

    library = finished(harness, post_export(
        harness, {"kind": "library", "selection": {"media_ids": [media_id]}}, ZIP, "edited"))
    assert (library["total"], library["copied"]) == (3, 3)
    assert harness.ctx.exports.get(library["id"])["selection"] == {"mode": "ids", "count": 1, "content": "edited"}
    assert sorted(zip_entries(harness, library)) == sorted([
        STEM.format(i=0) + EDIT_TAG + ".mp4", STEM.format(i=0) + "_editado_30s_nitido.mp4",
        STEM.format(i=0) + f"_{preset['name']}.mp4"])


def test_group_edited_takes_the_outputs_of_each_child_batch(harness, tmp_path):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    first = seed_rendition(harness, ids[0], params=EDIT_PARAMS)
    second = seed_rendition(harness, ids[1], params=OTHER_PARAMS)
    seed_rendition(harness, ids[1], params=EDIT_PARAMS)   # params do filho 1, mas a mídia 1 não está nele: fica fora
    group = harness.ctx.batches.create_group(content_type="all", requested_limit=None, preset_id=None,
                                             preset_name="Edição", params=None, process=True)
    seed_edit_batch(harness, [(ids[0], first)], params=EDIT_PARAMS, group_id=group)
    seed_edit_batch(harness, [(ids[1], second)], params=OTHER_PARAMS, group_id=group)
    export = finished(harness, post_export(harness, {"kind": "group", "id": group}, ZIP, "edited"))
    # Filho 1 (EDIT_PARAMS) só tem a mídia 0; filho 2 (OTHER_PARAMS) só a mídia 1.
    assert (export["total"], export["copied"]) == (2, 2)
    assert sorted(zip_entries(harness, export)) == sorted([STEM.format(i=0) + EDIT_TAG + ".mp4",
                                                           STEM.format(i=1) + "_editado_30s_nitido.mp4"])


def test_import_with_preset_exports_its_processed_versions(harness, tmp_path):
    """Params do lote e da rendition são o mesmo JSON canônico também na importação com preset."""
    prepare(harness, tmp_path)
    harness.downloader.items = [remote("P1", media_type="video"), remote("P2")]
    batch = harness.import_profile(process=True)
    harness.run_all()
    renditions = harness.ctx.db.fetchall("SELECT * FROM renditions ORDER BY id")
    assert len(renditions) == 2
    export = finished(harness, post_export(harness, {"kind": "batch", "id": batch["id"]}, ZIP, "edited"))
    assert (export["status"], export["total"], export["copied"]) == ("completed", 2, 2)

    entries = zip_entries(harness, export)
    assert sorted(hashlib.sha256(data).hexdigest() for data in entries.values()) == sorted(
        row["processed_sha256"] for row in renditions)
    assert all(f"_{batch['preset_name']}" in name for name in entries)


# --------------------------------------------------------------------------- nomes, ausentes, pasta


def test_edited_name_collision_and_long_profile(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    (media_id,) = seed_media(harness, 1)
    seed_rendition(harness, media_id, params=EDIT_PARAMS)
    seed_rendition(harness, media_id, params=EDIT_PARAMS)   # preset_id NULL: o índice único não deduplica
    (long_id,) = seed_media(harness, 1, profile="p" * 110, start=5)
    seed_rendition(harness, long_id, params=EDIT_PARAMS)
    export = folder_export(harness, {"kind": "library", "selection": {"media_ids": [media_id, long_id]}}, dest,
                           "edited")
    assert (export["status"], export["total"], export["copied"], export["renamed"]) == ("completed", 3, 3, 1)
    names = files_in(dest)
    assert STEM.format(i=0) + EDIT_TAG + ".mp4" in names and STEM.format(i=0) + EDIT_TAG + " (2).mp4" in names
    (long_name,) = [name for name in names if name.startswith("ppp")]
    assert len(long_name) <= MAX_NAME_LENGTH and long_name.endswith(EDIT_TAG + ".mp4")


def test_edited_missing_file_counts_missing_and_continues(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids, renditions, batch_id = edit_batch_with_two_outputs(harness)
    gone = harness.ctx.media.get_rendition(renditions[0])
    harness.ctx.storage.resolve(gone["processed_path"]).unlink()
    export = folder_export(harness, {"kind": "batch", "id": batch_id}, dest, "edited")
    assert (export["status"], export["total"], export["processed"], export["copied"], export["missing"]) == (
        "completed", 2, 2, 1, 1)
    # A versão ausente nunca cai para o original: só a outra versão está na pasta.
    assert files_in(dest) == [STEM.format(i=1) + EDIT_TAG + ".mp4"]

    # Versão apagada entre o pedido e a cópia: conta como ausente e processed == total.
    harness.ctx.db.execute("DELETE FROM renditions WHERE id = ?", (renditions[0],))
    other = tmp_path / "outra"
    other.mkdir()
    token = pick(harness, other)["token"]
    created = post_export(harness, {"kind": "batch", "id": batch_id}, {"token": token}, "edited")
    assert created["total"] == 1
    harness.ctx.db.execute("DELETE FROM renditions WHERE id = ?", (renditions[1],))
    late = finished(harness, created)
    assert (late["status"], late["total"], late["processed"], late["missing"]) == ("completed", 1, 1, 1)


def test_edited_folder_mode_twice_skips_identical_and_leaves_no_parts(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids, renditions, batch_id = edit_batch_with_two_outputs(harness)
    first = folder_export(harness, {"kind": "batch", "id": batch_id}, dest, "both")
    assert (first["status"], first["total"], first["copied"]) == ("completed", 4, 4)
    snapshot = {name: sha256_of(dest / name) for name in files_in(dest)}
    second = folder_export(harness, {"kind": "batch", "id": batch_id}, dest, "both")
    assert (second["status"], second["copied"], second["skipped_existing"]) == ("completed", 0, 4)
    assert {name: sha256_of(dest / name) for name in files_in(dest)} == snapshot
    assert snapshot[STEM.format(i=0) + ".jpg"] == harness.ctx.media.get(ids[0])["original_sha256"]
    assert not any(name.endswith(export_runner.TEMP_SUFFIX) for name in os.listdir(dest))


def test_edited_empty_batch_is_409_and_both_takes_originals(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    seed_rendition(harness, ids[0], params=EDIT_PARAMS)   # versão de outro contexto: não é do lote
    batch_id = seed_batch(harness, ids)   # lote só de download (sem params)
    for destination in (ZIP, {"token": pick(harness, dest)["token"]}):
        refused = post_export(harness, {"kind": "batch", "id": batch_id}, destination, "edited", expect=409)
        assert refused["code"] == "export_empty"
        assert refused["detail"] == "Este lote não tem versões editadas ou processadas."
    both = finished(harness, post_export(harness, {"kind": "batch", "id": batch_id}, ZIP, "both"))
    assert (both["total"], both["copied"]) == (2, 2)
    assert sorted(zip_entries(harness, both)) == [STEM.format(i=0) + ".jpg", STEM.format(i=1) + ".jpg"]


class _CancelOnWrite:
    def __init__(self, raw, on_write):
        self._raw, self._on_write = raw, on_write

    def write(self, data):
        self._on_write(len(data))
        return self._raw.write(data)

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._raw.close()


def test_cancel_edited_zip_removes_the_part(harness, tmp_path, monkeypatch):
    prepare(harness, tmp_path)
    ids = seed_media(harness, 2)
    small = seed_rendition(harness, ids[0], params=EDIT_PARAMS)
    big = seed_rendition(harness, ids[1], params=EDIT_PARAMS, size=5 * 1024 * 1024)
    batch_id = seed_edit_batch(harness, [(ids[0], small), (ids[1], big)], params=EDIT_PARAMS)
    before = storage_snapshot(harness)
    created = post_export(harness, {"kind": "batch", "id": batch_id}, ZIP, "edited")
    chunks = []

    def on_write(size):
        if size == export_runner.CHUNK_SIZE:
            chunks.append(size)
            if len(chunks) == 2:
                assert harness.client.post(f"/api/exports/{created['id']}/cancel").status_code == 200

    real_open = export_runner._open_zip_part
    with monkeypatch.context() as scoped:
        scoped.setattr(export_runner, "_open_zip_part", lambda path: _CancelOnWrite(real_open(path), on_write))
        run_exports(harness)
    export = harness.client.get(f"/api/exports/{created['id']}").json()
    assert (export["status"], export["copied"], export["zip_ready"]) == ("cancelled", 1, False)
    folder = export_zip.zip_dir(harness.settings)
    assert (sorted(os.listdir(folder)) if folder.is_dir() else []) == []   # nenhum .part nem .zip
    assert len(chunks) == 2 and storage_snapshot(harness) == before


# --------------------------------------------------------------------------- registros antigos e retenção


def test_old_export_rows_without_content_still_run_as_originals(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids, renditions, batch_id = edit_batch_with_two_outputs(harness)
    plain_batch = seed_batch(harness, ids)
    folder_a, folder_b = tmp_path / "a", tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    old_batch = harness.ctx.exports.create(kind="batch", batch_id=plain_batch, dest_path=str(folder_a.resolve()))
    old_library = harness.ctx.exports.create(kind="library", selection={"mode": "ids", "count": 3}, item_ids=ids,
                                             dest_path=str(folder_b.resolve()))
    run_exports(harness)
    for export, folder in ((old_batch, folder_a), (old_library, folder_b)):
        shown = harness.client.get(f"/api/exports/{export['id']}").json()
        assert (shown["status"], shown["total"], shown["copied"], shown["content"]) == (
            "completed", 3, 3, "originals")
        assert files_in(folder) == [STEM.format(i=i) + ".jpg" for i in range(3)]
    assert files_in(dest) == []


def test_purge_keeps_process_batch_media(harness, tmp_path):
    dest = prepare(harness, tmp_path)
    ids, renditions, batch_id = edit_batch_with_two_outputs(harness)
    # Lote posterior nas mesmas mídias: os jobs do primeiro deixam de ser "o último de cada mídia".
    later = [seed_rendition(harness, media_id, params=OTHER_PARAMS) for media_id in ids[:2]]
    seed_edit_batch(harness, list(zip(ids[:2], later)), params=OTHER_PARAMS)
    assert harness.ctx.jobs.purge_finished_before(utc_now(60)) >= 2
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND kind = 'process'"
                                 " AND status = 'completed'", (batch_id,)) == 0
    pairs = harness.ctx.db.fetchall("SELECT media_id FROM batch_media WHERE batch_id = ? ORDER BY media_id",
                                    (batch_id,))
    assert [row["media_id"] for row in pairs] == ids[:2]
    export = folder_export(harness, {"kind": "batch", "id": batch_id}, dest, "edited")
    assert (export["status"], export["total"], export["copied"]) == ("completed", 2, 2)
    assert files_in(dest) == [STEM.format(i=i) + EDIT_TAG + ".mp4" for i in range(2)]


# --------------------------------------------------------------------------- rodada de correções


def test_both_zip_of_edit_batch_uses_the_edit_prefix(harness, tmp_path):
    prepare(harness, tmp_path)
    _, _, batch_id = edit_batch_with_two_outputs(harness)
    both = finished(harness, post_export(harness, {"kind": "batch", "id": batch_id}, ZIP, "both"))
    assert re.fullmatch(r"edicao-\d{4}-\d{2}-\d{2}-\d{4}\.zip", both["zip_name"])
    # Lote de importação (download apenas) continua `importacao-`.
    plain = finished(harness, post_export(harness, {"kind": "batch", "id": seed_batch(harness, seed_media(
        harness, 1, start=50))}, ZIP))
    assert re.fullmatch(r"importacao-\d{4}-\d{2}-\d{2}-\d{4}\.zip", plain["zip_name"])


def test_batch_takes_the_same_rendition_the_reused_job_points_to(harness, tmp_path):
    """Duas versões com a mesma chave (preset_id NULL não deduplica no índice): entra a de MENOR id, a mesma que
    `find_rendition` devolve e para a qual o job reaproveitado aponta."""
    prepare(harness, tmp_path)
    (media_id,) = seed_media(harness, 1)
    first = seed_rendition(harness, media_id, params=EDIT_PARAMS)
    seed_rendition(harness, media_id, params=EDIT_PARAMS)
    assert harness.ctx.media.find_rendition(media_id, None, EDIT_PARAMS)["id"] == first
    batch_id = seed_edit_batch(harness, [(media_id, first)], params=EDIT_PARAMS, stage="skipped")
    export = finished(harness, post_export(harness, {"kind": "batch", "id": batch_id}, ZIP, "edited"))
    assert (export["total"], export["copied"]) == (1, 1)
    (data,) = zip_entries(harness, export).values()
    assert data == stored_bytes(harness, harness.ctx.media.get_rendition(first)["processed_path"])


def _seed_local(harness, filename: str, serial: int) -> int:
    payload = MAGIC["video"] + f"LOCAL-{serial}".encode()
    sha = hashlib.sha256(payload).hexdigest()
    relative = f"originals/2026/01/{sha[:2]}/{sha}.mp4"
    path = harness.ctx.storage.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return harness.ctx.media.insert({
        "source_platform": "local", "source_profile": None, "source_post_id": None, "source_media_index": 0,
        "media_type": "video", "original_filename": filename, "original_path": relative, "original_sha256": sha,
        "file_size": len(payload), "taken_at": None,
    })


def test_local_uploads_use_the_uploaded_name(harness, tmp_path):
    prepare(harness, tmp_path)
    ids = [_seed_local(harness, "clip.mp4", 1), _seed_local(harness, "clip.mp4", 2)]
    params = {**EDIT_PARAMS, "speed": 1.25, "sharpen": True}
    outputs = [seed_rendition(harness, media_id, params=params) for media_id in ids]
    batch_id = seed_edit_batch(harness, list(zip(ids, outputs)), params=params)
    export = finished(harness, post_export(harness, {"kind": "batch", "id": batch_id}, ZIP, "edited"))
    assert (export["copied"], export["renamed"]) == (2, 1)
    assert sorted(zip_entries(harness, export)) == ["clip_editado_15s_1.25x_nitido (2).mp4",
                                                    "clip_editado_15s_1.25x_nitido.mp4"]
    both = finished(harness, post_export(harness, {"kind": "batch", "id": batch_id}, ZIP, "both"))
    assert sorted(zip_entries(harness, both)) == ["clip (2).mp4", "clip.mp4", "clip_editado_15s_1.25x_nitido (2).mp4",
                                                  "clip_editado_15s_1.25x_nitido.mp4"]
