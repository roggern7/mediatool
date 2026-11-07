from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import PositiveId, PresetName, StrictModel

Resolution = Literal["original", "480P", "720P", "1080P", "2K", "4K"]
Bitrate = Literal["low", "recommended", "high"]
Fps = Literal["original", "24", "25", "30", "50", "60"]
WhisperModel = Literal["small", "medium", "large-v3-turbo"]
JobStatus = Literal["pending", "running", "waiting", "completed", "failed", "cancelled"]
Origin = Literal["manual", "auto", "api"]
Color = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
MaxWords = Annotated[int, Field(strict=True, ge=1, le=10)]
MaxChars = Annotated[int, Field(strict=True, ge=8, le=80)]
MaxAttempts = Annotated[int, Field(strict=True, ge=1, le=10)]
FolderText = Annotated[str, Field(max_length=1024)]
MAX_JOB_MEDIA = 500


class CapCutOptionsPatch(StrictModel):
    """Opções de legenda/exportação; só os campos enviados mudam (`output_dir` nulo ou vazio = pasta padrão)."""

    output_dir: FolderText | None = None
    resolution: Resolution | None = None
    bitrate: Bitrate | None = None
    fps: Fps | None = None
    highlight: bool | None = None
    highlight_color: Color | None = None
    max_words: MaxWords | None = None
    max_chars: MaxChars | None = None
    whisper_model: WhisperModel | None = None
    keep_original_audio: bool | None = None
    keep_draft: bool | None = None


class CapCutSettingsUpdate(CapCutOptionsPatch):
    auto_after_download: bool | None = None
    max_attempts: MaxAttempts | None = None
    default_style_id: Annotated[int, Field(strict=True, ge=0, le=2**53)] | None = None


class CapCutSettingsOut(BaseModel):
    output_dir: str
    output_dir_is_default: bool
    resolution: Resolution
    bitrate: Bitrate
    fps: Fps
    highlight: bool
    highlight_color: str | None
    max_words: int
    max_chars: int
    whisper_model: WhisperModel
    keep_original_audio: bool
    keep_draft: bool
    auto_after_download: bool
    max_attempts: int
    default_style_id: int | None


class QueueCounts(BaseModel):
    pending: int
    running: int
    waiting: int
    failed: int
    completed: int
    cancelled: int
    paused: bool


class CapCutStatusOut(BaseModel):
    available: bool                  # o núcleo importou e respondeu
    reason: str | None               # por que não está disponível
    installed: bool
    version: str | None
    drafts_dir: str | None
    running_pids: list[int]
    deps: dict[str, bool]
    skill: dict[str, Any]
    models: dict[str, bool]
    queue: QueueCounts
    settings: CapCutSettingsOut


class CaptionDraft(BaseModel):
    name: str
    modified_at: str | None = None
    text_segments: int = 0
    preview: str = ""


class StyleOut(BaseModel):
    id: int
    name: str
    builtin: bool
    is_default: bool
    source_draft: str | None
    summary: dict[str, Any] | None
    created_at: str | None


class StyleCreate(StrictModel):
    name: PresetName
    draft_name: Annotated[str, Field(min_length=1, max_length=255)]
    segment_index: Annotated[int, Field(strict=True, ge=0, le=10_000)] = 0


class JobsCreate(StrictModel):
    media_ids: Annotated[list[PositiveId], Field(min_length=1, max_length=MAX_JOB_MEDIA)]
    style_id: Annotated[int, Field(strict=True, ge=0, le=2**53)] | None = None
    overrides: CapCutOptionsPatch | None = None


class JobOut(BaseModel):
    id: int
    media_id: int | None
    media_filename: str | None
    media_profile: str | None
    media_type: str | None
    thumbnail_url: str | None
    status: JobStatus
    stage: str | None
    stage_progress: float | None
    progress: int
    detail: str | None
    attempts: int
    max_attempts: int
    next_attempt_at: str | None
    wait_reason: str | None
    error_code: str | None
    error_message: str | None
    cancel_requested: bool
    style_id: int | None
    style_name: str | None
    options: dict[str, Any]
    has_transcript: bool
    chunks: int | None
    draft_name: str | None
    output_path: str | None
    output_name: str | None
    output_exists: bool
    output_probe: dict[str, Any] | None
    warnings: list[str]
    origin: Origin
    batch_id: int | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str


class JobList(BaseModel):
    items: list[JobOut]
    total: int
    counts: dict[str, int]


class SkippedMedia(BaseModel):
    media_id: int
    reason: Literal["media_not_found", "not_video", "cover", "already_queued"]


class JobsCreated(BaseModel):
    created: list[JobOut]
    skipped: list[SkippedMedia]


class QueueState(BaseModel):
    paused: bool
