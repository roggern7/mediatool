from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import PositiveId, StrictModel
from app.schemas.jobs import JobOut

ContentType = Literal["reels", "posts", "videos", "images", "all"]


class ImportRequest(StrictModel):
    profile: str = Field(min_length=1, max_length=512)
    content_type: ContentType
    # A interface mostra só 10/20/50/100 até o dono autorizar volumes maiores; "Todos" (null) continua 422.
    limit: Literal[10, 20, 50, 100, 250, 500, 1000, 2000, 5000, 10000] | None
    preset_id: PositiveId | None = None
    # Download puro é o padrão: o processamento (FFmpeg) é opcional e só acontece com `process: true`.
    process: bool = Field(default=False, strict=True)


class RestartListingRequest(StrictModel):
    # Sem `confirm: true` a API responde 409 restart_confirmation_required com o aviso de custo.
    confirm: bool = Field(default=False, strict=True)


class BatchProgress(BaseModel):
    found: int
    downloaded: int
    processed: int
    skipped: int
    failed: int
    pending: int
    active: int


class ListingDropped(BaseModel):
    # Mídias/posts que a listagem não pôde adicionar (inesperado). Filtros de propósito (tipo não suportado,
    # duplicado, filtro de conteúdo) e item entregue em versão menor não entram. Não muda o status do lote.
    unexpected_total: int = 0
    reasons: dict[str, int] = Field(default_factory=dict)
    # Parte de `unexpected_total` que ainda é da página aberta (sem checkpoint; ex.: lote em needs_attention).
    pending: int = 0


class BatchOut(BaseModel):
    id: int
    source: Literal["instagram_import", "library"]
    source_profile: str | None
    content_type: str | None
    requested_limit: int | None
    preset_id: int | None
    preset_name: str | None
    process: bool
    status: Literal["listing", "running", "completed", "failed", "cancelled"]
    found: int
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    finished_at: str | None
    # Estado interno da listagem (pending|running|waiting_backpressure|waiting_rate_limit|paused|needs_attention|done|
    # cancelled; None em lotes da Biblioteca). `paused`/`needs_attention` aceitam POST /api/batches/{id}/resume-listing
    # (do checkpoint) e /restart-listing (da página 1, com confirmação).
    listing_state: str | None = None
    # Só em `waiting_rate_limit` (a listagem espera o teto de páginas por hora; o lote continua ativo e retoma
    # sozinho): retomada prevista (ISO UTC) e páginas usadas/teto da janela de 1 h.
    rate_limit_resume_at: str | None = None
    rate_limit_pages_used: int | None = None
    rate_limit_pages_max: int | None = None
    # Pausa do lote pelo disjuntor (seção 9, A4), só enquanto vigente: código de erro que se repetiu e fim previsto
    # (retomada automática). Em `needs_attention`, o motivo vem em error_code/error_message.
    paused_reason: str | None = None
    paused_until: str | None = None
    # Descartes inesperados da listagem do lote (zero em lotes antigos e de Biblioteca).
    listing_dropped: ListingDropped = Field(default_factory=ListingDropped)
    progress: BatchProgress
    counts: dict[str, int]
    # Só com ?include_jobs=true: no máximo 500 jobs (os mais recentes); a lista completa é GET /api/jobs?batch_id=.
    jobs: list[JobOut] | None = None
    jobs_truncated: bool | None = None


class BatchList(BaseModel):
    items: list[BatchOut]
    total: int
