"""Resposta de `GET /api/post-times/report` (os dados do relatório de padrões em JSON)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Regularity = Literal["regular", "variável", "irregular"]


class HourCount(BaseModel):
    hour: int
    count: int


class WeekdayCount(BaseModel):
    day: str
    count: int


class PatternSegment(BaseModel):
    index: int
    start: str
    end: str
    posts: int
    intervals: int
    avg_min: float
    avg_text: str
    kind: Literal["ritmo", "rajada", "pausa"]
    text: str


class PatternItem(BaseModel):
    n: int
    code: str
    url: str
    taken_at: str | None
    interval_min: float | None
    interval_text: str
    segment: int | None
    burst: bool
    kind: str | None
    pinned: bool
    in_sequence: bool
    source: str


class PatternAccount(BaseModel):
    username: str
    posts: int
    pinned_out: int
    estimated: int
    first_post: str | None
    last_post: str | None
    days: int
    posts_per_day: float | None
    interval_avg_min: float | None
    interval_median_min: float | None
    interval_min_min: float | None
    interval_max_min: float | None
    interval_avg_text: str | None
    interval_median_text: str | None
    interval_min_text: str | None
    interval_max_text: str | None
    regularity: Regularity | None
    cv: float | None
    bursts: int
    largest_burst: int | None
    pauses: int
    largest_pause_text: str | None
    typical_band: str | None
    top_hours: list[int]
    top_hours_share: int
    most_active_days: list[str]
    quiet_window: str | None
    top_kind: str | None
    patterns: list[str]
    hours: list[HourCount]
    weekdays: list[WeekdayCount]
    segments: list[PatternSegment]
    items: list[PatternItem]


class PatternComparison(BaseModel):
    username: str
    posts: int
    posts_per_day: float | None
    interval_avg_text: str | None
    interval_median_text: str | None
    bursts: int
    top_hour: int | None
    regularity: Regularity | None


class PatternReport(BaseModel):
    title: str
    generated_at: str
    timezone: str
    zip_url: str
    accounts: list[PatternAccount]
    comparison: list[PatternComparison]
    comparison_notes: list[str]
