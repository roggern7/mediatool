from fastapi import APIRouter

from app.api import edits, exports, imports, jobs, media, presets, settings, system, uploads

api_router = APIRouter(prefix="/api")
for module in (system, presets, imports, media, exports, jobs, settings, uploads, edits):
    api_router.include_router(module.router)

__all__ = ["api_router"]
