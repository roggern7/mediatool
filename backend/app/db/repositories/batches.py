from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from app.db.connection import Database, utc_now
from app.db.repositories.common import (
    BATCH_PROGRESS_CONDITIONS,
    OPEN_JOB_STATUSES_SQL,
    WAITING_PRIMARY_SQL,
    canonical_json,
    load_json,
    placeholders,
    row_dict,
)

BATCH_UPDATE_COLUMNS = frozenset({"status", "found", "error_code", "error_message", "finished_at"})
# Checkpoint interno da listagem (nunca sai pela API nem por log).
LISTING_COLUMNS = frozenset(
    {
        "listing_cursor", "listing_next_page", "listing_pages", "listing_posts_seen", "listing_items_compatible",
        "listing_unproductive_streak", "listing_has_next", "listing_state", "listing_retry_level", "listing_retry_at",
        "listing_cursor_refusals", "listing_attention_tries",
        # Espera pelo teto de páginas por hora (migration 0010): retomada prevista e páginas usadas/teto da janela.
        "rate_limit_resume_at", "rate_limit_pages_used", "rate_limit_pages_max",
        # Descartes da página ainda sem checkpoint (migration 0011); os totais só mudam por add_listing_dropped.
        "listing_dropped_partial",
    }
)
TERMINAL_BATCH_STATUSES = ("completed", "failed", "cancelled")
logger = logging.getLogger("media_tool.db")
_bad_counts_logged = False


def load_listing_counts(value: Any) -> dict[str, Any]:
    """JSON das colunas `listing_dropped_reasons`/`listing_dropped_partial` como dict; valor corrompido (não é JSON
    ou não é objeto) vira vazio, com UM aviso por execução: nunca derruba a API nem a listagem."""
    global _bad_counts_logged
    if value is None:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        return data
    if not _bad_counts_logged:
        _bad_counts_logged = True
        logger.warning("Contadores de descartes da listagem ilegíveis no banco; tratados como vazios")
    return {}


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
# Pausa que não justifica impedir a suspensão do Windows (has_active_work).
LONG_PAUSE_SECONDS = 30 * 60
# Contadores finais congelados no lote (seção 6: a retenção apaga jobs de lotes antigos). Subconsultas correlatas:
# só são avaliadas nas linhas que o UPDATE altera (nunca a cada job de um lote aberto). Invariante: `final_*`
# preenchidos => o lote está encerrado e sem job aberto (o presenter usa só eles, sem agregar o lote).
FINAL_CONDITIONS = {
    **BATCH_PROGRESS_CONDITIONS,
    "completed": "status = 'completed'",
    "cancelled": "status = 'cancelled'",
}
_FINAL_EXPRESSIONS = {
    "found": "MAX(found, (SELECT COUNT(*) FROM jobs WHERE batch_id = batches.id))",
    **{key: f"(SELECT COUNT(*) FROM jobs WHERE batch_id = batches.id AND {cond})" for key, cond in FINAL_CONDITIONS.items()},
}
_FREEZE_SQL = ", ".join(f"final_{key} = {expr}" for key, expr in _FINAL_EXPRESSIONS.items())
# Encerramento com jobs ainda abertos (ex.: cancelamento com jobs em execução): fica NULL; o refresh_status congela
# quando o último terminar.
_OPEN_JOBS_EXIST = f"EXISTS (SELECT 1 FROM jobs WHERE batch_id = batches.id AND status IN {OPEN_JOB_STATUSES_SQL})"
_FREEZE_IF_IDLE_SQL = ", ".join(
    f"final_{key} = CASE WHEN {_OPEN_JOBS_EXIST} THEN NULL ELSE {expr} END" for key, expr in _FINAL_EXPRESSIONS.items()
)
_UNFREEZE_SQL = ", ".join(f"final_{key} = NULL" for key in _FINAL_EXPRESSIONS)


def decode_batch(row) -> dict[str, Any] | None:
    data = row_dict(row)
    if data is not None:
        data["params"] = load_json(data["params"])
        data["process"] = bool(data["process"])
    return data


class BatchRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(
        self,
        *,
        source: str,
        status: str,
        source_profile: str | None = None,
        content_type: str | None = None,
        requested_limit: int | None = None,
        preset_id: int | None = None,
        preset_name: str | None = None,
        params: dict[str, Any] | None = None,
        process: bool = False,
    ) -> int:
        now = utc_now()
        cursor = self.db.execute(
            "INSERT INTO batches (source, source_profile, content_type, requested_limit, preset_id, preset_name,"
            " params, process, status, found, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                source, source_profile, content_type, requested_limit, preset_id, preset_name,
                None if params is None else canonical_json(params), int(process), status, now, now,
            ),
        )
        return cursor.lastrowid

    def get(self, batch_id: int) -> dict[str, Any] | None:
        return decode_batch(self.db.fetchone("SELECT * FROM batches WHERE id = ?", (batch_id,)))

    def list(self, *, limit: int, offset: int, source: str | None = None) -> tuple[list[dict[str, Any]], int]:
        where, params = ("WHERE source = ?", (source,)) if source else ("", ())
        total = int(self.db.scalar(f"SELECT COUNT(*) FROM batches {where}", params))
        rows = self.db.fetchall(
            f"SELECT * FROM batches {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)
        )
        return [decode_batch(row) for row in rows], total

    def get_many(self, batch_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not batch_ids:
            return {}
        rows = self.db.fetchall(f"SELECT * FROM batches WHERE id IN ({placeholders(len(batch_ids))})", tuple(batch_ids))
        return {row["id"]: decode_batch(row) for row in rows}

    def update(self, batch_id: int, **fields: Any) -> None:
        unknown = set(fields) - BATCH_UPDATE_COLUMNS
        if unknown:
            raise ValueError(f"Colunas desconhecidas: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        if fields.get("status") in TERMINAL_BATCH_STATUSES:
            assignments += f", {_FREEZE_IF_IDLE_SQL}"  # lote encerrado (cancelado/falhou): congela junto
        self.db.execute(
            f"UPDATE batches SET {assignments}, updated_at = ? WHERE id = ?", (*fields.values(), utc_now(), batch_id)
        )

    def next_listing(self) -> dict[str, Any] | None:
        return decode_batch(
            self.db.fetchone(
                "SELECT * FROM batches WHERE status = 'listing' AND source = 'instagram_import' ORDER BY id LIMIT 1"
            )
        )

    def listing_candidates(self) -> list[dict[str, Any]]:
        """Lotes de importação ainda listando (inclui os que aguardam backpressure ou pausa), em ordem de criação."""
        rows = self.db.fetchall(
            "SELECT * FROM batches WHERE status = 'listing' AND source = 'instagram_import' ORDER BY id"
        )
        return [decode_batch(row) for row in rows]

    def open_listing_for_profile(self, profile: str) -> int | None:
        """Lote de importação do perfil com listagem aberta (status `listing`), se houver."""
        row = self.db.fetchone(
            "SELECT id FROM batches WHERE status = 'listing' AND source = 'instagram_import'"
            " AND source_profile = ? COLLATE NOCASE LIMIT 1",
            (profile,),
        )
        return None if row is None else int(row["id"])

    def pending_ingest_global(self) -> int:
        """Ingests ainda não baixados de todos os lotes (backpressure global da listagem)."""
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'downloading') AND kind = 'ingest'"
                f" AND +media_id IS NULL AND batch_id IS NOT NULL AND NOT {WAITING_PRIMARY_SQL}"
            )
        )

    def resume_listing(self, batch_id: int, *, from_start: bool = False) -> bool:
        """Listagem `paused`/`needs_attention` volta a `pending` (backoff e tentativas zerados). `from_start`
        (só por ação explícita do usuário) descarta o checkpoint: a listagem recomeça da página 1 e o índice único dos
        jobs descarta o que já foi listado. False se o estado não permite."""
        restart = (
            ", listing_cursor = NULL, listing_next_page = NULL, listing_pages = 0, listing_posts_seen = 0,"
            " listing_items_compatible = 0, listing_unproductive_streak = 0, listing_has_next = NULL,"
            # As páginas serão listadas (e contadas) de novo: os descartes do lote recomeçam do zero.
            " listing_dropped_unexpected = 0, listing_dropped_reasons = NULL, listing_dropped_partial = NULL"
        ) if from_start else ""
        cursor = self.db.execute(
            "UPDATE batches SET listing_state = 'pending', listing_retry_at = NULL, listing_retry_level = 0,"
            f" listing_cursor_refusals = 0, listing_attention_tries = 0{restart},"
            " error_code = NULL, error_message = NULL, updated_at = ?"
            " WHERE id = ? AND status = 'listing' AND listing_state IN ('paused', 'needs_attention')",
            (utc_now(), batch_id),
        )
        return cursor.rowcount > 0

    def has_active_work(self, *, long_pause_seconds: float = LONG_PAUSE_SECONDS) -> bool:
        """Há lote com trabalho que justifica impedir a suspensão do Windows (seção 9): lote `running` ou listando.
        Não contam: listagem em `needs_attention` (aguarda ação/nova tentativa em horas), listagem pausada por backoff
        e lote pausado pelo disjuntor cujo prazo passa de `long_pause_seconds`."""
        horizon = utc_now(long_pause_seconds)
        row = self.db.fetchone(
            "SELECT 1 FROM batches WHERE status IN ('running', 'listing')"
            " AND NOT (status = 'listing' AND listing_state = 'needs_attention')"
            " AND NOT (status = 'listing' AND listing_state = 'paused' AND COALESCE(listing_retry_at, '') > :horizon)"
            " AND COALESCE(paused_until, '') <= :horizon LIMIT 1",
            {"horizon": horizon},
        )
        return row is not None

    def update_listing(self, batch_id: int, **fields: Any) -> None:
        """Atualiza o checkpoint/estado interno da listagem (colunas fixas; valores em parâmetros)."""
        unknown = set(fields) - LISTING_COLUMNS - BATCH_UPDATE_COLUMNS
        if unknown:
            raise ValueError(f"Colunas desconhecidas: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self.db.execute(
            f"UPDATE batches SET {assignments}, updated_at = ? WHERE id = ?", (*fields.values(), utc_now(), batch_id)
        )

    def add_listing_dropped(self, batch_id: int, counts: Mapping[str, Mapping[str, int]] | None = None, *,
                            merge_partial: bool = False) -> None:
        """Soma descartes da listagem aos contadores do lote (migration 0011). Chamar DENTRO da transação que grava
        o checkpoint da página (``counts`` = ListingCheckpoint.page_counts) ou que encerra a listagem
        (``merge_partial``: soma `listing_dropped_partial`, a página sem checkpoint, e o apaga)."""
        row = self.db.fetchone(
            "SELECT listing_dropped_reasons, listing_dropped_partial FROM batches WHERE id = ?", (batch_id,))
        if row is None:
            return
        extra = {kind: dict(values) for kind, values in (counts or {}).items()}
        partial = load_listing_counts(row["listing_dropped_partial"]) if merge_partial else {}
        if partial:
            unexpected = extra.setdefault("unexpected", {})
            for reason, amount in partial.items():
                unexpected[reason] = _count(unexpected.get(reason)) + _count(amount)
        if not extra and not (merge_partial and row["listing_dropped_partial"] is not None):
            return
        stored = {kind: values for kind, values in load_listing_counts(row["listing_dropped_reasons"]).items()
                  if isinstance(values, dict)}
        for kind, values in extra.items():
            target = stored.setdefault(kind, {})
            for reason, amount in values.items():
                target[reason] = _count(target.get(reason)) + _count(amount)
        clear = ", listing_dropped_partial = NULL" if merge_partial else ""
        self.db.execute(
            f"UPDATE batches SET listing_dropped_unexpected = ?, listing_dropped_reasons = ?{clear}, updated_at = ?"
            " WHERE id = ?",
            (sum(_count(v) for v in stored.get("unexpected", {}).values()),
             canonical_json(stored) if stored else None, utc_now(), batch_id),
        )

    def sync_found(self, batch_id: int) -> None:
        self.db.execute(
            "UPDATE batches SET found = (SELECT COUNT(*) FROM jobs WHERE batch_id = ?), updated_at = ? WHERE id = ?",
            (batch_id, utc_now(), batch_id),
        )

    def refresh_status(self, batch_id: int) -> bool:
        """`running` -> `completed` quando todos os jobs do lote estão em estado terminal. True se concluiu.

        Uma instrução só (sem transação explícita): `status IN (não terminais)` usa o índice (batch_id, status, ...);
        ao concluir, os contadores finais são congelados e o trigger tr_batches_completed_drop_refs apaga as
        referências remotas (URLs/cursor) dos jobs do lote. Lote cancelado cujos últimos jobs em execução terminaram
        depois do cancelamento tem os contadores finais recongelados.
        """
        now = utc_now()
        row = self.db.fetchone(
            "UPDATE batches SET"
            " status = CASE WHEN status = 'running' THEN 'completed' ELSE status END,"
            " finished_at = CASE WHEN status = 'running' THEN :now ELSE finished_at END,"
            f" updated_at = :now, {_FREEZE_SQL}"
            " WHERE id = :id AND status IN ('running', 'cancelled') AND NOT EXISTS ("
            f"   SELECT 1 FROM jobs WHERE batch_id = :id AND status IN {OPEN_JOB_STATUSES_SQL})"
            " RETURNING status",
            {"now": now, "id": batch_id},
        )
        return row is not None and row["status"] == "completed"  # só `running` vira `completed` aqui

    def reopen(self, batch_id: int) -> None:
        """Lote volta a `running` (ex.: retry de jobs) se já estava finalizado com jobs."""
        self.db.execute(
            f"UPDATE batches SET status = 'running', finished_at = NULL, {_UNFREEZE_SQL}, updated_at = ?"
            " WHERE id = ? AND status IN ('completed', 'cancelled', 'failed')",
            (utc_now(), batch_id),
        )
