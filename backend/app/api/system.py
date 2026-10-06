from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import __version__
from app.api.deps import Ctx
from app.db.connection import utc_now
from app.db.migrate import schema_version
from app.schemas.instagram import HealthOut
from app.services.diagnostics import build_diagnostics
from app.services.settings_service import effective_concurrency

router = APIRouter(tags=["sistema"])


@router.get("/health", response_model=HealthOut)
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "time": utc_now()}


@router.get("/diagnostics")
def diagnostics(ctx: Ctx) -> dict[str, Any]:
    worker = ctx.worker
    storage = ctx.storage
    return build_diagnostics(
        settings_like=ctx.settings,
        db_path=ctx.settings.database_path,
        schema_version=schema_version(ctx.db),
        ffmpeg=ctx.processors.ffmpeg_info(),
        ffprobe=ctx.processors.ffprobe_info(),
        worker={
            "running": bool(worker and worker.running),
            "concurrency": worker.concurrency if worker else effective_concurrency(ctx),
            "active_jobs": worker.active_count if worker else 0,
        },
        session_configured=bool(ctx.session_provider.status().configured),
        storage_paths={
            "root": storage.root,
            "originals": storage.originals,
            "processed": storage.processed,
            "temp": storage.temp,
            "thumbs": storage.thumbs,
        },
        host=ctx.settings.host,
        port=ctx.settings.port,
        version=__version__,
    )
