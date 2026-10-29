from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from app.api.deps import MAX_ID, Ctx
from app.db.repositories import edit_folders
from app.schemas.batches import BatchOut
from app.schemas.common import SafeText
from app.schemas.edits import EditFolderList, EditRequest
from app.schemas.media import MediaList
from app.services import edit_service
from app.services.presenters import batch_single, media_out

router = APIRouter(tags=["edição"])

FolderKey = Annotated[str, Path(pattern=edit_folders.FOLDER_KEY_PATTERN)]


@router.post("/edits", response_model=BatchOut, status_code=202)
def start_edit(ctx: Ctx, body: EditRequest) -> dict[str, Any]:
    """Edita vídeos da biblioteca em lote (seção 8). Progresso por GET /api/batches/{id}."""
    batch_id = edit_service.start(ctx, body.media_ids, body.settings)
    return batch_single(ctx, batch_id)


@router.get("/edit/folders", response_model=EditFolderList)
def list_edit_folders(ctx: Ctx) -> dict[str, Any]:
    """Pastas de vídeos da página Editar (seção 8.4): uma por importação/grupo, por dia de envio e "Outros"."""
    return {"items": edit_folders.list_folders(ctx.db)}


@router.get("/edit/folders/{key}/media", response_model=MediaList)
def list_edit_folder_media(
    ctx: Ctx,
    key: FolderKey,
    q: Annotated[SafeText | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
) -> dict[str, Any]:
    """Vídeos de uma pasta, id DESC, mesma paginação keyset de GET /media."""
    items, total, next_before_id = ctx.media.list(
        media_type="video", q=(q or "").strip() or None, limit=limit, before_id=before_id,
        scope=edit_folders.folder_clause(key),
    )
    return {"items": media_out(ctx, items), "total": total, "next_before_id": next_before_id}
