from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query

from app.api.deps import Ctx, IdPath
from app.core.errors import AppError, not_found
from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.validation import parse_profile_input
from app.jobs.errors import safe_message
from app.schemas.batches import BatchList, BatchOut, ImportRequest, RestartListingRequest
from app.schemas.instagram import ProfileInfoOut, ProfileRequest
from app.services import import_service
from app.services.deadline import CallInProgress, DeadlineExceeded, run_with_deadline
from app.services.instagram_gate import InstagramPaused
from app.services.instagram_pause import ensure_not_paused, raise_paused
from app.services.presenters import batch_single, batches_out

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
    )
    return batch_single(ctx, batch_id)


@router.get("/batches", response_model=BatchList)
def list_batches(
    ctx: Ctx,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
    source: Literal["instagram_import", "library"] | None = None,
) -> dict[str, Any]:
    items, total = ctx.batches.list(limit=limit, offset=offset, source=source)
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
