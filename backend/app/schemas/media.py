from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.db.repositories.edit_folders import FOLDER_KEY_PATTERN
from app.schemas.common import PositiveId, SafeText, StrictModel
from app.schemas.jobs import JobOut


class RenditionOut(BaseModel):
    id: int
    media_id: int
    preset_id: int | None
    preset_name: str | None
    job_id: int | None
    processed_sha256: str
    file_size: int
    duration: float | None
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool | None
    created_at: str
    file_url: str


class MediaOut(BaseModel):
    id: int
    source_platform: str
    source_profile: str | None
    source_url: str | None
    source_post_id: str | None
    source_media_index: int
    media_type: Literal["video", "image"]
    # "cover" = capa de um vídeo/Reel baixada sozinha (migration 0021); "original" = a mídia da publicação.
    role: Literal["original", "cover"] = "original"
    original_filename: str | None
    original_sha256: str
    file_size: int
    duration: float | None
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool | None
    video_codec: str | None
    mime_type: str | None
    taken_at: str | None
    created_at: str
    thumbnail_url: str | None
    original_url: str
    renditions: list[RenditionOut]
    last_job: JobOut | None


class MediaList(BaseModel):
    items: list[MediaOut]
    total: int
    next_before_id: int | None = None


class ProcessRequest(StrictModel):
    preset_id: PositiveId


class BulkProcessRequest(StrictModel):
    media_ids: Annotated[list[PositiveId], Field(min_length=1, max_length=500)]
    preset_id: PositiveId


class ProcessByFilterRequest(StrictModel):
    view: Literal["all", "originals", "processed"] = "all"
    media_type: Literal["video", "image"] | None = None
    role: Literal["original", "cover"] | None = None
    q: Annotated[SafeText | None, Field(max_length=200)] = None
    profile: Annotated[SafeText | None, Field(max_length=64)] = None
    preset_id: PositiveId


class MediaDeleteRequest(StrictModel):
    """Apagar várias mídias (POST /media/delete): `media_ids` OU `folder` (vídeos da pasta da página Editar,
    resolvidos no servidor). `delete_original: true` é a confirmação obrigatória."""

    media_ids: Annotated[list[PositiveId], Field(min_length=1, max_length=500)] | None = None
    folder: Annotated[str, Field(pattern=FOLDER_KEY_PATTERN)] | None = None
    # Estrito: só o booleano JSON `true` confirma ("true", 1 ou "yes" -> 422).
    delete_original: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def _one_source(self) -> MediaDeleteRequest:
        if (self.media_ids is None) == (self.folder is None):
            raise ValueError("Informe media_ids ou folder (um dos dois).")
        return self


class MediaDeleteIssue(BaseModel):
    id: int
    code: str
    message: str


class MediaDeleteResult(BaseModel):
    deleted: int
    deleted_ids: list[int]
    skipped: list[MediaDeleteIssue]
    failed: list[MediaDeleteIssue]


class OpenRequest(StrictModel):
    target: Literal["file", "folder"]
    rendition_id: PositiveId | None = None


class OpenResult(BaseModel):
    ok: bool
