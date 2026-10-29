"""Histórico de "Importações recentes": remover um item ou limpar os terminados (POST /api/imports/history/hide).
Só oculta da lista; os vídeos, as pastas da página Editar, a Fila e o lote continuam. 100% offline, valores FAKE."""

from __future__ import annotations

import pytest

from import_group_fakes import ProfilesDownloader, closed, drive
from media_fakes import remote

HIDE = "/api/imports/history/hide"


def _items(profile: str) -> list:
    code = "".join(ch for ch in profile.upper() if ch.isalnum())
    return [remote(f"{code}V", media_type="video", profile=profile), remote(f"{code}I", profile=profile)]


@pytest.fixture
def app(harness_factory):
    profiles = {name: _items(name) for name in ("perfil_a", "perfil_b", "perfil_c", "perfil_d", "perfil_e")}
    return harness_factory(downloader=ProfilesDownloader(profiles=profiles))


def _single(h, profile: str, *, finish: bool = True) -> int:
    batch_id = h.import_profile(profile=profile)["id"]
    if finish:
        drive(h, closed(h, batch_id))
    return batch_id


def _group(h, profiles: list[str], *, finish: bool = True) -> int:
    response = h.client.post("/api/import-groups", json={"profiles": profiles, "content_type": "all", "limit": 10})
    assert response.status_code == 202, response.text
    group_id = response.json()["group"]["id"]
    if finish:
        children = [c["id"] for c in response.json()["children"]]
        drive(h, closed(h, *children))
    return group_id


def _history(h) -> tuple[set[int], int, set[int], int]:
    batches = h.client.get("/api/batches", params={"source": "instagram_import", "ungrouped": "true",
                                                  "history": "true"}).json()
    groups = h.client.get("/api/import-groups", params={"history": "true"}).json()
    return ({b["id"] for b in batches["items"]}, batches["total"], {g["id"] for g in groups["items"]},
            groups["total"])


def _folders(h) -> dict[str, int]:
    return {f["key"]: f["video_count"] for f in h.client.get("/api/edit/folders").json()["items"]}


def _hide(h, expect: int = 200, **body) -> dict:
    response = h.client.post(HIDE, json=body)
    assert response.status_code == expect, response.text
    return response.json()


def test_hide_one_batch_keeps_videos_folders_queue_and_full_list(app):
    h = app
    keep, gone = _single(h, "perfil_a"), _single(h, "perfil_b")
    media_before = h.ctx.db.scalar("SELECT COUNT(*) FROM media")
    folders_before = _folders(h)
    assert folders_before[f"batch:{gone}"] == 1

    result = _hide(h, batch_ids=[gone])
    assert result == {"batches": 1, "groups": 0, "video_count": 1, "total_bytes": result["total_bytes"],
                      "shared_count": 0, "dry_run": False, "deleted": None}
    batches, total, _, _ = _history(h)
    assert batches == {keep} and total == 1
    # Fora do histórico, nada muda: lista completa, lote, jobs, mídias e pastas da página Editar.
    full = h.client.get("/api/batches", params={"source": "instagram_import", "ungrouped": "true"}).json()
    assert {b["id"] for b in full["items"]} == {keep, gone}
    assert h.client.get(f"/api/batches/{gone}").status_code == 200
    assert h.client.get("/api/jobs", params={"batch_id": gone}).json()["total"] == 2
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == media_before
    assert _folders(h) == folders_before
    # De novo: idempotente (já oculto continua oculto).
    assert _hide(h, batch_ids=[gone])["batches"] == 1


def test_hide_one_group(app):
    h = app
    group_id = _group(h, ["perfil_a", "perfil_b"])
    single = _single(h, "perfil_c")
    folders_before = _folders(h)
    assert folders_before[f"group:{group_id}"] == 2
    result = _hide(h, group_ids=[group_id])
    assert (result["batches"], result["groups"], result["video_count"]) == (0, 1, 2)
    batches, _, groups, groups_total = _history(h)
    assert batches == {single} and groups == set() and groups_total == 0
    assert h.client.get(f"/api/import-groups/{group_id}").status_code == 200
    assert _folders(h) == folders_before
    # Filho de grupo não sai sozinho: o grupo é o item do histórico.
    child = h.ctx.db.scalar("SELECT id FROM batches WHERE group_id = ? LIMIT 1", (group_id,))
    assert _hide(h, 422, batch_ids=[child])["code"] == "batch_in_group"


def test_clear_history_hides_only_finished_items(app):
    h = app
    done_batch = _single(h, "perfil_a")
    done_group = _group(h, ["perfil_b", "perfil_c"])
    running_batch = _single(h, "perfil_d", finish=False)
    running_group = _group(h, ["perfil_e"], finish=False)
    preview = _hide(h, all_finished=True, dry_run=True)
    assert (preview["batches"], preview["groups"], preview["video_count"], preview["dry_run"]) == (1, 1, 3, True)
    assert preview["total_bytes"] > 0
    assert _history(h)[0] == {done_batch, running_batch}   # dry_run não muda nada

    result = _hide(h, all_finished=True)
    assert (result["batches"], result["groups"]) == (1, 1)
    batches, total, groups, groups_total = _history(h)
    assert batches == {running_batch} and total == 1 and groups == {running_group} and groups_total == 1
    assert done_group not in groups
    # Nada mais para limpar: resposta com zeros.
    assert _hide(h, all_finished=True)["batches"] == 0


def test_in_progress_items_are_refused(app):
    h = app
    running_batch = _single(h, "perfil_a", finish=False)
    running_group = _group(h, ["perfil_b", "perfil_c"], finish=False)
    for body in ({"batch_ids": [running_batch]}, {"group_ids": [running_group]}):
        error = _hide(h, 409, **body)
        assert error["code"] == "import_in_progress" and "Cancele ou aguarde terminar" in error["detail"]
    assert _history(h)[0] == {running_batch} and _history(h)[2] == {running_group}
    # Cancelado = terminado: pode sair do histórico.
    assert h.client.post(f"/api/batches/{running_batch}/cancel").status_code == 200
    assert _hide(h, batch_ids=[running_batch])["batches"] == 1


def test_hide_with_delete_media_deletes_only_the_videos(app):
    h = app
    batch_id = _single(h, "perfil_a")
    other = _single(h, "perfil_b")
    preview = _hide(h, batch_ids=[batch_id], delete_media=True, dry_run=True)
    assert preview["video_count"] == 1 and preview["deleted"] is None
    assert h.ctx.db.scalar("SELECT COUNT(*) FROM media") == 4

    result = _hide(h, batch_ids=[batch_id], delete_media=True)
    assert result["deleted"]["deleted"] == 1 and result["deleted"]["skipped"] == []
    codes = {r["source_post_id"] for r in h.ctx.db.fetchall("SELECT source_post_id FROM media")}
    assert codes == {"PERFILAI", "PERFILBV", "PERFILBI"}   # a foto e a outra importação ficam
    assert f"batch:{batch_id}" not in _folders(h) and _folders(h)[f"batch:{other}"] == 1


@pytest.mark.parametrize("body", [
    {}, {"batch_ids": [1], "all_finished": True}, {"all_finished": "true"}, {"batch_ids": []},
    {"all_finished": True, "extra": 1},
])
def test_request_validation(harness, body):
    assert harness.client.post(HIDE, json=body).status_code == 422


def test_unknown_items(harness):
    assert _hide(harness, 404, batch_ids=[999])["code"] == "batch_not_found"
    assert _hide(harness, 404, group_ids=[999])["code"] == "group_not_found"


def test_shared_videos_are_counted_before_deleting(app):
    """O mesmo vídeo em duas importações visíveis: a contagem avisa; apagar continua apagando."""
    h = app
    first = _single(h, "perfil_a")
    second = _single(h, "perfil_a")   # mesma mídia (duplicata) ligada também à segunda importação
    assert _hide(h, batch_ids=[first], dry_run=True)["shared_count"] == 1
    assert _hide(h, batch_ids=[first, second], dry_run=True)["shared_count"] == 0
    _hide(h, batch_ids=[second])   # oculta: não conta mais como "outra importação visível"
    assert _hide(h, batch_ids=[first], dry_run=True)["shared_count"] == 0


def test_retry_brings_hidden_batch_and_group_back_to_history(app):
    h = app
    group_id = _group(h, ["perfil_b"])
    child = h.ctx.db.scalar("SELECT id FROM batches WHERE group_id = ?", (group_id,))
    batch_id = _single(h, "perfil_a")
    _hide(h, batch_ids=[batch_id])
    _hide(h, group_ids=[group_id])
    assert _history(h)[0] == set() and _history(h)[2] == set()
    for target in (batch_id, child):
        job = h.ctx.db.scalar("SELECT id FROM jobs WHERE batch_id = ? LIMIT 1", (target,))
        h.ctx.jobs.update(job, status="failed", error_code="processing_failed")
        h.ctx.batches.update(target, status="failed")
        assert h.client.post(f"/api/batches/{target}/retry-failed").status_code == 200
    assert _history(h)[0] == {batch_id} and _history(h)[2] == {group_id}
