from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from app.db.connection import Database, utc_now
from app.db.repositories.common import (
    ACTIVE_JOB_STATUSES,
    BATCH_PROGRESS_CONDITIONS,
    OPEN_JOB_STATUSES_SQL,
    WAITING_PRIMARY_SQL,
    canonical_json,
    load_json,
    placeholders,
    row_dict,
)

# Importação por link: aviso do lote concluído com parte dos links falha.
LINKS_PARTIAL_MESSAGE = "{failed} de {total} links não puderam ser importados."
# Lote por link cancelado enquanto esperava a ação do dono por uma consulta recusada (só o motivo, sem botões).
LINKS_HOLD_CANCELLED_MESSAGE = (
    "O Instagram recusou uma consulta deste lote antes do cancelamento. Isso não indica sessão inválida."
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
        # Por que a listagem terminou (migration 0012): 'profile_end' | 'limit'.
        "listing_end",
        # Lote por link (migration 0014): links seguidos falhos sem nenhum resolvido entre eles.
        "link_fail_streak",
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
        group_id: int | None = None,
        group_position: int | None = None,
        import_mode: str | None = None,
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
        if group_id is not None:   # filho de grupo (migration 0012); lote avulso não toca as colunas novas
            self.db.execute("UPDATE batches SET group_id = ?, group_position = ? WHERE id = ?",
                            (group_id, group_position, cursor.lastrowid))
        if import_mode is not None:   # lote por link (migration 0014); os demais não tocam a coluna nova
            self.db.execute("UPDATE batches SET import_mode = ? WHERE id = ?", (import_mode, cursor.lastrowid))
        return cursor.lastrowid

    def get(self, batch_id: int) -> dict[str, Any] | None:
        return decode_batch(self.db.fetchone("SELECT * FROM batches WHERE id = ?", (batch_id,)))

    def list(self, *, limit: int, offset: int, source: str | None = None, group_id: int | None = None,
             ungrouped: bool = False, history: bool = False) -> tuple[list[dict[str, Any]], int]:
        conditions: list[str] = []
        values: list[Any] = []
        if history:   # histórico de importações: sem os itens removidos dele (migration 0018)
            conditions.append("history_hidden_at IS NULL")
        if source:
            conditions.append("source = ?")
            values.append(source)
        if group_id is not None:
            conditions.append("group_id = ?")
            values.append(group_id)
        if ungrouped:
            conditions.append("group_id IS NULL")
        where, params = ("WHERE " + " AND ".join(conditions) if conditions else ""), tuple(values)
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

    def open_listing_profiles(self) -> set[str]:
        """Perfis (minúsculas) com listagem aberta: a criação de um grupo confere todos numa consulta só."""
        rows = self.db.fetchall(
            "SELECT source_profile FROM batches WHERE status = 'listing' AND source = 'instagram_import'"
            " AND source_profile IS NOT NULL"
        )
        return {str(row["source_profile"]).lower() for row in rows}

    # ---- importação por link (migration 0014) ------------------------------------------------------------------
    def add_links(self, batch_id: int, links: list[tuple[str, str]]) -> None:
        """Grava os links `(shortcode, url normalizada)` do lote na ordem dada (`position` 1-based), todos
        `pending`. Chamar dentro da transação que cria o lote."""
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT INTO batch_links (batch_id, position, shortcode, url) VALUES (?, ?, ?, ?)",
                [(batch_id, position, shortcode, url) for position, (shortcode, url) in enumerate(links, start=1)],
            )

    def next_pending_link(self, batch_id: int) -> dict[str, Any] | None:
        """Próximo link ainda não pedido ao Instagram, em ordem de `position`."""
        return row_dict(self.db.fetchone(
            "SELECT * FROM batch_links WHERE batch_id = ? AND status = 'pending' ORDER BY position LIMIT 1",
            (batch_id,)))

    def resolve_link(self, batch_id: int, position: int, *, items_found: int, profile: str | None) -> None:
        """Link resolvido (chamar na MESMA transação que cria os jobs das mídias dele)."""
        self.db.execute(
            "UPDATE batch_links SET status = 'resolved', items_found = ?, profile = ?, resolved_at = ?,"
            " error_code = NULL, error_message = NULL WHERE batch_id = ? AND position = ?",
            (items_found, profile, utc_now(), batch_id, position),
        )

    def fail_link(self, batch_id: int, position: int, *, code: str, message: str) -> None:
        """Erro só deste link: não é pedido de novo e não muda o estado dos outros."""
        self.db.execute(
            "UPDATE batch_links SET status = 'failed', error_code = ?, error_message = ?, resolved_at = ?"
            " WHERE batch_id = ? AND position = ? AND status = 'pending'",
            (code, message, utc_now(), batch_id, position),
        )

    def bump_link_attention(self, batch_id: int, position: int) -> int:
        """Mais uma vez em que este link deixaria o lote em `needs_attention`. Retorna o total do link."""
        row = self.db.fetchone(
            "UPDATE batch_links SET attention_count = attention_count + 1 WHERE batch_id = ? AND position = ?"
            " RETURNING attention_count", (batch_id, position))
        return int(row[0]) if row is not None else 0

    # ---- lote por link: recusa em espera (docs/BATCH_ROBUSTNESS.md, seção 0.2) --------------------------------
    # Estado único e persistente, sem coluna nova: `batches.error_code = 'access_refused'` num lote
    # `import_mode = 'links'`. Enquanto vale, nem o runner (próximo link) nem a renovação de URL consultam o
    # Instagram por esse lote. Só os dois caminhos de consulta armam (sob a trava do lote) e só a ação do dono desarma.
    def links_hold(self, batch_id: int) -> bool:
        """O lote por link está com a recusa em espera (aguarda a ação do dono). Lote cancelado nunca está: é
        terminal e nada mais consulta por ele (o `error_code` fica só como registro do motivo)."""
        return self.db.scalar(
            "SELECT 1 FROM batches WHERE id = ? AND import_mode = 'links' AND error_code = 'access_refused'"
            " AND status <> 'cancelled'", (batch_id,)) is not None

    def arm_links_hold(self, batch_id: int, message: str) -> None:
        """Arma a recusa em espera (quem chama segura a trava de consulta do lote). Só em lote ainda aberto
        (`listing`/`running`): lote cancelado, falho ou já concluído não é armado (não há mais consulta a
        segurar, e um lote concluído ficaria com o erro preso)."""
        self.db.execute(
            "UPDATE batches SET error_code = 'access_refused', error_message = ?, updated_at = ?"
            " WHERE id = ? AND import_mode = 'links' AND status IN ('listing', 'running')",
            (message, utc_now(), batch_id))

    def release_cancelled_links_hold(self, batch_id: int) -> None:
        """Lote por link cancelado com a recusa em espera: a espera deixa de existir. O `error_code` continua como
        registro do motivo (como nos outros erros de lote cancelado); a mensagem deixa de apontar botões."""
        self.db.execute(
            "UPDATE batches SET error_message = ?, updated_at = ? WHERE id = ? AND import_mode = 'links'"
            " AND error_code = 'access_refused' AND status = 'cancelled'",
            (LINKS_HOLD_CANCELLED_MESSAGE, utc_now(), batch_id))

    def _release_finished_links_hold(self, batch_id: int) -> None:
        """Lote por link que acabou de concluir (todos os jobs terminais) com a recusa em espera: sem job falho
        que o dono possa repetir nada mais consulta sozinho, então a espera é desarmada e o lote fica com o aviso
        normal ("X de N links não puderam ser importados." quando há link falho). Com job falho ela permanece
        ("Tentar falhas novamente" existe e desarma)."""
        cursor = self.db.execute(
            "UPDATE batches SET error_code = NULL, error_message = NULL WHERE id = ?"  # só lote por link chega aqui
            " AND error_code = 'access_refused' AND status = 'completed'"
            " AND NOT EXISTS (SELECT 1 FROM jobs WHERE batch_id = ? AND status = 'failed')",
            (batch_id, batch_id))
        if cursor.rowcount > 0:
            counts = self.link_counts([batch_id]).get(batch_id, {})
            if counts.get("failed"):
                self.db.execute(
                    "UPDATE batches SET error_message = ? WHERE id = ?",
                    (LINKS_PARTIAL_MESSAGE.format(failed=counts["failed"], total=counts.get("total", 0)), batch_id))

    def clear_links_hold(self, batch_id: int) -> bool:
        """Desarma a recusa em espera de um lote que já saiu de `listing` (ação do dono: repetir jobs falhos). Com o
        lote ainda em `listing` quem desarma é `resume_listing` ("Retomar importação"). True se desarmou."""
        cursor = self.db.execute(
            "UPDATE batches SET error_code = NULL, error_message = NULL, updated_at = ?"
            " WHERE id = ? AND import_mode = 'links' AND error_code = 'access_refused' AND status <> 'listing'",
            (utc_now(), batch_id))
        return cursor.rowcount > 0

    def link_position(self, batch_id: int, shortcode: str) -> int | None:
        """`position` do link desta publicação no lote (chave do registro de renovação de URL em `listing_pages`)."""
        value = self.db.scalar(
            "SELECT position FROM batch_links WHERE batch_id = ? AND shortcode = ?", (batch_id, shortcode))
        return None if value is None else int(value)

    def link_counts(self, batch_ids: list[int]) -> dict[int, dict[str, int]]:
        """`{total, pending, resolved, failed}` dos links de cada lote, numa consulta para a lista inteira."""
        if not batch_ids:
            return {}
        rows = self.db.fetchall(
            "SELECT batch_id, COUNT(*) AS total, SUM(status = 'pending') AS pending,"
            " SUM(status = 'resolved') AS resolved, SUM(status = 'failed') AS failed"
            f" FROM batch_links WHERE batch_id IN ({placeholders(len(batch_ids))}) GROUP BY batch_id",
            tuple(batch_ids),
        )
        return {row["batch_id"]: {key: int(row[key] or 0) for key in ("total", "pending", "resolved", "failed")}
                for row in rows}

    def list_links(self, batch_id: int, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        """Links do lote em ordem de `position` (paginação por `offset`) e o total."""
        total = int(self.db.scalar("SELECT COUNT(*) FROM batch_links WHERE batch_id = ?", (batch_id,)))
        rows = self.db.fetchall(
            "SELECT * FROM batch_links WHERE batch_id = ? ORDER BY position LIMIT ? OFFSET ?",
            (batch_id, limit, offset))
        return [row_dict(row) for row in rows], total

    # ---- grupos de importação (migration 0012) -----------------------------------------------------------------
    def create_group(self, *, content_type: str, requested_limit: int | None, preset_id: int | None,
                     preset_name: str | None, params: dict[str, Any] | None, process: bool) -> int:
        cursor = self.db.execute(
            "INSERT INTO import_groups (content_type, requested_limit, preset_id, preset_name, params, process,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (content_type, requested_limit, preset_id, preset_name,
             None if params is None else canonical_json(params), int(process), utc_now()),
        )
        return cursor.lastrowid

    def get_group(self, group_id: int) -> dict[str, Any] | None:
        return row_dict(self.db.fetchone("SELECT * FROM import_groups WHERE id = ?", (group_id,)))

    def list_groups(self, *, limit: int, before_id: int | None = None,
                    history: bool = False) -> tuple[list[dict[str, Any]], int, int | None]:
        """Grupos em ordem id DESC. `before_id` (keyset): só ids menores; `next_before_id` é None no fim. `history`:
        sem os grupos removidos do histórico (migration 0018)."""
        hidden = "history_hidden_at IS NULL" if history else "1 = 1"
        total = int(self.db.scalar(f"SELECT COUNT(*) FROM import_groups WHERE {hidden}"))
        where, params = ((f"WHERE id < ? AND {hidden}", (before_id,)) if before_id is not None
                         else (f"WHERE {hidden}", ()))
        rows = self.db.fetchall(
            f"SELECT * FROM import_groups {where} ORDER BY id DESC LIMIT ?", (*params, limit + 1))
        next_before_id = rows[limit - 1]["id"] if len(rows) > limit else None
        return [row_dict(row) for row in rows[:limit]], total, next_before_id

    def group_children(self, group_id: int, *, limit: int,
                       after_position: int | None = None) -> tuple[list[dict[str, Any]], int | None]:
        """Filhos do grupo em ordem de `group_position` (keyset `after_position`)."""
        rows = self.db.fetchall(
            "SELECT * FROM batches WHERE group_id = ? AND group_position > ? ORDER BY group_position LIMIT ?",
            (group_id, after_position or 0, limit + 1),
        )
        next_after = rows[limit - 1]["group_position"] if len(rows) > limit else None
        return [decode_batch(row) for row in rows[:limit]], next_after

    def group_open_children(self, group_id: int) -> list[dict[str, Any]]:
        """`{id, status}` dos filhos ainda abertos (cancelamento do grupo), em ordem de `group_position`."""
        rows = self.db.fetchall(
            "SELECT id, status FROM batches WHERE group_id = ? AND status IN ('listing', 'running')"
            " ORDER BY group_position", (group_id,))
        return [{"id": int(row["id"]), "status": row["status"]} for row in rows]

    def group_aggregates(self, group_ids: list[int]) -> dict[int, dict[str, int | str | None]]:
        """Agregados por grupo em DUAS consultas, qualquer que seja o número de filhos (sem N+1): estados dos filhos
        e contadores congelados numa, e os jobs dos filhos ainda não congelados na outra (índice coberto)."""
        result: dict[int, dict[str, Any]] = {}
        if not group_ids:
            return result
        marks = placeholders(len(group_ids))
        frozen = ", ".join(f"SUM(COALESCE(final_{key}, 0)) AS {key}" for key in BATCH_PROGRESS_CONDITIONS)
        rows = self.db.fetchall(
            f"""
            SELECT group_id,
                   COUNT(*) AS total,
                   SUM(status = 'completed') AS completed,
                   SUM(status = 'failed') AS failed_profiles,
                   SUM(status = 'cancelled') AS cancelled,
                   SUM(status = 'listing' AND listing_state = 'needs_attention') AS attention,
                   SUM(status = 'listing' AND COALESCE(listing_state, 'pending') = 'pending'
                       AND listing_next_page IS NULL) AS waiting,
                   SUM(COALESCE(final_found, found)) AS found,
                   SUM(listing_dropped_unexpected) AS dropped,
                   SUM(listing_end = 'profile_end') AS profile_end,
                   MAX(finished_at) AS finished_at,
                   {frozen}
            FROM batches WHERE group_id IN ({marks}) GROUP BY group_id
            """,
            tuple(group_ids),
        )
        for row in rows:
            data = {key: (row[key] if key == "finished_at" else int(row[key] or 0)) for key in row.keys()}
            data.update(pending=0, active=0)
            result[data.pop("group_id")] = data
        live = ", ".join(f"SUM({cond}) AS {key}" for key, cond in BATCH_PROGRESS_CONDITIONS.items())
        active = ", ".join(f"'{s}'" for s in ACTIVE_JOB_STATUSES)
        rows = self.db.fetchall(
            f"""
            SELECT (SELECT group_id FROM batches WHERE id = jobs.batch_id) AS group_id,
                   {live},
                   SUM(status = 'pending') AS pending,
                   SUM(status IN ({active})) AS active
            FROM jobs
            WHERE batch_id IN (SELECT id FROM batches WHERE group_id IN ({marks}) AND final_found IS NULL)
            GROUP BY 1
            """,
            tuple(group_ids),
        )
        for row in rows:
            data = result.get(row["group_id"])
            if data is not None:
                for key in (*BATCH_PROGRESS_CONDITIONS, "pending", "active"):
                    data[key] += int(row[key] or 0)
        return result

    def start_listing(self, batch_id: int, *, clear_error: bool = False) -> bool:
        """Transição ATÔMICA para `running` no início de um trecho: só se o lote ainda está em `listing`. False (nada
        gravado) se foi cancelado/encerrado entre a escolha do lote e o início do trecho: quem chamou não lista."""
        extra = ", error_code = NULL, error_message = NULL" if clear_error else ""
        cursor = self.db.execute(
            f"UPDATE batches SET listing_state = 'running', listing_retry_at = NULL{extra}, updated_at = ?"
            " WHERE id = ? AND status = 'listing'", (utc_now(), batch_id))
        return cursor.rowcount > 0

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
            " listing_dropped_unexpected = 0, listing_dropped_reasons = NULL, listing_dropped_partial = NULL,"
            " listing_end = NULL"
        ) if from_start else ""
        cursor = self.db.execute(
            "UPDATE batches SET listing_state = 'pending', listing_retry_at = NULL, listing_retry_level = 0,"
            f" listing_cursor_refusals = 0, listing_attention_tries = 0, link_fail_streak = 0{restart},"
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
            " RETURNING status, import_mode, error_code",
            {"now": now, "id": batch_id},
        )
        done = row is not None and row["status"] == "completed"  # só `running` vira `completed` aqui
        if done and row["import_mode"] == "links" and row["error_code"] == "access_refused":
            # Ponto único em que o lote conclui (worker, recovery, fim da listagem). Só lote por link com a recusa em
            # espera faz a instrução extra: a conclusão dos outros lotes continua com uma instrução só.
            self._release_finished_links_hold(batch_id)
        return done

    def reopen(self, batch_id: int) -> None:
        """Lote volta a `running` (ex.: retry de jobs) se já estava finalizado com jobs."""
        self.db.execute(
            f"UPDATE batches SET status = 'running', finished_at = NULL, {_UNFREEZE_SQL}, updated_at = ?"
            " WHERE id = ? AND status IN ('completed', 'cancelled', 'failed')",
            (utc_now(), batch_id),
        )
        # Lote reaberto volta ao histórico de importações (migration 0018), e o grupo dele também.
        self.db.execute("UPDATE batches SET history_hidden_at = NULL WHERE id = ? AND history_hidden_at IS NOT NULL",
                        (batch_id,))
        self.db.execute("UPDATE import_groups SET history_hidden_at = NULL WHERE history_hidden_at IS NOT NULL"
                        " AND id = (SELECT group_id FROM batches WHERE id = ?)", (batch_id,))
