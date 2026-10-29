"""Tipos e interface do adapter de Instagram (contrato, seção 4)."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# "photos_carousels" (interface: "Publicações do feed") = tudo o que não é Reel: fotos simples, vídeos avulsos
# antigos (não-Reel) e todos os itens de carrosséis (inclusive vídeos de carrossel).
ContentType = Literal["reels", "posts", "videos", "images", "all", "photos_carousels"]
CONTENT_TYPES: tuple[str, ...] = ("reels", "posts", "videos", "images", "all", "photos_carousels")

# Máximo de VALIDAÇÃO de "Últimos N" (só rejeita lixo; não é teto de listagem). ``limit=None`` ("Todos") lista até o
# fim real do perfil (has_next_page=false), sem teto de quantidade nem de páginas.
MAX_IMPORT_LIMIT = 1_000_000
# Teto interno do provider legado gallery-dl (gallerydl.GALLERY_DL_MAX_ITEMS): acima disso ele truncaria a listagem.
LEGACY_MAX_ITEMS = 5000

# ``diagnostic["detail"]`` dos erros de FIM ANÔMALO da paginação (não é fim do perfil): página vazia, cursor ausente
# ou repetido, ou páginas seguidas sem publicação nova com has_next_page=true, e has_next_page que não é booleano.
# Quem lista NÃO conclui o lote: mantém o checkpoint e deixa em `needs_attention` (import runner).
LISTING_ANOMALY_DETAILS = frozenset({
    "empty_page", "cursor_missing", "cursor_missing_with_errors", "repeated_cursor", "no_new_items",
    "has_next_invalid",
})

ERROR_CODES: dict[str, tuple[str, bool]] = {
    # code: (mensagem padrão, retryable)
    "session_missing": ("Nenhuma sessão do Instagram configurada.", False),
    "session_invalid": ("A sessão do Instagram não foi aceita (inválida ou deslogada).", False),
    "session_expired": ("A sessão do Instagram expirou; configure um sessionid novo.", False),
    # Um request recusado por autenticação mesmo após a única renovação de token (401, require_login, 1357001,
    # login_required). Não prova sessão inválida: pausa de verificação (auth_check, 15 min) e retomada do checkpoint.
    "request_auth_failed": (
        "O Instagram recusou um acesso desta vez. O Media Tool aguarda 15 minutos e verifica de novo; a sessão "
        "salva continua guardada.",
        True,
    ),
    "checkpoint_required": (
        "O Instagram pediu uma verificação de segurança. Abra o Instagram no navegador com esta conta, conclua a "
        "verificação e salve a sessão de novo.",
        False,
    ),
    "profile_not_found": ("Perfil não encontrado.", False),
    "profile_private": ("O perfil é privado e a conta da sessão não o segue.", False),
    "rate_limited": ("O Instagram limitou o acesso temporariamente; tente de novo mais tarde.", True),
    # Resposta com campos explícitos de restrição (400/403 com feedback_*/spam/restriction_enrollment_data): prova
    # só que ESTA chamada foi recusada com aviso de restrição, não que a conta inteira esteja restrita.
    "action_blocked": (
        "O Instagram respondeu a esta consulta com um aviso de restrição. Como "
        "proteção, o Media Tool pausa o acesso automático ao Instagram por 1 hora; a pausa pode ser liberada em "
        "Configurações.",
        False,
    ),
    # Redirect para a página inicial sem encerrar a sessão: prova só que a chamada usada foi recusada (ex.: REST do
    # gallery-dl; o Instagram Web atual usa GraphQL com doc_id). Sem pausa. Mensagem sem jargão técnico.
    "access_refused": (
        "O Instagram recusou a consulta feita pelo Media Tool, redirecionando para a página inicial. Isso não "
        "indica sessão inválida nem restrição da conta; esta operação não funcionou com esta sessão.",
        False,
    ),
    "content_unavailable": ("Conteúdo indisponível.", False),
    # O formato esperado do Instagram Web sumiu (marcador do bootstrap, root_field, doc_id ou JSON sem `data`):
    # o Media Tool precisa de atualização. Não é problema da sessão nem da conta; sem pausa e sem nova tentativa.
    "contract_changed": (
        "O Instagram mudou o formato das respostas que o Media Tool usa. Isso não indica sessão inválida nem "
        "restrição da conta; o Media Tool precisa de atualização.",
        False,
    ),
    "unsupported_media": (
        "O perfil só trouxe publicações de um tipo que o Media Tool ainda não sabe baixar.",
        False,
    ),
    "network_error": ("Falha de conexão ao acessar o Instagram.", True),
    "timeout": ("O Instagram não respondeu a tempo.", True),
    "download_failed": ("Falha ao baixar o arquivo de mídia.", True),
    # O servidor de mídia recusou o link assinado (403/404): a URL expirou. Não é retryable aqui; a pipeline renova
    # a página da listagem (refresh_page) uma vez e tenta de novo.
    "media_url_expired": ("O link temporário da mídia expirou.", False),
    "invalid_input": ("Entrada inválida.", False),
    "internal_error": ("Erro interno na integração com o Instagram.", False),
}


class InstagramError(Exception):
    """Erro classificado do adapter. A mensagem nunca contém cookies."""

    def __init__(self, code: str, message: str | None = None, retryable: bool | None = None):
        if code not in ERROR_CODES:
            code = "internal_error"
        default_message, default_retryable = ERROR_CODES[code]
        self.code = code
        self.message = message or default_message
        self.retryable = default_retryable if retryable is None else retryable
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"InstagramError(code={self.code!r}, message={self.message!r}, retryable={self.retryable!r})"


@dataclass
class ProfileInfo:
    username: str
    user_id: str | None = None
    full_name: str | None = None
    avatar_url: str | None = None
    post_count: int | None = None
    is_private: bool | None = None
    is_verified: bool | None = None


@dataclass(frozen=True)
class ListingRef:
    """Página da listagem que entregou a mídia (opaca para a pipeline). O cursor nunca é logado nem exposto."""

    page_index: int                              # 1-based
    cursor: str | None = field(repr=False)       # cursor usado para PEDIR a página (None = 1a página)
    page_size: int


@dataclass(frozen=True, repr=False)
class ListingCheckpoint:
    """Ponto de retomada da listagem, emitido depois que todos os itens de uma página foram entregues.

    Opaco para a pipeline; repr redigido; o cursor nunca é logado nem exposto. Retomar de ``resume_cursor`` nunca
    pula itens (pode repetir alguns). Contadores são totais da listagem (valem entre retomadas)."""

    resume_cursor: str | None
    next_page_index: int
    pages: int
    posts_seen: int
    items_compatible: int
    unproductive_streak: int
    has_next: bool
    # Descartes da página que este checkpoint encerra (só contagens por motivo fixo, nunca ids/URLs):
    # {"unexpected": {motivo: n}, "expected": {...}, "degraded": {...}}; chaves vazias omitidas; None = nenhum.
    # Quem persiste o checkpoint soma estas contagens na MESMA transação (cada página conta uma única vez).
    page_counts: Mapping[str, Mapping[str, int]] | None = field(default=None, compare=False)
    # Filtros da importação por perfil (só com ``min_views`` e/ou ``posted_after``): mídias da página que este
    # checkpoint encerra por veredito {"approved", "below", "no_count", "out_of_period"} (só totais); None = filtros
    # desligados. Somado na mesma transação do checkpoint, como ``page_counts``.
    page_views: Mapping[str, int] | None = field(default=None, compare=False)
    # True = a listagem terminou porque chegou às publicações mais antigas que o corte do período (``has_next`` é
    # False junto): não é o fim real do perfil.
    period_end: bool = field(default=False, compare=False)

    def __repr__(self) -> str:
        return (f"ListingCheckpoint(next_page_index={self.next_page_index}, pages={self.pages}, "
                f"posts_seen={self.posts_seen}, items_compatible={self.items_compatible}, "
                f"unproductive_streak={self.unproductive_streak}, has_next={self.has_next}, "
                f"resume_cursor=<redacted>)")


@dataclass(kw_only=True)
class RemoteMedia:
    source_platform: str = "instagram"
    source_profile: str
    source_post_id: str  # shortcode do post
    post_id: str | None
    media_id: str | None
    source_media_index: int  # 0-based dentro do post/carrossel
    media_type: Literal["video", "image"]
    is_reel: bool
    source_url: str
    media_url: str
    thumbnail_url: str | None
    width: int | None
    height: int | None
    duration: float | None
    taken_at: str | None
    extension: str | None
    # Página da listagem que entregou a mídia (provider web); None fora da listagem ou em outros providers.
    listing_ref: ListingRef | None = None
    # Descartes inesperados (motivo -> quantidade) da página desta mídia até o nó dela, inclusive; None = nenhum.
    # Só serve para a página que termina sem checkpoint (limite atingido no meio dela); nunca ids/URLs.
    listing_dropped: dict[str, int] | None = field(default=None, compare=False, repr=False)
    # A mídia é item de um carrossel (filtro "photos_carousels"; vídeo de carrossel não é Reel).
    in_carousel: bool = field(default=False, compare=False)
    # Fluxos separados (DASH) de maior qualidade, só no lote por link (web/dash_manifest.py); None = baixar
    # `media_url`, que continua sendo SEMPRE o MP4 direto (fallback). As duas URLs são tão sensíveis quanto
    # `media_url`. O download delas e a junção NUNCA rodaram contra o CDN real.
    dash_video_url: str | None = field(default=None, repr=False)
    dash_audio_url: str | None = field(default=None, repr=False)
    dash_video_bytes: int | None = None
    dash_audio_bytes: int | None = None
    dash_width: int | None = None
    dash_height: int | None = None
    dash_duration: float | None = None
    # Filtros da importação por perfil (só com ``min_views``/``posted_after``): vereditos da página desta mídia até
    # ela, inclusive (mesmo papel de ``listing_dropped`` para a página que termina sem checkpoint); None = desligados.
    listing_views: dict[str, int] | None = field(default=None, compare=False, repr=False)


SessionTestOutcome = Literal[
    "valid", "invalid", "expired", "checkpoint_required", "rate_limited", "restricted", "api_refused", "timeout",
    "network_error", "instagram_error", "session_missing",
]
SESSION_TEST_RESULTS: tuple[str, ...] = (
    "valid", "invalid", "expired", "checkpoint_required", "rate_limited", "restricted", "api_refused", "timeout",
    "network_error", "instagram_error", "session_missing",
)
# Estado da chamada de listagem no teste de sessão (separado do reconhecimento da sessão).
LISTING_API_STATES: tuple[str, ...] = ("ok", "refused", "rate_limited", "unknown")
# Estado das etapas bootstrap/GraphQL no teste de sessão do provider web.
WEB_STAGE_STATES: tuple[str, ...] = ("ok", "failed", "unknown")
# result -> code (códigos antigos mantidos para compatibilidade).
SESSION_RESULT_CODES: dict[str, str] = {
    "valid": "ok",
    "invalid": "session_invalid",
    "expired": "session_expired",
    "checkpoint_required": "checkpoint_required",
    "rate_limited": "rate_limited",
    "restricted": "action_blocked",
    "api_refused": "access_refused",
    "timeout": "network_error",
    "network_error": "network_error",
    "instagram_error": "internal_error",
    "session_missing": "session_missing",
}
_CODE_RESULTS: dict[str, str] = {
    "ok": "valid",
    "session_invalid": "invalid",
    "session_expired": "expired",
    "checkpoint_required": "checkpoint_required",
    "rate_limited": "rate_limited",
    "action_blocked": "restricted",
    "access_refused": "api_refused",
    "network_error": "network_error",
    "timeout": "timeout",
    "session_missing": "session_missing",
}


@dataclass
class SessionTestResult:
    ok: bool
    code: str
    message: str
    username: str | None
    # Sem `result`, ele é derivado de ok/code (construções antigas continuam válidas).
    result: str | None = None
    http_status: int | None = None
    duration_ms: int | None = None
    # Subtipo seguro, ex.: redirect_home, redirect_login, redirect_consent (None quando não se aplica).
    detail: str | None = None
    # Provider web: True quando data.xdt_viewer.user.id da resposta é o ds_user_id da sessão; False quando é outra
    # conta ou há evidência confiável de deslogado (401/require_login, redirect para login, sessionid apagado, sessão
    # recusada após a única renovação); None quando não dá para confirmar. O USER_ID do HTML inicial não conta (vem
    # "0" mesmo logado). Provider gallery-dl: True quando a resposta não encerrou a sessão.
    session_recognized: bool | None = None
    # "ok", "refused", "rate_limited" ou "unknown" (LISTING_API_STATES); None em construções antigas.
    listing_api: str | None = None
    # Provider web: sessão presente no armazenamento; bootstrap e GraphQL: "ok", "failed" ou "unknown".
    # None em construções antigas (gallery-dl).
    session_present: bool | None = None
    bootstrap: str | None = None
    graphql: str | None = None

    def __post_init__(self) -> None:
        if self.result is None:
            self.result = "valid" if self.ok else _CODE_RESULTS.get(self.code, "instagram_error")


class InstagramDownloader(ABC):
    @abstractmethod
    def test_session(self) -> SessionTestResult: ...

    @abstractmethod
    def get_profile(self, username: str) -> ProfileInfo: ...

    @abstractmethod
    def iter_profile_media(
        self,
        username: str,
        content: ContentType,
        limit: int | None,
        *,
        cancel: threading.Event | None = None,
        resume: ListingCheckpoint | None = None,
        on_checkpoint: Callable[[ListingCheckpoint], None] | None = None,
    ) -> Iterator[RemoteMedia]: ...

    def refresh_page(
        self, profile: str, ref: ListingRef, *, cancel: threading.Event | None = None
    ) -> list[RemoteMedia]:
        """Pede de novo a página ``ref`` da listagem e devolve as mídias dela com URLs novas. Padrão: indisponível."""
        raise InstagramError("content_unavailable", "Este provider não renova links de mídia expirados.")

    # True: `resolve_post` consulta com a sessão do dono (uma publicação entregue prova a sessão). O provider web
    # lê o documento público sem sessão e declara False.
    resolve_post_authenticated: bool = True

    @abstractmethod
    def resolve_post(self, shortcode: str, *, cancel: threading.Event | None = None,
                     path_kind: str | None = None) -> list[RemoteMedia]:
        """TODAS as mídias da publicação (carrossel inteiro, na ordem), sem filtro de conteúdo. ``path_kind``:
        "p" ou "reel", o tipo do link de quem chama (validation.post_path_kind); só o provider web o usa."""

    @abstractmethod
    def download(
        self,
        item: RemoteMedia,
        dest_dir: Path,
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> Path: ...

    def download_url(
        self,
        url: str,
        dest_dir: Path,
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        """Baixa UM fluxo separado (`dash_video_url`/`dash_audio_url`). Padrão: indisponível (a pipeline usa o MP4
        direto, sem requisição)."""
        raise InstagramError("content_unavailable", "Este provider não baixa fluxos separados.")
