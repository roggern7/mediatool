from fastapi import APIRouter

from app.api import imports, jobs, media, presets, settings, system

api_router = APIRouter(prefix="/api")
for module in (system, presets, imports, media, jobs, settings):
    api_router.include_router(module.router)

__all__ = ["api_router"]
