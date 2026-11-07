"""Legendas CapCut (migration 0029): fila persistente dos jobs (um por vez) e modelos de legenda salvos."""

from __future__ import annotations

from typing import Any

from app.db.connection import Database, utc_now
from app.db.repositories.common import canonical_json, load_json, placeholders, row_dict

JOB_STATUSES = ("pending", "running", "waiting", "completed", "failed", "cancelled")
OPEN_STATUSES = ("pending", "running", "waiting")
OPEN_STATUSES_SQL = "('pending', 'running', 'waiting')"
TERMINAL_STATUSES_SQL = "('completed', 'failed', 'cancelled')"
RETRYABLE_STATUSES = ("failed", "cancelled")
_JSON_COLUMNS = ("options", "output_probe", "warnings")
# Colunas da listagem: o `transcript` (pode ter milhares de palavras) só é lido pelo executor.
_LIST_COLUMNS = (
    "j.id, j.media_id, j.source_path, j.status, j.stage, j.stage_progress, j.progress, j.detail, j.attempts,"
    " j.max_attempts, j.next_attempt_at, j.wait_reason, j.error_code, j.error_message, j.cancel_requested, j.options,"
    " j.chunks, j.draft_name, j.output_path, j.output_probe, j.warnings, j.origin, j.batch_id, j.created_at,"
    " j.started_at, j.finished_at, j.updated_at, (j.transcript IS NOT NULL) AS has_transcript,"
    " m.original_filename AS media_filename, m.source_profile AS media_profile, m.thumbnail_path AS media_thumbnail,"
    " m.media_type AS media_type"
)


def decode_job(row) -> dict[str, Any] | None:
    data = row_dict(row)
    if data is None:
        return None
    for column in _JSON_COLUMNS:
        if column in data:
            data[column] = load_json(data[column])
    if "transcript" in data:
        data["transcript"] = load_json(data["transcript"])
    return data


def decode_style(row) -> dict[str, Any] | None:
    data = row_dict(row)
    if data is not None:
        data["style"] = load_json(data["style"])
        data["summary"] = load_json(data["summary"])
    return data


class CapCutRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---- jobs --------------------------------------------------------------------------------------------------
    def create_job(self, *, media_id: int, source_path: str, options: dict[str, Any], max_attempts: int, origin: str,
                   batch_id: int | None) -> int:
        now = utc_now()
        cursor = self.db.execute(
            "INSERT INTO capcut_jobs (media_id, source_path, status, max_attempts, options, origin, batch_id,"
            " created_at, updated_at) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
            (media_id, source_path, max_attempts, canonical_json(options), origin, batch_id, now, now))
        return int(cursor.lastrowid)

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return decode_job(self.db.fetchone("SELECT * FROM capcut_jobs WHERE id = ?", (job_id,)))

    def get_job_view(self, job_id: int) -> dict[str, Any] | None:
        return decode_job(self.db.fetchone(
            f"SELECT {_LIST_COLUMNS} FROM capcut_jobs j LEFT JOIN media m ON m.id = j.media_id WHERE j.id = ?",
            (job_id,)))

    def list_jobs(self, *, status: str | None, media_id: int | None, limit: int,
                  offset: int) -> tuple[list[dict[str, Any]], int]:
        clauses, params = [], []
        if status is not None:
            clauses.append("j.status = ?")
            params.append(status)
        if media_id is not None:
            clauses.append("j.media_id = ?")
            params.append(media_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.fetchall(
            f"SELECT {_LIST_COLUMNS} FROM capcut_jobs j LEFT JOIN media m ON m.id = j.media_id {where}"
            " ORDER BY j.id DESC LIMIT ? OFFSET ?", (*params, limit, offset))
        total = int(self.db.scalar(f"SELECT COUNT(*) FROM capcut_jobs j {where}", tuple(params)))
        return [decode_job(row) for row in rows], total

    def counts(self) -> dict[str, int]:
        rows = self.db.fetchall("SELECT status, COUNT(*) AS n FROM capcut_jobs GROUP BY status")
        result = dict.fromkeys(JOB_STATUSES, 0)
        result.update({row["status"]: int(row["n"]) for row in rows})
        return result

    def open_media_ids(self, media_ids: list[int]) -> set[int]:
        """Mídias que já têm job aberto (pending/running/waiting)."""
        if not media_ids:
            return set()
        rows = self.db.fetchall(
            f"SELECT DISTINCT media_id FROM capcut_jobs WHERE status IN {OPEN_STATUSES_SQL}"
            f" AND media_id IN ({placeholders(len(media_ids))})", tuple(media_ids))
        return {row["media_id"] for row in rows}

    def claim_next(self, now: str) -> dict[str, Any] | None:
        """O job mais antigo com prazo vencido (`pending` ou `waiting`) vira `running`, numa só transação. A tentativa
        conta aqui (a espera por CapCut ocupado a devolve)."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM capcut_jobs WHERE status IN ('pending', 'waiting') AND cancel_requested = 0"
                " AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE capcut_jobs SET status = 'running', attempts = attempts + 1, next_attempt_at = NULL,"
                " wait_reason = NULL, stage = NULL, stage_progress = NULL, progress = 0,"
                " started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?", (now, now, row["id"]))
        return self.get_job(row["id"])

    def update_job(self, job_id: int, **fields: Any) -> None:
        for column in _JSON_COLUMNS + ("transcript",):
            if column in fields and fields[column] is not None:
                fields[column] = canonical_json(fields[column])
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self.db.execute(f"UPDATE capcut_jobs SET {assignments}, updated_at = ? WHERE id = ?",
                        (*fields.values(), utc_now(), job_id))

    def cancel_requested(self, job_id: int) -> bool:
        return bool(self.db.scalar("SELECT cancel_requested FROM capcut_jobs WHERE id = ?", (job_id,)))

    def requeue_job(self, job_id: int, **fields: Any) -> bool:
        """Devolve o job à fila (`pending`/`waiting`) só se o cancelamento NÃO foi pedido, na mesma instrução (sem a
        corrida entre ler `cancel_requested` e gravar). False = cancelamento pedido: quem chama finaliza cancelado."""
        for column in _JSON_COLUMNS + ("transcript",):
            if column in fields and fields[column] is not None:
                fields[column] = canonical_json(fields[column])
        assignments = ", ".join(f"{column} = ?" for column in fields)
        cursor = self.db.execute(
            f"UPDATE capcut_jobs SET {assignments}, updated_at = ? WHERE id = ? AND cancel_requested = 0",
            (*fields.values(), utc_now(), job_id))
        return cursor.rowcount > 0

    def finish_orphan_cancel(self, job_id: int) -> bool:
        """`running` com cancelamento pedido que nenhum executor está rodando termina cancelado."""
        now = utc_now()
        cursor = self.db.execute(
            "UPDATE capcut_jobs SET status = 'cancelled', stage = NULL, stage_progress = NULL, detail = NULL,"
            " next_attempt_at = NULL, wait_reason = NULL, finished_at = ?, updated_at = ?"
            " WHERE id = ? AND status = 'running' AND cancel_requested = 1", (now, now, job_id))
        return cursor.rowcount > 0

    def request_cancel(self, job_id: int) -> str | None:
        """`pending`/`waiting` terminam cancelados na hora; `running` só recebe a marca (o executor para). Terminal não
        muda. Retorna o status ANTERIOR (None se o job não existe)."""
        now = utc_now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status FROM capcut_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            if row["status"] in ("pending", "waiting"):
                conn.execute(
                    "UPDATE capcut_jobs SET status = 'cancelled', next_attempt_at = NULL, wait_reason = NULL,"
                    " finished_at = ?, updated_at = ? WHERE id = ?", (now, now, job_id))
            elif row["status"] == "running":
                conn.execute("UPDATE capcut_jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?", (now, job_id))
            return row["status"]

    def retry(self, job_id: int) -> bool:
        """`failed`/`cancelled` voltam para a fila com as tentativas zeradas; a transcrição fica."""
        now = utc_now()
        cursor = self.db.execute(
            "UPDATE capcut_jobs SET status = 'pending', attempts = 0, cancel_requested = 0, next_attempt_at = NULL,"
            " wait_reason = NULL, error_code = NULL, error_message = NULL, stage = NULL, stage_progress = NULL,"
            " progress = 0, detail = NULL, finished_at = NULL, updated_at = ?"
            " WHERE id = ? AND status IN ('failed', 'cancelled')", (now, job_id))
        return cursor.rowcount > 0

    def delete_job(self, job_id: int) -> bool:
        """Só job encerrado; a condição no próprio DELETE fecha a corrida com uma retentativa."""
        cursor = self.db.execute(f"DELETE FROM capcut_jobs WHERE id = ? AND status IN {TERMINAL_STATUSES_SQL}",
                                 (job_id,))
        return cursor.rowcount > 0

    def recover(self) -> tuple[int, int]:
        """Startup: aberto (`pending`/`running`/`waiting`) com cancelamento pedido termina cancelado; os demais
        `running` voltam para a fila devolvendo a tentativa da execução interrompida (nunca acima do máximo)."""
        now = utc_now()
        with self.db.transaction() as conn:
            cancelled = conn.execute(
                "UPDATE capcut_jobs SET status = 'cancelled', finished_at = ?, updated_at = ?, next_attempt_at = NULL,"
                " wait_reason = NULL WHERE cancel_requested = 1 AND status IN ('pending', 'running', 'waiting')",
                (now, now)).rowcount
            resumed = conn.execute(
                "UPDATE capcut_jobs SET status = 'pending', attempts = MAX(attempts - 1, 0), stage = NULL,"
                " stage_progress = NULL, progress = 0, detail = 'Retomado após interrupção', updated_at = ?"
                " WHERE status = 'running'", (now,)).rowcount
        return cancelled, resumed

    # ---- modelos de legenda -----------------------------------------------------------------------------------
    def list_styles(self) -> list[dict[str, Any]]:
        return [decode_style(row) for row in self.db.fetchall("SELECT * FROM capcut_styles ORDER BY name, id")]

    def get_style(self, style_id: int) -> dict[str, Any] | None:
        return decode_style(self.db.fetchone("SELECT * FROM capcut_styles WHERE id = ?", (style_id,)))

    def style_name_taken(self, name: str) -> bool:
        return self.db.scalar("SELECT 1 FROM capcut_styles WHERE name = ? COLLATE NOCASE", (name,)) is not None

    def create_style(self, *, name: str, source_draft: str, style: dict[str, Any],
                     summary: dict[str, Any] | None) -> int:
        cursor = self.db.execute(
            "INSERT INTO capcut_styles (name, source_draft, style, summary, is_default, created_at)"
            " VALUES (?, ?, ?, ?, 0, ?)",
            (name, source_draft, canonical_json(style), None if summary is None else canonical_json(summary),
             utc_now()))
        return int(cursor.lastrowid)

    def delete_style(self, style_id: int) -> bool:
        return self.db.execute("DELETE FROM capcut_styles WHERE id = ?", (style_id,)).rowcount > 0

    def mark_default(self, style_id: int | None) -> None:
        """Espelha `default_style_id` das configurações (fonte da verdade) na coluna `is_default`."""
        self.db.execute("UPDATE capcut_styles SET is_default = (id IS ?)", (style_id,))
