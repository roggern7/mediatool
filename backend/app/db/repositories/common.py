from __future__ import annotations

import json
import sqlite3
from typing import Any

JOB_STATUSES = ("pending", "downloading", "downloaded", "processing", "completed", "failed", "cancelled")
TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")
ACTIVE_JOB_STATUSES = ("downloading", "downloaded", "processing")
BATCH_STATUSES = ("listing", "running", "completed", "failed", "cancelled")
OPEN_JOB_STATUSES = ("pending", "downloading", "downloaded", "processing")
# `status IN (não terminais)` (em vez de NOT IN) para o SQLite usar os índices que começam por status.
OPEN_JOB_STATUSES_SQL = "('pending', 'downloading', 'downloaded', 'processing')"
# Contadores de progresso do lote (condição por job); os mesmos no agregado ao vivo e nos contadores congelados.
BATCH_PROGRESS_CONDITIONS = {
    "downloaded": "kind = 'ingest' AND media_id IS NOT NULL AND is_duplicate = 0",
    "processed": "status = 'completed' AND rendition_id IS NOT NULL AND COALESCE(stage, '') <> 'skipped'",
    "skipped": "status = 'completed' AND stage = 'skipped'",
    "failed": "status = 'failed'",
}


def canonical_json(value: Any) -> str:
    """JSON estável (chaves ordenadas) para comparar snapshots de params."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def load_json(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def placeholders(count: int) -> str:
    return ",".join("?" * count)


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def delete_batch_remote_refs(conn: sqlite3.Connection, batch_id: int) -> int:
    """Apaga URLs assinadas e cursores (job_remote_refs) de todos os jobs do lote (lote concluído ou cancelado)."""
    return conn.execute(
        "DELETE FROM job_remote_refs WHERE job_id IN (SELECT id FROM jobs WHERE batch_id = ?)", (batch_id,)
    ).rowcount


# Job dependente (duplicate_in_flight) cujo primário ainda não baixou: não conta no backpressure (seção 9).
WAITING_PRIMARY_SQL = (
    "(primary_job_id IS NOT NULL AND EXISTS (SELECT 1 FROM jobs p WHERE p.id = jobs.primary_job_id"
    " AND p.status IN ('pending', 'downloading')))"
)
