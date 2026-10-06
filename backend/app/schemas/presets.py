from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from app.schemas.common import PresetName, StrictModel


class PresetParams(StrictModel):
    metadata_cleanup: Annotated[bool, Field(strict=True)]
    trim_start: Annotated[float, Field(strict=True, ge=0, le=30, allow_inf_nan=False)]
    mirror: Annotated[bool, Field(strict=True)]
    speed: Annotated[float, Field(strict=True, ge=0.5, le=2, allow_inf_nan=False)]
    auto_adjust: Annotated[bool, Field(strict=True)]
    reencode: Annotated[bool, Field(strict=True)]


class PresetCreate(StrictModel):
    name: PresetName
    params: PresetParams


class PresetUpdate(StrictModel):
    name: PresetName | None = None
    params: PresetParams | None = None


class PresetDuplicate(StrictModel):
    name: PresetName | None = None


class PresetOut(BaseModel):
    id: int
    name: str
    is_builtin: bool
    params: PresetParams
    created_at: str
    updated_at: str
