from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query

from app.api.deps import MAX_ID, Ctx, IdPath
from app.core.errors import AppError, not_found
from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.validation import parse_profile_input
from app.jobs.errors import safe_message
from app.schemas.batches import (
    BatchLinkList,
    BatchList,
    BatchOut,
    ClassifyOut,
    ClassifyRequest,
    HistoryHideRequest,
    HistoryHideResult,
    ImportGroupDetail,
    ImportGroupList,
    ImportGroupRequest,
    ImportLinksOut,
    ImportLinksRequest,
    ImportRequest,
    LinksPreview,
    LinksPreviewRequest,
    ProfilesPreview,
    ProfilesPreviewRequest,
    RestartListingRequest,
    UnifiedImportOut,
    UnifiedImportRequest,
)
from app.schemas.instagram import ProfileInfoOut, ProfileRequest
from app.services import import_history, import_service
from app.services.deadline import CallInProgress, DeadlineExceeded, run_with_deadline
from app.services.instagram_gate import InstagramPaused
from app.services.instagram_pause import ensure_not_paused, raise_paused
from app.services.presenters import batch_links_out, batch_single, batches_out, group_detail, groups_out

logger = logging.getLogger("media_tool.imports")

router = APIRouter(tags=["importação"])

_PROFILE_ERROR_STATUS = {
    "invalid_input": 422,
    "session_missing": 409,
    "session_invalid": 409,
    "session_expired": 409,
    "checkpoint_required": 409,
    "profile_not_found": 404,
    "profile_private": 403,
    "content_unavailable": 404,
    "rate_limited": 429,
    "action_blocked": 403,
    "access_refused": 403,
    "network_error": 502,
    "timeout": 504,
}


@router.post("/instagram/profile", response_model=ProfileInfoOut)
def instagram_profile(ctx: Ctx, body: ProfileRequest) -> dict[str, Any]:
    try:
        username = parse_profile_input(body.profile)
    except ValueError as exc:
        raise AppError(422, "invalid_input", f"Perfil inválido: {exc}") from exc
    ensure_not_paused(ctx)
    timeout = ctx.settings.profile_preview_timeout
    try:
        info = run_with_deadline(
            lambda: ctx.downloader_factory().get_profile(username), timeout, name="instagram-profile-preview",
            lock=ctx.profile_preview_lock,
        )
    except CallInProgress:
        raise AppError(
            409, "profile_preview_in_progress", "Já existe uma consulta de perfil em andamento."
        ) from None
    except InstagramPaused:
        raise_paused(ctx)
    except DeadlineExceeded:
        logger.warning("Prévia do perfil excedeu o prazo de %ss", int(timeout))
        raise AppError(
            504, "timeout", f"O Instagram não respondeu em {int(timeout)} s. Tente de novo em alguns minutos."
        ) from None
    except InstagramError as exc:
        if ctx.worker is not None:
            ctx.worker.pause_for(exc.code)
        raise AppError(_PROFILE_ERROR_STATUS.get(exc.code, 502), exc.code, safe_message(exc.message)) from None
    return asdict(info)


@router.post("/imports", response_model=BatchOut, status_code=202)
def create_import(ctx: Ctx, body: ImportRequest) -> dict[str, Any]:
    batch_id = import_service.create_import(
        ctx,
        profile=body.profile,
        content_type=body.content_type,
        limit=body.limit,
        preset_id=body.preset_id,
        process=body.process,
        edit=body.edit,
        min_views=body.min_views,
        posted_within_days=body.posted_within_days,
    )
    return batch_single(ctx, batch_id)


@router.post("/imports/classify", response_model=ClassifyOut)
def classify_imports(body: ClassifyRequest) -> dict[str, Any]:
    """Importação unificada: classifica as entradas em perfis, publicações e inválidas, sem efeitos e sem nenhuma
    requisição ao Instagram."""
    return import_service.classify_inputs(list(body.inputs) if body.inputs is not None else [body.text or ""])


@router.post("/imports/start", response_model=UnifiedImportOut, status_code=202)
def start_unified_import(ctx: Ctx, body: UnifiedImportRequest) -> dict[str, Any]:
    """Importação unificada (publicações e/ou perfis numa entrada só): 1 perfil -> lote avulso; 2+ perfis -> grupo;
    publicações -> um lote de links. Progresso em GET /api/batches/{id} e GET /api/import-groups/{id}."""
    return import_service.start_unified(ctx, body)


@router.post("/import-groups/preview", response_model=ProfilesPreview)
def preview_import_group(body: ProfilesPreviewRequest) -> dict[str, Any]:
    """Pré-validação da lista de perfis, sem efeitos e sem nenhuma requisição ao Instagram."""
    entries = list(body.entries or [])
    if body.text:
        entries.append(body.text)
    valid, invalid, duplicates = import_service.parse_profile_entries(entries)
    return {"valid": valid, "invalid": invalid, "duplicates": duplicates}


@router.post("/import-groups", response_model=ImportGroupDetail, status_code=202)
def create_import_group(
    ctx: Ctx, body: ImportGroupRequest, children_limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    group_id, skipped, duplicates = import_service.create_import_group(
        ctx,
        profiles=body.profiles,
        content_type=body.content_type,
        limit=body.limit,
        preset_id=body.preset_id,
        process=body.process,
        edit=body.edit,
        min_views=body.min_views,
        posted_within_days=body.posted_within_days,
    )
    detail = group_detail(ctx, ctx.batches.get_group(group_id), children_limit=children_limit)
    return {**detail, "skipped": skipped, "duplicates": duplicates}


@router.get("/import-groups", response_model=ImportGroupList)
def list_import_groups(
    ctx: Ctx,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    history: bool = False,
) -> dict[str, Any]:
    """Ordem id DESC. Paginação keyset: `before_id` + `next_before_id` (null no fim). `history=true`: sem os grupos
    removidos do histórico."""
    groups, total, next_before_id = ctx.batches.list_groups(limit=limit, before_id=before_id, history=history)
    return {"items": groups_out(ctx, groups), "total": total, "next_before_id": next_before_id}


@router.get("/import-groups/{group_id}", response_model=ImportGroupDetail)
def get_import_group(
    ctx: Ctx,
    group_id: IdPath,
    children_limit: Annotated[int, Query(ge=1, le=500)] = 100,
    after_position: Annotated[int | None, Query(ge=0, le=MAX_ID)] = None,
) -> dict[str, Any]:
    """Grupo + filhos em ordem de `group_position` (keyset `after_position` + `next_after_position`)."""
    group = ctx.batches.get_group(group_id)
    if group is None:
        raise not_found("Grupo de importação", "group_not_found")
    return group_detail(ctx, group, children_limit=children_limit, after_position=after_position)


@router.post("/import-groups/{group_id}/cancel", response_model=ImportGroupDetail)
def cancel_import_group(
    ctx: Ctx, group_id: IdPath, children_limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Cancela os filhos ainda abertos; os encerrados ficam. Um filho sozinho: POST /api/batches/{id}/cancel."""
    import_service.cancel_group(ctx, group_id)
    return group_detail(ctx, ctx.batches.get_group(group_id), children_limit=children_limit)


@router.post("/import-links/preview", response_model=LinksPreview)
def preview_import_links(body: LinksPreviewRequest) -> dict[str, Any]:
    """Pré-validação da lista de links, sem efeitos e sem nenhuma requisição ao Instagram."""
    entries = list(body.entries or [])
    if body.text:
        entries.append(body.text)
    valid, invalid, duplicates = import_service.parse_link_entries(entries)
    return {"valid": valid, "invalid": invalid, "duplicates": duplicates}


FILTERS_PROFILE_ONLY_MESSAGE = "O filtro de período/visualizações vale só para importação por perfil."


@router.post("/import-links", response_model=ImportLinksOut, status_code=202)
def create_import_links(ctx: Ctx, body: ImportLinksRequest) -> dict[str, Any]:
    """Um lote com todos os links (um job por mídia de cada publicação); o import runner resolve um link por vez."""
    if body.min_views is not None or body.posted_within_days is not None:
        raise AppError(422, "filters_profile_only", FILTERS_PROFILE_ONLY_MESSAGE)
    batch_id, duplicates = import_service.create_import_links(
        ctx, links=body.links, preset_id=body.preset_id, process=body.process, edit=body.edit)
    return {**batch_single(ctx, batch_id), "duplicates": duplicates}


@router.post("/imports/history/hide", response_model=HistoryHideResult)
def hide_import_history(ctx: Ctx, body: HistoryHideRequest) -> dict[str, Any]:
    """Remove do histórico (oculta) lotes avulsos/grupos terminados; os vídeos ficam, salvo `delete_media: true`."""
    return import_history.hide(ctx, batch_ids=body.batch_ids or [], group_ids=body.group_ids or [],
                               all_finished=body.all_finished, delete_media=body.delete_media, dry_run=body.dry_run)


@router.get("/batches/{batch_id}/links", response_model=BatchLinkList)
def list_batch_links(
    ctx: Ctx,
    batch_id: IdPath,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
) -> dict[str, Any]:
    """Links de um lote por link, na ordem colada. Lote que não é por link: lista vazia."""
    batch = ctx.batches.get(batch_id)
    if batch is None:
        raise not_found("Lote", "batch_not_found")
    links, total = ctx.batches.list_links(batch_id, limit=limit, offset=offset)
    return {"items": batch_links_out(batch, links), "total": total}


@router.get("/batches", response_model=BatchList)
def list_batches(
    ctx: Ctx,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
    source: Literal["instagram_import", "library"] | None = None,
    group_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    ungrouped: bool = False,
    history: bool = False,
) -> dict[str, Any]:
    """Por padrão lista todos os lotes, inclusive filhos de grupo (compatibilidade). `ungrouped=true` deixa só os
    avulsos (lotes antigos e importações de um perfil); `group_id=` só os filhos daquele grupo; `history=true` tira os removidos do histórico."""
    items, total = ctx.batches.list(limit=limit, offset=offset, source=source, group_id=group_id,
                                    ungrouped=ungrouped, history=history)
    return {"items": batches_out(ctx, items), "total": total}


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(ctx: Ctx, batch_id: IdPath, include_jobs: bool = False) -> dict[str, Any]:
    """Resumo do lote sem jobs; `include_jobs=true` embute no máximo 500 (mais recentes) e `jobs_truncated`."""
    batch = batch_single(ctx, batch_id, with_jobs=include_jobs)
    if batch is None:
        raise not_found("Lote", "batch_not_found")
    return batch


@router.post("/batches/{batch_id}/cancel", response_model=BatchOut)
def cancel_batch(ctx: Ctx, batch_id: IdPath) -> dict[str, Any]:
    import_service.cancel_batch(ctx, batch_id)
    return batch_single(ctx, batch_id)


@router.post("/batches/{batch_id}/retry-failed", response_model=BatchOut)
def retry_failed(ctx: Ctx, batch_id: IdPath) -> dict[str, Any]:
    import_service.retry_failed(ctx, batch_id)
    return batch_single(ctx, batch_id)


@router.post("/batches/{batch_id}/resume-listing", response_model=BatchOut)
def resume_listing(ctx: Ctx, batch_id: IdPath) -> dict[str, Any]:
    """Retoma a listagem `paused`/`needs_attention` do checkpoint salvo (seção 9)."""
    import_service.resume_listing(ctx, batch_id)
    return batch_single(ctx, batch_id)


@router.post("/batches/{batch_id}/restart-listing", response_model=BatchOut)
def restart_listing(ctx: Ctx, batch_id: IdPath, body: RestartListingRequest | None = None) -> dict[str, Any]:
    """Recomeça a listagem `paused`/`needs_attention` da página 1 (só com `confirm: true`; aviso de custo)."""
    import_service.restart_listing(ctx, batch_id, confirm=bool(body and body.confirm))
    return batch_single(ctx, batch_id)
