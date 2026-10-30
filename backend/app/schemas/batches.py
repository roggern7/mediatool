from __future__ import annotations

from typing import Annotated, Any, Literal

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
# Filtro de visualizações mínimas (só vídeos: content_type "reels" ou "videos"; só o método web). Inteiro estrito:
# "500000", 5e5, true, 0 e negativo são 422. Omitido ou `null` = sem filtro.
ImportMinViews = Annotated[int, Field(strict=True, ge=1, le=10_000_000_000)]
# Período de postagem: "últimos N dias" (1 a 3650; inteiro estrito). Omitido ou `null` = qualquer data. Só importação
# por perfil (e vários perfis); a importação por link não tem filtros.
ImportPostedWithinDays = Annotated[int, Field(strict=True, ge=1, le=3650)]


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
    min_views: ImportMinViews | None = None
    posted_within_days: ImportPostedWithinDays | None = None


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
    min_views: ImportMinViews | None = None   # POR PERFIL
    posted_within_days: ImportPostedWithinDays | None = None   # o mesmo corte para todos os perfis


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
    # Filtros só existem na importação por perfil: aceitos no schema apenas para responder 422 `filters_profile_only`
    # com mensagem clara (qualquer valor não nulo é recusado em api/imports.py).
    min_views: Any = None
    posted_within_days: Any = None


class AutoExportOut(BaseModel):
    content: Literal["originals", "edited", "both"]
    # "waiting" (aguardando o fim), "failed" (não criada; motivo em error_message) ou o estado da exportação criada.
    status: Literal["waiting", "pending", "running", "completed", "failed", "cancelled", "expired"]
    export_id: int | None = None
    error_message: str | None = None
    zip_ready: bool = False
    download_url: str | None = None
    # Pedido com perfis E links (migration 0024): o ZIP é UM para o pedido inteiro; as partes mostram o mesmo.
    request_id: int | None = None


class SkippedProfile(BaseModel):
    profile: str
    reason: str


# Importação unificada (docs/CONTRACTS.md, seção 8.6): publicações e/ou perfis numa entrada só.
# "videos": Reels e qualquer vídeo (inclusive de carrossel); "photos": fotos (inclusive de carrossel); "covers": só a
# capa de cada vídeo/Reel; "videos_covers": vídeos + a capa de cada um; "all": todos os itens originais (carrossel
# inteiro, na ordem) + a capa de cada vídeo. Itens de outro tipo são ignorados e contados em `filtered`.
MediaChoice = Literal["videos", "photos", "covers", "videos_covers", "all"]


class AutoExportRequest(StrictModel):
    # Exportação ZIP automática quando a importação (lote ou grupo) terminar: originais, editados ou os dois.
    content: Literal["originals", "edited", "both"]


class ClassifyRequest(StrictModel):
    # Exatamente um dos dois: lista de entradas ou texto colado (quebra de linha, vírgula e ponto e vírgula separam;
    # espaço só separa URLs do Instagram e `@usuario`).
    inputs: list[str] | None = Field(default=None, min_length=1, max_length=100_000)
    text: str | None = Field(default=None, max_length=2_000_000)

    @model_validator(mode="after")
    def _one_source(self) -> ClassifyRequest:
        if (self.inputs is None) == (self.text is None):
            raise ValueError("Informe `inputs` ou `text` (um dos dois).")
        return self


class UnifiedImportRequest(ClassifyRequest):
    media: MediaChoice = "videos"
    # POR PERFIL; `null` = todas as publicações disponíveis (exige `confirm_all: true` quando há perfil).
    limit: ImportLimit | None = None
    confirm_all: bool = Field(default=False, strict=True)
    # Filtros: só perfis (publicações por link não têm filtro; ignorados com aviso em `notes`). Visualizações só com
    # `media` "videos" ou "covers" quando há perfis.
    posted_within_days: ImportPostedWithinDays | None = None
    min_views: ImportMinViews | None = None
    max_views: ImportMinViews | None = None
    # Pós-processamento: vale para vídeos; capas e fotos só são baixadas.
    post_processing: Literal["none", "preset", "edit"] = "none"
    preset_id: PositiveId | None = None
    edit: EditSettings | None = None
    # Exportação ZIP automática ao concluir (migration 0023); null = sem exportação automática.
    auto_export: AutoExportRequest | None = None

    @model_validator(mode="after")
    def _consistent(self) -> UnifiedImportRequest:
        if self.min_views is not None and self.max_views is not None and self.max_views < self.min_views:
            raise ValueError("O máximo de visualizações precisa ser maior ou igual ao mínimo.")
        if (self.post_processing == "edit") != (self.edit is not None):
            raise ValueError("`edit` só vale (e é obrigatório) com post_processing \"edit\".")
        if self.preset_id is not None and self.post_processing != "preset":
            raise ValueError("`preset_id` só vale com post_processing \"preset\".")
        return self


class ClassifiedLink(BaseModel):
    shortcode: str
    url: str
    kind: Literal["p", "reel"]


class ClassifyOut(BaseModel):
    profiles: list[str]
    links: list[ClassifiedLink]
    invalid: list[InvalidProfileEntry]
    duplicates: int


class UnifiedImportOut(BaseModel):
    batch_ids: list[int]          # lotes avulsos criados (perfil único e/ou links)
    group_id: int | None          # grupo quando há 2+ perfis
    links_batch_id: int | None    # lote de links quando há publicações
    profiles: int
    links: int
    invalid: list[InvalidProfileEntry]
    duplicates: int               # entradas repetidas na colagem
    # Perfis do grupo pulados por já terem importação em andamento (os demais foram criados).
    skipped: list[SkippedProfile] = Field(default_factory=list)
    # Avisos sem erro (ex.: filtros de perfil ignorados nas publicações por link).
    notes: list[str] = Field(default_factory=list)


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
    # Importação unificada: mídias que já existiam (dedup por origem ou sha256) e itens ignorados pelo tipo de mídia,
    # período ou visualizações (a listagem não cria job para eles). `skipped` continua como antes.
    duplicates: int = 0
    filtered: int = 0


class ListingDropped(BaseModel):
    # Mídias/posts que a listagem não pôde adicionar (inesperado). Filtros de propósito (tipo não suportado,
    # duplicado, filtro de conteúdo) e item entregue em versão menor não entram. Não muda o status do lote.
    unexpected_total: int = 0
    reasons: dict[str, int] = Field(default_factory=dict)
    # Parte de `unexpected_total` que ainda é da página aberta (sem checkpoint; ex.: lote em needs_attention).
    pending: int = 0


class ViewsFilterOut(BaseModel):
    # Filtros da importação por perfil (visualizações mínimas e/ou período de postagem): mídias analisadas
    # (= approved + below + no_count + out_of_period) por veredito. `approved` = passou em todos os filtros ligados; não
    # é "baixados": mídia aprovada que já está na Biblioteca vira job duplicado (o número real está em
    # progress.downloaded). `below`/`no_count` só com `min_views`; `out_of_period` só com o período. `pending`: parte
    # dos analisados que ainda é da página aberta (sem checkpoint).
    min_views: int | None = None
    max_views: int | None = None   # migration 0021
    posted_within_days: int | None = None
    posted_after: str | None = None   # corte gravado na criação (UTC)
    analyzed: int = 0
    approved: int = 0
    below: int = 0
    no_count: int = 0
    out_of_period: int = 0
    above: int = 0   # acima do máximo (só com `max_views`)
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
    # "period_end": a listagem chegou às publicações mais antigas que o corte do período de postagem.
    # "empty_first_page": a 1a página do perfil veio vazia e sem próxima (sem posts, privado ou limite temporário).
    listing_end: Literal["profile_end", "limit", "period_end", "empty_first_page"] | None = None
    # Filtro de visualizações mínimas do lote; None = sem filtro (lotes antigos e criados sem ele).
    views_filter: ViewsFilterOut | None = None
    # Tipo de mídia pedido (importação unificada; `MediaChoice`); None em lotes antigos de outro conteúdo e nos da
    # Biblioteca.
    media: MediaChoice | None = None
    # Exportação ZIP automática ao concluir (migration 0023); None sem ela. Filho de grupo: a do grupo.
    auto_export: AutoExportOut | None = None
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
    profile_end_count: int   # filhos que chegaram ao fim REAL do perfil (sem os que pararam pelo período)
    period_end_count: int = 0   # filhos cuja listagem terminou pelo corte do período de postagem
    progress: BatchProgress
    listing_dropped_total: int
    # Filtro de visualizações mínimas do grupo (soma dos filhos); None = sem filtro.
    views_filter: ViewsFilterOut | None = None
    media: MediaChoice | None = None
    auto_export: AutoExportOut | None = None
    # Última exportação para pasta CONCLUÍDA do grupo inteiro (None se nunca foi exportado).
    last_export: LastExport | None = None


class ImportGroupList(BaseModel):
    items: list[ImportGroupOut]
    total: int
    next_before_id: int | None = None


class ImportGroupDetail(BaseModel):
    group: ImportGroupOut
    children: list[BatchOut]
    next_after_position: int | None = None
    # Só na resposta da criação: perfis pulados (já tinham importação ativa) e repetições removidas da entrada.
    skipped: list[SkippedProfile] | None = None
    duplicates: int | None = None
