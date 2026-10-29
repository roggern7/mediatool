from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import Ctx
from app.schemas.batches import BatchOut
from app.schemas.edits import EditRequest
from app.services import edit_service
from app.services.presenters import batch_single

router = APIRouter(tags=["edição"])


@router.post("/edits", response_model=BatchOut, status_code=202)
def start_edit(ctx: Ctx, body: EditRequest) -> dict[str, Any]:
    """Edita vídeos da biblioteca em lote (seção 8). Progresso por GET /api/batches/{id}."""
    batch_id = edit_service.start(ctx, body.media_ids, body.settings)
    return batch_single(ctx, batch_id)
