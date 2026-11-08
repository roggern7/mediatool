from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import StrictModel


class TopVideosRequest(StrictModel):
    profile: str = Field(min_length=1, max_length=512)
    top: int = Field(default=10, ge=1, le=50)
    # 3000 = "Todos os Reels do perfil" na tela (para quando a aba acabar antes)
    scan_limit: int = Field(default=120, ge=12, le=3000)


class TopVideosCreatedOut(BaseModel):
    id: str


class TopVideoOut(BaseModel):
    code: str
    url: str
    views: int
    posted_at: str | None
    pinned: bool
    thumbnail_url: str | None


class TopVideosOut(BaseModel):
    id: str
    profile: str
    status: Literal["running", "completed", "failed", "cancelled"]
    scanned: int
    pages: int
    items: list[TopVideoOut]
    error_code: str | None
    error_message: str | None
