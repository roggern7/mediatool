"""Apoio aos testes da exportação para pasta: seletor de pasta SEMPRE falso, pastas temporárias do pytest e mídias
semeadas direto no banco/storage de teste (sem rede)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from media_fakes import MAGIC


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def storage_snapshot(harness) -> dict[str, tuple[int, int, str]]:
    """Todos os arquivos do storage: caminho relativo -> (tamanho, mtime_ns, sha256)."""
    root = harness.ctx.storage.root
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns, sha256_of(path))
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def prepare(harness, tmp_path: Path) -> Path:
    """Documentos falso e pasta de destino vazia (irmã da raiz do app de teste)."""
    documents = tmp_path / "Documentos"
    documents.mkdir(exist_ok=True)
    harness.ctx.documents_dir = lambda: documents
    dest = tmp_path / "saida"
    dest.mkdir(exist_ok=True)
    return dest


def pick(harness, folder: Path, *, expect: int = 200) -> dict:
    harness.ctx.folder_picker = lambda initial: folder
    response = harness.client.post("/api/exports/pick-folder")
    assert response.status_code == expect, response.text
    return response.json()


def run_exports(harness) -> None:
    while harness.ctx.export_runner.run_next():
        pass


def start_export(harness, source: dict, folder: Path, *, expect: int = 202) -> dict:
    token = pick(harness, folder)["token"]
    response = harness.client.post("/api/exports", json={"source": source, "destination": {"token": token}})
    assert response.status_code == expect, response.text
    return response.json()


def export_to(harness, source: dict, folder: Path) -> dict:
    created = start_export(harness, source, folder)
    run_exports(harness)
    return harness.client.get(f"/api/exports/{created['id']}").json()


def seed_media(harness, count: int, *, profile: str | None = "semente", start: int = 0, size: int | None = None,
               code: str | None = "S{i:05d}", taken_at: str | None = "2026-02-03T10:00:00Z", index: int = 0,
               ext: str = ".jpg") -> list[int]:
    """Cria `count` originais pequenos (ou de `size` bytes) no storage de teste e as linhas em `media`."""
    storage = harness.ctx.storage
    ids: list[int] = []
    with harness.ctx.db.transaction():
        for i in range(start, start + count):
            data = MAGIC["image"] + f"SEED-{profile}-{i}-{index}".encode()
            if size is not None:
                data = (data * (size // len(data) + 1))[:size]
            sha = hashlib.sha256(data).hexdigest()
            relative = f"originals/2026/01/{sha[:2]}/{sha}{ext}"
            path = storage.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            ids.append(harness.ctx.media.insert({
                "source_platform": "instagram", "source_profile": profile,
                "source_post_id": None if code is None else code.format(i=i), "source_media_index": index,
                "media_type": "image", "original_path": relative, "original_sha256": sha, "file_size": len(data),
                "taken_at": taken_at,
            }))
    return ids


def original_of(harness, media_id: int) -> Path:
    return harness.ctx.storage.resolve(harness.ctx.media.get(media_id)["original_path"])


def files_in(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


EDIT_PARAMS = {"metadata_cleanup": False, "trim_start": 0.0, "mirror": False, "speed": 1.5, "auto_adjust": False,
               "reencode": True, "max_duration": 15.03, "sharpen": False, "audio_sync_start": True}


def seed_rendition(harness, media_id: int, *, params: dict, preset_name: str | None = "Edição",
                   preset_id: int | None = None, ext: str = ".mp4", job_id: int | None = None,
                   size: int | None = None) -> int:
    """Uma versão (rendition) com arquivo próprio em `processed/...` do storage de teste (conteúdo único)."""
    storage = harness.ctx.storage
    serial = harness.ctx.db.scalar("SELECT COALESCE(MAX(id), 0) + 1 FROM renditions")
    data = MAGIC["video"] + f"RENDITION-{media_id}-{serial}-{preset_name}".encode()
    if size is not None:
        data = (data * (size // len(data) + 1))[:size]
    sha = hashlib.sha256(data).hexdigest()
    relative = f"processed/2026/01/{sha[:2]}/{sha}{ext}"
    path = storage.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return harness.ctx.media.insert_rendition({
        "media_id": media_id, "preset_id": preset_id, "preset_name": preset_name, "params": params,
        "job_id": job_id, "processed_path": relative, "processed_sha256": sha, "file_size": len(data),
    })


def seed_edit_batch(harness, jobs: list[tuple[int, int | None]], *, params: dict, preset_id: int | None = None,
                    preset_name: str = "Edição", stage: str = "done", group_id: int | None = None) -> int:
    """Lote de processamento da Biblioteca (padrão: Edição, preset_id NULL) já encerrado: um job `process` por
    `(media_id, rendition_id)`; com rendition o job está concluído (`stage`), sem rendition falhou."""
    from app.db.connection import utc_now

    batch_id = harness.ctx.batches.create(
        source="library", status="completed", preset_id=preset_id, preset_name=preset_name, params=params,
        process=True, group_id=group_id, group_position=0 if group_id is not None else None,
    )
    for media_id, rendition_id in jobs:
        job_id = harness.ctx.jobs.create_process(media=harness.ctx.media.get(media_id), batch_id=batch_id,
                                                 preset_id=preset_id, preset_name=preset_name, params=params)
        harness.ctx.db.execute(
            "UPDATE jobs SET status = ?, stage = ?, rendition_id = ?, finished_at = ? WHERE id = ?",
            ("completed" if rendition_id else "failed", stage if rendition_id else "failed", rendition_id,
             utc_now(), job_id),
        )
    return batch_id


def seed_batch(harness, media_ids: list[int], *, profile: str = "semente") -> int:
    """Lote concluído cujas mídias estão só na relação durável `batch_media` (como um lote antigo, depois da
    retenção): origem distinta para testes de fila sem passar pela importação."""
    batch_id = harness.ctx.batches.create(source="instagram_import", status="completed", source_profile=profile)
    with harness.ctx.db.transaction() as conn:
        conn.executemany("INSERT INTO batch_media (batch_id, media_id) VALUES (?, ?)",
                         [(batch_id, media_id) for media_id in media_ids])
    return batch_id
