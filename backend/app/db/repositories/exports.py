"""Exportações para pasta (migration 0013): registro, fila (uma por vez) e leitura paginada das mídias."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.db.connection import Database, utc_now
from app.db.repositories.common import canonical_json, load_json, placeholders, row_dict
from app.db.repositories.media import MediaRepository

COUNT_COLUMNS = ("copied", "skipped_existing", "renamed", "missing", "failed")
OPEN_EXPORT_STATUSES = ("pending", "running")
# Mídias de um lote: toda mídia ligada a um job de ingest CONCLUÍDO (inclusive duplicatas, que apontam para a mídia
# que já existia na Biblioteca) UNIDA à relação durável `batch_media` (pares guardados antes de a retenção apagar os
# jobs antigos). A união já elimina repetição: cada mídia uma vez.
_BATCH_MEDIA = (
    "SELECT j.media_id FROM jobs j WHERE j.batch_id = ? AND j.kind = 'ingest' AND j.status = 'completed'"
    " AND j.media_id IS NOT NULL"
    " UNION SELECT bm.media_id FROM batch_media bm WHERE bm.batch_id = ?"
)
_GROUP_MEDIA = (
    "SELECT j.media_id FROM jobs j JOIN batches b ON b.id = j.batch_id WHERE b.group_id = ? AND j.kind = 'ingest'"
    " AND j.status = 'completed' AND j.media_id IS NOT NULL"
    " UNION SELECT bm.media_id FROM batch_media bm JOIN batches b ON b.id = bm.batch_id WHERE b.group_id = ?"
)
_ITEMS = "SELECT i.media_id FROM export_items i WHERE i.export_id = ?"


def decode_export(row) -> dict[str, Any] | None:
    data = row_dict(row)
    if data is not None:
        data["selection"] = load_json(data["selection"])
    return data


class ExportRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---- seleção -----------------------------------------------------------------------------------------------
    @staticmethod
    def _where(export: dict[str, Any]) -> tuple[str, list[Any]]:
        """Cláusula WHERE (sobre `media m`) das mídias da exportação; SQL fixo, valores em parâmetros."""
        kind = export["kind"]
        if kind == "batch":
            return f"WHERE m.id IN ({_BATCH_MEDIA})", [export["batch_id"], export["batch_id"]]
        if kind == "group":
            return f"WHERE m.id IN ({_GROUP_MEDIA})", [export["group_id"], export["group_id"]]
        selection = export["selection"] or {}
        if selection.get("mode") == "filter":
            where, params = MediaRepository.filter_where(
                view=selection.get("view") or "all", media_type=selection.get("media_type"),
                profile=selection.get("profile"), q=selection.get("q"),
            )
            # Filtro congelado no pedido: mídias importadas depois (id maior) não entram no meio da cópia.
            extra = f"m.id <= ? AND m.id NOT IN ({_ITEMS})"
            where = f"{where} AND {extra}" if where else f"WHERE {extra}"
            return where, [*params, int(selection.get("max_id") or 0), export["id"]]
        return f"WHERE m.id IN ({_ITEMS})", [export["id"]]

    def sum_bytes(self, export: dict[str, Any]) -> int:
        """Soma de `file_size` das mídias da exportação (consulta agregada; a lista não é carregada)."""
        where, params = self._where(export)
        return int(self.db.scalar(f"SELECT COALESCE(SUM(m.file_size), 0) FROM media m {where}", tuple(params)))

    def delete(self, export_id: int) -> None:
        self.db.execute("DELETE FROM exports WHERE id = ?", (export_id,))

    def count_media(self, export: dict[str, Any]) -> int:
        where, params = self._where(export)
        return int(self.db.scalar(f"SELECT COUNT(*) FROM media m {where}", tuple(params)))

    def page(self, export: dict[str, Any], *, after_id: int, limit: int) -> list[dict[str, Any]]:
        """Próxima página de mídias (keyset por id crescente; cada mídia aparece uma vez)."""
        where, params = self._where(export)
        rows = self.db.fetchall(
            f"SELECT m.* FROM media m {where} AND m.id > ? ORDER BY m.id LIMIT ?", (*params, after_id, limit)
        )
        return [dict(row) for row in rows]

    def carousel_posts(self, post_ids: Iterable[str]) -> set[str]:
        """Dos posts informados, os que têm mais de um item na Biblioteca (carrossel)."""
        ids = sorted({post_id for post_id in post_ids if post_id})
        if not ids:
            return set()
        rows = self.db.fetchall(
            f"SELECT DISTINCT source_post_id FROM media WHERE source_post_id IN ({placeholders(len(ids))})"
            " AND source_media_index > 0",
            tuple(ids),
        )
        return {row["source_post_id"] for row in rows}

    # ---- registro ----------------------------------------------------------------------------------------------
    def create(
        self,
        *,
        kind: str,
        dest_path: str,
        batch_id: int | None = None,
        group_id: int | None = None,
        selection: dict[str, Any] | None = None,
        item_ids: Iterable[int] = (),
        dest_created: bool = False,
        mode: str = "folder",
        zip_fields: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Cria a exportação `pending` com o total já contado. Sem nenhuma mídia: nada é gravado e retorna None."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO exports (kind, batch_id, group_id, selection, dest_path, dest_created, mode, zip_path,"
                " zip_name, zip_token, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (kind, batch_id, group_id, None if selection is None else canonical_json(selection), dest_path,
                 int(dest_created), mode, (zip_fields or {}).get("zip_path"), (zip_fields or {}).get("zip_name"),
                 (zip_fields or {}).get("zip_token"), utc_now()),
            )
            export_id = cursor.lastrowid
            conn.executemany(
                "INSERT OR IGNORE INTO export_items (export_id, media_id) VALUES (?, ?)",
                ((export_id, int(media_id)) for media_id in item_ids),
            )
            export = self.get(export_id)
            total = self.count_media(export)
            if total == 0:
                conn.execute("DELETE FROM exports WHERE id = ?", (export_id,))
                return None
            conn.execute("UPDATE exports SET total = ? WHERE id = ?", (total, export_id))
        return self.get(export_id)

    def get(self, export_id: int) -> dict[str, Any] | None:
        return decode_export(self.db.fetchone("SELECT * FROM exports WHERE id = ?", (export_id,)))

    def list(self, *, limit: int, batch_id: int | None = None, group_id: int | None = None,
             kind: str | None = None) -> list[dict[str, Any]]:
        conditions: list[str] = []
        values: list[Any] = []
        if kind is not None:
            conditions.append("kind = ?")
            values.append(kind)
        if batch_id is not None:
            conditions.append("batch_id = ?")
            values.append(batch_id)
        if group_id is not None:
            conditions.append("group_id = ?")
            values.append(group_id)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self.db.fetchall(f"SELECT * FROM exports {where} ORDER BY id DESC LIMIT ?", (*values, limit))
        return [decode_export(row) for row in rows]

    def last_completed(self, column: str, ids: list[int]) -> dict[int, dict[str, Any]]:
        """Última exportação PARA PASTA concluída de cada lote (`batch_id`) ou grupo (`group_id`): uma consulta
        para todos. ZIPs não entram: é ela que o "Abrir pasta" da importação abre."""
        if column not in ("batch_id", "group_id"):
            raise ValueError("Coluna inválida")
        if not ids:
            return {}
        rows = self.db.fetchall(
            f"SELECT e.* FROM exports e JOIN (SELECT MAX(id) AS id FROM exports WHERE status = 'completed' AND mode = 'folder'"
            f" AND {column} IN ({placeholders(len(ids))}) GROUP BY {column}) last ON last.id = e.id",
            tuple(ids),
        )
        return {row[column]: decode_export(row) for row in rows}

    # ---- fila --------------------------------------------------------------------------------------------------
    def claim_next(self) -> dict[str, Any] | None:
        """A exportação `pending` mais antiga vira `running` (uma por vez; quem chama garante a exclusividade)."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT id FROM exports WHERE status = 'pending' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE exports SET status = 'running', started_at = ? WHERE id = ?", (utc_now(), row["id"]))
        return self.get(row["id"])

    def update_progress(self, export_id: int, counts: dict[str, int]) -> None:
        assignments = ", ".join(f"{column} = ?" for column in COUNT_COLUMNS)
        self.db.execute(
            f"UPDATE exports SET {assignments} WHERE id = ?", (*(counts[c] for c in COUNT_COLUMNS), export_id)
        )

    def open_for_source(self, *, kind: str, batch_id: int | None = None, group_id: int | None = None,
                        mode: str = "folder") -> int | None:
        """Id da exportação `pending`/`running` da MESMA origem (mesmo lote, mesmo grupo, ou qualquer exportação da
        Biblioteca) e do MESMO modo (pasta e ZIP são destinos diferentes e podem conviver na fila), ou None."""
        if kind == "batch":
            condition, values = "kind = 'batch' AND batch_id = ?", (batch_id,)
        elif kind == "group":
            condition, values = "kind = 'group' AND group_id = ?", (group_id,)
        else:
            condition, values = "kind = 'library'", ()
        return self.db.scalar(
            f"SELECT id FROM exports WHERE status IN ('pending', 'running') AND mode = ? AND {condition}"
            " ORDER BY id LIMIT 1",
            (mode, *values),
        )

    def pending_count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM exports WHERE status = 'pending'"))

    def finish(self, export_id: int, status: str, *, counts: dict[str, int] | None = None,
               error_code: str | None = None, error_message: str | None = None, total: int | None = None,
               zip_bytes: int | None = None, zip_expires_at: str | None = None) -> None:
        with self.db.transaction() as conn:
            if zip_bytes is not None:
                conn.execute("UPDATE exports SET zip_bytes = ?, zip_expires_at = ? WHERE id = ?",
                             (zip_bytes, zip_expires_at, export_id))
            if counts is not None:
                self.update_progress(export_id, counts)
            if total is not None:
                conn.execute("UPDATE exports SET total = ? WHERE id = ?", (total, export_id))
            conn.execute(
                "UPDATE exports SET status = ?, error_code = ?, error_message = ?, finished_at = ? WHERE id = ?",
                (status, error_code, error_message, utc_now(), export_id),
            )

    def request_cancel(self, export_id: int, message: str) -> str | None:
        """Pede o cancelamento. `pending` vira `cancelled` na hora; `running` só recebe a marca (o executor para).
        Retorna o status ANTERIOR (None se a exportação não existe)."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status FROM exports WHERE id = ?", (export_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "pending":
                conn.execute(
                    "UPDATE exports SET status = 'cancelled', cancel_requested = 1, error_message = ?, finished_at = ?"
                    " WHERE id = ?", (message, utc_now(), export_id),
                )
            elif row["status"] == "running":
                conn.execute("UPDATE exports SET cancel_requested = 1 WHERE id = ?", (export_id,))
            return row["status"]

    def running(self) -> list[dict[str, Any]]:
        return [decode_export(row) for row in self.db.fetchall("SELECT * FROM exports WHERE status = 'running'")]

    # ---- ZIP ---------------------------------------------------------------------------------------------------
    def expired_zips(self, now: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM exports WHERE mode = 'zip' AND status = 'completed' AND zip_expires_at IS NOT NULL"
            " AND zip_expires_at <= ?", (now,))
        return [decode_export(row) for row in rows]

    def set_expired(self, export_id: int) -> None:
        self.db.execute("UPDATE exports SET status = 'expired' WHERE id = ? AND mode = 'zip'", (export_id,))

    def alive_zip_names(self) -> set[str]:
        """Nomes internos dos ZIPs de registros vivos (na fila, em preparação ou prontos)."""
        rows = self.db.fetchall(
            "SELECT zip_path FROM exports WHERE mode = 'zip' AND zip_path IS NOT NULL"
            " AND status IN ('pending', 'running', 'completed')")
        return {row["zip_path"] for row in rows}

    def mark_downloaded(self, export_id: int, now: str, expires_at: str) -> None:
        """Download completo concluído: o prazo passa a ser o MENOR entre o atual e `expires_at`."""
        self.db.execute(
            "UPDATE exports SET zip_downloaded_at = COALESCE(zip_downloaded_at, ?),"
            " zip_expires_at = MIN(COALESCE(zip_expires_at, ?), ?) WHERE id = ? AND mode = 'zip'",
            (now, expires_at, expires_at, export_id))

    def open_zips(self) -> list[dict[str, Any]]:
        """ZIPs na fila ou em preparação (para a reserva lógica de espaço)."""
        rows = self.db.fetchall("SELECT * FROM exports WHERE mode = 'zip' AND status IN ('pending', 'running')")
        return [decode_export(row) for row in rows]

    def downloaded_zips(self) -> list[dict[str, Any]]:
        """ZIPs prontos que JÁ foram baixados por inteiro (podem sair antes do prazo se o disco apertar)."""
        rows = self.db.fetchall(
            "SELECT * FROM exports WHERE mode = 'zip' AND status = 'completed' AND zip_downloaded_at IS NOT NULL")
        return [decode_export(row) for row in rows]
