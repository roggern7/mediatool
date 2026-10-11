from __future__ import annotations

from typing import Any

from app.db.connection import Database, utc_now
from app.db.repositories.common import (
    OPEN_JOB_STATUSES_SQL,
    canonical_json,
    escape_like,
    load_json,
    placeholders,
    row_dict,
)
from app.db.repositories.jobs import decode_job

MEDIA_COLUMNS = frozenset(
    {
        "source_platform", "source_profile", "source_url", "source_post_id", "source_media_index", "post_id",
        "media_id_remote", "media_type", "original_filename", "original_path", "original_sha256", "file_size",
        "duration", "width", "height", "fps", "has_audio", "mime_type", "thumbnail_path", "taken_at",
        "video_codec",
    }
)
RENDITION_COLUMNS = frozenset(
    {
        "media_id", "preset_id", "preset_name", "params", "job_id", "processed_path", "processed_sha256",
        "file_size", "duration", "width", "height", "fps", "has_audio",
    }
)


def _check_columns(fields: dict[str, Any], allowed: frozenset[str]) -> None:
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Colunas desconhecidas: {sorted(unknown)}")


def _decode_rendition(row) -> dict[str, Any] | None:
    data = row_dict(row)
    if data is not None:
        data["params"] = load_json(data["params"])
    return data


class MediaRepository:
    def __init__(self, db: Database):
        self.db = db

    # ---- media -------------------------------------------------------------------------------------------------
    def get(self, media_id: int) -> dict[str, Any] | None:
        return row_dict(self.db.fetchone("SELECT * FROM media WHERE id = ?", (media_id,)))

    def find_by_source(self, platform: str, source_post_id: str, index: int) -> dict[str, Any] | None:
        return row_dict(
            self.db.fetchone(
                "SELECT * FROM media WHERE source_platform = ? AND source_post_id = ? AND source_media_index = ?",
                (platform, source_post_id, index),
            )
        )

    def ingest_in_flight(self, platform: str, source_post_id: str, index: int, *, exclude_batch_id: int) -> int | None:
        """Job de ingest de outro lote ainda não baixado para o mesmo item (ix_jobs_ingest_in_flight), ou None.

        Prefere o job que vai baixar de fato (não dependente); um dependente também serve (ele espera o próprio
        primário e a cadeia se resolve em ordem de id)."""
        return self.db.scalar(
            "SELECT id FROM jobs WHERE kind = 'ingest' AND status IN ('pending', 'downloading')"
            " AND source_post_id = ? AND source_media_index = ? AND COALESCE(source_platform, 'instagram') = ?"
            " AND batch_id IS NOT NULL AND batch_id <> ? ORDER BY primary_job_id IS NOT NULL, id LIMIT 1",
            (source_post_id, index, platform, exclude_batch_id),
        )

    def find_by_sha(self, sha256: str) -> dict[str, Any] | None:
        return row_dict(self.db.fetchone("SELECT * FROM media WHERE original_sha256 = ?", (sha256,)))

    def insert(self, fields: dict[str, Any]) -> int:
        _check_columns(fields, MEDIA_COLUMNS)
        now = utc_now()
        data = {**fields, "created_at": now, "updated_at": now}
        columns = list(data)
        cursor = self.db.execute(
            f"INSERT INTO media ({', '.join(columns)}) VALUES ({placeholders(len(columns))})",
            tuple(data[c] for c in columns),
        )
        return cursor.lastrowid

    def update(self, media_id: int, **fields: Any) -> None:
        _check_columns(fields, MEDIA_COLUMNS)
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self.db.execute(
            f"UPDATE media SET {assignments}, updated_at = ? WHERE id = ?",
            (*fields.values(), utc_now(), media_id),
        )

    def delete(self, media_id: int) -> None:
        self.db.execute("DELETE FROM media WHERE id = ?", (media_id,))

    @staticmethod
    def filter_where(
        *, view: str = "all", media_type: str | None = None, profile: str | None = None, q: str | None = None
    ) -> tuple[str, list[Any]]:
        """Cláusula WHERE (sobre `media m`) do filtro da Biblioteca; SQL fixo, valores em parâmetros."""
        clauses: list[str] = []
        params: list[Any] = []
        if view == "processed":
            clauses.append("EXISTS (SELECT 1 FROM renditions r WHERE r.media_id = m.id)")
        elif view == "originals":
            clauses.append("NOT EXISTS (SELECT 1 FROM renditions r WHERE r.media_id = m.id)")
        if media_type:
            clauses.append("m.media_type = ?")
            params.append(media_type)
        if profile:
            clauses.append("m.source_profile = ? COLLATE NOCASE")  # ix_media_profile_nocase
            params.append(profile)
        if q:
            pattern = f"%{escape_like(q)}%"
            clauses.append(
                "(m.source_post_id LIKE ? ESCAPE '\\' OR m.source_profile LIKE ? ESCAPE '\\'"
                " OR m.original_filename LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern])
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params

    @classmethod
    def process_filter_where(
        cls, *, view: str, media_type: str | None, profile: str | None, q: str | None, preset_id: int | None,
        params: dict[str, Any],
    ) -> tuple[str, list[Any]]:
        """Filtro da Biblioteca menos as mídias com job aberto que vai processar com o MESMO preset/params e as que já
        têm rendition do mesmo preset/params (processar por filtro idempotente, seção 9 M3)."""
        where, values = cls.filter_where(view=view, media_type=media_type, profile=profile, q=q)
        extra = (
            "NOT EXISTS (SELECT 1 FROM jobs j WHERE j.media_id = m.id AND j.preset_id IS ? AND j.params = ?"
            f" AND j.status IN {OPEN_JOB_STATUSES_SQL})"
            " AND NOT EXISTS (SELECT 1 FROM renditions r WHERE r.media_id = m.id AND r.preset_id IS ? AND r.params = ?)"
        )
        where = f"{where} AND {extra}" if where else f"WHERE {extra}"
        key = canonical_json(params)
        return where, [*values, preset_id, key, preset_id, key]

    def count_where(self, where: str, params: list[Any]) -> int:
        """Conta mídias de uma cláusula de `filter_where`/`process_filter_where` (sobre `media m`)."""
        return int(self.db.scalar(f"SELECT COUNT(*) FROM media m {where}", tuple(params)))

    def count(self, *, view: str = "all", media_type: str | None = None, profile: str | None = None,
              q: str | None = None) -> int:
        where, params = self.filter_where(view=view, media_type=media_type, profile=profile, q=q)
        return int(self.db.scalar(f"SELECT COUNT(*) FROM media m {where}", tuple(params)))

    def list(
        self,
        *,
        view: str = "all",
        media_type: str | None = None,
        profile: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        before_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], int, int | None]:
        """Mídias em ordem id DESC. `before_id` (keyset): só ids menores; `next_before_id` None no fim.

        `total` ignora o keyset (vale para o filtro inteiro)."""
        where, params = self.filter_where(view=view, media_type=media_type, profile=profile, q=q)
        total = self.db.scalar(f"SELECT COUNT(*) FROM media m {where}", tuple(params))
        if before_id is not None:
            where = f"{where} AND m.id < ?" if where else "WHERE m.id < ?"
            params = [*params, before_id]
        rows = self.db.fetchall(
            f"SELECT m.* FROM media m {where} ORDER BY m.id DESC LIMIT ? OFFSET ?", (*params, limit + 1, offset)
        )
        next_before_id = rows[limit - 1]["id"] if len(rows) > limit else None
        return [dict(row) for row in rows[:limit]], int(total), next_before_id

    def last_jobs(self, media_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not media_ids:
            return {}
        rows = self.db.fetchall(
            f"SELECT j.* FROM jobs j JOIN (SELECT media_id, MAX(id) AS id FROM jobs WHERE media_id IN "
            f"({placeholders(len(media_ids))}) GROUP BY media_id) last ON last.id = j.id",
            tuple(media_ids),
        )
        return {row["media_id"]: decode_job(row) for row in rows}

    def active_job_count(self, media_id: int) -> int:
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM jobs WHERE media_id = ? AND status IN ('downloading', 'downloaded', 'processing')",
                (media_id,),
            )
        )

    # ---- renditions --------------------------------------------------------------------------------------------
    def renditions_for(self, media_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        result: dict[int, list[dict[str, Any]]] = {media_id: [] for media_id in media_ids}
        if not media_ids:
            return result
        rows = self.db.fetchall(
            f"SELECT * FROM renditions WHERE media_id IN ({placeholders(len(media_ids))}) ORDER BY id",
            tuple(media_ids),
        )
        for row in rows:
            result[row["media_id"]].append(_decode_rendition(row))
        return result

    def get_rendition(self, rendition_id: int) -> dict[str, Any] | None:
        return _decode_rendition(self.db.fetchone("SELECT * FROM renditions WHERE id = ?", (rendition_id,)))

    def find_rendition(self, media_id: int, preset_id: int | None, params: dict[str, Any]) -> dict[str, Any] | None:
        return _decode_rendition(
            self.db.fetchone(
                "SELECT * FROM renditions WHERE media_id = ? AND preset_id IS ? AND params = ? ORDER BY id LIMIT 1",
                (media_id, preset_id, canonical_json(params)),
            )
        )

    def insert_rendition(self, fields: dict[str, Any]) -> int:
        _check_columns(fields, RENDITION_COLUMNS)
        data = {**fields, "params": canonical_json(fields["params"]), "created_at": utc_now()}
        columns = list(data)
        cursor = self.db.execute(
            f"INSERT INTO renditions ({', '.join(columns)}) VALUES ({placeholders(len(columns))})",
            tuple(data[c] for c in columns),
        )
        return cursor.lastrowid

    def delete_rendition(self, rendition_id: int) -> None:
        self.db.execute("DELETE FROM renditions WHERE id = ?", (rendition_id,))
