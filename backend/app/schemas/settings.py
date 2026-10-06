from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, SecretStr, model_validator

from app.schemas.common import PositiveId, SafeText, StrictModel

# Formato aceito para valores de cookie (mesmo do provider); tamanho limitado. Os valores ficam em SecretStr:
# nunca aparecem em repr/log e só são revelados ao chamar o provider.
_COOKIE_RE = re.compile(r"[A-Za-z0-9%:_\-.]+")


def _cookie_format(value: SecretStr) -> SecretStr:
    if not _COOKIE_RE.fullmatch(value.get_secret_value()):
        raise ValueError("formato inválido (permitidos: letras, números e % : _ - .)")
    return value


CookieValue = Annotated[SecretStr, Field(min_length=1, max_length=512), AfterValidator(_cookie_format)]


def _cookie_format_or_empty(value: SecretStr) -> SecretStr:
    return value if value.get_secret_value() == "" else _cookie_format(value)


# No PATCH de cookies, texto vazio (ou null) remove a chave.
RemovableCookieValue = Annotated[SecretStr, Field(max_length=512), AfterValidator(_cookie_format_or_empty)]


SessionTestOutcome = Literal[
    "valid", "invalid", "expired", "checkpoint_required", "rate_limited", "restricted", "api_refused", "timeout",
    "network_error", "instagram_error", "session_missing",
]


class LastTest(BaseModel):
    ok: bool
    result: SessionTestOutcome
    code: str
    message: str
    username: str | None
    http_status: int | None
    duration_ms: int
    detail: str | None
    # True: a resposta não encerrou a sessão; False: encerrou ou pediu login; None: desconhecido.
    session_recognized: bool | None = None
    listing_api: Literal["ok", "refused", "rate_limited", "unknown"] | None = None
    # Provider web: sessão presente; etapas bootstrap e GraphQL. None em registros antigos/gallery-dl.
    session_present: bool | None = None
    bootstrap: Literal["ok", "failed", "unknown"] | None = None
    graphql: Literal["ok", "failed", "unknown"] | None = None
    tested_at: str


class InstagramStatus(BaseModel):
    session_configured: bool
    source: Literal["file", "env"] | None
    updated_at: str | None
    extra_keys: list[str]
    paused_until: str | None
    pause_reason: Literal["rate_limited", "action_blocked", "checkpoint_required", "session_invalid",
                          "auth_check"] | None
    # "session": só operações que usam a sessão aguardam; "instagram": downloads de ingest também aguardam.
    pause_scope: Literal["session", "instagram"] | None = None
    # Próxima verificação automática (ISO UTC) e se a pausa pede ação do usuário (checkpoint/sessão inválida).
    next_attempt_at: str | None = None
    action_required: bool = False
    last_test: LastTest | None


class StoragePaths(BaseModel):
    originals: str
    processed: str
    temp: str
    thumbs: str


class EffectiveTools(BaseModel):
    ffmpeg_path: str | None
    ffprobe_path: str | None


InstagramProvider = Literal["auto", "web", "gallery_dl"]
_DOC_ID_RE = re.compile(r"[0-9]{5,25}", re.ASCII)


def _known_query_names() -> list[str]:
    from app.integrations.instagram.web.queries import KNOWN_QUERIES

    return sorted(KNOWN_QUERIES)


def _doc_ids(value: dict[str, str]) -> dict[str, str]:
    known = _known_query_names()
    folded: set[str] = set()
    for name, doc_id in value.items():
        if name not in known:
            raise ValueError(f"consulta desconhecida: {name[:60]!r} (aceitas: {', '.join(known)})")
        if name.lower() in folded:
            raise ValueError(f"consulta repetida: {name!r}")
        folded.add(name.lower())
        if not _DOC_ID_RE.fullmatch(doc_id):
            raise ValueError(f"doc_id inválido para {name}: use de 5 a 25 dígitos (0-9)")
    return value


# Overrides de doc_id do Instagram Web (nome da consulta -> doc_id), ex.: {"PolarisProfilePostsQuery": "123..."}.
DocIdOverrides = Annotated[dict[str, str], AfterValidator(_doc_ids)]


class DiskStatus(BaseModel):
    disk_full: bool  # pausa local de downloads por disco insuficiente (verificada a cada 1 min)
    ok: bool
    free_bytes: int  # -1: não foi possível medir
    required_bytes: int


class SettingsOut(BaseModel):
    ffmpeg_path: str | None
    ffprobe_path: str | None
    concurrency: int  # pool de processamento FFmpeg (1..2)
    download_concurrency: int  # pool de downloads (1..4)
    processing_concurrency: int  # mesmo valor de `concurrency` (nome da seção 9)
    disk: DiskStatus  # o mesmo de GET /api/settings/diagnostics
    default_preset_id: int | None
    instagram_provider: InstagramProvider
    instagram_web_doc_ids: dict[str, str]
    effective: EffectiveTools
    storage: StoragePaths
    database_path: str
    instagram: InstagramStatus


ToolPath = Annotated[SafeText, Field(max_length=1024)]


class SettingsUpdate(StrictModel):
    ffmpeg_path: ToolPath | None = None
    ffprobe_path: ToolPath | None = None
    concurrency: Annotated[int, Field(strict=True, ge=1, le=2)] | None = None
    download_concurrency: Annotated[int, Field(strict=True, ge=1, le=4)] | None = None
    # Nome da seção 9 para `concurrency` (pool FFmpeg); se os dois vierem, precisam ser iguais.
    processing_concurrency: Annotated[int, Field(strict=True, ge=1, le=2)] | None = None
    default_preset_id: PositiveId | None = None
    instagram_provider: InstagramProvider | None = None
    instagram_web_doc_ids: DocIdOverrides | None = None

    @model_validator(mode="after")
    def _same_processing(self) -> SettingsUpdate:
        fields = self.model_fields_set
        if {"concurrency", "processing_concurrency"} <= fields and self.concurrency != self.processing_concurrency:
            raise ValueError("concurrency e processing_concurrency divergem")
        return self


class PausedBatch(BaseModel):
    batch_id: int
    paused_reason: str | None
    paused_code: str | None
    paused_until: str | None


class ExecutionDiagnostics(BaseModel):
    download_concurrency: int
    processing_concurrency: int
    active_downloads: int
    active_processing: int
    disk: DiskStatus
    paused_batches: list[PausedBatch]


class SessionCookiesUpdate(StrictModel):
    """Atualiza só os cookies auxiliares da sessão salva. Vazio ou null remove a chave."""

    csrftoken: RemovableCookieValue | None = None
    ds_user_id: RemovableCookieValue | None = None
    mid: RemovableCookieValue | None = None
    ig_did: RemovableCookieValue | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> SessionCookiesUpdate:
        if not self.model_fields_set:
            raise ValueError("informe pelo menos um cookie")
        return self


class SessionUpdate(StrictModel):
    sessionid: CookieValue
    csrftoken: CookieValue | None = None
    ds_user_id: CookieValue | None = None
    mid: CookieValue | None = None
    ig_did: CookieValue | None = None
