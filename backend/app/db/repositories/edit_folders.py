"""Pastas de vídeos da página Editar (somente leitura): uma pasta por vez que o dono importou ou enviou vídeos.

- `group:<id>`: um grupo de importação (vários perfis de uma vez);
- `batch:<id>`: um lote de importação (perfil ou link) sem grupo, inclusive cancelado com mídias;
- `local:<aaaa-mm-dd>`: vídeos enviados do computador (`source_platform = 'local'`) no dia local de `created_at`;
- `other`: vídeos que não caem em nenhuma das anteriores.

Lote -> mídia segue a mesma regra da exportação (`_BATCH_MEDIA`/`_GROUP_MEDIA`): jobs `ingest`/`process` concluídos
com mídia UNIDOS a `batch_media`. Só lotes de importação formam pastas (lotes de Edição/processamento não). O dia
local usa o fuso do computador (`date(..., 'localtime')`), o mesmo do navegador no app local.
"""

from __future__ import annotations

from typing import Any

from app.db.connection import Database
from app.db.repositories.common import placeholders
from app.db.repositories.exports import (
    _BATCH_MEDIA,
    _GROUP_MEDIA,
    ExportRepository,
    decode_export,
    export_content,
)

FOLDER_KEY_PATTERN = r"^(group:[1-9][0-9]{0,15}|batch:[1-9][0-9]{0,15}|local:[0-9]{4}-[0-9]{2}-[0-9]{2}|other)$"

# Pares (lote, mídia) de todos os lotes de uma vez, pela mesma regra de `_BATCH_MEDIA`.
_LINKS = (
    "SELECT j.batch_id, j.media_id FROM jobs j WHERE j.kind IN ('ingest', 'process') AND j.status = 'completed'"
    " AND j.media_id IS NOT NULL UNION SELECT bm.batch_id, bm.media_id FROM batch_media bm"
)
_IMPORT_MEDIA = (
    f"SELECT l.media_id FROM ({_LINKS}) l JOIN batches b ON b.id = l.batch_id WHERE b.source = 'instagram_import'"
)
# Uma consulta agregada para todas as pastas: chave, vídeos, versões, bytes (originais + versões), último envio e
# quantos vídeos da pasta também estão em outra pasta (apagar a pasta os tira de lá também).
_FOLDERS = f"""
WITH imported AS (
    SELECT DISTINCT CASE WHEN b.group_id IS NULL THEN 'batch:' || b.id ELSE 'group:' || b.group_id END AS key,
           l.media_id
    FROM ({_LINKS}) l JOIN batches b ON b.id = l.batch_id WHERE b.source = 'instagram_import'
),
uploaded AS (
    SELECT 'local:' || date(m.created_at, 'localtime') AS key, m.id AS media_id FROM media m
    WHERE m.media_type = 'video' AND m.source_platform = 'local'
),
orphan AS (
    SELECT 'other' AS key, m.id AS media_id FROM media m
    WHERE m.media_type = 'video' AND m.source_platform <> 'local' AND m.id NOT IN (SELECT media_id FROM imported)
),
everything AS (
    SELECT key, media_id FROM imported UNION ALL SELECT key, media_id FROM uploaded
    UNION ALL SELECT key, media_id FROM orphan
),
versions AS (SELECT media_id, COUNT(*) AS n, SUM(file_size) AS bytes FROM renditions GROUP BY media_id),
shared AS (SELECT media_id FROM everything GROUP BY media_id HAVING COUNT(*) > 1)
SELECT e.key, COUNT(*) AS video_count, MAX(m.created_at) AS last_at,
       COALESCE(SUM(v.n), 0) AS rendition_count, SUM(m.file_size) + COALESCE(SUM(v.bytes), 0) AS total_bytes,
       SUM(e.media_id IN (SELECT media_id FROM shared)) AS shared_count
FROM everything e JOIN media m ON m.id = e.media_id AND m.media_type = 'video'
LEFT JOIN versions v ON v.media_id = m.id
GROUP BY e.key
"""


def folder_clause(key: str) -> tuple[str, list[Any]]:
    """Condição (sobre `media m`) das mídias de uma pasta; `key` já validada por FOLDER_KEY_PATTERN."""
    kind, _, value = key.partition(":")
    if kind == "group":
        return f"m.id IN ({_GROUP_MEDIA})", [int(value), int(value)]
    if kind == "batch":
        return f"m.id IN ({_BATCH_MEDIA})", [int(value), int(value)]
    if kind == "local":
        return "m.source_platform = 'local' AND date(m.created_at, 'localtime') = ?", [value]
    return f"m.source_platform <> 'local' AND m.id NOT IN ({_IMPORT_MEDIA})", []


def folder_video_ids(db: Database, key: str) -> list[int]:
    where, params = folder_clause(key)
    rows = db.fetchall(f"SELECT m.id FROM media m WHERE m.media_type = 'video' AND {where} ORDER BY m.id",
                       tuple(params))
    return [int(row["id"]) for row in rows]


def list_folders(db: Database) -> list[dict[str, Any]]:
    """Pastas com ao menos um vídeo, da mais recente para a mais antiga ("Outros vídeos" por último)."""
    stats = {row["key"]: dict(row) for row in db.fetchall(_FOLDERS)}
    groups = {row["id"]: row["created_at"] for row in db.fetchall("SELECT id, created_at FROM import_groups")}
    group_profiles: dict[int, list[str]] = {}
    for row in db.fetchall(
        "SELECT group_id, source_profile FROM batches WHERE group_id IS NOT NULL AND source = 'instagram_import'"
        " ORDER BY group_id, group_position, id"
    ):
        if row["source_profile"]:
            group_profiles.setdefault(row["group_id"], []).append(row["source_profile"])
    batches = {
        row["id"]: dict(row)
        for row in db.fetchall(
            "SELECT id, source_profile, import_mode, created_at FROM batches"
            " WHERE source = 'instagram_import' AND group_id IS NULL"
        )
    }
    folders: list[dict[str, Any]] = []
    for key, stat in stats.items():
        kind, _, value = key.partition(":")
        folder = {
            "key": key, "kind": kind, "import_mode": None, "profiles": [], "created_at": stat["last_at"],
            "day": None, "video_count": int(stat["video_count"]), "rendition_count": int(stat["rendition_count"]),
            "total_bytes": int(stat["total_bytes"] or 0), "shared_count": int(stat["shared_count"] or 0),
        }
        if kind == "group":
            folder.update(import_mode="profile", profiles=group_profiles.get(int(value), []),
                          created_at=groups.get(int(value)) or stat["last_at"])
        elif kind == "batch":
            batch = batches.get(int(value))
            if batch is None:
                continue
            links = batch["import_mode"] == "links"
            folder.update(import_mode="links" if links else "profile", created_at=batch["created_at"],
                          profiles=[] if links or not batch["source_profile"] else [batch["source_profile"]])
        elif kind == "local":
            folder["day"] = value
        folders.append(folder)
    folders.sort(key=lambda f: (f["kind"] != "other", f["created_at"] or ""), reverse=True)
    return folders


def media_in_open_exports(db: Database, media_ids: list[int]) -> set[int]:
    """Quais destas mídias estão numa exportação pendente ou em andamento (uma consulta por exportação aberta)."""
    if not media_ids:
        return set()
    busy: set[int] = set()
    for row in db.fetchall("SELECT * FROM exports WHERE status IN ('pending', 'running')"):
        where, params = ExportRepository._where(decode_export(row))
        rows = db.fetchall(
            f"SELECT m.id FROM media m {where} AND m.id IN ({placeholders(len(media_ids))})",
            (*params, *media_ids),
        )
        busy.update(int(r["id"]) for r in rows)
    return busy


def rendition_in_open_export(db: Database, rendition_id: int) -> bool:
    """A versão está numa exportação pendente ou em andamento que leva versões (`edited`/`both`, pasta ou ZIP)?"""
    exports = ExportRepository(db)
    for row in db.fetchall("SELECT * FROM exports WHERE status IN ('pending', 'running')"):
        export = decode_export(row)
        if export_content(export) == "originals":
            continue
        ids, params = exports._rendition_ids(export)
        if db.scalar(f"SELECT 1 FROM renditions r WHERE r.id = ? AND r.id IN ({ids})", (rendition_id, *params)):
            return True
    return False
