from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import PositiveId, StrictModel


class EditSettings(StrictModel):
    """Configurações da edição (seção 8, POST /edits). `custom_seconds` só é usado (e exigido) em `custom`."""

    duration_mode: Literal["original", "15", "30", "60", "custom"]
    custom_seconds: Annotated[float, Field(strict=True, gt=0, le=7200, allow_inf_nan=False)] | None = None
    speed: Annotated[float, Field(strict=True, ge=1.0, le=1.5, allow_inf_nan=False)]
    sharpen: Annotated[bool, Field(strict=True)]
    output_type: Literal["mp4", "auto"]

    @model_validator(mode="after")
    def _custom_requires_seconds(self) -> EditSettings:
        if self.duration_mode == "custom" and self.custom_seconds is None:
            raise ValueError("Informe a duração personalizada em segundos.")
        return self


class EditRequest(StrictModel):
    media_ids: Annotated[list[PositiveId], Field(min_length=1, max_length=500)]
    settings: EditSettings


class EditFolderOut(BaseModel):
    """Pasta da página Editar (GET /edit/folders): `key` vai em GET /edit/folders/{key}/media e POST /media/delete."""

    key: str
    kind: Literal["group", "batch", "local", "other"]
    import_mode: Literal["profile", "links"] | None
    profiles: list[str]
    created_at: str | None
    day: str | None
    video_count: int
    rendition_count: int
    total_bytes: int
    shared_count: int  # vídeos da pasta que também estão em outra pasta


class EditFolderList(BaseModel):
    items: list[EditFolderOut]
