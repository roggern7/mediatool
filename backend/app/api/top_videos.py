"""Top vídeos de um perfil (aba Importar): busca em segundo plano dos Reels com mais visualizações
(app/services/top_videos.py). Baixar os escolhidos é uma importação por links comum (`POST /api/imports/links`)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path
from starlette.responses import Response

from app.api.deps import Ctx
from app.core.errors import AppError
from app.schemas.top_videos import TopVideosCreatedOut, TopVideosOut, TopVideosRequest

router = APIRouter(tags=["importação"])

SearchId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
Code = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{5,64}$")]


def _service(ctx: Ctx) -> Any:
    service = getattr(ctx, "top_videos", None)
    if service is None:
        raise AppError(503, "top_videos_unavailable", "A busca de top vídeos não está disponível.")
    return service


@router.post("/top-videos", response_model=TopVideosCreatedOut, status_code=202)
def start_top_videos(ctx: Ctx, body: TopVideosRequest) -> dict[str, Any]:
    """Começa a busca e responde na hora; acompanhe por `GET /api/top-videos/{id}`."""
    return {"id": _service(ctx).start(body.profile, top=body.top, scan_limit=body.scan_limit)}


@router.get("/top-videos/{search_id}", response_model=TopVideosOut)
def get_top_videos(ctx: Ctx, search_id: SearchId) -> dict[str, Any]:
    return _service(ctx).get(search_id)


@router.post("/top-videos/{search_id}/cancel", response_model=TopVideosOut)
def cancel_top_videos(ctx: Ctx, search_id: SearchId) -> dict[str, Any]:
    return _service(ctx).cancel(search_id)


@router.get("/top-videos/{search_id}/thumb/{code}", response_class=Response)
def top_video_thumbnail(ctx: Ctx, search_id: SearchId, code: Code) -> Response:
    content, content_type = _service(ctx).thumbnail(search_id, code)
    return Response(content, media_type=content_type,
                    headers={"cache-control": "private, max-age=3600", "x-content-type-options": "nosniff"})
