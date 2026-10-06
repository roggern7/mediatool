from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

JobStatus = Literal["pending", "downloading", "downloaded", "processing", "completed", "failed", "cancelled"]
JobStage = Literal[
    "queued", "waiting_retry", "downloading", "hashing", "probing", "thumbnail", "processing", "finalizing", "done",
    "skipped",
]


class JobOut(BaseModel):
    id: int
    batch_id: int | None
    media_id: int | None
    rendition_id: int | None
    kind: Literal["ingest", "process"]
    preset_id: int | None
    preset_name: str | None
    status: JobStatus
    stage: JobStage | None
    progress: float | None
    error_code: str | None
    error_message: str | None
    attempts: int
    max_attempts: int
    next_attempt_at: str | None
    is_duplicate: bool
    duplicate_reason: Literal["source_id", "sha256", "duplicate_in_flight"] | None
    source_platform: str | None
    source_profile: str | None
    source_url: str | None
    source_post_id: str | None
    source_media_index: int | None
    media_type: Literal["video", "image"] | None
    thumbnail_url: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str
    elapsed_seconds: float | None


class JobList(BaseModel):
    items: list[JobOut]
    total: int
    counts: dict[str, int]
    next_before_id: int | None = None
