from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import MAX_ID, Ctx, IdPath
from app.core.errors import AppError, not_found
from app.db.repositories.common import JOB_STATUSES
from app.schemas.jobs import JobList, JobOut
from app.services import import_service
from app.services.presenters import job_single, jobs_out

router = APIRouter(prefix="/jobs", tags=["fila"])

MAX_IDS = 500


def _parse_statuses(raw: str | None) -> list[str] | None:
    if raw is None or not raw.strip():
        return None
    values = [part.strip() for part in raw.split(",") if part.strip()]
    invalid = [value for value in values if value not in JOB_STATUSES]
    if invalid:
        raise AppError(422, "validation_error", f"Status inválido: {', '.join(invalid[:5])}.")
    return list(dict.fromkeys(values))


def _parse_ids(raw: str | None) -> list[int] | None:
    if raw is None or not raw.strip():
        return None
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not (1 <= int(part) <= MAX_ID):
            raise AppError(422, "validation_error", "ids deve ser uma lista de inteiros positivos separados por vírgula.")
        ids.append(int(part))
    if len(ids) > MAX_IDS:
        raise AppError(422, "validation_error", f"No máximo {MAX_IDS} ids por consulta.")
    return list(dict.fromkeys(ids))


@router.get("", response_model=JobList)
def list_jobs(
    ctx: Ctx,
    status: Annotated[str | None, Query(max_length=200)] = None,
    batch_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    ids: Annotated[str | None, Query(max_length=8000)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
    before_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
) -> dict[str, Any]:
    """Ordem id DESC. Paginação keyset: `before_id` + `next_before_id` (null no fim); `offset` por compatibilidade."""
    items, total, counts, next_before_id = ctx.jobs.list(
        statuses=_parse_statuses(status), batch_id=batch_id, ids=_parse_ids(ids), limit=limit, offset=offset,
        before_id=before_id,
    )
    return {"items": jobs_out(ctx, items), "total": total, "counts": counts, "next_before_id": next_before_id}


@router.get("/{job_id}", response_model=JobOut)
def get_job(ctx: Ctx, job_id: IdPath) -> dict[str, Any]:
    job = job_single(ctx, job_id)
    if job is None:
        raise not_found("Job", "job_not_found")
    return job


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_job(ctx: Ctx, job_id: IdPath) -> dict[str, Any]:
    import_service.cancel_job(ctx, job_id)
    return job_single(ctx, job_id)


@router.post("/{job_id}/retry", response_model=JobOut)
def retry_job(ctx: Ctx, job_id: IdPath) -> dict[str, Any]:
    import_service.retry_job(ctx, job_id)
    return job_single(ctx, job_id)
