"""Análise de Perfis (migration 0025): fila persistente (uma por vez, FIFO), itens por perfil e agregados."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from app.db.connection import Database, utc_now
from app.db.repositories.common import canonical_json, load_json, placeholders, row_dict

OPEN_STATUSES = ("pending", "running", "waiting")
OPEN_STATUSES_SQL = "('pending', 'running', 'waiting')"
# Quem já começou vem antes: uma análise antiga retomada (`pending`, id menor) não fura a que está em andamento.
_QUEUE_ORDER = "ORDER BY status = 'pending', id"
TERMINAL_STATUSES = ("completed", "cancelled", "interrupted")
RESUMABLE_STATUSES = ("cancelled", "interrupted")
ITEMS_PAGE = 500
SNAPSHOT_COLUMNS = ("account_status", "display_name", "posts_state", "posts_count", "posts_exact", "posts_text",
                    "views_state", "views_total", "views_reels", "stories", "highlights", "error_code",
                    "error_message")
_STATS = (
    "SELECT analysis_id, SUM(state = 'done') AS done, SUM(state IN ('pending', 'running')) AS pending,"
    " MAX(CASE WHEN state = 'running' THEN username END) AS running_username,"
    " SUM(state = 'cancelled') AS cancelled, SUM(account_status = 'active') AS active,"
    " SUM(account_status = 'private') AS private, SUM(account_status = 'restricted') AS restricted,"
    " SUM(account_status = 'unavailable') AS unavailable,"
    " SUM(account_status = 'unverified') AS unverified, SUM(stories = 'yes') AS stories_yes,"
    " SUM(highlights = 'yes') AS highlights_yes,"
    " SUM(CASE WHEN views_state = 'known' THEN views_total ELSE 0 END) AS views_total"
    " FROM profile_analysis_items WHERE analysis_id IN ({ids}) GROUP BY analysis_id"
)


def decode_analysis(row) -> dict[str, Any] | None:
    data = row_dict(row)
    if data is not None:
        data["invalid_lines"] = load_json(data["invalid_lines"]) or []
    return data


class ProfileAnalysisRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---- registro ----------------------------------------------------------------------------------------------
    def create(self, usernames: Iterable[str], *, invalid_count: int, duplicate_count: int,
               invalid_lines: list[dict[str, str]]) -> dict[str, Any]:
        names = list(usernames)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO profile_analyses (status, total, invalid_count, duplicate_count, invalid_lines,"
                " created_at) VALUES ('pending', ?, ?, ?, ?, ?)",
                (len(names), invalid_count, duplicate_count, canonical_json(invalid_lines), utc_now()),
            )
            analysis_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO profile_analysis_items (analysis_id, position, username) VALUES (?, ?, ?)",
                ((analysis_id, position, name) for position, name in enumerate(names, start=1)),
            )
        return self.get(analysis_id)

    def get(self, analysis_id: int) -> dict[str, Any] | None:
        return decode_analysis(self.db.fetchone("SELECT * FROM profile_analyses WHERE id = ?", (analysis_id,)))

    def list(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        rows = self.db.fetchall("SELECT * FROM profile_analyses ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
        return [decode_analysis(row) for row in rows]

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM profile_analyses"))

    def stats(self, ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        """Agregados dos itens por análise: os gravados ao terminar e, para as abertas, uma varredura dos itens."""
        wanted = list(ids)
        if not wanted:
            return {}
        stored = self.db.fetchall(
            f"SELECT id, final_stats FROM profile_analyses WHERE id IN ({placeholders(len(wanted))})"
            " AND final_stats IS NOT NULL", tuple(wanted))
        result = {row["id"]: load_json(row["final_stats"]) for row in stored}
        live = [analysis_id for analysis_id in wanted if analysis_id not in result]
        if live:
            rows = self.db.fetchall(_STATS.format(ids=placeholders(len(live))), tuple(live))
            result.update({row["analysis_id"]: dict(row) for row in rows})
        return result

    @staticmethod
    def _store_stats(conn, analysis_id: int) -> None:
        """Na transação que encerra a análise, depois da última mudança nos itens."""
        row = conn.execute(_STATS.format(ids="?"), (analysis_id,)).fetchone()
        conn.execute("UPDATE profile_analyses SET final_stats = ? WHERE id = ?",
                     (canonical_json(dict(row) if row else {}), analysis_id))

    def items(self, analysis_id: int, *, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM profile_analysis_items WHERE analysis_id = ? ORDER BY position LIMIT ? OFFSET ?",
            (analysis_id, limit, offset),
        )
        return [dict(row) for row in rows]

    def iter_items(self, analysis_id: int, page: int = ITEMS_PAGE) -> Iterator[dict[str, Any]]:
        """Todos os itens por `position`, lidos em blocos (keyset): memória limitada a um bloco, sem cursor aberto."""
        after = 0
        while True:
            rows = self.db.fetchall(
                "SELECT * FROM profile_analysis_items WHERE analysis_id = ? AND position > ? ORDER BY position LIMIT ?",
                (analysis_id, after, page))
            if not rows:
                return
            after = rows[-1]["position"]
            for row in rows:
                yield dict(row)

    # ---- fila --------------------------------------------------------------------------------------------------
    def claim_next(self, now: str) -> dict[str, Any] | None:
        """A análise aberta mais antiga; `pending` vira `running`, `running`/`waiting` seguem como estão (retomada).
        Item `running` deixado por uma execução que caiu volta para a fila, com a tentativa contada: é o que limita a
        repetição de um item que falha fora da consulta."""
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT id, status FROM profile_analyses WHERE status IN {OPEN_STATUSES_SQL} {_QUEUE_ORDER} LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "pending":
                conn.execute(
                    "UPDATE profile_analyses SET status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ?",
                    (now, row["id"]))
            self._requeue_running(conn, row["id"])
        return self.get(row["id"])

    @staticmethod
    def _requeue_running(conn, analysis_id: int) -> None:
        conn.execute("UPDATE profile_analysis_items SET state = 'pending' WHERE analysis_id = ? AND state = 'running'",
                     (analysis_id,))

    def current_id(self) -> int | None:
        """A análise que o executor pega agora: a que já está em andamento ou, sem ela, a mais antiga na fila."""
        return self.db.scalar(
            f"SELECT id FROM profile_analyses WHERE status IN {OPEN_STATUSES_SQL} {_QUEUE_ORDER} LIMIT 1")

    def open_analyses(self) -> list[dict[str, Any]]:
        rows = self.db.fetchall(f"SELECT * FROM profile_analyses WHERE status IN {OPEN_STATUSES_SQL} ORDER BY id")
        return [decode_analysis(row) for row in rows]

    def requeue_running(self, analysis_id: int) -> None:
        """Startup: a tentativa do item em voo fica contada. Ele ainda é consultado de novo, mas `MAX_ATTEMPTS` vale
        entre reinícios: um ciclo de quedas não repete o mesmo perfil sem limite."""
        with self.db.transaction() as conn:
            self._requeue_running(conn, analysis_id)

    def cancel_requested(self, analysis_id: int) -> bool:
        return bool(self.db.scalar("SELECT cancel_requested FROM profile_analyses WHERE id = ?", (analysis_id,)))

    def request_cancel(self, analysis_id: int) -> str | None:
        """`pending` termina cancelada na hora; `running`/`waiting` só recebem a marca (o executor para). Terminal não
        muda. Retorna o status ANTERIOR (None se a análise não existe)."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status FROM profile_analyses WHERE id = ?", (analysis_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "pending":
                conn.execute("UPDATE profile_analyses SET cancel_requested = 1 WHERE id = ?", (analysis_id,))
                self._finish_cancelled(conn, analysis_id, utc_now())
            elif row["status"] in ("running", "waiting"):
                conn.execute("UPDATE profile_analyses SET cancel_requested = 1 WHERE id = ?", (analysis_id,))
            return row["status"]

    @staticmethod
    def _finish_cancelled(conn, analysis_id: int, now: str) -> None:
        conn.execute(
            "UPDATE profile_analysis_items SET state = 'cancelled' WHERE analysis_id = ?"
            " AND state IN ('pending', 'running')", (analysis_id,))
        conn.execute(
            "UPDATE profile_analyses SET status = 'cancelled', waiting_until = NULL, wait_reason = NULL,"
            " finished_at = ? WHERE id = ?", (now, analysis_id))
        ProfileAnalysisRepository._store_stats(conn, analysis_id)

    def finish_cancelled(self, analysis_id: int, now: str) -> None:
        with self.db.transaction() as conn:
            self._finish_cancelled(conn, analysis_id, now)

    def finish(self, analysis_id: int, status: str, now: str, *, error: str | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE profile_analyses SET status = ?, error = ?, waiting_until = NULL, wait_reason = NULL,"
                " finished_at = ? WHERE id = ?", (status, error, now, analysis_id))
            self._store_stats(conn, analysis_id)

    def set_waiting(self, analysis_id: int, reason: str, until: str | None, level: int) -> None:
        self.db.execute(
            "UPDATE profile_analyses SET status = 'waiting', wait_reason = ?, waiting_until = ?, wait_level = ?"
            " WHERE id = ?", (reason, until, level, analysis_id))

    def resume(self, analysis_id: int) -> None:
        """Fim da espera: o degrau (`wait_level`) continua até um sucesso."""
        self.db.execute(
            "UPDATE profile_analyses SET status = 'running', wait_reason = NULL, waiting_until = NULL WHERE id = ?",
            (analysis_id,))

    def reset_level(self, analysis_id: int) -> None:
        self.db.execute("UPDATE profile_analyses SET wait_level = 0 WHERE id = ?", (analysis_id,))

    def set_cautious(self, analysis_id: int) -> None:
        self.db.execute("UPDATE profile_analyses SET cautious_pace = 1 WHERE id = ?", (analysis_id,))

    def set_resume_after(self, analysis_id: int, position: int) -> None:
        """Sequência confirmada pelo canário: ele só volta a olhar adiante desta posição."""
        self.db.execute("UPDATE profile_analyses SET resume_after = ? WHERE id = ?", (position, analysis_id))

    def resume_analysis(self, analysis_id: int) -> bool:
        """Retomada de análise terminal com itens não consultados, numa só transação: eles voltam à fila e a análise
        reabre. Itens `done` não mudam, e `resume_after` deixa todos fora do alcance do canário. `started_at` e
        `created_*` ficam. False = nada a retomar (inclusive o 2o clique, que já encontra a análise aberta)."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status FROM profile_analyses WHERE id = ?", (analysis_id,)).fetchone()
            if row is None or row["status"] not in RESUMABLE_STATUSES:
                return False
            reopened = conn.execute(
                "UPDATE profile_analysis_items SET state = 'pending', error_code = NULL, error_message = NULL,"
                " attempts = 0 WHERE analysis_id = ? AND state = 'cancelled'", (analysis_id,))
            if reopened.rowcount == 0:
                return False
            conn.execute(
                "UPDATE profile_analyses SET status = 'pending', cancel_requested = 0, error = NULL,"
                " waiting_until = NULL, wait_reason = NULL, finished_at = NULL, wait_level = 0, final_stats = NULL,"
                " resume_after = COALESCE((SELECT MAX(position) FROM profile_analysis_items"
                " WHERE analysis_id = ? AND state = 'done'), 0) WHERE id = ?", (analysis_id, analysis_id))
        return True

    # ---- itens -------------------------------------------------------------------------------------------------
    def next_item(self, analysis_id: int) -> dict[str, Any] | None:
        """Próximo item. `running` aqui é um item que não pôde ser devolvido à fila por falha de gravação."""
        return row_dict(self.db.fetchone(
            "SELECT * FROM profile_analysis_items WHERE analysis_id = ? AND state IN ('pending', 'running')"
            " ORDER BY position LIMIT 1", (analysis_id,)))

    def start_item(self, item_id: int) -> None:
        self.db.execute(
            "UPDATE profile_analysis_items SET state = 'running', attempts = attempts + 1 WHERE id = ?", (item_id,))

    def release_item(self, item_id: int, *, consume_attempt: bool = False) -> None:
        """O item volta para a fila; só a nova tentativa após timeout/rede conta a tentativa já feita."""
        decrement = 0 if consume_attempt else 1
        self.db.execute(
            "UPDATE profile_analysis_items SET state = 'pending', attempts = MAX(attempts - ?, 0) WHERE id = ?",
            (decrement, item_id))

    def save_result(self, item_id: int, fields: Mapping[str, Any], checked_at: str) -> None:
        """Item `done` com as colunas de `SNAPSHOT_COLUMNS` presentes em `fields`."""
        assignments = ", ".join(f"{column} = ?" for column in SNAPSHOT_COLUMNS)
        self.db.execute(
            f"UPDATE profile_analysis_items SET state = 'done', checked_at = ?, {assignments} WHERE id = ?",
            (checked_at, *(fields.get(column) for column in SNAPSHOT_COLUMNS), item_id))

    def finish_interrupted(self, analysis_id: int, *, code: str, message: str, error: str, now: str) -> bool:
        """Análise interrompida, numa só transação: o que faltava nunca foi consultado, então fica `cancelled`, sem
        `checked_at` e sem status de conta. `error_code` = motivo; `cancelled` com `error_code` nulo é cancelamento do
        usuário. Com cancelamento já pedido nada muda e retorna False: vale o pedido do usuário."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT cancel_requested FROM profile_analyses WHERE id = ?", (analysis_id,)).fetchone()
            if row["cancel_requested"]:
                return False
            conn.execute(
                "UPDATE profile_analysis_items SET state = 'cancelled', account_status = NULL, checked_at = NULL,"
                " error_code = ?, error_message = ? WHERE analysis_id = ? AND state IN ('pending', 'running')",
                (code, message, analysis_id))
            conn.execute(
                "UPDATE profile_analyses SET status = 'interrupted', error = ?, waiting_until = NULL,"
                " wait_reason = NULL, finished_at = ? WHERE id = ?", (error, now, analysis_id))
            self._store_stats(conn, analysis_id)
        return True

    def reopen_items(self, item_ids: Iterable[int]) -> None:
        """Resultado não confirmado pelo canário: o item volta para a fila como se nunca tivesse sido consultado."""
        ids = list(item_ids)
        cleared = ", ".join(f"{column} = NULL" for column in SNAPSHOT_COLUMNS)
        self.db.execute(
            f"UPDATE profile_analysis_items SET state = 'pending', attempts = 0, checked_at = NULL, {cleared}"
            f" WHERE id IN ({placeholders(len(ids))})", tuple(ids))

    def trailing_unverified(self, analysis_id: int, limit: int) -> int:
        """Quantos dos últimos perfis concluídos (até `limit`) terminaram `unverified` seguidos, ignorando `unavailable`
        (não confirma nem nega nada): vem do banco, então o disjuntor atravessa um reinício."""
        rows = self.db.fetchall(
            "SELECT account_status FROM profile_analysis_items WHERE analysis_id = ? AND state = 'done'"
            " AND account_status <> 'unavailable' ORDER BY position DESC LIMIT ?", (analysis_id, limit))
        streak = 0
        for row in rows:
            if row["account_status"] != "unverified":
                break
            streak += 1
        return streak

    def trailing_unavailable(self, analysis_id: int, after_position: int, limit: int) -> list[dict[str, Any]]:
        """Últimos perfis concluídos depois de `after_position` (até `limit`) que terminaram `unavailable` seguidos: a
        sequência do canário vem do banco, então atravessa um reinício."""
        rows = self.db.fetchall(
            "SELECT id, position, account_status FROM profile_analysis_items WHERE analysis_id = ? AND state = 'done'"
            " AND position > ? ORDER BY position DESC LIMIT ?", (analysis_id, after_position, limit))
        run: list[dict[str, Any]] = []
        for row in rows:
            if row["account_status"] != "unavailable":
                break
            run.append(dict(row))
        return run
