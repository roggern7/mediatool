from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import StrictModel


class ProfileRequest(StrictModel):
    profile: str = Field(min_length=1, max_length=512)


class ProfileInfoOut(BaseModel):
    username: str
    user_id: str | None
    full_name: str | None
    avatar_url: str | None
    post_count: int | None
    is_private: bool | None
    is_verified: bool | None


class HealthOut(BaseModel):
    status: str
    version: str
    time: str
