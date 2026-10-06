from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import Ctx
from app.schemas.settings import (
    ExecutionDiagnostics,
    InstagramStatus,
    SessionCookiesUpdate,
    SessionUpdate,
    SettingsOut,
    SettingsUpdate,
)
from app.services import settings_service

router = APIRouter(prefix="/settings", tags=["configurações"])


@router.get("", response_model=SettingsOut)
def get_settings(ctx: Ctx) -> dict[str, Any]:
    return settings_service.get_settings(ctx)


@router.get("/diagnostics", response_model=ExecutionDiagnostics)
def execution_diagnostics(ctx: Ctx) -> dict[str, Any]:
    return settings_service.execution_diagnostics(ctx)


@router.put("", response_model=SettingsOut)
def update_settings(ctx: Ctx, body: SettingsUpdate) -> dict[str, Any]:
    settings_service.update_settings(ctx, body.model_dump(include=body.model_fields_set))
    return settings_service.get_settings(ctx)


@router.put("/instagram/session", response_model=InstagramStatus)
def save_session(ctx: Ctx, body: SessionUpdate) -> dict[str, Any]:
    extra = {key: getattr(body, key) for key in ("csrftoken", "ds_user_id", "mid", "ig_did")}
    settings_service.save_session(ctx, body.sessionid, extra)
    return settings_service.instagram_status(ctx)


@router.patch("/instagram/session/cookies", response_model=InstagramStatus)
def update_session_cookies(ctx: Ctx, body: SessionCookiesUpdate) -> dict[str, Any]:
    changes = {key: getattr(body, key) for key in body.model_fields_set}
    settings_service.update_session_cookies(ctx, changes)
    return settings_service.instagram_status(ctx)


@router.delete("/instagram/pause", response_model=InstagramStatus)
def clear_instagram_pause(ctx: Ctx) -> dict[str, Any]:
    settings_service.clear_instagram_pause(ctx)
    return settings_service.instagram_status(ctx)


@router.delete("/instagram/session", response_model=InstagramStatus)
def clear_session(ctx: Ctx) -> dict[str, Any]:
    settings_service.clear_session(ctx)
    return settings_service.instagram_status(ctx)


@router.post("/instagram/session/test", response_model=InstagramStatus)
def test_session(ctx: Ctx) -> dict[str, Any]:
    settings_service.test_session(ctx)
    return settings_service.instagram_status(ctx)
