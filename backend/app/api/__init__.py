from fastapi import APIRouter

from app.api import (
    capcut,
    edits,
    exports,
    extension,
    imports,
    jobs,
    media,
    post_times,
    presets,
    profile_analyses,
    settings,
    system,
    top_videos,
    uploads,
)

api_router = APIRouter(prefix="/api")
for module in (system, presets, imports, media, exports, jobs, settings, uploads, edits, extension,
               profile_analyses, post_times, capcut, top_videos):
    api_router.include_router(module.router)

__all__ = ["api_router"]
