from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.responses import Response

from app.api.deps import MAX_ID, Ctx, IdPath
from app.core.security import CLIENT_HEADER
from app.db.repositories.edit_folders import FOLDER_KEY_PATTERN
from app.schemas.capcut import (
    AnimationPage,
    AnimationType,
    BatchCreate,
    BatchCreated,
    BatchesHidden,
    BatchesHide,
    BatchList,
    BatchOut,
    BatchStatus,
    CaptionDraft,
    CapCutSettingsOut,
    CapCutMode,
    CapCutSettingsUpdate,
    CapCutStatusOut,
    EstimateOut,
    FontPage,
    FontSource,
    JobList,
    JobOut,
    JobsCreate,
    JobsCreated,
    JobStatus,
    LibraryClear,
    LibraryPage,
    LibraryShow,
    LibraryViewOut,
    PreviewCreate,
    PreviewCreated,
    PreviewOut,
    QueueState,
    ReviewApprove,
    ReviewOut,
    StyleCreate,
    StyleOut,
    StyleUpdate,
)
from app.schemas.common import SafeText
from app.services import capcut_batch_service, capcut_review, capcut_service
from app.services.storage import mime_for

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


@router.get("/jobs/{job_id}/review", response_model=ReviewOut)
def get_review(ctx: Ctx, job_id: IdPath) -> dict[str, Any]:
    """Frases da transcrição (agrupadas por `seg`), com as palavras duvidosas marcadas, para a revisão das legendas."""
    return capcut_review.review_view(ctx, job_id)


@router.get("/jobs/{job_id}/source", response_class=FileResponse)
def job_source(ctx: Ctx, job_id: IdPath) -> FileResponse:
    """O arquivo exato que foi transcrito (original da mídia ou rendition da edição), para o player da revisão."""
    path = capcut_review.source_path(ctx, capcut_review._job(ctx, job_id))
    return FileResponse(path, media_type=mime_for(path))


@router.post("/jobs/{job_id}/review", response_model=JobOut)
def approve_review(ctx: Ctx, job_id: IdPath, body: ReviewApprove) -> dict[str, Any]:
    """Aprova a revisão: só as frases alteradas (texto vazio remove a frase), realinhadas no áudio; o job volta para
    a fila. 409 `capcut_job_not_in_review` fora da revisão. Síncrona (threadpool): decodifica e alinha fora do loop."""
    return capcut_review.approve(ctx, job_id, [edit.model_dump() for edit in body.segments])


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


# ---- lotes (docs/CAPCUT.md, "Lotes") --------------------------------------------------------------------------------
@router.get("/library", response_model=LibraryPage)
def library(
    ctx: Ctx,
    q: Annotated[SafeText | None, Query(max_length=200)] = None,
    profile: Annotated[SafeText | None, Query(max_length=64)] = None,
    folder: Annotated[str | None, Query(pattern=FOLDER_KEY_PATTERN)] = None,
    captioned: Literal["any", "yes", "no"] = "any",
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
    before_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
) -> dict[str, Any]:
    """Seletor: vídeos originais (sem capas) visíveis na visão da lista ("Limpar lista"), id DESC, keyset; cada item
    com `capcut: {done, last_output_name, open}`."""
    return capcut_batch_service.library(ctx, q=q, profile=profile, folder=folder, captioned=captioned, limit=limit,
                                        before_id=before_id)


@router.get("/library/view", response_model=LibraryViewOut)
def library_view(ctx: Ctx) -> dict[str, Any]:
    """Visão da lista do seletor: `cleared` (houve "Limpar lista") e quantos vídeos saíram um a um."""
    return capcut_batch_service.get_library_view(ctx)


@router.post("/library/clear", response_model=LibraryViewOut)
def clear_library(ctx: Ctx, body: LibraryClear | None = None) -> dict[str, Any]:
    """Sem `media_ids`: os vídeos atuais saem da lista (os novos continuam aparecendo). Com ids: só esses saem.
    Nada é apagado; vale para a lista e para "todos os filtrados" (não para `media_ids` explícitos de um lote)."""
    return capcut_batch_service.clear_library_view(ctx, body.media_ids if body is not None else None)


@router.post("/library/show", response_model=LibraryViewOut)
def show_in_library(ctx: Ctx, body: LibraryShow) -> dict[str, Any]:
    """Os ids voltam à lista (ex.: envio do computador que é duplicata de um vídeo que saiu da lista)."""
    return capcut_batch_service.show_in_library_view(ctx, body.media_ids)


@router.post("/library/restore", response_model=LibraryViewOut)
def restore_library(ctx: Ctx) -> dict[str, Any]:
    """Todos os vídeos voltam à lista."""
    return capcut_batch_service.restore_library_view(ctx)


@router.get("/estimate", response_model=EstimateOut)
def estimate(ctx: Ctx, mode: CapCutMode, count: Annotated[int, Query(ge=0, le=100_000)] = 1) -> dict[str, Any]:
    """Média real por vídeo dos últimos 20 concluídos do modo; sem histórico, nulos."""
    return capcut_batch_service.estimate(ctx, mode, count)


@router.post("/batches", response_model=BatchCreated, status_code=202)
def create_batch(ctx: Ctx, body: BatchCreate) -> dict[str, Any]:
    """Um job por vídeo (por `media_ids` ou por `filter`, até 1000); pula os que já estão na fila e os já concluídos
    no mesmo modo (sem `include_done`)."""
    return capcut_batch_service.create_batch(ctx, body)


@router.get("/batches", response_model=BatchList)
def list_batches(
    ctx: Ctx,
    status: BatchStatus | None = None,
    mode: CapCutMode | None = None,
    q: Annotated[SafeText | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0, le=2**31)] = 0,
) -> dict[str, Any]:
    """Histórico (sem os removidos), mais recentes primeiro."""
    return capcut_batch_service.list_batches(ctx, status=status, mode=mode, q=q, limit=limit, offset=offset)


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(ctx: Ctx, batch_id: IdPath) -> dict[str, Any]:
    return capcut_batch_service.get_batch(ctx, batch_id)


@router.get("/batches/{batch_id}/jobs", response_model=JobList)
def list_batch_jobs(
    ctx: Ctx,
    batch_id: IdPath,
    status: JobStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0, le=2**31)] = 0,
) -> dict[str, Any]:
    """Na ordem da fila; `counts` = contagens do lote."""
    return capcut_batch_service.list_batch_jobs(ctx, batch_id, status=status, limit=limit, offset=offset)


@router.post("/batches/{batch_id}/pause", response_model=BatchOut)
def pause_batch(ctx: Ctx, batch_id: IdPath) -> dict[str, Any]:
    return capcut_batch_service.set_paused(ctx, batch_id, True)


@router.post("/batches/{batch_id}/resume", response_model=BatchOut)
def resume_batch(ctx: Ctx, batch_id: IdPath) -> dict[str, Any]:
    return capcut_batch_service.set_paused(ctx, batch_id, False)


@router.post("/batches/{batch_id}/cancel", response_model=BatchOut)
def cancel_batch(ctx: Ctx, batch_id: IdPath) -> dict[str, Any]:
    return capcut_batch_service.cancel_batch(ctx, batch_id)


@router.post("/batches/{batch_id}/retry-failed", response_model=BatchOut)
def retry_failed_batch(ctx: Ctx, batch_id: IdPath) -> dict[str, Any]:
    return capcut_batch_service.retry_failed(ctx, batch_id)


@router.post("/batches/{batch_id}/reveal")
def reveal_batch(ctx: Ctx, batch_id: IdPath) -> dict[str, bool]:
    """Abre a pasta do lote no Explorer."""
    capcut_batch_service.reveal_batch(ctx, batch_id)
    return {"ok": True}


@router.get("/batches/{batch_id}/zip", response_class=FileResponse)
def batch_zip(ctx: Ctx, batch_id: IdPath) -> FileResponse:
    """ZIP (sem compressão) com os vídeos concluídos que ainda existem; o temporário é apagado depois do envio."""
    path = capcut_batch_service.build_zip(ctx, batch_id)
    return FileResponse(path, media_type="application/zip", filename=f"lote-{batch_id}.zip",
                        background=BackgroundTask(path.unlink, missing_ok=True))


@router.post("/batches/hide", response_model=BatchesHidden)
def hide_batches(ctx: Ctx, body: BatchesHide) -> dict[str, Any]:
    """Remove do histórico vários lotes (`batch_ids`) ou todos os terminados (`all`); os arquivos ficam. Lotes com vídeo
    aberto ficam e voltam em `active`."""
    return capcut_batch_service.hide_batches(ctx, body.batch_ids, body.all)


@router.delete("/batches/{batch_id}", status_code=204)
def delete_batch(ctx: Ctx, batch_id: IdPath) -> Response:
    """Remove do histórico (os arquivos ficam); 409 `capcut_batch_active` com vídeo na fila ou em andamento."""
    capcut_batch_service.hide_batch(ctx, batch_id)
    return Response(status_code=204)


@router.post("/jobs/{job_id}/hold", response_model=JobOut)
def hold_job(ctx: Ctx, job_id: IdPath) -> dict[str, Any]:
    """Pausa individual (só na fila); 409 `capcut_job_not_holdable`."""
    return capcut_batch_service.hold_job(ctx, job_id, True)


@router.post("/jobs/{job_id}/release", response_model=JobOut)
def release_job(ctx: Ctx, job_id: IdPath) -> dict[str, Any]:
    return capcut_batch_service.hold_job(ctx, job_id, False)
