from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import PositiveId, PresetName, StrictModel

Resolution = Literal["original", "480P", "720P", "1080P", "2K", "4K"]
Bitrate = Literal["low", "recommended", "high"]
Fps = Literal["original", "24", "25", "30", "50", "60"]
WhisperModel = Literal["small", "medium", "large-v3-turbo"]
JobStatus = Literal["pending", "running", "waiting", "completed", "failed", "cancelled"]
Origin = Literal["manual", "auto", "api"]
ExportMode = Literal["isolated", "interactive"]
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
    export_mode: ExportMode | None = None
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
    export_mode: ExportMode


class QueueCounts(BaseModel):
    pending: int
    running: int
    waiting: int
    failed: int
    completed: int
    cancelled: int
    paused: bool


class IsolatedModeInfo(BaseModel):
    available: bool                  # pré-requisitos do modo isolado presentes (Windows + pywin32 + uiautomation)
    reason: str | None


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
    export_mode: ExportMode          # modo configurado (mesmo valor de settings.export_mode)
    isolated: IsolatedModeInfo


class CaptionDraft(BaseModel):
    name: str
    modified_at: str | None = None
    text_segments: int = 0
    preview: str = ""


# ---- editor visual: `design` (JSON versionado; docs/CAPCUT.md "Editor visual") ------------------------------------
FontSource = Literal["bundled", "capcut", "windows"]
AnimationType = Literal["in", "out", "loop"]
Unit = Annotated[float, Field(ge=0, le=1)]
ResourceId = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[0-9]+$")]
LongText = Annotated[str, Field(max_length=200)]


class DesignFont(StrictModel):
    source: FontSource
    id: ResourceId | None = None
    path: Annotated[str, Field(min_length=1, max_length=1024)] | None = None
    name: Annotated[str, Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def _source_fields(self) -> DesignFont:
        if self.source == "capcut" and not self.id:
            raise ValueError("fonte do CapCut precisa do id do catálogo")
        if self.source != "capcut" and not self.path:
            raise ValueError("fonte do Windows/embutida precisa do arquivo")
        return self


class DesignStroke(StrictModel):
    enabled: bool
    color: Color
    width: Annotated[float, Field(ge=0, le=100)]


class DesignShadow(StrictModel):
    enabled: bool
    color: Color
    opacity: Unit
    blur: Annotated[float, Field(ge=0, le=100)]
    distance: Annotated[float, Field(ge=0, le=100)]
    angle: Annotated[float, Field(ge=-180, le=180)]


class DesignBackground(StrictModel):
    enabled: bool
    color: Color
    opacity: Unit
    radius: Unit


class DesignPosition(StrictModel):
    x: Annotated[float, Field(ge=-1, le=1)]
    y: Annotated[float, Field(ge=-1, le=1)]


class DesignHighlight(StrictModel):
    enabled: bool
    color: Color


class DesignAnimation(StrictModel):
    id: ResourceId
    name: LongText = ""
    duration_ms: Annotated[int, Field(ge=100, le=10_000)]


class DesignAnimations(StrictModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    in_: DesignAnimation | None = Field(default=None, alias="in")
    out: DesignAnimation | None = None
    loop: DesignAnimation | None = None


class CaptionDesign(StrictModel):
    version: Literal[1]
    font: DesignFont
    size: Annotated[float, Field(ge=2, le=30)]
    scale: Annotated[float, Field(ge=0.3, le=4)]
    bold: bool
    italic: bool
    underline: bool
    uppercase: bool
    color: Color
    opacity: Unit
    stroke: DesignStroke
    shadow: DesignShadow
    background: DesignBackground
    align: Literal["center", "left", "right"]
    letter_spacing: Annotated[float, Field(ge=-20, le=100)]
    line_spacing: Annotated[float, Field(ge=-20, le=100)]
    position: DesignPosition
    max_line_width: Annotated[float, Field(ge=0.1, le=1)]
    highlight: DesignHighlight
    animation: DesignAnimations = Field(default_factory=DesignAnimations)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


class StyleOut(BaseModel):
    id: int
    name: str
    builtin: bool
    is_default: bool
    source_draft: str | None
    summary: dict[str, Any] | None
    created_at: str | None
    design: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)  # avisos do design (POST/PUT de modelos do editor)


class StyleCreate(StrictModel):
    """Do rascunho do CapCut (`draft_name` + `segment_index`) OU do editor visual (`design`)."""

    name: PresetName
    draft_name: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    segment_index: Annotated[int, Field(strict=True, ge=0, le=10_000)] = 0
    design: CaptionDesign | None = None

    @model_validator(mode="after")
    def _one_source(self) -> StyleCreate:
        if (self.draft_name is None) == (self.design is None):
            raise ValueError("informe draft_name ou design (um dos dois)")
        return self


class StyleUpdate(StrictModel):
    name: PresetName | None = None
    design: CaptionDesign


class FontItem(BaseModel):
    key: str
    name: str
    source: FontSource
    id: str | None
    path: str | None
    is_pro: bool
    downloaded: bool
    previewable: bool


class FontPage(BaseModel):
    items: list[FontItem]
    total: int


class AnimationItem(BaseModel):
    id: str
    name: str
    type: AnimationType
    is_pro: bool
    downloaded: bool
    default_duration_ms: int


class AnimationPage(BaseModel):
    items: list[AnimationItem]
    total: int


class PreviewCreate(StrictModel):
    """Prévia real (CapCut) de `design` ou de um modelo salvo (`style_id`, 0 = embutido)."""

    design: CaptionDesign | None = None
    style_id: Annotated[int, Field(strict=True, ge=0, le=2**53)] | None = None
    media_id: PositiveId | None = None
    start_s: Annotated[float, Field(ge=0, le=86_400)] = 0
    seconds: Annotated[float, Field(ge=3, le=12)] = 6

    @model_validator(mode="after")
    def _one_style(self) -> PreviewCreate:
        if (self.design is None) == (self.style_id is None):
            raise ValueError("informe design ou style_id (um dos dois)")
        return self


class PreviewCreated(BaseModel):
    id: str
    status: JobStatus
    warnings: list[str] = Field(default_factory=list)  # avisos do design


class PreviewOut(BaseModel):
    id: str
    status: JobStatus
    stage: str | None
    progress: int
    detail: str | None
    error_code: str | None
    error_message: str | None
    file_url: str | None
    media_id: int | None
    start_s: float
    seconds: float
    warnings: list[str]
    export_mode: ExportMode | None = None   # modo usado (None enquanto não começou)


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
    export_mode: ExportMode | None = None   # modo usado na última execução (None = ainda não rodou)
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
