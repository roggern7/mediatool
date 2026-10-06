"""Recovery no startup (contrato, seção 7). Roda antes do worker e do import runner iniciarem."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.services.storage import JOB_TEMP_PREFIX

if TYPE_CHECKING:
    from app.services.context import AppContext

logger = logging.getLogger("media_tool.recovery")


@dataclass(frozen=True)
class RecoveryReport:
    requeued: int
    failed: int
    temp_removed: int
    processed_orphans: int
    listing_batches: int


def migrate_thumbnails(ctx: AppContext) -> int:
    """Seção 9, B3: thumbs fatiados por id. Move os arquivos antigos e atualiza `media.thumbnail_path` só das
    mídias cujo arquivo já está no lugar novo (idempotente; roda a cada startup). Retorna quantas atualizou."""
    moved = ctx.storage.migrate_flat_thumbs()
    rows = ctx.db.fetchall(
        "SELECT id FROM media WHERE thumbnail_path LIKE 'thumbs/%' AND thumbnail_path NOT LIKE 'thumbs/%/%'")
    updates = []
    for row in rows:
        target = ctx.storage.thumb_path(row["id"])
        if target.is_file():
            updates.append((ctx.storage.relative(target), row["id"]))
    if updates:
        with ctx.db.transaction() as conn:
            conn.executemany("UPDATE media SET thumbnail_path = ? WHERE id = ?", updates)
    if moved or updates:
        logger.info("Thumbnails fatiadas: %s arquivos movidos, %s registros atualizados", moved, len(updates))
    return len(updates)


def run_recovery(ctx: AppContext) -> RecoveryReport:
    """Jobs interrompidos voltam para a fila (ou falham se esgotaram tentativas); temporários órfãos são removidos.

    Lotes em `listing` continuam em `listing` e são retomados pelo import runner (jobs já criados não duplicam).
    Nenhum job é marcado como concluído aqui.
    """
    counts = ctx.jobs.recover_interrupted()

    # No startup nenhum job está ativo; se o worker já roda, preserva só os temporários dos jobs em execução.
    active = ctx.worker.active_job_ids() if ctx.worker is not None else []
    keep = {f"{JOB_TEMP_PREFIX}{job_id}" for job_id in active}
    removed = ctx.storage.cleanup_orphan_temps(keep)

    # Processados promovidos sem rendition (crash entre promote e insert): nome do sistema e anteriores a agora.
    referenced = {row["processed_path"] for row in ctx.db.fetchall("SELECT processed_path FROM renditions")}
    orphans = ctx.storage.remove_orphan_processed(referenced, older_than=time.time())

    migrate_thumbnails(ctx)

    for batch_id in [row["id"] for row in ctx.db.fetchall("SELECT id FROM batches WHERE status = 'running'")]:
        ctx.batches.refresh_status(batch_id)
    listing = int(ctx.db.scalar("SELECT COUNT(*) FROM batches WHERE status = 'listing'"))

    report = RecoveryReport(counts["requeued"], counts["failed"], removed, orphans, listing)
    if any((report.requeued, report.failed, report.temp_removed, report.processed_orphans, report.listing_batches)):
        logger.info(
            "Recovery: %s jobs reenfileirados, %s falhos por tentativas esgotadas, %s temporários removidos,"
            " %s processados órfãos removidos, %s listagens a retomar",
            report.requeued, report.failed, report.temp_removed, report.processed_orphans, report.listing_batches,
        )
    return report
