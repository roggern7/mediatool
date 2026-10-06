from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from app.api.deps import Ctx, IdPath
from app.core.errors import AppError, not_found
from app.db.repositories.presets import DuplicateNameError
from app.schemas.presets import PresetCreate, PresetDuplicate, PresetOut, PresetUpdate

router = APIRouter(prefix="/presets", tags=["presets"])

NAME_MAX = 60


def _duplicate_name_error(name: str) -> AppError:
    return AppError(409, "preset_name_taken", f"Já existe um preset chamado \"{name}\".")


def _get_or_404(ctx: Ctx, preset_id: int) -> dict[str, Any]:
    preset = ctx.presets.get(preset_id)
    if preset is None:
        raise not_found("Preset", "preset_not_found")
    return preset


def _copy_name(ctx: Ctx, base: str) -> str:
    suffix = " (cópia)"
    candidate = f"{base[: NAME_MAX - len(suffix)]}{suffix}"
    counter = 2
    while ctx.presets.name_exists(candidate):
        tail = f" (cópia {counter})"
        candidate = f"{base[: NAME_MAX - len(tail)]}{tail}"
        counter += 1
    return candidate


@router.get("", response_model=list[PresetOut])
def list_presets(ctx: Ctx) -> list[dict[str, Any]]:
    return ctx.presets.list()


@router.post("", response_model=PresetOut, status_code=201)
def create_preset(ctx: Ctx, body: PresetCreate) -> dict[str, Any]:
    if ctx.presets.name_exists(body.name):
        raise _duplicate_name_error(body.name)
    try:
        return ctx.presets.create(body.name, body.params.model_dump())
    except DuplicateNameError:
        raise _duplicate_name_error(body.name) from None


@router.get("/{preset_id}", response_model=PresetOut)
def get_preset(ctx: Ctx, preset_id: IdPath) -> dict[str, Any]:
    return _get_or_404(ctx, preset_id)


@router.put("/{preset_id}", response_model=PresetOut)
def update_preset(ctx: Ctx, preset_id: IdPath, body: PresetUpdate) -> dict[str, Any]:
    preset = _get_or_404(ctx, preset_id)
    if preset["is_builtin"] and body.name is not None and body.name != preset["name"]:
        raise AppError(409, "preset_builtin_name", "Presets padrão não podem ser renomeados; os parâmetros são editáveis.")
    if body.name is not None and ctx.presets.name_exists(body.name, exclude_id=preset_id):
        raise _duplicate_name_error(body.name)
    try:
        return ctx.presets.update(
            preset_id, name=body.name, params=None if body.params is None else body.params.model_dump()
        )
    except DuplicateNameError:
        raise _duplicate_name_error(body.name or "") from None


@router.delete("/{preset_id}", status_code=204)
def delete_preset(ctx: Ctx, preset_id: IdPath) -> Response:
    preset = _get_or_404(ctx, preset_id)
    if preset["is_builtin"]:
        raise AppError(409, "preset_builtin", "Presets padrão não podem ser excluídos.")
    with ctx.db.transaction():
        if ctx.app_settings.get("default_preset_id") == preset_id:
            ctx.app_settings.delete("default_preset_id")
        ctx.presets.delete(preset_id)
    return Response(status_code=204)


@router.post("/{preset_id}/duplicate", response_model=PresetOut, status_code=201)
def duplicate_preset(ctx: Ctx, preset_id: IdPath, body: PresetDuplicate | None = None) -> dict[str, Any]:
    preset = _get_or_404(ctx, preset_id)
    name = body.name if body is not None and body.name is not None else _copy_name(ctx, preset["name"])
    if ctx.presets.name_exists(name):
        raise _duplicate_name_error(name)
    try:
        return ctx.presets.create(name, preset["params"])
    except DuplicateNameError:
        raise _duplicate_name_error(name) from None
