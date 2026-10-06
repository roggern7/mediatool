from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

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
    original_filename: str | None
    original_sha256: str
    file_size: int
    duration: float | None
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool | None
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
    q: Annotated[SafeText | None, Field(max_length=200)] = None
    profile: Annotated[SafeText | None, Field(max_length=64)] = None
    preset_id: PositiveId


class OpenRequest(StrictModel):
    target: Literal["file", "folder"]
    rendition_id: PositiveId | None = None


class OpenResult(BaseModel):
    ok: bool
