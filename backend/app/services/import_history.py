"""Histórico de "Importações recentes": remover um item (lote avulso ou grupo) ou limpar os terminados.

Só OCULTA (`history_hidden_at`, migration 0018): os lotes continuam existindo, então pastas da página Editar,
exportações, Fila e Biblioteca seguem iguais. Opcionalmente apaga os vídeos dessas importações com
`media_service.delete_many` (mesmas proteções: job/exportação em andamento é pulado com o motivo).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.errors import AppError, not_found
from app.db.connection import utc_now
from app.db.repositories import edit_folders
from app.services import media_service
from app.services.context import AppContext

logger = logging.getLogger("media_tool.imports")

FINISHED = ("completed", "failed", "cancelled")
IN_PROGRESS_MESSAGE = "Importação em andamento não sai do histórico. Cancele ou aguarde terminar."
# Grupo com algum filho ainda aberto (listando, baixando, pausado ou aguardando ação): não sai do histórico.
_OPEN_CHILD = ("EXISTS (SELECT 1 FROM batches c WHERE c.group_id = import_groups.id"
               " AND c.status NOT IN ('completed', 'failed', 'cancelled'))")


def _targets(ctx: AppContext, batch_ids: list[int], group_ids: list[int],
             all_finished: bool) -> tuple[list[int], list[int]]:
    db = ctx.db
    if all_finished:
        batches = [int(r["id"]) for r in db.fetchall(
            "SELECT id FROM batches WHERE source = 'instagram_import' AND group_id IS NULL"
            " AND history_hidden_at IS NULL AND status IN ('completed', 'failed', 'cancelled') ORDER BY id")]
        groups = [int(r["id"]) for r in db.fetchall(
            f"SELECT id FROM import_groups WHERE history_hidden_at IS NULL AND NOT {_OPEN_CHILD} ORDER BY id")]
        return batches, groups
    batches = list(dict.fromkeys(batch_ids))
    groups = list(dict.fromkeys(group_ids))
    for batch_id in batches:
        batch = ctx.batches.get(batch_id)
        if batch is None or batch["source"] != "instagram_import":
            raise not_found("Lote", "batch_not_found")
        if batch["group_id"] is not None:
            raise AppError(422, "batch_in_group",
                           "Este lote faz parte de uma importação de vários perfis: remova a importação inteira.")
        if batch["status"] not in FINISHED:
            raise AppError(409, "import_in_progress", IN_PROGRESS_MESSAGE, extra={"batch_id": batch_id})
    for group_id in groups:
        if ctx.batches.get_group(group_id) is None:
            raise not_found("Grupo de importação", "group_not_found")
        if db.scalar(f"SELECT 1 FROM import_groups WHERE id = ? AND {_OPEN_CHILD}", (group_id,)):
            raise AppError(409, "import_in_progress", IN_PROGRESS_MESSAGE, extra={"group_id": group_id})
    return batches, groups


def _videos(ctx: AppContext, batches: list[int], groups: list[int]) -> dict[int, int]:
    """Vídeos (id -> bytes do original + versões) das importações, pela mesma regra das pastas da página Editar."""
    sizes: dict[int, int] = {}
    for key in [f"batch:{b}" for b in batches] + [f"group:{g}" for g in groups]:
        where, params = edit_folders.folder_clause(key)
        for row in ctx.db.fetchall(
            "SELECT m.id, COALESCE(m.file_size, 0) + COALESCE((SELECT SUM(r.file_size) FROM renditions r"
            f" WHERE r.media_id = m.id), 0) AS bytes FROM media m WHERE m.media_type = 'video' AND {where}",
            tuple(params),
        ):
            sizes[int(row["id"])] = int(row["bytes"])
    return sizes


_IMPORT_MEDIA_ROWS = (
    "SELECT x.media_id, b.id AS batch_id, b.group_id FROM ("
    " SELECT j.media_id, j.batch_id FROM jobs j WHERE j.kind IN ('ingest', 'process') AND j.status = 'completed'"
    " AND j.media_id IS NOT NULL UNION SELECT bm.media_id, bm.batch_id FROM batch_media bm) x"
    " JOIN batches b ON b.id = x.batch_id LEFT JOIN import_groups g ON g.id = b.group_id"
    " WHERE b.source = 'instagram_import' AND b.history_hidden_at IS NULL AND (g.id IS NULL OR g.history_hidden_at IS NULL)"
)


def _shared(ctx: AppContext, videos: dict[int, int], batches: list[int], groups: list[int]) -> int:
    """Quantos desses vídeos também estão em OUTRA importação ainda visível no histórico (sairão dela se apagados)."""
    if not videos:
        return 0
    own_batches, own_groups = set(batches), set(groups)
    shared = {int(row["media_id"]) for row in ctx.db.fetchall(_IMPORT_MEDIA_ROWS)
              if int(row["media_id"]) in videos and row["batch_id"] not in own_batches
              and (row["group_id"] is None or row["group_id"] not in own_groups)}
    return len(shared)


def hide(ctx: AppContext, *, batch_ids: list[int], group_ids: list[int], all_finished: bool, delete_media: bool,
         dry_run: bool) -> dict[str, Any]:
    """Oculta do histórico os lotes avulsos/grupos terminados pedidos (ou todos, com `all_finished`). Em andamento:
    409 `import_in_progress`. Com `delete_media`, apaga depois os vídeos deles (resumo em `deleted`)."""
    batches, groups = _targets(ctx, batch_ids, group_ids, all_finished)
    videos = _videos(ctx, batches, groups)
    result: dict[str, Any] = {"batches": len(batches), "groups": len(groups), "video_count": len(videos),
                              "total_bytes": sum(videos.values()), "shared_count": _shared(ctx, videos, batches, groups),
                              "dry_run": dry_run, "deleted": None}
    if dry_run:
        return result
    now = utc_now()
    with ctx.db.transaction():
        for batch_id in batches:
            # A condição de status repete a conferência: lote reaberto (repetir falhos) no meio não é ocultado.
            ctx.db.execute("UPDATE batches SET history_hidden_at = ? WHERE id = ?"
                           " AND status IN ('completed', 'failed', 'cancelled')", (now, batch_id))
        for group_id in groups:
            ctx.db.execute(f"UPDATE import_groups SET history_hidden_at = ? WHERE id = ? AND NOT {_OPEN_CHILD}",
                           (now, group_id))
    logger.info("Histórico: %s lotes e %s grupos removidos (apagar vídeos: %s)", len(batches), len(groups),
                delete_media)
    if delete_media:
        result["deleted"] = media_service.delete_many(ctx, sorted(videos))
    return result
