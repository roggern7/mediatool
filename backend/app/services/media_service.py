"""Operações de arquivo da biblioteca: resolver, abrir e excluir mídia/renditions com consistência disco-banco."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.core.errors import AppError, not_found
from app.core.security import UnsafePathError, safe_join
from app.db.repositories import edit_folders
from app.services.context import AppContext
from app.services.import_service import cancel_job_ids
from app.services.storage import FileBusyError
from app.services.system_open import SystemOpenError, open_path

logger = logging.getLogger("media_tool.media")

_OPEN_ERROR_STATUS = {"not_found": 404, "not_allowed": 403, "invalid_target": 422, "unsupported": 501}


def stored_file(ctx: AppContext, relative: str | None, *, what: str) -> Path:
    """Caminho absoluto de um arquivo do storage; 404 se ausente."""
    if not relative:
        raise not_found(what, "file_not_found")
    try:
        path = safe_join(ctx.storage.root, relative)
    except UnsafePathError as exc:
        logger.error("Caminho inválido no banco para %s", what)
        raise AppError(500, "invalid_stored_path", "Caminho de arquivo inválido no banco.") from exc
    if not path.is_file():
        raise AppError(404, "file_missing", f"{what} não encontrado no disco.")
    return path


_BUSY_DETAIL = "O vídeo tem um job em andamento (baixando ou processando). Aguarde terminar ou cancele o job."
_EXPORT_DETAIL = "numa exportação em andamento (pasta ou ZIP). Aguarde terminar ou cancele a exportação."


def delete_rendition(ctx: AppContext, rendition_id: int) -> None:
    rendition = ctx.media.get_rendition(rendition_id)
    if rendition is None:
        raise not_found("Versão processada", "rendition_not_found", feminine=True)
    # Sob o lock de criação de exportação: nenhuma exportação nova começa entre a checagem e a exclusão.
    with ctx.export_create_lock:
        if ctx.media.active_job_count(rendition["media_id"]):
            raise AppError(409, "media_busy", _BUSY_DETAIL)
        if edit_folders.rendition_in_open_export(ctx.db, rendition_id):
            raise AppError(409, "export_in_progress", f"A versão editada está {_EXPORT_DETAIL}")
        path = ctx.storage.resolve(rendition["processed_path"])
        staged = _stage(ctx, [path])
        try:
            ctx.media.delete_rendition(rendition_id)
        except BaseException:
            staged.rollback()
            raise
        staged.commit()
    logger.info("Rendition %s excluída", rendition_id)


def delete_media(ctx: AppContext, media_id: int) -> None:
    media = ctx.media.get(media_id)
    if media is None:
        raise not_found("Mídia", "media_not_found", feminine=True)
    # Sob o lock de criação de exportação: nenhuma exportação nova começa entre a checagem e a exclusão.
    with ctx.export_create_lock:
        if ctx.media.active_job_count(media_id):
            raise AppError(409, "media_busy", "A mídia tem um job em andamento. Cancele o job antes de excluir.")
        if edit_folders.media_in_open_exports(ctx.db, [media_id]):
            raise AppError(409, "export_in_progress", f"A mídia está {_EXPORT_DETAIL}")
        cancel_job_ids(ctx, ctx.jobs.non_terminal_ids(media_id=media_id))

        renditions = ctx.media.renditions_for([media_id])[media_id]
        paths = [ctx.storage.resolve(r["processed_path"]) for r in renditions]
        if media["thumbnail_path"]:
            paths.append(ctx.storage.resolve(media["thumbnail_path"]))
        paths.append(ctx.storage.resolve(media["original_path"]))

        staged = _stage(ctx, paths)
        try:
            with ctx.db.transaction():
                ctx.media.delete(media_id)  # renditions saem por ON DELETE CASCADE
        except BaseException:
            staged.rollback()
            raise
        leftovers = staged.commit()
    if leftovers:
        logger.warning("Mídia %s excluída; %s arquivo(s) de descarte ficaram no disco", media_id, len(leftovers))
    logger.info("Mídia %s excluída (original, %s renditions e thumbnail)", media_id, len(renditions))


# Motivos de pular (a mídia continua na biblioteca, nada quebrou); os demais erros contam como falha.
_SKIP_CODES = frozenset({"media_not_found", "media_busy", "export_in_progress"})


def delete_many(ctx: AppContext, media_ids: list[int]) -> dict[str, Any]:
    """Apaga mídias uma a uma com `delete_media` (mesmas garantias, inclusive a checagem de exportação aberta feita
    por item sob o lock de criação de exportação). Mídia inexistente, com job em andamento ou numa exportação
    pendente/em andamento é pulada; erro de um item não interrompe os outros."""
    deleted: list[int] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for media_id in media_ids:
        try:
            delete_media(ctx, media_id)
        except AppError as exc:
            issue = {"id": media_id, "code": exc.code, "message": exc.detail}
            (skipped if exc.code in _SKIP_CODES else failed).append(issue)
        except Exception:
            logger.exception("Falha ao excluir a mídia %s", media_id)
            failed.append({"id": media_id, "code": "delete_failed", "message": "Não foi possível excluir a mídia."})
        else:
            deleted.append(media_id)
    logger.info("Exclusão em massa: %s apagadas, %s puladas, %s com falha", len(deleted), len(skipped), len(failed))
    return {"deleted": len(deleted), "deleted_ids": deleted, "skipped": skipped, "failed": failed}


def _stage(ctx: AppContext, paths: list[Path]):
    try:
        return ctx.storage.stage_deletion(paths)
    except FileBusyError as exc:
        raise AppError(
            409, "file_in_use", "Um dos arquivos está em uso por outro programa. Feche-o e tente de novo."
        ) from exc


def open_media(ctx: AppContext, media_id: int, target: str, rendition_id: int | None) -> None:
    media = ctx.media.get(media_id)
    if media is None:
        raise not_found("Mídia", "media_not_found", feminine=True)
    if rendition_id is not None:
        rendition = ctx.media.get_rendition(rendition_id)
        if rendition is None or rendition["media_id"] != media_id:
            raise not_found("Versão processada", "rendition_not_found", feminine=True)
        path = stored_file(ctx, rendition["processed_path"], what="Arquivo processado")
    else:
        path = stored_file(ctx, media["original_path"], what="Arquivo original")
    try:
        open_path(path, allowed_roots=[ctx.storage.root], target=target)
    except SystemOpenError as exc:
        raise AppError(_OPEN_ERROR_STATUS.get(exc.code, 500), exc.code, exc.message) from exc
