from __future__ import annotations

import unicodedata
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorOut(BaseModel):
    detail: str
    code: str


def _no_control_chars(value: str) -> str:
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("não pode conter caracteres de controle")
    return value


def _strip(value: str) -> str:
    return value.strip()


PositiveId = Annotated[int, Field(strict=True, ge=1, le=2**53)]
PresetName = Annotated[str, AfterValidator(_strip), Field(min_length=1, max_length=60), AfterValidator(_no_control_chars)]
SafeText = Annotated[str, AfterValidator(_no_control_chars)]
