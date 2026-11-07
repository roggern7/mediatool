"""Legendas CapCut (migrations 0029/0030/0031): fila persistente dos jobs (um por vez), modelos de legenda salvos (com
o `design` do editor visual) e lotes da página (`capcut_batches`)."""

from __future__ import annotations

from typing import Any

from app.db.connection import Database, utc_now
from app.db.repositories.common import canonical_json, escape_like, load_json, placeholders, row_dict

JOB_STATUSES = ("pending", "running", "waiting", "completed", "failed", "cancelled")
OPEN_STATUSES = ("pending", "running", "waiting")
OPEN_STATUSES_SQL = "('pending', 'running', 'waiting')"
TERMINAL_STATUSES_SQL = "('completed', 'failed', 'cancelled')"
RETRYABLE_STATUSES = ("failed", "cancelled")
# Revisão das legendas: `waiting` com wait_reason 'review' espera o dono (o executor nunca pega; não conta em `waiting`).
REVIEW_SQL = "(status = 'waiting' AND wait_reason = 'review')"
NOT_REVIEW_SQL = "(wait_reason IS NULL OR wait_reason <> 'review')"
_JSON_COLUMNS = ("options", "output_probe", "warnings")
# Colunas da listagem: o `transcript` (pode ter milhares de palavras) só é lido pelo executor.
_LIST_COLUMNS = (
    "j.id, j.media_id, j.source_path, j.status, j.stage, j.stage_progress, j.progress, j.detail, j.attempts,"
    " j.max_attempts, j.next_attempt_at, j.wait_reason, j.error_code, j.error_message, j.cancel_requested, j.options,"
    " j.chunks, j.draft_name, j.output_path, j.output_probe, j.warnings, j.origin, j.batch_id, j.created_at,"
    " j.started_at, j.finished_at, j.updated_at, (j.transcript IS NOT NULL) AS has_transcript,"
    " j.capcut_batch_id, j.mode, j.held, j.edit_job_id,"
    # Frases com trecho duvidoso: só no job em revisão (a transcrição inteira não é lida na listagem).
    " CASE WHEN j.status = 'waiting' AND j.wait_reason = 'review' AND j.transcript IS NOT NULL THEN"
    " (SELECT COUNT(DISTINCT COALESCE(json_extract(t.value, '$.seg'), 0)) FROM json_each(j.transcript) t"
    "  WHERE json_extract(t.value, '$.doubt') = 1) ELSE 0 END AS doubt_count,"
    " m.original_filename AS media_filename, m.source_profile AS media_profile, m.thumbnail_path AS media_thumbnail,"
    " m.media_type AS media_type, e.status AS edit_status, e.progress AS edit_progress_raw,"
    " e.error_message AS edit_error_message"
)
# Mídia e job de edição (lotes com edição) de cada job da listagem.
_LIST_FROM = "capcut_jobs j LEFT JOIN media m ON m.id = j.media_id LEFT JOIN jobs e ON e.id = j.edit_job_id"
EDIT_TERMINAL_SQL = "('completed', 'failed', 'cancelled')"   # estados finais da tabela `jobs` (migration 0001)
BATCH_MODES = ("caption", "edit", "edit_caption")
# Contagens por lote; `held` = na fila com pausa individual (fora de `pending`/`waiting`). Progresso: concluído = 100.
_BATCH_AGG = (
    "SELECT capcut_batch_id AS bid, COUNT(*) AS total, SUM(status = 'running') AS running,"
    f" SUM(status = 'pending' AND held = 0) AS pending, SUM(status = 'waiting' AND held = 0 AND {NOT_REVIEW_SQL})"
    f" AS waiting, SUM(status IN ('pending', 'waiting') AND held = 1 AND {NOT_REVIEW_SQL}) AS held,"
    f" SUM{REVIEW_SQL} AS review, SUM(status = 'completed') AS completed,"
    " SUM(status = 'failed') AS failed, SUM(status = 'cancelled') AS cancelled,"
    " SUM(CASE WHEN status = 'completed' THEN 100 ELSE progress END) AS progress_sum"
    " FROM capcut_jobs WHERE capcut_batch_id IS NOT NULL GROUP BY capcut_batch_id"
)
_OPEN_EXPR = ("(COALESCE(a.running, 0) + COALESCE(a.pending, 0) + COALESCE(a.waiting, 0) + COALESCE(a.held, 0)"
              " + COALESCE(a.review, 0))")
# Status derivado do lote (docs/CAPCUT.md, "Lotes").
_BATCH_STATUS_SQL = (
    f"CASE WHEN {_OPEN_EXPR} = 0 AND (b.cancel_requested = 1 OR (a.total > 0 AND a.cancelled = a.total))"
    " THEN 'cancelled'"
    f" WHEN {_OPEN_EXPR} = 0 AND COALESCE(a.failed, 0) + COALESCE(a.cancelled, 0) > 0 THEN 'completed_with_errors'"
    f" WHEN {_OPEN_EXPR} = 0 THEN 'completed'"
    " WHEN b.cancel_requested = 1 THEN 'cancelled'"
    " WHEN a.running > 0 THEN 'running'"
    " WHEN COALESCE(a.pending, 0) + COALESCE(a.waiting, 0) = 0 AND COALESCE(a.review, 0) > 0 THEN 'review'"
    " WHEN b.paused = 1 OR COALESCE(a.pending, 0) + COALESCE(a.waiting, 0) = 0 THEN 'paused'"
    " ELSE 'running' END"
)
_BATCH_SELECT = (
    f"WITH a AS ({_BATCH_AGG}) SELECT b.*, COALESCE(a.total, 0) AS c_total, COALESCE(a.pending, 0) AS c_pending,"
    " COALESCE(a.running, 0) AS c_running, COALESCE(a.waiting, 0) AS c_waiting, COALESCE(a.held, 0) AS c_held,"
    " COALESCE(a.completed, 0) AS c_completed, COALESCE(a.failed, 0) AS c_failed,"
    " COALESCE(a.cancelled, 0) AS c_cancelled, COALESCE(a.review, 0) AS c_review,"
    " COALESCE(a.progress_sum, 0) AS progress_sum,"
    f" {_BATCH_STATUS_SQL} AS status FROM capcut_batches b LEFT JOIN a ON a.bid = b.id"
)
_BATCH_COUNT_KEYS = ("total", "pending", "running", "waiting", "held", "completed", "failed", "cancelled", "review")


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


def decode_batch(row) -> dict[str, Any] | None:
    data = row_dict(row)
    if data is None:
        return None
    data["options"] = load_json(data["options"]) or {}
    if "c_total" in data:
        data["counts"] = {key: int(data.pop(f"c_{key}") or 0) for key in _BATCH_COUNT_KEYS}
    return data


def decode_style(row) -> dict[str, Any] | None:
    data = row_dict(row)
    if data is not None:
        data["style"] = load_json(data["style"])
        data["summary"] = load_json(data["summary"])
        data["design"] = load_json(data.get("design"))
    return data


class CapCutRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---- jobs --------------------------------------------------------------------------------------------------
    def create_job(self, *, media_id: int, source_path: str, options: dict[str, Any], max_attempts: int, origin: str,
                   batch_id: int | None, capcut_batch_id: int | None = None, mode: str = "caption",
                   edit_job_id: int | None = None) -> int:
        now = utc_now()
        cursor = self.db.execute(
            "INSERT INTO capcut_jobs (media_id, source_path, status, max_attempts, options, origin, batch_id,"
            " capcut_batch_id, mode, edit_job_id, created_at, updated_at)"
            " VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (media_id, source_path, max_attempts, canonical_json(options), origin, batch_id, capcut_batch_id, mode,
             edit_job_id, now, now))
        return int(cursor.lastrowid)

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return decode_job(self.db.fetchone("SELECT * FROM capcut_jobs WHERE id = ?", (job_id,)))

    def get_job_view(self, job_id: int) -> dict[str, Any] | None:
        return decode_job(self.db.fetchone(
            f"SELECT {_LIST_COLUMNS} FROM {_LIST_FROM} WHERE j.id = ?",
            (job_id,)))

    def list_jobs(self, *, status: str | None, media_id: int | None, limit: int, offset: int,
                  capcut_batch_id: int | None = None) -> tuple[list[dict[str, Any]], int]:
        """Mais recentes primeiro; os jobs de um lote (`capcut_batch_id`) vêm na ordem da fila (id crescente)."""
        clauses, params = [], []
        if capcut_batch_id is not None:
            clauses.append("j.capcut_batch_id = ?")
            params.append(capcut_batch_id)
        if status is not None:
            clauses.append("j.status = ?")
            params.append(status)
        if media_id is not None:
            clauses.append("j.media_id = ?")
            params.append(media_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.fetchall(
            f"SELECT {_LIST_COLUMNS} FROM {_LIST_FROM} {where}"
            f" ORDER BY j.id {'ASC' if capcut_batch_id is not None else 'DESC'} LIMIT ? OFFSET ?",
            (*params, limit, offset))
        total = int(self.db.scalar(f"SELECT COUNT(*) FROM capcut_jobs j {where}", tuple(params)))
        return [decode_job(row) for row in rows], total

    def counts(self) -> dict[str, int]:
        """Por status; `review` (aguardando a revisão das legendas) fica fora de `waiting`."""
        rows = self.db.fetchall(
            f"SELECT CASE WHEN {REVIEW_SQL} THEN 'review' ELSE status END AS s, COUNT(*) AS n FROM capcut_jobs"
            " GROUP BY s")
        result = dict.fromkeys((*JOB_STATUSES, "review"), 0)
        result.update({row["s"]: int(row["n"]) for row in rows})
        return result

    def held_count(self) -> int:
        """Jobs na fila com pausa individual (`held`), fora os em revisão."""
        return int(self.db.scalar(
            f"SELECT COUNT(*) FROM capcut_jobs WHERE held = 1 AND status IN ('pending', 'waiting') AND {NOT_REVIEW_SQL}"))

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
        conta aqui (a espera por CapCut ocupado a devolve). Ficam de fora: job com pausa individual (`held`), job de
        lote pausado ou em cancelamento, job aguardando a revisão das legendas (`wait_reason='review'`) e job cuja
        edição (`edit_job_id`) ainda não terminou (job de edição inexistente não segura)."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT j.id FROM capcut_jobs j LEFT JOIN capcut_batches b ON b.id = j.capcut_batch_id"
                " LEFT JOIN jobs e ON e.id = j.edit_job_id"
                " WHERE j.status IN ('pending', 'waiting') AND j.cancel_requested = 0 AND j.held = 0"
                " AND (j.wait_reason IS NULL OR j.wait_reason <> 'review')"
                " AND (b.id IS NULL OR (b.paused = 0 AND b.cancel_requested = 0))"
                f" AND (j.edit_job_id IS NULL OR e.id IS NULL OR e.status IN {EDIT_TERMINAL_SQL})"
                " AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= ?) ORDER BY j.id LIMIT 1",
                (now,)).fetchone()
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

    def approve_review(self, job_id: int, *, transcript: list[dict[str, Any]], options: dict[str, Any]) -> bool:
        """Revisão aprovada: grava a transcrição e as opções e devolve o job à fila, só se ele ainda está em revisão
        e sem cancelamento pedido (condição na própria gravação). False = não estava mais em revisão."""
        cursor = self.db.execute(
            "UPDATE capcut_jobs SET transcript = ?, options = ?, status = 'pending', wait_reason = NULL,"
            " next_attempt_at = NULL, stage = NULL, stage_progress = NULL, progress = 0, detail = NULL, updated_at = ?"
            " WHERE id = ? AND status = 'waiting' AND wait_reason = 'review' AND cancel_requested = 0",
            (canonical_json(transcript), canonical_json(options), utc_now(), job_id))
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
                    "UPDATE capcut_jobs SET status = 'cancelled', held = 0, next_attempt_at = NULL, wait_reason = NULL,"
                    " finished_at = ?, updated_at = ? WHERE id = ?", (now, now, job_id))
            elif row["status"] == "running":
                conn.execute("UPDATE capcut_jobs SET cancel_requested = 1, updated_at = ? WHERE id = ?", (now, job_id))
            return row["status"]

    def retry(self, job_id: int) -> bool:
        """`failed`/`cancelled` voltam para a fila com as tentativas zeradas; a transcrição fica."""
        now = utc_now()
        cursor = self.db.execute(
            "UPDATE capcut_jobs SET status = 'pending', attempts = 0, cancel_requested = 0, held = 0,"
            " next_attempt_at = NULL,"
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
        `running` voltam para a fila devolvendo a tentativa da execução interrompida (nunca acima do máximo). Lote com
        cancelamento pedido (queda no meio do cancelamento): os jobs dele que restaram na fila terminam cancelados."""
        now = utc_now()
        with self.db.transaction() as conn:
            cancelled = conn.execute(
                "UPDATE capcut_jobs SET status = 'cancelled', finished_at = ?, updated_at = ?, next_attempt_at = NULL,"
                " wait_reason = NULL WHERE cancel_requested = 1 AND status IN ('pending', 'running', 'waiting')",
                (now, now)).rowcount
            # Lote com cancelamento pedido: o que restou aberto termina cancelado (no startup nada está rodando).
            cancelled += conn.execute(
                "UPDATE capcut_jobs SET status = 'cancelled', held = 0, finished_at = ?, updated_at = ?,"
                " next_attempt_at = NULL, wait_reason = NULL, stage = NULL, stage_progress = NULL, detail = NULL"
                " WHERE status IN ('pending', 'running', 'waiting')"
                " AND capcut_batch_id IN (SELECT id FROM capcut_batches WHERE cancel_requested = 1)",
                (now, now)).rowcount
            resumed = conn.execute(
                "UPDATE capcut_jobs SET status = 'pending', attempts = MAX(attempts - 1, 0), stage = NULL,"
                " stage_progress = NULL, progress = 0, detail = 'Retomado após interrupção', updated_at = ?"
                " WHERE status = 'running'", (now,)).rowcount
            conn.execute(
                "UPDATE capcut_batches SET finished_at = ?, updated_at = ? WHERE finished_at IS NULL AND NOT EXISTS"
                " (SELECT 1 FROM capcut_jobs j WHERE j.capcut_batch_id = capcut_batches.id"
                f" AND j.status IN {OPEN_STATUSES_SQL})", (now, now))
        return cancelled, resumed

    def hold(self, job_id: int, held: bool) -> bool:
        """Pausa individual: só job `pending`/`waiting` (False = estado não permite)."""
        cursor = self.db.execute(
            "UPDATE capcut_jobs SET held = ?, updated_at = ? WHERE id = ? AND status IN ('pending', 'waiting')",
            (1 if held else 0, utc_now(), job_id))
        return cursor.rowcount > 0

    # ---- lotes ------------------------------------------------------------------------------------------------
    def create_batch(self, *, name: str, mode: str, options: dict[str, Any], output_dir: str,
                     edit_batch_id: int | None) -> int:
        now = utc_now()
        cursor = self.db.execute(
            "INSERT INTO capcut_batches (name, mode, options, output_dir, edit_batch_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, mode, canonical_json(options), output_dir, edit_batch_id, now, now))
        return int(cursor.lastrowid)

    def update_batch(self, batch_id: int, **fields: Any) -> None:
        if "options" in fields:
            fields["options"] = canonical_json(fields["options"])
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self.db.execute(f"UPDATE capcut_batches SET {assignments}, updated_at = ? WHERE id = ?",
                        (*fields.values(), utc_now(), batch_id))

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        """Lote com as contagens (`counts`), `progress_sum` e o `status` derivado."""
        return decode_batch(self.db.fetchone(f"{_BATCH_SELECT} WHERE b.id = ?", (batch_id,)))

    def list_batches(self, *, status: str | None, mode: str | None, q: str | None, limit: int,
                     offset: int) -> tuple[list[dict[str, Any]], int]:
        """Lotes visíveis (sem os ocultos), id DESC, filtrados pelo status derivado, modo e nome."""
        clauses, params = ["b.hidden = 0"], []
        if mode is not None:
            clauses.append("b.mode = ?")
            params.append(mode)
        if q:
            clauses.append("b.name LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(q)}%")
        inner = f"{_BATCH_SELECT} WHERE {' AND '.join(clauses)}"
        outer = ""
        if status is not None:
            outer = "WHERE status = ?"
            params.append(status)
        rows = self.db.fetchall(f"SELECT * FROM ({inner}) {outer} ORDER BY id DESC LIMIT ? OFFSET ?",
                                (*params, limit, offset))
        total = int(self.db.scalar(f"SELECT COUNT(*) FROM ({inner}) {outer}", tuple(params)))
        return [decode_batch(row) for row in rows], total

    def batch_jobs(self, batch_id: int, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        """Jobs do lote nos estados pedidos (sem a transcrição), na ordem da fila."""
        rows = self.db.fetchall(
            "SELECT id, media_id, status, mode, edit_job_id, output_path, options, started_at, finished_at"
            f" FROM capcut_jobs WHERE capcut_batch_id = ? AND status IN ({placeholders(len(statuses))}) ORDER BY id",
            (batch_id, *statuses))
        return [decode_job(row) for row in rows]

    def running_in_batch(self, batch_id: int) -> dict[str, Any] | None:
        return decode_job(self.db.fetchone(
            f"SELECT {_LIST_COLUMNS} FROM {_LIST_FROM} WHERE j.capcut_batch_id = ? AND j.status = 'running'"
            " ORDER BY j.id LIMIT 1", (batch_id,)))

    def cancel_idle_in_batch(self, batch_id: int) -> int:
        """`pending`/`waiting` do lote terminam cancelados na hora (mesma gravação de `request_cancel`)."""
        now = utc_now()
        return self.db.execute(
            "UPDATE capcut_jobs SET status = 'cancelled', held = 0, next_attempt_at = NULL, wait_reason = NULL,"
            " finished_at = ?, updated_at = ? WHERE capcut_batch_id = ? AND status IN ('pending', 'waiting')",
            (now, now, batch_id)).rowcount

    def refresh_batch_finished(self, batch_id: int) -> None:
        """`finished_at` do lote: preenchido quando não resta job aberto; limpo quando volta a ter."""
        now = utc_now()
        self.db.execute(
            "UPDATE capcut_batches SET finished_at = CASE WHEN EXISTS (SELECT 1 FROM capcut_jobs j"
            f" WHERE j.capcut_batch_id = capcut_batches.id AND j.status IN {OPEN_STATUSES_SQL}) THEN NULL"
            " ELSE COALESCE(finished_at, ?) END, updated_at = ? WHERE id = ?", (now, now, batch_id))

    def average_seconds(self, *, batch_id: int | None = None, mode: str | None = None,
                        limit: int = 20) -> tuple[float | None, int]:
        """(média de finished_at - started_at em segundos, amostras) dos concluídos do lote ou, sem `batch_id`, dos
        `limit` últimos concluídos do `mode` (qualquer lote)."""
        if batch_id is not None:
            where, params, tail = "capcut_batch_id = ?", [batch_id], ""
        else:
            where, params, tail = "mode = ?", [mode], f" ORDER BY finished_at DESC LIMIT {int(limit)}"
        row = self.db.fetchone(
            "SELECT AVG(s) AS avg, COUNT(*) AS n FROM (SELECT (julianday(finished_at) - julianday(started_at))"
            f" * 86400.0 AS s FROM capcut_jobs WHERE status = 'completed' AND {where} AND started_at IS NOT NULL"
            f" AND finished_at IS NOT NULL{tail})", tuple(params))
        count = int(row["n"] or 0)
        if not count or row["avg"] is None:
            return None, count
        return max(0.0, float(row["avg"])), count

    def media_capcut_info(self, media_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Por mídia: concluídos com legenda (`caption`/`edit_caption`), saída do último legendado e se há job aberto
        (qualquer modo)."""
        if not media_ids:
            return {}
        rows = self.db.fetchall(
            "SELECT media_id, SUM(status = 'completed' AND mode IN ('caption', 'edit_caption')) AS done,"
            f" MAX(status IN {OPEN_STATUSES_SQL}) AS open,"
            " (SELECT x.output_path FROM capcut_jobs x WHERE x.media_id = capcut_jobs.media_id"
            "  AND x.status = 'completed' AND x.mode IN ('caption', 'edit_caption') ORDER BY x.id DESC LIMIT 1)"
            " AS last_output"
            f" FROM capcut_jobs WHERE media_id IN ({placeholders(len(media_ids))}) GROUP BY media_id",
            tuple(media_ids))
        return {int(row["media_id"]): dict(row) for row in rows}

    def done_media_ids(self, media_ids: list[int], mode: str) -> set[int]:
        """Mídias que já têm job concluído no `mode`."""
        if not media_ids:
            return set()
        rows = self.db.fetchall(
            "SELECT DISTINCT media_id FROM capcut_jobs WHERE status = 'completed' AND mode = ?"
            f" AND media_id IN ({placeholders(len(media_ids))})", (mode, *media_ids))
        return {row["media_id"] for row in rows}

    # ---- modelos de legenda -----------------------------------------------------------------------------------
    def list_styles(self) -> list[dict[str, Any]]:
        return [decode_style(row) for row in self.db.fetchall("SELECT * FROM capcut_styles ORDER BY name, id")]

    def get_style(self, style_id: int) -> dict[str, Any] | None:
        return decode_style(self.db.fetchone("SELECT * FROM capcut_styles WHERE id = ?", (style_id,)))

    def style_name_taken(self, name: str) -> bool:
        return self.db.scalar("SELECT 1 FROM capcut_styles WHERE name = ? COLLATE NOCASE", (name,)) is not None

    def create_style(self, *, name: str, source_draft: str | None, style: dict[str, Any],
                     summary: dict[str, Any] | None, design: dict[str, Any] | None = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO capcut_styles (name, source_draft, style, summary, design, is_default, created_at)"
            " VALUES (?, ?, ?, ?, ?, 0, ?)",
            (name, source_draft, canonical_json(style), None if summary is None else canonical_json(summary),
             None if design is None else canonical_json(design), utc_now()))
        return int(cursor.lastrowid)

    def update_design_style(self, style_id: int, *, name: str, style: dict[str, Any], summary: dict[str, Any] | None,
                            design: dict[str, Any]) -> bool:
        """Só modelo do editor (`design` não nulo); False se não existe ou foi copiado de um rascunho."""
        cursor = self.db.execute(
            "UPDATE capcut_styles SET name = ?, style = ?, summary = ?, design = ? WHERE id = ? AND design IS NOT NULL",
            (name, canonical_json(style), None if summary is None else canonical_json(summary), canonical_json(design),
             style_id))
        return cursor.rowcount > 0

    def style_name_taken_by_other(self, name: str, style_id: int) -> bool:
        return self.db.scalar("SELECT 1 FROM capcut_styles WHERE name = ? COLLATE NOCASE AND id <> ?",
                              (name, style_id)) is not None

    def delete_style(self, style_id: int) -> bool:
        return self.db.execute("DELETE FROM capcut_styles WHERE id = ?", (style_id,)).rowcount > 0

    def mark_default(self, style_id: int | None) -> None:
        """Espelha `default_style_id` das configurações (fonte da verdade) na coluna `is_default`."""
        self.db.execute("UPDATE capcut_styles SET is_default = (id IS ?)", (style_id,))
