from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.integrations.instagram.downloader import MAX_IMPORT_LIMIT
from app.schemas.common import PositiveId, StrictModel
from app.schemas.edits import EditSettings
from app.schemas.exports import LastExport
from app.schemas.jobs import JobOut
from app.schemas.media import MediaDeleteResult

# Interface: "all" (Todos), "reels" (Reels) e "photos_carousels" (Publicações do feed). "posts", "videos" e "images"
# continuam aceitos por compatibilidade.
ContentType = Literal["reels", "posts", "videos", "images", "all", "photos_carousels"]
# "Últimos N": inteiro positivo livre (o máximo só rejeita lixo). `null` = "Todos": até o fim real do perfil.
ImportLimit = Annotated[int, Field(strict=True, ge=1, le=MAX_IMPORT_LIMIT)]


class ImportRequest(StrictModel):
    profile: str = Field(min_length=1, max_length=512)
    content_type: ContentType
    # N conta mídias do TIPO pedido (100 + reels = 100 Reels); `null` = "Todos".
    limit: ImportLimit | None
    preset_id: PositiveId | None = None
    # Download puro é o padrão: o processamento (FFmpeg) é opcional e só acontece com `process: true`.
    process: bool = Field(default=False, strict=True)
    # "Editar após baixar": cada vídeo baixado ganha a versão editada (as mesmas configurações de POST /edits).
    # Exclusivo com o preset: `edit` junto com `process: true` ou `preset_id` = 422 `edit_with_preset`.
    edit: EditSettings | None = None


class ProfilesPreviewRequest(StrictModel):
    # Lista de entradas e/ou texto colado: um perfil por linha (vírgula e ponto e vírgula também separam; espaço
    # NÃO separa: entrada com espaço interno é inválida).
    entries: list[str] | None = Field(default=None, max_length=100_000)
    text: str | None = Field(default=None, max_length=2_000_000)


class InvalidProfileEntry(BaseModel):
    entry: str
    reason: str


class ProfilesPreview(BaseModel):
    valid: list[str]
    invalid: list[InvalidProfileEntry]
    duplicates: int


class ImportGroupRequest(StrictModel):
    # Cada entrada: `usuario`, `@usuario` ou URL do perfil. Entrada inválida = 422 `invalid_profiles` (com a lista).
    profiles: list[str] = Field(min_length=1, max_length=100_000)
    content_type: ContentType
    limit: ImportLimit | None   # POR PERFIL
    preset_id: PositiveId | None = None
    process: bool = Field(default=False, strict=True)
    edit: EditSettings | None = None   # aplicada a todos os lotes filhos


class LinksPreviewRequest(StrictModel):
    # Lista de entradas e/ou texto colado: um link de publicação por linha (vírgula, ponto e vírgula e espaço em
    # branco também separam: link não tem espaço).
    entries: list[str] | None = Field(default=None, max_length=100_000)
    text: str | None = Field(default=None, max_length=2_000_000)


class ValidLink(BaseModel):
    shortcode: str
    url: str   # normalizada: https://www.instagram.com/p/<código>/ ou .../reel/<código>/


class LinksPreview(BaseModel):
    valid: list[ValidLink]
    invalid: list[InvalidProfileEntry]
    duplicates: int


class ImportLinksRequest(StrictModel):
    # Cada entrada: link de uma publicação. Entrada inválida = 422 `invalid_links` (com a lista).
    links: list[str] = Field(min_length=1, max_length=100_000)
    preset_id: PositiveId | None = None
    process: bool = Field(default=False, strict=True)
    edit: EditSettings | None = None


class HistoryHideRequest(StrictModel):
    """Remover do histórico de importações (POST /imports/history/hide): itens (`batch_ids` de lotes avulsos e/ou
    `group_ids`) OU `all_finished: true` (todos os terminados). Só oculta; `delete_media: true` também apaga os vídeos
    (como POST /media/delete). `dry_run: true` só conta (vídeos e espaço), sem mudar nada."""

    batch_ids: Annotated[list[PositiveId], Field(max_length=500)] | None = None
    group_ids: Annotated[list[PositiveId], Field(max_length=500)] | None = None
    all_finished: bool = Field(default=False, strict=True)
    delete_media: bool = Field(default=False, strict=True)
    dry_run: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def _one_target(self) -> HistoryHideRequest:
        if bool(self.batch_ids or self.group_ids) == self.all_finished:
            raise ValueError("Informe batch_ids/group_ids ou all_finished (um dos dois).")
        return self


class HistoryHideResult(BaseModel):
    batches: int        # lotes avulsos ocultados (ou que seriam, em dry_run)
    groups: int
    video_count: int    # vídeos dessas importações (mesma regra das pastas da página Editar)
    total_bytes: int    # originais + versões desses vídeos
    shared_count: int = 0   # desses vídeos, quantos também estão em outra importação ainda visível
    dry_run: bool
    deleted: MediaDeleteResult | None = None   # só com delete_media (sem dry_run)


class BatchLinkOut(BaseModel):
    position: int   # 1-based, na ordem colada
    url: str
    shortcode: str
    # `cancelled`: link que ainda estava pendente quando o lote foi cancelado.
    status: Literal["pending", "resolved", "failed", "cancelled"]
    error_code: str | None
    error_message: str | None
    items_found: int | None
    profile: str | None


class BatchLinkList(BaseModel):
    items: list[BatchLinkOut]
    total: int


class BatchLinks(BaseModel):
    # Links do lote por estado gravado (`pending + resolved + failed = total`).
    total: int
    pending: int
    resolved: int
    failed: int


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
    # Por que a listagem terminou: "profile_end" (fim real do perfil; pode ter menos que o pedido) ou "limit"
    # (quantidade pedida atingida). None enquanto lista, em erro/cancelamento e em lotes antigos.
    listing_end: Literal["profile_end", "limit"] | None = None
    # Como o lote foi criado: "profile" (importação por perfil), "links" (importação por link) ou None (Biblioteca).
    import_mode: Literal["profile", "links"] | None = None
    # Só em lote por link: quantos links há e em que estado (lista: GET /api/batches/{id}/links).
    links: BatchLinks | None = None
    # Grupo de importação (vários perfis): None em lotes avulsos e antigos. `group_position` é 1-based.
    group_id: int | None = None
    group_position: int | None = None
    # Só em filhos de grupo: waiting (na fila, listagem ainda não começou) | active | attention | completed |
    # failed | cancelled.
    group_state: Literal["waiting", "active", "attention", "completed", "failed", "cancelled"] | None = None
    # Última exportação para pasta CONCLUÍDA deste lote (None se nunca foi exportado).
    last_export: LastExport | None = None
    progress: BatchProgress
    counts: dict[str, int]
    # Só com ?include_jobs=true: no máximo 500 jobs (os mais recentes); a lista completa é GET /api/jobs?batch_id=.
    jobs: list[JobOut] | None = None
    jobs_truncated: bool | None = None


class ImportLinksOut(BatchOut):
    # Só na resposta de POST /api/import-links: repetições removidas da lista enviada.
    duplicates: int = 0


class BatchList(BaseModel):
    items: list[BatchOut]
    total: int


class GroupProfiles(BaseModel):
    # Filhos por estado (os seis somam `total`).
    total: int
    waiting: int
    active: int
    completed: int
    attention: int
    failed: int
    cancelled: int


class ImportGroupOut(BaseModel):
    id: int
    content_type: str
    requested_limit: int | None
    preset_id: int | None
    preset_name: str | None
    process: bool
    created_at: str
    finished_at: str | None
    # Derivado dos filhos: active (algum na fila ou trabalhando) | attention (nenhum trabalhando, algum aguardando
    # ação) | completed (todos encerrados) | cancelled (todos cancelados).
    status: Literal["active", "attention", "completed", "cancelled"]
    # Algum filho em `attention` ou `failed` (vale também com o grupo ainda ativo).
    attention: bool
    profiles: GroupProfiles
    # Filhos cuja listagem chegou ao fim real do perfil.
    profile_end_count: int
    progress: BatchProgress
    listing_dropped_total: int
    # Última exportação para pasta CONCLUÍDA do grupo inteiro (None se nunca foi exportado).
    last_export: LastExport | None = None


class ImportGroupList(BaseModel):
    items: list[ImportGroupOut]
    total: int
    next_before_id: int | None = None


class SkippedProfile(BaseModel):
    profile: str
    reason: str


class ImportGroupDetail(BaseModel):
    group: ImportGroupOut
    children: list[BatchOut]
    next_after_position: int | None = None
    # Só na resposta da criação: perfis pulados (já tinham importação ativa) e repetições removidas da entrada.
    skipped: list[SkippedProfile] | None = None
    duplicates: int | None = None
