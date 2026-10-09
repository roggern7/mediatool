from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer

from app.schemas.common import PresetName, StrictModel

# Ajustes opcionais e seu valor neutro (desligado). Chave ausente = neutro.
OPTIONAL_NEUTRALS: dict[str, float] = {
    "zoom": 0.0, "brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "trim_end": 0.0, "pitch_shift": 0.0,
    "grain": 0.0,
}


class PresetParams(StrictModel):
    metadata_cleanup: Annotated[bool, Field(strict=True)]
    trim_start: Annotated[float, Field(strict=True, ge=0, le=30, allow_inf_nan=False)]
    mirror: Annotated[bool, Field(strict=True)]
    speed: Annotated[float, Field(strict=True, ge=0.5, le=2, allow_inf_nan=False)]
    auto_adjust: Annotated[bool, Field(strict=True)]
    reencode: Annotated[bool, Field(strict=True)]
    zoom: Annotated[float, Field(strict=True, ge=0, le=10, allow_inf_nan=False)] = 0.0
    brightness: Annotated[float, Field(strict=True, ge=-0.1, le=0.1, allow_inf_nan=False)] = 0.0
    contrast: Annotated[float, Field(strict=True, ge=0.9, le=1.1, allow_inf_nan=False)] = 1.0
    saturation: Annotated[float, Field(strict=True, ge=0.8, le=1.2, allow_inf_nan=False)] = 1.0
    trim_end: Annotated[float, Field(strict=True, ge=0, le=30, allow_inf_nan=False)] = 0.0
    pitch_shift: Annotated[float, Field(strict=True, ge=-3, le=3, allow_inf_nan=False)] = 0.0
    grain: Annotated[float, Field(strict=True, ge=0, le=4, allow_inf_nan=False)] = 0.0

    @model_serializer(mode="wrap")
    def _omit_neutral(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Ajuste opcional neutro nunca é gravado nem devolvido: os params ficam iguais aos de antes de o ajuste
        existir, e a dedupe de renditions por `canonical_json(params)` continua casando (seção 9)."""
        return {k: v for k, v in handler(self).items() if OPTIONAL_NEUTRALS.get(k) != v}


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
