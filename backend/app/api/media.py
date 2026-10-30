from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from fastapi.responses import FileResponse

from app.api.deps import MAX_ID, Ctx, IdPath
from app.core.errors import AppError, not_found
from app.db.repositories import edit_folders
from app.schemas.batches import BatchOut
from app.schemas.common import SafeText
from app.schemas.jobs import JobOut
from app.schemas.media import (
    BulkProcessRequest,
    MediaDeleteRequest,
    MediaDeleteResult,
    MediaList,
    MediaOut,
    OpenRequest,
    OpenResult,
    ProcessByFilterRequest,
    ProcessRequest,
)
from app.services import import_service, media_service
from app.services.presenters import batch_single, job_single, media_out
from app.services.storage import mime_for

router = APIRouter(tags=["biblioteca"])


def _media_or_404(ctx: Ctx, media_id: int) -> dict[str, Any]:
    media = ctx.media.get(media_id)
    if media is None:
        raise not_found("Mídia", "media_not_found", feminine=True)
    return media


@router.get("/media", response_model=MediaList)
def list_media(
    ctx: Ctx,
    view: Literal["all", "originals", "processed"] = "all",
    media_type: Literal["video", "image"] | None = None,
    # Papel (migration 0021): "cover" = só capas; "original" = sem as capas; omitido = todas.
    role: Literal["original", "cover"] | None = None,
    profile: Annotated[SafeText | None, Query(max_length=64)] = None,
    q: Annotated[SafeText | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
    before_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
) -> dict[str, Any]:
    """Ordem id DESC. Paginação keyset: `before_id` + `next_before_id` (null no fim); `offset` por compatibilidade."""
    items, total, next_before_id = ctx.media.list(
        view=view,
        media_type=media_type,
        role=role,
        profile=(profile or "").strip().lstrip("@") or None,
        q=(q or "").strip() or None,
        limit=limit,
        offset=offset,
        before_id=before_id,
    )
    return {"items": media_out(ctx, items), "total": total, "next_before_id": next_before_id}


@router.post("/media/process", response_model=BatchOut, status_code=202)
def process_media_bulk(ctx: Ctx, body: BulkProcessRequest) -> dict[str, Any]:
    batch_id = import_service.process_media_bulk(ctx, body.media_ids, body.preset_id)
    return batch_single(ctx, batch_id)


@router.post("/media/process-by-filter", response_model=BatchOut, status_code=202)
def process_media_by_filter(ctx: Ctx, body: ProcessByFilterRequest) -> dict[str, Any]:
    """Processa todas as mídias do filtro da Biblioteca (mesmo filtro de GET /media), até MAX_BULK_PROCESS."""
    batch_id = import_service.process_media_by_filter(
        ctx,
        view=body.view,
        media_type=body.media_type,
        q=(body.q or "").strip() or None,
        profile=(body.profile or "").strip().lstrip("@") or None,
        preset_id=body.preset_id,
        role=body.role,
    )
    return batch_single(ctx, batch_id)


@router.post("/media/delete", response_model=MediaDeleteResult)
def delete_media_bulk(ctx: Ctx, body: MediaDeleteRequest) -> dict[str, Any]:
    """Apaga várias mídias (ou os vídeos de uma pasta da página Editar) item a item; devolve o resumo."""
    if not body.delete_original:
        raise AppError(
            400, "confirmation_required",
            "Excluir uma mídia apaga o original. Confirme com delete_original=true.",
        )
    ids = body.media_ids if body.media_ids is not None else edit_folders.folder_video_ids(ctx.db, body.folder or "")
    return media_service.delete_many(ctx, list(dict.fromkeys(ids)))


@router.get("/media/{media_id}", response_model=MediaOut)
def get_media(ctx: Ctx, media_id: IdPath) -> dict[str, Any]:
    return media_out(ctx, [_media_or_404(ctx, media_id)])[0]


@router.get("/media/{media_id}/thumbnail", response_class=FileResponse)
def media_thumbnail(ctx: Ctx, media_id: IdPath) -> FileResponse:
    media = _media_or_404(ctx, media_id)
    path = media_service.stored_file(ctx, media["thumbnail_path"], what="Thumbnail")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/media/{media_id}/original", response_class=FileResponse)
def media_original(ctx: Ctx, media_id: IdPath) -> FileResponse:
    media = _media_or_404(ctx, media_id)
    path = media_service.stored_file(ctx, media["original_path"], what="Arquivo original")
    return FileResponse(path, media_type=media["mime_type"] or mime_for(path), content_disposition_type="inline")


@router.get("/renditions/{rendition_id}/file", response_class=FileResponse)
def rendition_file(ctx: Ctx, rendition_id: IdPath) -> FileResponse:
    rendition = ctx.media.get_rendition(rendition_id)
    if rendition is None:
        raise not_found("Versão processada", "rendition_not_found", feminine=True)
    path = media_service.stored_file(ctx, rendition["processed_path"], what="Arquivo processado")
    return FileResponse(path, media_type=mime_for(path), content_disposition_type="inline")


@router.post("/media/{media_id}/process", response_model=JobOut, status_code=202)
def process_media(ctx: Ctx, media_id: IdPath, body: ProcessRequest) -> dict[str, Any]:
    job_id = import_service.process_media(ctx, media_id, body.preset_id)
    return job_single(ctx, job_id)


@router.delete("/renditions/{rendition_id}", status_code=204)
def delete_rendition(ctx: Ctx, rendition_id: IdPath) -> Response:
    media_service.delete_rendition(ctx, rendition_id)
    return Response(status_code=204)


@router.delete("/media/{media_id}", status_code=204)
def delete_media(ctx: Ctx, media_id: IdPath, delete_original: bool = False) -> Response:
    if not delete_original:
        raise AppError(
            400, "confirmation_required",
            "Excluir uma mídia apaga o original. Confirme com delete_original=true.",
        )
    media_service.delete_media(ctx, media_id)
    return Response(status_code=204)


@router.post("/media/{media_id}/open", response_model=OpenResult)
def open_media(ctx: Ctx, media_id: IdPath, body: OpenRequest) -> dict[str, bool]:
    media_service.open_media(ctx, media_id, body.target, body.rendition_id)
    return {"ok": True}
