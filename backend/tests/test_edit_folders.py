"""Página Editar: pastas de vídeos (GET /api/edit/folders, GET /api/edit/folders/{key}/media) e exclusão em massa
(POST /api/media/delete). Só banco e storage temporários do harness."""

from __future__ import annotations

import hashlib
from datetime import datetime

from export_helpers import EDIT_PARAMS, pick, prepare, seed_edit_batch, seed_rendition, start_export, storage_snapshot
from media_fakes import MAGIC, remote

_serial = iter(range(1, 10_000))


def _videos(harness, count: int, *, platform: str = "instagram", profile: str | None = "perfil",
            created_at: str | None = None, media_type: str = "video") -> list[int]:
    """Mídias de vídeo (ou imagem) com arquivo no storage de teste; `created_at` opcional (UTC ISO)."""
    storage = harness.ctx.storage
    ids = []
    for _ in range(count):
        data = MAGIC[media_type] + f"EDIT-FOLDER-{next(_serial)}".encode()
        sha = hashlib.sha256(data).hexdigest()
        relative = f"originals/2026/01/{sha[:2]}/{sha}.mp4"
        (storage.root / relative).parent.mkdir(parents=True, exist_ok=True)
        (storage.root / relative).write_bytes(data)
        media_id = harness.ctx.media.insert({
            "source_platform": platform, "source_profile": profile, "media_type": media_type,
            "original_filename": f"cdn-{sha[:8]}.mp4", "original_path": relative, "original_sha256": sha,
            "file_size": len(data),
        })
        if created_at:
            harness.ctx.db.execute("UPDATE media SET created_at = ? WHERE id = ?", (created_at, media_id))
        ids.append(media_id)
    return ids


def _batch(harness, media_ids: list[int], *, profile: str | None = "perfil", status: str = "completed",
           group_id: int | None = None, position: int | None = None, import_mode: str | None = None,
           source: str = "instagram_import", created_at: str | None = None) -> int:
    batch_id = harness.ctx.batches.create(source=source, status=status, source_profile=profile, group_id=group_id,
                                          group_position=position, import_mode=import_mode)
    with harness.ctx.db.transaction() as conn:
        conn.executemany("INSERT INTO batch_media (batch_id, media_id) VALUES (?, ?)",
                         [(batch_id, media_id) for media_id in media_ids])
        if created_at:
            conn.execute("UPDATE batches SET created_at = ? WHERE id = ?", (created_at, batch_id))
    return batch_id


def _group(harness, created_at: str) -> int:
    group_id = harness.ctx.batches.create_group(content_type="all", requested_limit=None, preset_id=None,
                                                preset_name=None, params=None, process=False)
    harness.ctx.db.execute("UPDATE import_groups SET created_at = ? WHERE id = ?", (created_at, group_id))
    return group_id


def _local_day(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().date().isoformat()


def _folders(harness) -> dict[str, dict]:
    response = harness.client.get("/api/edit/folders")
    assert response.status_code == 200, response.text
    return {f["key"]: f for f in response.json()["items"]}


def _folder_ids(harness, key: str, **params) -> list[int]:
    response = harness.client.get(f"/api/edit/folders/{key}/media", params=params)
    assert response.status_code == 200, response.text
    return [m["id"] for m in response.json()["items"]]


def _delete(harness, body: dict, *, expect: int = 200) -> dict:
    response = harness.client.post("/api/media/delete", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def _db_snapshot(harness) -> dict[str, list]:
    tables = ("media", "renditions", "batches", "batch_media", "import_groups", "jobs")
    return {t: [tuple(r) for r in harness.ctx.db.fetchall(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in tables}


def _scenario(harness) -> dict:
    """Grupo (2 perfis), lote por perfil sem grupo, link, cancelado com mídias, uploads em 2 dias, órfão e imagens."""
    s: dict = {}
    s["g1"], s["g2"] = _videos(harness, 2, profile="ana"), _videos(harness, 1, profile="bia")
    group = _group(harness, "2026-10-06T12:00:00.000Z")
    _batch(harness, s["g1"] + _videos(harness, 1, media_type="image"), profile="ana", group_id=group, position=1)
    _batch(harness, s["g2"], profile="bia", group_id=group, position=2)
    s["group"] = group
    s["solo"] = _videos(harness, 2, profile="carla")
    s["solo_batch"] = _batch(harness, s["solo"], profile="carla", created_at="2026-10-07T12:00:00.000Z")
    s["link"] = _videos(harness, 1, profile="dani")
    s["link_batch"] = _batch(harness, s["link"], profile=None, import_mode="links",
                             created_at="2026-10-09T12:00:00.000Z")
    s["cancelled"] = _videos(harness, 1, profile="eva")
    s["cancelled_batch"] = _batch(harness, s["cancelled"] + s["solo"][:1], profile="eva", status="cancelled",
                                  created_at="2026-10-08T12:00:00.000Z")
    _batch(harness, [], profile="vazio", created_at="2026-10-10T12:00:00.000Z")  # sem vídeos: omitido
    _batch(harness, _videos(harness, 1, media_type="image"), profile="fotos", created_at="2026-10-11T12:00:00.000Z")
    s["day1_iso"], s["day2_iso"] = "2026-10-20T15:00:00.000Z", "2026-10-22T15:00:00.000Z"
    s["day1"] = _videos(harness, 2, platform="local", profile=None, created_at=s["day1_iso"])
    s["day2"] = _videos(harness, 1, platform="local", profile=None, created_at=s["day2_iso"])
    s["orphan"] = _videos(harness, 1, profile="solto")
    # Vídeo só num lote de Edição (library): lote de Edição não vira pasta e o vídeo fica em "Outros".
    s["edited_orphan"] = _videos(harness, 1, profile="editado")
    _batch(harness, s["edited_orphan"], source="library", profile=None)
    return s


def test_folders_one_per_import_and_upload_day(harness):
    s = _scenario(harness)
    seed_rendition(harness, s["solo"][0], params=EDIT_PARAMS)
    before = _db_snapshot(harness)
    folders = _folders(harness)
    day1, day2 = _local_day(s["day1_iso"]), _local_day(s["day2_iso"])
    expected_keys = {f"group:{s['group']}", f"batch:{s['solo_batch']}", f"batch:{s['link_batch']}",
                     f"batch:{s['cancelled_batch']}", f"local:{day1}", f"local:{day2}", "other"}
    assert set(folders) == expected_keys

    group = folders[f"group:{s['group']}"]
    assert (group["kind"], group["import_mode"], group["profiles"], group["video_count"]) == (
        "group", "profile", ["ana", "bia"], 3)  # a imagem do grupo não conta
    assert group["created_at"] == "2026-10-06T12:00:00.000Z"
    solo = folders[f"batch:{s['solo_batch']}"]
    assert (solo["kind"], solo["import_mode"], solo["profiles"], solo["video_count"]) == (
        "batch", "profile", ["carla"], 2)
    assert solo["rendition_count"] == 1
    solo_bytes = sum(harness.ctx.media.get(i)["file_size"] for i in s["solo"])
    solo_bytes += harness.ctx.db.scalar("SELECT SUM(file_size) FROM renditions")
    assert solo["total_bytes"] == solo_bytes
    link = folders[f"batch:{s['link_batch']}"]
    assert (link["import_mode"], link["profiles"], link["video_count"]) == ("links", [], 1)
    cancelled = folders[f"batch:{s['cancelled_batch']}"]
    assert (cancelled["video_count"], cancelled["shared_count"]) == (2, 1)  # reimportou um vídeo do lote solo
    assert solo["shared_count"] == 1 and group["shared_count"] == 0
    assert folders[f"local:{day1}"]["video_count"] == 2 and folders[f"local:{day1}"]["day"] == day1
    assert folders[f"local:{day2}"]["video_count"] == 1
    assert folders["other"]["video_count"] == 2  # órfão + o que só está num lote de Edição

    order = [f["key"] for f in harness.client.get("/api/edit/folders").json()["items"]]
    assert order[-1] == "other"
    assert order[:5] == [f"local:{day2}", f"local:{day1}", f"batch:{s['link_batch']}",
                         f"batch:{s['cancelled_batch']}", f"batch:{s['solo_batch']}"]

    assert set(_folder_ids(harness, f"group:{s['group']}")) == set(s["g1"] + s["g2"])
    assert _folder_ids(harness, f"local:{day1}") == sorted(s["day1"], reverse=True)
    assert set(_folder_ids(harness, "other")) == set(s["orphan"] + s["edited_orphan"])
    assert set(_folder_ids(harness, f"batch:{s['cancelled_batch']}")) == set(s["cancelled"] + s["solo"][:1])
    assert _db_snapshot(harness) == before  # só leitura


def test_folder_media_paginates_and_searches(harness):
    ids = _videos(harness, 5, profile="carla")
    _videos(harness, 1, profile="outra")  # fora da pasta
    batch = _batch(harness, ids, profile="carla")
    first = harness.client.get(f"/api/edit/folders/batch:{batch}/media", params={"limit": 2}).json()
    assert [m["id"] for m in first["items"]] == ids[::-1][:2] and first["total"] == 5
    second = harness.client.get(f"/api/edit/folders/batch:{batch}/media",
                                params={"limit": 2, "before_id": first["next_before_id"]}).json()
    assert [m["id"] for m in second["items"]] == ids[::-1][2:4]
    name = harness.ctx.media.get(ids[2])["original_filename"]
    assert _folder_ids(harness, f"batch:{batch}", q=name) == [ids[2]]
    assert _folder_ids(harness, f"batch:{batch}", q="outra") == []
    assert len(_folder_ids(harness, f"batch%3A{batch}")) == 5  # chave codificada (encodeURIComponent)


def test_folder_from_real_import_uses_ingest_jobs(harness):
    harness.downloader.items = [remote("V1", media_type="video"), remote("V2", media_type="video"), remote("IMG")]
    batch = harness.import_profile()
    harness.run_all()
    folder = _folders(harness)[f"batch:{batch['id']}"]
    assert (folder["video_count"], folder["profiles"]) == (2, ["perfil_teste"])
    assert len(_folder_ids(harness, f"batch:{batch['id']}")) == 2


def test_invalid_folder_key_is_rejected(harness):
    for key in ("lote:1", "group:0", "local:ontem", "batch:1;DROP"):
        assert harness.client.get(f"/api/edit/folders/{key}/media").status_code == 422, key
    assert _folder_ids(harness, "batch:999") == []


def test_range_requests_on_original(harness):
    media_id = _videos(harness, 1)[0]
    response = harness.client.get(f"/api/media/{media_id}/original", headers={"Range": "bytes=0-3"})
    assert response.status_code == 206 and len(response.content) == 4


# ---- exclusão -------------------------------------------------------------------------------------------------
def test_delete_requires_confirmation(harness):
    ids = _videos(harness, 1)
    for body in ({"media_ids": ids}, {"media_ids": ids, "delete_original": False}):
        assert _delete(harness, body, expect=400)["code"] == "confirmation_required"
    assert _delete(harness, {"delete_original": True}, expect=422)
    assert _delete(harness, {"media_ids": ids, "folder": "other", "delete_original": True}, expect=422)
    assert harness.ctx.media.get(ids[0]) is not None


def test_delete_one_and_selection_remove_files_and_versions(harness):
    ids = _videos(harness, 3)
    rendition = seed_rendition(harness, ids[0], params=EDIT_PARAMS)
    files = [harness.ctx.storage.resolve(harness.ctx.media.get(i)["original_path"]) for i in ids]
    processed = harness.ctx.storage.resolve(harness.ctx.media.get_rendition(rendition)["processed_path"])

    one = _delete(harness, {"media_ids": [ids[0]], "delete_original": True})
    assert (one["deleted"], one["deleted_ids"], one["skipped"], one["failed"]) == (1, [ids[0]], [], [])
    assert not files[0].exists() and not processed.exists()
    assert harness.ctx.media.get_rendition(rendition) is None

    many = _delete(harness, {"media_ids": [ids[1], ids[2], ids[1], 999_999], "delete_original": True})
    assert many["deleted_ids"] == [ids[1], ids[2]]
    assert many["skipped"] == [{"id": 999_999, "code": "media_not_found", "message": "Mídia não encontrada."}]
    assert not any(f.exists() for f in files)
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM media") == 0


def test_delete_whole_folders(harness):
    s = _scenario(harness)
    day1 = _local_day(s["day1_iso"])
    group_image = harness.ctx.db.scalar("SELECT id FROM media WHERE media_type = 'image' ORDER BY id LIMIT 1")
    for key, ids in ((f"group:{s['group']}", s["g1"] + s["g2"]), (f"batch:{s['solo_batch']}", s["solo"]),
                     (f"local:{day1}", s["day1"])):
        result = _delete(harness, {"folder": key, "delete_original": True})
        assert sorted(result["deleted_ids"]) == sorted(ids), key
        assert key not in _folders(harness)  # pasta vazia some
    assert harness.ctx.media.get(group_image) is not None  # só os vídeos da pasta
    remaining = _folders(harness)
    assert f"local:{_local_day(s['day2_iso'])}" in remaining and f"batch:{s['link_batch']}" in remaining


def test_delete_only_versions_keeps_original(harness):
    media_id = _videos(harness, 1)[0]
    batch = _batch(harness, [media_id])
    r1, r2 = (seed_rendition(harness, media_id, params={**EDIT_PARAMS, "speed": s}) for s in (1.25, 1.5))
    for rendition in (r1, r2):
        assert harness.client.delete(f"/api/renditions/{rendition}").status_code == 204
    assert harness.ctx.media.get(media_id) is not None
    assert harness.ctx.storage.resolve(harness.ctx.media.get(media_id)["original_path"]).exists()
    assert _folders(harness)[f"batch:{batch}"]["rendition_count"] == 0


def test_busy_media_and_open_export_are_skipped(harness, tmp_path):
    busy, exporting, free = _videos(harness, 3)
    job = harness.ctx.jobs.create_process(media=harness.ctx.media.get(busy), batch_id=None, preset_id=None,
                                          preset_name="Edição", params=EDIT_PARAMS)
    harness.ctx.db.execute("UPDATE jobs SET status = 'processing' WHERE id = ?", (job,))
    export_batch = _batch(harness, [exporting])
    start_export(harness, {"kind": "batch", "id": export_batch}, prepare(harness, tmp_path))  # fica pendente

    result = _delete(harness, {"media_ids": [busy, exporting, free], "delete_original": True})
    assert result["deleted_ids"] == [free] and result["failed"] == []
    assert [(i["id"], i["code"]) for i in result["skipped"]] == [(busy, "media_busy"),
                                                                 (exporting, "export_in_progress")]
    assert harness.ctx.media.get(busy) is not None and harness.ctx.media.get(exporting) is not None


def test_listing_folders_writes_nothing_to_storage(harness):
    _scenario(harness)
    before = storage_snapshot(harness)
    _folders(harness)
    assert storage_snapshot(harness) == before


def test_delete_original_must_be_json_true(harness):
    ids = _videos(harness, 1)
    for value in ("true", 1, "yes", "1"):
        assert _delete(harness, {"media_ids": ids, "delete_original": value}, expect=422)["code"] == "validation_error"
    assert harness.ctx.media.get(ids[0]) is not None


def test_export_created_during_bulk_delete_is_respected(harness, tmp_path, monkeypatch):
    first, second = _videos(harness, 2)
    batch = _batch(harness, [second])
    original = harness.ctx.media.active_job_count
    created = []

    def busy_count(media_id):
        if not created:  # uma exportação nova aparece logo depois de começar a apagar o primeiro
            created.append(harness.ctx.exports.create(kind="batch", batch_id=batch, dest_path=str(tmp_path)))
        return original(media_id)

    monkeypatch.setattr(harness.ctx.media, "active_job_count", busy_count)
    result = _delete(harness, {"media_ids": [first, second], "delete_original": True})
    assert created[0] is not None
    assert result["deleted_ids"] == [first]
    assert [(i["id"], i["code"]) for i in result["skipped"]] == [(second, "export_in_progress")]
    assert harness.ctx.media.get(second) is not None


def test_single_delete_refused_while_in_open_export(harness, tmp_path):
    media_id = _videos(harness, 1)[0]
    start_export(harness, {"kind": "batch", "id": _batch(harness, [media_id])}, prepare(harness, tmp_path))
    response = harness.client.delete(f"/api/media/{media_id}", params={"delete_original": "true"})
    assert response.status_code == 409 and response.json()["code"] == "export_in_progress"
    assert "exportação em andamento" in response.json()["detail"]
    assert harness.ctx.media.get(media_id) is not None


def _export_edited(harness, tmp_path, batch_id: int) -> dict:
    token = pick(harness, prepare(harness, tmp_path))["token"]
    response = harness.client.post("/api/exports", json={"source": {"kind": "batch", "id": batch_id},
                                                         "destination": {"token": token}, "content": "edited"})
    assert response.status_code == 202, response.text
    return response.json()


def test_version_delete_refused_while_exported_or_busy(harness, tmp_path):
    exported, busy = _videos(harness, 2)
    r_exported = seed_rendition(harness, exported, params=EDIT_PARAMS)
    r_busy = seed_rendition(harness, busy, params=EDIT_PARAMS)
    _export_edited(harness, tmp_path, seed_edit_batch(harness, [(exported, r_exported)], params=EDIT_PARAMS))
    job = harness.ctx.jobs.create_process(media=harness.ctx.media.get(busy), batch_id=None, preset_id=None,
                                          preset_name="Edição", params={**EDIT_PARAMS, "speed": 1.25})
    harness.ctx.db.execute("UPDATE jobs SET status = 'processing' WHERE id = ?", (job,))

    refused = harness.client.delete(f"/api/renditions/{r_exported}")
    assert refused.status_code == 409 and refused.json()["code"] == "export_in_progress"
    assert "versão editada" in refused.json()["detail"]
    busy_response = harness.client.delete(f"/api/renditions/{r_busy}")
    assert busy_response.status_code == 409 and busy_response.json()["code"] == "media_busy"
    for rendition in (r_exported, r_busy):
        row = harness.ctx.media.get_rendition(rendition)
        assert row is not None and harness.ctx.storage.resolve(row["processed_path"]).exists()


def test_version_delete_allowed_when_open_export_takes_only_originals(harness, tmp_path):
    media_id = _videos(harness, 1)[0]
    rendition = seed_rendition(harness, media_id, params=EDIT_PARAMS)
    start_export(harness, {"kind": "batch", "id": _batch(harness, [media_id])}, prepare(harness, tmp_path))
    assert harness.client.delete(f"/api/renditions/{rendition}").status_code == 204
    missing = harness.client.delete(f"/api/renditions/{rendition}")
    assert missing.status_code == 404 and missing.json()["detail"] == "Versão processada não encontrada."
