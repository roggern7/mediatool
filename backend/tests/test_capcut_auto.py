"""Legendas CapCut automáticas após o download: vídeo NOVO enfileira um job `auto` do lote; desligado, imagem, capa e
duplicata não enfileiram; falha ao enfileirar não muda o resultado do download."""

from __future__ import annotations

from media_fakes import remote

from app.services import capcut_service


def capcut_jobs(harness) -> list[dict]:
    return [dict(row) for row in harness.ctx.db.fetchall(
        "SELECT media_id, origin, batch_id, status FROM capcut_jobs ORDER BY id")]


def enable_auto(harness, value: bool = True) -> None:
    response = harness.client.put("/api/capcut/settings", json={"auto_after_download": value})
    assert response.status_code == 200, response.text


def test_new_video_download_enqueues_auto_job(harness):
    enable_auto(harness)
    harness.downloader.items = [remote("V1", media_type="video"), remote("I1", media_type="image")]
    batch = harness.import_profile()
    harness.run_all()
    result = harness.batch(batch["id"])
    assert result["status"] == "completed", result
    videos = [row for row in harness.ctx.db.fetchall("SELECT id, media_type FROM media") if row["media_type"] == "video"]
    assert capcut_jobs(harness) == [{"media_id": videos[0]["id"], "origin": "auto", "batch_id": batch["id"],
                                     "status": "pending"}]

    # O mesmo vídeo de novo (outro lote): reaproveitado, não dispara outro job. O 1o job é cancelado antes para a
    # regra "já está na fila" não esconder um disparo indevido.
    first_id = harness.ctx.db.scalar("SELECT id FROM capcut_jobs")
    assert harness.client.post(f"/api/capcut/jobs/{first_id}/cancel").json()["status"] == "cancelled"
    again = harness.import_profile()
    harness.run_all()
    assert harness.batch(again["id"])["status"] == "completed"
    assert len(capcut_jobs(harness)) == 1


def test_disabled_auto_creates_nothing(harness):
    harness.downloader.items = [remote("V2", media_type="video")]
    harness.import_profile()
    harness.run_all()
    assert harness.ctx.db.scalar("SELECT COUNT(*) FROM media") == 1
    assert capcut_jobs(harness) == []


def test_enqueue_failure_does_not_break_download(harness, monkeypatch):
    enable_auto(harness)

    def broken(*_args, **_kwargs):
        raise RuntimeError("falha ao enfileirar")

    monkeypatch.setattr(capcut_service, "enqueue_after_download", broken)
    harness.downloader.items = [remote("V3", media_type="video")]
    batch = harness.import_profile()
    harness.run_all()
    result = harness.batch(batch["id"])
    assert result["status"] == "completed" and all(job["status"] == "completed" for job in result["jobs"])
    assert capcut_jobs(harness) == []
