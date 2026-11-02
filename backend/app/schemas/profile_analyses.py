from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.schemas.common import StrictModel

MAX_TEXT_CHARS = 1_048_576       # igual ao limite de corpo: quem decide o tamanho é ele (413 com mensagem própria)

AnalysisStatus = Literal["pending", "running", "waiting", "completed", "cancelled", "interrupted"]
AccountStatus = Literal["active", "private", "restricted", "unavailable", "unverified"]
Tristate = Literal["yes", "no", "unverified"]


class AnalysisCreate(StrictModel):
    text: Annotated[str, Field(max_length=MAX_TEXT_CHARS)]


class InvalidLine(BaseModel):
    line: str
    reason: str


class AnalysisSummary(BaseModel):
    active: int
    private: int
    restricted: int
    unavailable: int
    unverified: int
    cancelled: int
    stories_yes: int
    highlights_yes: int
    views_total: int


class AccountTotals(BaseModel):
    active: int
    inactive: int
    unverified: int
    not_consulted: int


class AnalysisOut(BaseModel):
    id: int
    status: AnalysisStatus
    total: int
    done: int
    pending: int
    running_username: str | None
    invalid_count: int
    duplicate_count: int
    invalid_lines: list[InvalidLine]
    waiting_until: str | None
    wait_reason: str | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    created_date: str | None
    created_time: str | None
    finished_time: str | None
    report_ready: bool
    cautious_pace: bool
    resumable: bool
    summary: AnalysisSummary
    accounts: AccountTotals


class HistoryCleared(BaseModel):
    deleted: int
    kept_open: int


class AnalysisList(BaseModel):
    items: list[AnalysisOut]
    total: int
    open_id: int | None        # análise que o executor atende agora, esteja ou não nesta página


class AnalysisItemOut(BaseModel):
    position: int
    username: str
    state: Literal["pending", "running", "done", "cancelled"]
    account_status: AccountStatus | None
    display_name: str | None
    posts_state: str | None
    posts_count: int | None
    posts_exact: int | None
    posts_text: str | None
    views_state: str | None
    views_total: int | None
    views_reels: int | None
    stories: Tristate | None
    highlights: Tristate | None
    error_code: str | None
    error_message: str | None
    checked_at: str | None
    checked_date: str | None
    checked_time: str | None
    attempts: int


class AnalysisItemList(BaseModel):
    items: list[AnalysisItemOut]
    total: int
