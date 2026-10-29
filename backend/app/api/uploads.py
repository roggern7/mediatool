"""Upload de vídeos locais para a biblioteca (multipart, um arquivo por requisição, em streaming).

Isento do limite de 1 MB do corpo (`BodySizeLimitMiddleware`, `exempt_paths`, só o caminho exato); o teto é
`settings.max_download_bytes`. Detalhes em app/services/upload_service.py.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import Ctx
from app.schemas.uploads import UploadOut
from app.services.presenters import media_out
from app.services.upload_service import UploadSlots, receive_upload

router = APIRouter(tags=["biblioteca"])


def _slots(request: Request) -> UploadSlots:
    slots = getattr(request.app.state, "upload_slots", None)
    if slots is None:
        slots = request.app.state.upload_slots = UploadSlots()
    return slots


@router.post(
    "/library/uploads",
    response_model=UploadOut,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file"],
                "properties": {"file": {"type": "string", "format": "binary"}},
            }}},
        }
    },
)
async def upload_local_video(ctx: Ctx, request: Request) -> dict[str, Any]:
    """Recebe um vídeo local (campo `file`), registra na biblioteca com `source_platform='local'`.
    Conteúdo idêntico a uma mídia existente devolve a existente com `duplicate: true`."""
    media, duplicate = await receive_upload(ctx, request, _slots(request))
    out = await run_in_threadpool(media_out, ctx, [media])
    return {"media": out[0], "duplicate": duplicate}
