from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import FileResponse
from starlette.responses import Response

from app.api.deps import MAX_ID, Ctx, IdPath
from app.core.security import CLIENT_HEADER
from app.schemas.capcut import (
    AnimationPage,
    AnimationType,
    CaptionDraft,
    CapCutSettingsOut,
    CapCutSettingsUpdate,
    CapCutStatusOut,
    FontPage,
    FontSource,
    JobList,
    JobOut,
    JobsCreate,
    JobsCreated,
    JobStatus,
    PreviewCreate,
    PreviewCreated,
    PreviewOut,
    QueueState,
    StyleCreate,
    StyleOut,
    StyleUpdate,
)
from app.services import capcut_service

router = APIRouter(prefix="/capcut", tags=["legendas capcut"])

StyleIdPath = Annotated[int, Path(ge=0, le=MAX_ID)]   # 0 = estilo embutido
PreviewIdPath = Annotated[str, Path(pattern=r"^[0-9a-f]{12}$")]
CatalogQuery = Annotated[str | None, Query(max_length=100)]
CatalogLimit = Annotated[int, Query(ge=1, le=200)]
CatalogOffset = Annotated[int, Query(ge=0, le=100_000)]


@router.get("/status", response_model=CapCutStatusOut)
def status(ctx: Ctx) -> dict[str, Any]:
    """CapCut, dependências, skill e modelos (só leitura) + fila e configurações. Sem o núcleo: `available=false`."""
    return capcut_service.core_status(ctx)


@router.get("/settings", response_model=CapCutSettingsOut)
def get_settings(ctx: Ctx) -> dict[str, Any]:
    return capcut_service.get_settings(ctx)


@router.put("/settings", response_model=CapCutSettingsOut)
def update_settings(ctx: Ctx, body: CapCutSettingsUpdate) -> dict[str, Any]:
    """Só os campos enviados mudam; `null` volta ao padrão. A pasta de saída é criada se não existir."""
    return capcut_service.update_settings(ctx, body.model_dump(exclude_unset=True))


@router.get("/drafts", response_model=list[CaptionDraft])
def list_drafts(ctx: Ctx) -> list[dict[str, Any]]:
    """Projetos do CapCut com texto (os criados pelo Media Tool ficam de fora)."""
    return capcut_service.list_drafts()


@router.get("/styles", response_model=list[StyleOut])
def list_styles(ctx: Ctx) -> list[dict[str, Any]]:
    return capcut_service.list_styles(ctx)


@router.post("/styles", response_model=StyleOut, status_code=201)
def create_style(ctx: Ctx, body: StyleCreate) -> dict[str, Any]:
    """Copia o estilo do texto `segment_index` do projeto `draft_name` do CapCut, ou cria pelo editor (`design`)."""
    if body.design is not None:
        return capcut_service.create_design_style(ctx, body.name, body.design.to_json())
    return capcut_service.create_style(ctx, body.name, body.draft_name, body.segment_index)


@router.put("/styles/{style_id}", response_model=StyleOut)
def update_style(ctx: Ctx, style_id: IdPath, body: StyleUpdate) -> dict[str, Any]:
    """Só modelos criados pelo editor (409 `style_not_editable` para os copiados de rascunho)."""
    return capcut_service.update_design_style(ctx, style_id, body.name, body.design.to_json())


@router.get("/catalog/fonts", response_model=FontPage)
def catalog_fonts(
    ctx: Ctx,
    q: CatalogQuery = None,
    source: FontSource | None = None,
    include_pro: bool = False,
    limit: CatalogLimit = 50,
    offset: CatalogOffset = 0,
) -> dict[str, Any]:
    """Fontes embutidas do CapCut, do Windows e do catálogo local do CapCut (Pro só com `include_pro`)."""
    return capcut_service.catalog_fonts(ctx, q=q, source=source, include_pro=include_pro, limit=limit, offset=offset)


@router.get("/catalog/animations", response_model=AnimationPage)
def catalog_animations(
    ctx: Ctx,
    type: AnimationType | None = None,  # noqa: A002 - nome do contrato
    q: CatalogQuery = None,
    include_pro: bool = False,
    limit: CatalogLimit = 50,
    offset: CatalogOffset = 0,
) -> dict[str, Any]:
    """Animações de texto do catálogo local, classificadas em entrada/saída/loop (Pro só com `include_pro`)."""
    return capcut_service.catalog_animations(ctx, type=type, q=q, include_pro=include_pro, limit=limit,
                                             offset=offset)


@router.get("/fonts/file", response_class=FileResponse)
def font_file(ctx: Ctx, key: Annotated[str, Query(min_length=3, max_length=300)]) -> FileResponse:
    """Arquivo da fonte para o @font-face da prévia, só de pastas permitidas; 404 sem arquivo local."""
    path = capcut_service.font_file(key)
    media_type = "font/otf" if path.suffix.lower() == ".otf" else "font/ttf"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"})


@router.post("/previews", response_model=PreviewCreated, status_code=202)
def create_preview(ctx: Ctx, body: PreviewCreate) -> dict[str, Any]:
    """Prévia real (CapCut, 720P) de um trecho de `seconds` a partir de `start_s`; prioridade sobre os jobs."""
    return capcut_service.create_preview(ctx, design=body.design.to_json() if body.design is not None else None,
                                         style_id=body.style_id, media_id=body.media_id, start_s=body.start_s,
                                         seconds=body.seconds)


@router.get("/previews/{preview_id}", response_model=PreviewOut)
def get_preview(ctx: Ctx, preview_id: PreviewIdPath) -> dict[str, Any]:
    return capcut_service.get_preview(ctx, preview_id)


@router.get("/previews/{preview_id}/file", response_class=FileResponse)
def preview_file(ctx: Ctx, preview_id: PreviewIdPath) -> FileResponse:
    path = capcut_service.preview_file(ctx, preview_id)
    return FileResponse(path, media_type="video/mp4")


@router.post("/previews/{preview_id}/cancel", response_model=PreviewOut)
def cancel_preview(ctx: Ctx, preview_id: PreviewIdPath) -> dict[str, Any]:
    return capcut_service.cancel_preview(ctx, preview_id)


@router.delete("/styles/{style_id}", status_code=204)
def delete_style(ctx: Ctx, style_id: IdPath) -> Response:
    capcut_service.delete_style(ctx, style_id)
    return Response(status_code=204)


@router.post("/styles/{style_id}/default", response_model=list[StyleOut])
def set_default_style(ctx: Ctx, style_id: StyleIdPath) -> list[dict[str, Any]]:
    """Modelo usado quando o job não escolhe outro; 0 = estilo embutido."""
    return capcut_service.set_default_style(ctx, style_id)


@router.get("/jobs", response_model=JobList)
def list_jobs(
    ctx: Ctx,
    status: JobStatus | None = None,
    media_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=2**31)] = 0,
) -> dict[str, Any]:
    """Mais recentes primeiro."""
    return capcut_service.list_jobs(ctx, status=status, media_id=media_id, limit=limit, offset=offset)


@router.post("/jobs", response_model=JobsCreated, status_code=202)
def create_jobs(ctx: Ctx, body: JobsCreate, request: Request) -> dict[str, Any]:
    """Um job por vídeo; pula mídia inexistente, não-vídeo, capa e vídeo que já está na fila."""
    origin = "manual" if request.headers.get(CLIENT_HEADER) == "panel" else "api"
    overrides = body.overrides.model_dump(exclude_unset=True) if body.overrides is not None else None
    return capcut_service.create_jobs(ctx, body.media_ids, style_id=body.style_id, overrides=overrides, origin=origin)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(ctx: Ctx, job_id: IdPath) -> dict[str, Any]:
    return capcut_service.get_job(ctx, job_id)


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(ctx: Ctx, job_id: IdPath) -> Response:
    """Remove do histórico (só job encerrado); o arquivo de saída fica."""
    capcut_service.delete_job(ctx, job_id)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(ctx: Ctx, job_id: IdPath) -> dict[str, Any]:
    return capcut_service.cancel_job(ctx, job_id)


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(ctx: Ctx, job_id: IdPath) -> dict[str, Any]:
    """Com erro ou cancelado: volta para a fila com as tentativas zeradas (a transcrição é reaproveitada)."""
    return capcut_service.retry_job(ctx, job_id)


@router.get("/jobs/{job_id}/file", response_class=FileResponse)
def job_file(ctx: Ctx, job_id: IdPath) -> FileResponse:
    path, _ = capcut_service.output_file(ctx, job_id)
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.post("/jobs/{job_id}/reveal")
def reveal_job(ctx: Ctx, job_id: IdPath) -> dict[str, bool]:
    """Abre o Explorer com o vídeo legendado selecionado."""
    capcut_service.reveal_output(ctx, job_id)
    return {"ok": True}


@router.post("/queue/pause", response_model=QueueState)
def pause_queue(ctx: Ctx) -> dict[str, bool]:
    """O job em andamento termina; os próximos esperam a retomada."""
    return capcut_service.set_queue_paused(ctx, True)


@router.post("/queue/resume", response_model=QueueState)
def resume_queue(ctx: Ctx) -> dict[str, bool]:
    return capcut_service.set_queue_paused(ctx, False)
