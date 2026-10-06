from __future__ import annotations

from typing import Any

from app.db.connection import Database, utc_now
from app.db.repositories.common import (
    ACTIVE_JOB_STATUSES,
    BATCH_PROGRESS_CONDITIONS,
    JOB_STATUSES,
    OPEN_JOB_STATUSES_SQL,
    TERMINAL_JOB_STATUSES,
    WAITING_PRIMARY_SQL,
    canonical_json,
    delete_batch_remote_refs,
    load_json,
    placeholders,
    row_dict,
)

JOB_UPDATE_COLUMNS = frozenset(
    {
        "media_id", "rendition_id", "status", "stage", "progress", "error_code", "error_message", "attempts",
        "max_attempts", "next_attempt_at", "cancel_requested", "is_duplicate", "duplicate_reason", "temp_path",
        "started_at", "finished_at", "heartbeat_at", "media_url_refreshed_at",
    }
)
SOURCE_COLUMNS = (
    "source_platform", "source_profile", "source_url", "source_post_id", "source_media_index", "post_id",
    "media_id_remote", "media_type", "taken_at",
    # Referência de página (não sensível): índice de página do lote e renovação da página 1 de jobs repetidos.
    "listing_page_index", "listing_page_size",
)
# Referência remota do job (tabela job_remote_refs; URLs e cursor são sensíveis: nunca saem pela API nem por log).
# Só serve enquanto o download está pendente: linha apagada quando o job conclui; URLs apagadas quando falha ou é
# cancelado (cursor/página ficam para o retry com renovação); tudo apagado quando o lote conclui ou é cancelado.
# Lote concluído: o trigger tr_batches_completed_drop_refs (migration 0005) apaga tudo na instrução da conclusão.
REMOTE_REF_COLUMNS = ("media_url", "thumbnail_url", "listing_cursor", "listing_page_index", "listing_page_size")
# Elegibilidade do claim; uma busca por status em ordem de id (ix_jobs_status_id) para pending e outra para
# downloaded, cada uma parando no primeiro elegível (sem ordenar todos os pendentes).
_CLAIM_ELIGIBLE = """claimed_by IS NULL AND cancel_requested = 0
                  AND (next_attempt_at IS NULL OR next_attempt_at <= :now)
                  AND (:allow = 1 OR NOT (kind = 'ingest' AND status = 'pending' AND media_id IS NULL))
                  AND (:url_only = 0 OR NOT (kind = 'ingest' AND status = 'pending' AND media_id IS NULL)
                       OR EXISTS (SELECT 1 FROM job_remote_refs r WHERE r.job_id = jobs.id
                                  AND r.media_url IS NOT NULL))
                  AND (primary_job_id IS NULL OR NOT EXISTS (SELECT 1 FROM jobs p WHERE p.id = jobs.primary_job_id
                                                             AND p.status IN ('pending', 'downloading')))"""
# Pausa do disjuntor que bloqueia cada pool (escopo da pausa do lote).
_PAUSE_SCOPE_SQL: dict[str | None, str] = {
    None: "",
    "download": "AND COALESCE(paused_scope, 'ingest') = 'ingest'",
    "process": "AND paused_scope = 'process'",
}
# Interrupções seguidas (reinício abrupto) do mesmo job antes de falhar com interrupted_repeatedly.
MAX_CRASHES = 3
# Ramos do claim por pool (status, filtro de tipo); cada um para no primeiro elegível em ordem de id.
_POOL_BRANCHES: dict[str | None, list[tuple[str, str]]] = {
    None: [("pending", ""), ("downloaded", "")],
    "download": [("pending", "AND kind = 'ingest'")],
    "process": [("pending", "AND kind = 'process'"), ("downloaded", "")],
}
_CLEAR_URLS_SQL = "UPDATE job_remote_refs SET media_url = NULL, thumbnail_url = NULL WHERE job_id = ?"

_TERMINAL_SQL = "('completed', 'failed', 'cancelled')"


def decode_job(row) -> dict[str, Any] | None:
    data = row_dict(row)
    if data is not None:
        data["params"] = load_json(data["params"])
        data["is_duplicate"] = bool(data["is_duplicate"])
        data["cancel_requested"] = bool(data["cancel_requested"])
    return data


class JobRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---- criação -----------------------------------------------------------------------------------------------
    def create_ingest(
        self,
        *,
        batch_id: int,
        source: dict[str, Any],
        preset_id: int | None,
        preset_name: str | None,
        params: dict[str, Any] | None,
        media_id: int | None = None,
        is_duplicate: bool = False,
        duplicate_reason: str | None = None,
        completed_skipped: bool = False,
        primary_job_id: int | None = None,
    ) -> int | None:
        """Cria job de ingestão. Retorna None se o item já existe no lote (chave de origem).

        `primary_job_id`: job de outro lote que já vai baixar o mesmo item; este fica `pending` aguardando-o."""
        now = utc_now()
        data: dict[str, Any] = {column: source.get(column) for column in SOURCE_COLUMNS}
        data.update(
            batch_id=batch_id,
            media_id=media_id,
            kind="ingest",
            preset_id=preset_id,
            preset_name=preset_name,
            params=None if params is None else canonical_json(params),
            status="completed" if completed_skipped else "pending",
            stage="skipped" if completed_skipped else "queued",
            progress=1.0 if completed_skipped else None,
            is_duplicate=int(is_duplicate),
            duplicate_reason=duplicate_reason,
            created_at=now,
            updated_at=now,
            finished_at=now if completed_skipped else None,
            primary_job_id=primary_job_id,
        )
        columns = list(data)
        ref = {column: source.get(column) for column in REMOTE_REF_COLUMNS}
        with self.db.transaction() as conn:
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO jobs ({', '.join(columns)}) VALUES ({placeholders(len(columns))})",
                tuple(data[c] for c in columns),
            )
            if not cursor.rowcount:
                return None
            job_id = cursor.lastrowid
            if not completed_skipped and any(value is not None for value in ref.values()):
                conn.execute(
                    "INSERT INTO job_remote_refs (job_id, media_url, thumbnail_url, listing_cursor, listing_page_index,"
                    " listing_page_size) VALUES (?, ?, ?, ?, ?, ?)",
                    (job_id, *(ref[c] for c in REMOTE_REF_COLUMNS)),
                )
        return job_id

    def create_process(
        self,
        *,
        media: dict[str, Any],
        batch_id: int | None,
        preset_id: int,
        preset_name: str,
        params: dict[str, Any],
    ) -> int:
        now = utc_now()
        data: dict[str, Any] = {
            "batch_id": batch_id,
            "media_id": media["id"],
            "kind": "process",
            "preset_id": preset_id,
            "preset_name": preset_name,
            "params": canonical_json(params),
            "status": "pending",
            "stage": "queued",
            "source_platform": media["source_platform"],
            "source_profile": media["source_profile"],
            "source_url": media["source_url"],
            "source_post_id": media["source_post_id"],
            "source_media_index": media["source_media_index"],
            "post_id": media["post_id"],
            "media_id_remote": media["media_id_remote"],
            "media_type": media["media_type"],
            "taken_at": media["taken_at"],
            "created_at": now,
            "updated_at": now,
        }
        columns = list(data)
        cursor = self.db.execute(
            f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({placeholders(len(columns))})",
            tuple(data[c] for c in columns),
        )
        return cursor.lastrowid

    def create_process_for_filter(
        self, *, batch_id: int, preset_id: int, preset_name: str, params: dict[str, Any], media_where: str,
        media_params: list[Any],
    ) -> int:
        """Um job de process por mídia do filtro da Biblioteca (`media_where` de MediaRepository.filter_where),
        num único INSERT ... SELECT (o chamador abre a transação). Retorna quantos jobs criou."""
        now = utc_now()
        return self.db.execute(
            "INSERT INTO jobs (batch_id, media_id, kind, preset_id, preset_name, params, status, stage,"
            " source_platform, source_profile, source_url, source_post_id, source_media_index, post_id,"
            " media_id_remote, media_type, taken_at, created_at, updated_at)"
            " SELECT ?, m.id, 'process', ?, ?, ?, 'pending', 'queued', m.source_platform, m.source_profile,"
            " m.source_url, m.source_post_id, m.source_media_index, m.post_id, m.media_id_remote, m.media_type,"
            f" m.taken_at, ?, ? FROM media m {media_where} ORDER BY m.id",
            (batch_id, preset_id, preset_name, canonical_json(params), now, now, *media_params),
        ).rowcount

    # ---- leitura -----------------------------------------------------------------------------------------------
    def get(self, job_id: int) -> dict[str, Any] | None:
        return decode_job(self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,)))

    def get_remote_ref(self, job_id: int) -> dict[str, Any]:
        """URLs/cursor/página do job (dict vazio se não há referência). Uso interno: nunca vai para API ou log."""
        return row_dict(self.db.fetchone("SELECT * FROM job_remote_refs WHERE job_id = ?", (job_id,))) or {}

    def for_batch(self, batch_id: int) -> list[dict[str, Any]]:
        rows = self.db.fetchall("SELECT * FROM jobs WHERE batch_id = ? ORDER BY id", (batch_id,))
        return [decode_job(row) for row in rows]

    def recent_for_batch(self, batch_id: int, limit: int) -> tuple[list[dict[str, Any]], bool]:
        """Os `limit` jobs mais recentes do lote (em ordem crescente de id) e se havia mais."""
        rows = self.db.fetchall(
            "SELECT * FROM jobs WHERE batch_id = ? ORDER BY id DESC LIMIT ?", (batch_id, limit + 1)
        )
        truncated = len(rows) > limit
        return [decode_job(row) for row in reversed(rows[:limit])], truncated

    def list(
        self,
        *,
        statuses: list[str] | None = None,
        batch_id: int | None = None,
        ids: list[int] | None = None,
        limit: int = 100,
        offset: int = 0,
        before_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], int, dict[str, int], int | None]:
        """Jobs em ordem id DESC. `before_id` (keyset): só ids menores; `next_before_id` é None no fim.

        `total` e `counts` ignoram o keyset (valem para o filtro inteiro) e saem de uma única agregação por status
        (com `batch_id`, coberta por ix_jobs_batch_status_id). A página vem de (batch_id, id) ou, com um status só,
        de (batch_id, status, id), sem TEMP B-TREE."""
        base: list[str] = []
        params: list[Any] = []
        if batch_id is not None:
            base.append("batch_id = ?")
            params.append(batch_id)
        if ids:
            base.append(f"id IN ({placeholders(len(ids))})")
            params.extend(ids)
        base_where = f"WHERE {' AND '.join(base)}" if base else ""
        counts = {status: 0 for status in JOB_STATUSES}
        for row in self.db.fetchall(f"SELECT status, COUNT(*) AS n FROM jobs {base_where} GROUP BY status", tuple(params)):
            counts[row["status"]] = row["n"]
        wanted = set(statuses or ())
        total = sum(n for status, n in counts.items() if not wanted or status in wanted)

        clauses = list(base)
        full_params = list(params)
        if statuses:
            # Vários status com lote: `+status` deixa a ordem por id vir de (batch_id, id) (sem TEMP B-TREE).
            column = "+status" if batch_id is not None and len(wanted) > 1 else "status"
            clauses.append(f"{column} IN ({placeholders(len(statuses))})")
            full_params.extend(statuses)
        if before_id is not None:
            clauses.append("id < ?")
            full_params.append(before_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.fetchall(
            f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT ? OFFSET ?", (*full_params, limit + 1, offset)
        )
        next_before_id = rows[limit - 1]["id"] if len(rows) > limit else None
        return [decode_job(row) for row in rows[:limit]], total, counts, next_before_id

    def count_for_batch(self, batch_id: int) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM jobs WHERE batch_id = ?", (batch_id,)))

    def pending_ingest_count(self, batch_id: int) -> int:
        """Jobs de ingest do lote ainda não baixados (backpressure da listagem)."""
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND status IN ('pending', 'downloading')"
                f" AND kind = 'ingest' AND +media_id IS NULL AND NOT {WAITING_PRIMARY_SQL}",
                (batch_id,),
            )
        )

    def count_on_page(self, batch_id: int, page_index: int) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM jobs WHERE batch_id = ? AND listing_page_index = ?", (batch_id, page_index)
            )
        )

    def page_refreshed_at(self, batch_id: int, page_index: int) -> str | None:
        """Última renovação de URL registrada para jobs desta página do lote (inclui jobs já terminados)."""
        return self.db.scalar(
            "SELECT MAX(media_url_refreshed_at) FROM jobs WHERE batch_id = ? AND listing_page_index = ?",
            (batch_id, page_index),
        )

    def apply_refreshed_urls(
        self, batch_id: int, urls: list[tuple[str, int, str, str | None]], refreshed_at: str
    ) -> int:
        """Grava URLs renovadas (post, índice, media_url, thumbnail_url) nos jobs não terminais do lote.

        A referência remota é criada se não existir (job repetido depois de terminar já não tem linha)."""
        updated = 0
        with self.db.transaction() as conn:
            for post_id, index, media_url, thumbnail_url in urls:
                ids = [
                    row["id"]
                    for row in conn.execute(
                        "UPDATE jobs SET media_url_refreshed_at = ?, updated_at = ?"
                        " WHERE batch_id = ? AND kind = 'ingest' AND source_post_id = ? AND source_media_index = ?"
                        f" AND status IN {OPEN_JOB_STATUSES_SQL} RETURNING id",
                        (refreshed_at, refreshed_at, batch_id, post_id, index),
                    ).fetchall()
                ]
                for job_id in ids:
                    conn.execute(
                        "INSERT INTO job_remote_refs (job_id, media_url, thumbnail_url) VALUES (?, ?, ?)"
                        " ON CONFLICT (job_id) DO UPDATE SET media_url = excluded.media_url,"
                        " thumbnail_url = excluded.thumbnail_url",
                        (job_id, media_url, thumbnail_url),
                    )
                updated += len(ids)
        return updated

    def thumbnails_for(self, media_ids: list[int]) -> set[int]:
        """IDs de mídia que têm thumbnail local."""
        if not media_ids:
            return set()
        rows = self.db.fetchall(
            f"SELECT id FROM media WHERE thumbnail_path IS NOT NULL AND id IN ({placeholders(len(media_ids))})",
            tuple(media_ids),
        )
        return {row["id"] for row in rows}

    # ---- fila --------------------------------------------------------------------------------------------------
    def claim(
        self, *, allow_downloads: bool, owner: str = "direct", url_only: bool = False, pool: str | None = None,
        batch_id: int | None = None, exclude_batch_ids: frozenset[int] | set[int] = frozenset(),
        block_process_ingest: bool = False,
    ) -> dict[str, Any] | None:
        """Claim atômico do próximo job elegível (respeita lease, `next_attempt_at`, pausa de downloads e pausa do
        lote pelo disjuntor).

        `allow_downloads=False`: ingest ainda não baixado espera (pausa de escopo "instagram").
        `url_only=True`: ingest ainda não baixado só é reivindicado se tem URL de mídia (pausa de escopo "session":
        o download de CDN não usa a sessão; job sem URL precisaria renovar a listagem). Process nunca espera.
        `pool` (seção 9, A3): "download" só ingest pendente; "process" só a etapa FFmpeg (ingest `downloaded` e
        process pendente); None, qualquer um. `batch_id`: só jobs desse lote (rodízio entre lotes do worker).
        Backpressure de processamento (só no pool "download"): `exclude_batch_ids` não baixa desses lotes;
        `block_process_ingest` não baixa ingest com processamento de nenhum lote (teto global).
        O job recebe `claimed_by = owner` e só volta a ser elegível depois de `release` pelo executor.
        """
        now = utc_now()
        scope = "AND batch_id = :batch" if batch_id is not None else (
            "AND (batch_id IS NULL OR batch_id NOT IN (SELECT id FROM batches WHERE paused_until > :now"
            f" {_PAUSE_SCOPE_SQL[pool]}))")
        if pool == "download":
            if exclude_batch_ids:
                ids = ", ".join(str(int(i)) for i in sorted(exclude_batch_ids))
                scope += f" AND (batch_id IS NULL OR batch_id NOT IN ({ids}))"
            if block_process_ingest:
                scope += " AND params IS NULL"
        branches = [f"SELECT id FROM (SELECT id FROM jobs WHERE status = '{status}' {kind} {scope} AND "
                    + _CLAIM_ELIGIBLE + " ORDER BY id LIMIT 1)" for status, kind in _POOL_BRANCHES[pool]]
        row = self.db.fetchone(
            """
            UPDATE jobs SET
                status = CASE WHEN kind = 'ingest' AND status = 'pending' THEN 'downloading' ELSE 'processing' END,
                stage = CASE WHEN kind = 'ingest' AND status = 'pending' THEN 'downloading' ELSE 'processing' END,
                claimed_by = :owner,
                progress = NULL,
                next_attempt_at = NULL,
                started_at = COALESCE(started_at, :now),
                heartbeat_at = :now,
                updated_at = :now
            WHERE id = (SELECT MIN(id) FROM (""" + " UNION ALL ".join(branches) + """))
              AND status IN ('pending', 'downloaded')
              AND claimed_by IS NULL
              AND cancel_requested = 0
            RETURNING *
            """,
            {"now": now, "owner": owner, "allow": int(allow_downloads), "url_only": int(url_only),
             "batch": batch_id},
        )
        return decode_job(row)

    def process_backlog(self) -> dict[int, int]:
        """Backlog de processamento por lote: ingests com processamento (`params`) baixando, baixados aguardando o
        FFmpeg ou processando (seção 9). Lotes sem processamento não aparecem."""
        rows = self.db.fetchall(
            "SELECT batch_id, COUNT(*) FROM jobs WHERE status IN ('downloading', 'downloaded', 'processing')"
            " AND kind = 'ingest' AND params IS NOT NULL AND batch_id IS NOT NULL GROUP BY batch_id"
        )
        return {int(row[0]): int(row[1]) for row in rows}

    def has_processing_work(self) -> bool:
        """Há job aberto que ainda vai usar o FFmpeg (process, ou ingest com processamento)? Lotes download-only
        (ingest sem `params`) não contam."""
        return bool(self.db.scalar(
            f"SELECT EXISTS (SELECT 1 FROM jobs WHERE status IN {OPEN_JOB_STATUSES_SQL}"
            " AND (kind = 'process' OR params IS NOT NULL))"
        ))

    def claimable_batch_ids(self, pool: str | None = None) -> list[int]:
        """Lotes abertos e não pausados pelo disjuntor para o `pool`, em ordem de id (candidatos do rodízio)."""
        rows = self.db.fetchall(
            "SELECT id FROM batches WHERE status IN ('listing', 'running')"
            f" AND NOT (paused_until IS NOT NULL AND paused_until > :now {_PAUSE_SCOPE_SQL[pool]}) ORDER BY id",
            {"now": utc_now()},
        )
        return [row["id"] for row in rows]

    # ---- páginas da listagem e disjuntor do lote (seção 9, A1/A4) ---------------------------------------------
    def listing_page(self, batch_id: int, page_index: int) -> dict[str, Any] | None:
        return row_dict(self.db.fetchone(
            "SELECT * FROM listing_pages WHERE batch_id = ? AND page_index = ?", (batch_id, page_index)))

    def record_refresh(self, batch_id: int, page_index: int, *, ok: bool, code: str | None, at: str) -> int:
        """Registra uma tentativa de renovação da página. Retorna quantas renovações seguidas do lote falharam com
        este mesmo código (0 em sucesso)."""
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO listing_pages (batch_id, page_index, last_refresh_at, last_refresh_ok, last_refresh_code,"
                " refresh_attempts) VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT (batch_id, page_index) DO UPDATE SET"
                " last_refresh_at = excluded.last_refresh_at, last_refresh_ok = excluded.last_refresh_ok,"
                " last_refresh_code = excluded.last_refresh_code, refresh_attempts = refresh_attempts + 1",
                (batch_id, page_index, at, int(ok), None if ok else code),
            )
            row = conn.execute(
                "UPDATE batches SET"
                " refresh_fail_streak = CASE WHEN :ok THEN 0 WHEN refresh_fail_code = :code"
                "   THEN refresh_fail_streak + 1 ELSE 1 END,"
                " refresh_fail_code = CASE WHEN :ok THEN NULL ELSE :code END"
                # Sucesso só grava se havia sequência de falhas (seção 9: o disjuntor escreve só em falha ou
                # mudança de estado).
                " WHERE id = :id AND (NOT :ok OR refresh_fail_streak <> 0 OR refresh_fail_code IS NOT NULL)"
                " RETURNING refresh_fail_streak",
                {"ok": int(ok), "code": code, "id": batch_id},
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def record_batch_failure(self, batch_id: int, code: str) -> int:
        """Mais um job do lote falhou com `code` (o chamador prefixa o escopo, ex.: "ingest:content_unavailable").
        Retorna quantos jobs seguidos falharam com esse código."""
        row = self.db.fetchone(
            "UPDATE batches SET fail_streak = CASE WHEN fail_streak_code = :code THEN fail_streak + 1 ELSE 1 END,"
            " fail_streak_code = :code WHERE id = :id RETURNING fail_streak",
            {"code": code, "id": batch_id},
        )
        return int(row[0]) if row is not None else 0

    def record_batch_success(self, batch_id: int) -> None:
        """Um job do lote avançou com sucesso: zera os contadores e o degrau do backoff do disjuntor.

        Só escreve quando há algo a zerar (leitura antes): o caminho feliz não grava a linha do lote a cada job."""
        row = self.db.fetchone("SELECT fail_streak, pause_level FROM batches WHERE id = ?", (batch_id,))
        if row is None or not (row[0] or row[1]):
            return
        self.db.execute(
            "UPDATE batches SET fail_streak = 0, fail_streak_code = NULL, pause_level = 0"
            " WHERE id = ? AND (fail_streak <> 0 OR pause_level <> 0)",
            (batch_id,),
        )

    def pause_batch(self, batch_id: int, *, reason: str, code: str, steps: tuple[float, ...],
                    scope: str = "ingest") -> str | None:
        """Pausa o lote pelo degrau atual do backoff; sobe o degrau e zera os contadores. `scope` "ingest": claims de
        ingest e listagem do lote param; "process": só o FFmpeg do lote. Retorna o fim da pausa (ISO UTC)."""
        row = self.db.fetchone("SELECT pause_level FROM batches WHERE id = ?", (batch_id,))
        if row is None:
            return None
        level = int(row[0] or 0)
        until = utc_now(steps[min(level, len(steps) - 1)])
        self.db.execute(
            "UPDATE batches SET paused_reason = ?, paused_code = ?, paused_until = ?, paused_scope = ?,"
            " pause_level = ?, fail_streak = 0, fail_streak_code = NULL, refresh_fail_streak = 0,"
            " refresh_fail_code = NULL, updated_at = ? WHERE id = ?",
            (reason, code, until, scope, level + 1, utc_now(), batch_id),
        )
        return until

    def is_batch_paused(self, batch_id: int, scope: str | None = "ingest") -> bool:
        """Lote pausado pelo disjuntor (pausa vigente). Padrão: escopo "ingest" (o que a listagem respeita);
        "process": só FFmpeg; None: qualquer escopo."""
        row = self.db.fetchone("SELECT paused_until, paused_scope FROM batches WHERE id = ?", (batch_id,))
        if row is None or row["paused_until"] is None or row["paused_until"] <= utc_now():
            return False
        return scope is None or (row["paused_scope"] or "ingest") == scope

    def batch_pause(self, batch_id: int) -> dict[str, Any] | None:
        """{paused_reason, paused_code, paused_until, paused_scope} da pausa vigente do lote, ou None."""
        return row_dict(self.db.fetchone(
            "SELECT paused_reason, paused_code, paused_until, paused_scope FROM batches"
            " WHERE id = ? AND paused_until > ?", (batch_id, utc_now())))

    def release(self, job_id: int, owner: str) -> None:
        """Libera o lease ao fim da execução (o job volta a ser elegível se ficou pending/downloaded)."""
        self.db.execute("UPDATE jobs SET claimed_by = NULL, crash_count = 0 WHERE id = ? AND claimed_by = ?",
                        (job_id, owner))

    def next_wakeup(self) -> str | None:
        return self.db.scalar(
            "SELECT MIN(next_attempt_at) FROM jobs WHERE status = 'pending' AND next_attempt_at IS NOT NULL"
        )

    def update(self, job_id: int, **fields: Any) -> bool:
        """Grava os campos. Retorna o `cancel_requested` atual do job (lido na mesma instrução, sem SELECT extra)."""
        unknown = set(fields) - JOB_UPDATE_COLUMNS
        if unknown:
            raise ValueError(f"Colunas desconhecidas: {sorted(unknown)}")
        if not fields:
            return False
        assignments = ", ".join(f"{column} = ?" for column in fields)
        row = self.db.fetchone(
            f"UPDATE jobs SET {assignments}, updated_at = ? WHERE id = ? RETURNING cancel_requested",
            (*fields.values(), utc_now(), job_id),
        )
        return bool(row is not None and row[0])

    def finish(self, job_id: int, **fields: Any) -> None:
        """Estado terminal + limpeza da referência remota.

        Concluído (caminho de todo job): UPDATE + DELETE da linha sem transação explícita (sem BEGIN/COMMIT por
        job). Se o processo cair entre os dois, a linha que sobrar é apagada quando o lote conclui (trigger
        tr_batches_completed_drop_refs) ou é cancelado. Falhou/cancelou: numa transação, apaga só as URLs
        (cursor/página ficam para o retry com renovação)."""
        if fields.get("status") == "completed":
            self.update(job_id, **fields)
            self.db.execute("DELETE FROM job_remote_refs WHERE job_id = ?", (job_id,))
            return
        with self.db.transaction() as conn:
            self.update(job_id, **fields)
            conn.execute(_CLEAR_URLS_SQL, (job_id,))

    def clear_remote_urls(self, job_id: int) -> None:
        self.db.execute(_CLEAR_URLS_SQL, (job_id,))

    def heartbeat(self, job_ids: list[int]) -> None:
        if job_ids:
            now = utc_now()
            self.db.execute(
                f"UPDATE jobs SET heartbeat_at = ? WHERE id IN ({placeholders(len(job_ids))})", (now, *job_ids)
            )

    def is_cancel_requested(self, job_id: int) -> bool:
        return bool(self.db.scalar("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)))

    def request_cancel(self, job_id: int) -> None:
        self.db.execute(
            f"UPDATE jobs SET cancel_requested = 1, updated_at = ? WHERE id = ? AND status NOT IN {_TERMINAL_SQL}",
            (utc_now(), job_id),
        )

    def cancel_if_idle(self, job_id: int) -> bool:
        """Cancela direto um job que não está em execução (pending/downloaded)."""
        now = utc_now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'cancelled', stage = NULL, progress = NULL, finished_at = ?, updated_at = ?,"
                " next_attempt_at = NULL WHERE id = ? AND status IN ('pending', 'downloaded') AND claimed_by IS NULL",
                (now, now, job_id),
            )
            if cursor.rowcount:
                conn.execute(_CLEAR_URLS_SQL, (job_id,))
        return cursor.rowcount > 0

    def finalize_cancel_requests(self) -> list[int]:
        """Jobs ociosos com cancelamento pedido (ex.: reagendados durante o cancelamento) viram cancelled.

        Usa o índice parcial ix_jobs_cancel. Retorna os batch_id afetados.
        """
        now = utc_now()
        with self.db.transaction() as conn:
            rows = conn.execute(
                "UPDATE jobs SET status = 'cancelled', stage = NULL, progress = NULL, finished_at = ?, updated_at = ?,"
                " next_attempt_at = NULL WHERE cancel_requested = 1 AND +status IN ('pending', 'downloaded')"
                " AND claimed_by IS NULL RETURNING id, batch_id",
                (now, now),
            ).fetchall()
            _clear_urls(conn, [row["id"] for row in rows])
        return sorted({row["batch_id"] for row in rows if row["batch_id"] is not None})

    def cancel_batch_jobs(self, batch_id: int) -> list[int]:
        """Cancelamento do lote num UPDATE único: ociosos viram `cancelled`; em execução recebem cancel_requested.

        Apaga as referências remotas (URLs e cursor) de todos os jobs do lote. Deve rodar dentro da transação do
        cancelamento. Retorna os ids em execução (o chamador sinaliza cada um ao worker)."""
        now = utc_now()
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                UPDATE jobs SET
                    cancel_requested = 1,
                    status = CASE WHEN idle THEN 'cancelled' ELSE status END,
                    stage = CASE WHEN idle THEN NULL ELSE stage END,
                    progress = CASE WHEN idle THEN NULL ELSE progress END,
                    finished_at = CASE WHEN idle THEN :now ELSE finished_at END,
                    next_attempt_at = CASE WHEN idle THEN NULL ELSE next_attempt_at END,
                    updated_at = :now
                FROM (SELECT id AS jid, (status IN ('pending', 'downloaded') AND claimed_by IS NULL) AS idle
                      FROM jobs WHERE batch_id = :batch AND status IN """ + OPEN_JOB_STATUSES_SQL + """) AS sel
                WHERE jobs.id = sel.jid
                RETURNING jobs.id AS id, jobs.status AS status
                """,
                {"now": now, "batch": batch_id},
            ).fetchall()
            delete_batch_remote_refs(conn, batch_id)
        return [row["id"] for row in rows if row["status"] != "cancelled"]

    def non_terminal_ids(self, *, batch_id: int | None = None, media_id: int | None = None) -> list[int]:
        clauses = [f"status IN {OPEN_JOB_STATUSES_SQL}"]
        params: list[Any] = []
        if batch_id is not None:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        if media_id is not None:
            clauses.append("media_id = ?")
            params.append(media_id)
        rows = self.db.fetchall(f"SELECT id FROM jobs WHERE {' AND '.join(clauses)}", tuple(params))
        return [row["id"] for row in rows]

    def reset_for_retry(self, job_ids: list[int]) -> int:
        """Volta jobs falhos/cancelados para a fila com tentativas zeradas."""
        if not job_ids:
            return 0
        now = utc_now()
        cursor = self.db.execute(
            "UPDATE jobs SET status = 'pending', stage = 'queued', progress = NULL, error_code = NULL,"
            " error_message = NULL, attempts = 0, crash_count = 0, next_attempt_at = NULL, cancel_requested = 0,"
            " finished_at = NULL,"
            " started_at = NULL, heartbeat_at = NULL, updated_at = ?"
            f" WHERE id IN ({placeholders(len(job_ids))}) AND status IN ('failed', 'cancelled')",
            (now, *job_ids),
        )
        return cursor.rowcount

    def failed_ids(self, batch_id: int) -> list[int]:
        rows = self.db.fetchall("SELECT id FROM jobs WHERE batch_id = ? AND status = 'failed'", (batch_id,))
        return [row["id"] for row in rows]

    # ---- recovery ----------------------------------------------------------------------------------------------
    def recover_interrupted(self) -> dict[str, int]:
        """Startup (seção 7): jobs que estavam em execução voltam à fila. downloading -> pending,
        processing/downloaded -> downloaded; error_code=interrupted. Reinício abrupto não consome tentativa
        (seção 9, M7), mas conta `crash_count`: na MAX_CRASHES-ésima interrupção seguida o job falha com
        interrupted_repeatedly (job que derruba o processo não volta para sempre)."""
        # Sem OR: UNION de duas buscas indexadas (ix_jobs_status_id e ix_jobs_claimed).
        result = self._requeue_interrupted(
            "id IN (SELECT id FROM jobs WHERE status IN ('downloading', 'processing')"
            " UNION SELECT id FROM jobs WHERE claimed_by IS NOT NULL)", (),
            "Interrompido por reinício do aplicativo.", consume_attempt=False,
        )
        # Cancelamentos pedidos para jobs parados também são concluídos no startup (índice parcial ix_jobs_cancel).
        now = utc_now()
        with self.db.transaction() as conn:
            rows = conn.execute(
                "UPDATE jobs SET status = 'cancelled', stage = NULL, progress = NULL, finished_at = ?, updated_at = ?"
                f" WHERE cancel_requested = 1 AND claimed_by IS NULL AND +status IN {OPEN_JOB_STATUSES_SQL}"
                " RETURNING id",
                (now, now),
            ).fetchall()
            _clear_urls(conn, [row["id"] for row in rows])
        return result

    def recover_stale(self, *, cutoff: str, exclude_ids: list[int]) -> dict[str, int]:
        """Runtime: job com lease, sem heartbeat desde `cutoff` e sem executor vivo é tratado como interrompido."""
        where = (
            f"claimed_by IS NOT NULL AND +status IN {OPEN_JOB_STATUSES_SQL}"
            " AND (heartbeat_at IS NULL OR heartbeat_at < ?)"
        )
        params: tuple = (cutoff,)
        if exclude_ids:
            where += f" AND id NOT IN ({placeholders(len(exclude_ids))})"
            params = (cutoff, *exclude_ids)
        return self._requeue_interrupted(where, params, "Execução travada (sem sinal de vida); job reenfileirado.")

    def _requeue_interrupted(self, where: str, params: tuple, message: str, *,
                             consume_attempt: bool = True) -> dict[str, int]:
        """Aplica a regra de interrupção aos jobs selecionados por `where` (SQL fixo do código, valores em params).
        `consume_attempt=False` (reinício abrupto): ninguém falha por tentativas esgotadas e `attempts` não muda."""
        bump = "attempts + 1" if consume_attempt else "attempts"
        crash = "crash_count" if consume_attempt else "crash_count + 1"
        now = utc_now()
        # `+status`: o status não escolhe índice aqui (o filtro seletivo é lease/cancelamento/UNION por id).
        open_jobs = f"+status IN {OPEN_JOB_STATUSES_SQL}"
        with self.db.transaction() as conn:
            batch_rows = conn.execute(
                f"SELECT batch_id FROM jobs WHERE ({where}) AND {open_jobs} AND +batch_id IS NOT NULL", params
            ).fetchall()
            cancelled_rows = conn.execute(
                "UPDATE jobs SET status = 'cancelled', stage = NULL, progress = NULL, finished_at = ?, updated_at = ?,"
                " claimed_by = NULL, temp_path = NULL"
                f" WHERE ({where}) AND {open_jobs} AND cancel_requested = 1 RETURNING id",
                (now, now, *params),
            ).fetchall()
            if consume_attempt:
                failed_rows = conn.execute(
                    "UPDATE jobs SET status = 'failed', stage = NULL, progress = NULL, attempts = attempts + 1,"
                    " error_code = 'interrupted', error_message = ?, finished_at = ?, updated_at = ?,"
                    f" claimed_by = NULL, temp_path = NULL WHERE ({where}) AND {open_jobs}"
                    " AND attempts + 1 >= max_attempts RETURNING id",
                    (message + " Tentativas esgotadas.", now, now, *params),
                ).fetchall()
            else:
                failed_rows = conn.execute(
                    "UPDATE jobs SET status = 'failed', stage = NULL, progress = NULL, crash_count = crash_count + 1,"
                    " error_code = 'interrupted_repeatedly', error_message = ?, finished_at = ?, updated_at = ?,"
                    f" claimed_by = NULL, temp_path = NULL WHERE ({where}) AND {open_jobs}"
                    " AND crash_count + 1 >= ? RETURNING id",
                    (f"O job foi interrompido {MAX_CRASHES} vezes seguidas (o aplicativo fechou durante a execução)."
                     " Tente de novo manualmente.", now, now, *params, MAX_CRASHES),
                ).fetchall()
            _clear_urls(conn, [row["id"] for row in (*cancelled_rows, *failed_rows)])
            cancelled, failed = len(cancelled_rows), len(failed_rows)
            requeued = conn.execute(
                "UPDATE jobs SET"
                " status = CASE WHEN status IN ('downloading', 'pending') THEN 'pending' ELSE 'downloaded' END,"
                f" stage = 'queued', progress = NULL, attempts = {bump}, crash_count = {crash},"
                " error_code = 'interrupted',"
                " error_message = ?, next_attempt_at = NULL, temp_path = NULL, claimed_by = NULL, updated_at = ?"
                f" WHERE ({where}) AND {open_jobs}",
                (message, now, *params),
            ).rowcount
        return {
            "failed": failed, "requeued": requeued, "cancelled": cancelled,
            "batch_ids": list(dict.fromkeys(row["batch_id"] for row in batch_rows)),
        }

    # ---- retenção -------------------------------------------------------------------------------------------------
    def purge_finished_before(self, cutoff: str, *, chunk: int = 500) -> int:
        """Apaga jobs terminais com `finished_at < cutoff`, preservando o último job de cada mídia (usado pela
        Biblioteca) e os jobs de lotes ainda abertos (listing/running). Em trechos curtos de escrita.

        Antes de apagar, na mesma transação, a relação lote -> mídia dos ingests concluídos do trecho vai para
        `batch_media` (migration 0013): a exportação de um lote antigo continua completa depois da retenção."""
        removed = 0
        while True:
            with self.db.transaction() as conn:
                ids = [row["id"] for row in conn.execute(
                    """
                    SELECT j.id FROM jobs j
                    WHERE j.status IN ('completed', 'failed', 'cancelled') AND j.finished_at < :cutoff
                      AND (j.batch_id IS NULL OR j.batch_id NOT IN
                           (SELECT b.id FROM batches b WHERE b.status IN ('listing', 'running')))
                      AND (j.media_id IS NULL
                           OR j.id <> (SELECT MAX(l.id) FROM jobs l WHERE l.media_id = j.media_id))
                    LIMIT :chunk
                    """,
                    {"cutoff": cutoff, "chunk": chunk},
                ).fetchall()]
                count = len(ids)
                for start in range(0, count, 500):
                    part = tuple(ids[start:start + 500])
                    marks = placeholders(len(part))
                    conn.execute(
                        "INSERT OR IGNORE INTO batch_media (batch_id, media_id) SELECT batch_id, media_id FROM jobs"
                        f" WHERE id IN ({marks}) AND kind = 'ingest' AND status = 'completed'"
                        " AND media_id IS NOT NULL AND batch_id IS NOT NULL",
                        part,
                    )
                    conn.execute(f"DELETE FROM jobs WHERE id IN ({marks})", part)
            removed += count
            if count < chunk:
                return removed

    # ---- agregados por lote ------------------------------------------------------------------------------------
    def batch_stats(self, batch_ids: list[int]) -> dict[int, dict[str, Any]]:
        stats: dict[int, dict[str, Any]] = {}
        if not batch_ids:
            return stats
        status_cols = ", ".join(f"SUM(status = '{s}') AS c_{s}" for s in JOB_STATUSES)
        progress_cols = ", ".join(f"SUM({cond}) AS {key}" for key, cond in BATCH_PROGRESS_CONDITIONS.items())
        active = ", ".join(f"'{s}'" for s in ACTIVE_JOB_STATUSES)
        terminal = ", ".join(f"'{s}'" for s in TERMINAL_JOB_STATUSES)
        rows = self.db.fetchall(
            f"""
            SELECT batch_id,
                   COUNT(*) AS total,
                   {progress_cols},
                   SUM(status = 'pending') AS pending,
                   SUM(status IN ({active})) AS active,
                   SUM(status NOT IN ({terminal})) AS open,
                   {status_cols}
            FROM jobs WHERE batch_id IN ({placeholders(len(batch_ids))})
            GROUP BY batch_id
            """,
            tuple(batch_ids),
        )
        for row in rows:
            stats[row["batch_id"]] = {key: int(row[key] or 0) for key in row.keys() if key != "batch_id"}
        return stats


def _clear_urls(conn, job_ids: list[int]) -> None:
    """Apaga as URLs assinadas (mantém cursor/página) dos jobs que falharam ou foram cancelados."""
    if job_ids:
        conn.executemany(_CLEAR_URLS_SQL, [(job_id,) for job_id in job_ids])
