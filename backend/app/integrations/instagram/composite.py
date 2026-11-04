"""CompositeInstagramDownloader: escolhe o provider do Instagram pela estratégia (web | gallery_dl | auto).

Padrão `web` (sem fallback). Em `auto` (opção explícita) usa o provider web e cai para o gallery-dl SOMENTE
quando o web responde `contract_changed` ou `unsupported_media`, uma vez por operação e com log. Nunca há fallback em 429, checkpoint, restrição, sessão
inválida/expirada, rede ou timeout: esses erros sobem como estão (pausas do worker continuam valendo). Também não há
fallback na listagem para `contract_changed` com detalhe `cursor_missing_with_errors` (NO_FALLBACK_DETAILS).
Listagem com ``resume`` (retomada de checkpoint) nunca tem fallback.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

from app.core.config import INSTAGRAM_PROVIDERS
from app.integrations.instagram.downloader import (
    LEGACY_MAX_ITEMS,
    LISTING_ANOMALY_DETAILS,
    ContentType,
    InstagramDownloader,
    InstagramError,
    ListingCheckpoint,
    ListingRef,
    ProfileInfo,
    RemoteMedia,
    SessionTestResult,
)

log = logging.getLogger(__name__)

DEFAULT_INSTAGRAM_PROVIDER = "web"
FALLBACK_CODES: frozenset[str] = frozenset({"contract_changed", "unsupported_media"})
# Details de `contract_changed` que NÃO acionam o fallback: a resposta veio utilizável e só a paginação quebrou
# (has_next_page=true sem cursor junto com `errors`); o erro sobe visível, sem requisições novas pelo gallery-dl.
# Vale para TODO fim anômalo da paginação (LISTING_ANOMALY_DETAILS): o erro do web fica visível e o lote segue a
# regra de `needs_attention`; o gallery-dl nunca "conclui" no lugar dele.
# A publicação por link não passa por aqui: `resolve_post` nunca faz fallback (ver o método).
NO_FALLBACK_DETAILS: frozenset[str] = frozenset({"cursor_missing_with_errors"}) | LISTING_ANOMALY_DETAILS
# Etapa "aba Reels" (migration 0027): só o provider web a lê.
REELS_TAB_WEB_ONLY_MESSAGE = "a aba Reels só é lida pelo método de importação web (o gallery-dl não a consulta)"


# "Todos" (limit None), `photos_carousels` e os filtros de visualizações mínimas e de período de postagem só existem
# no provider web: o gallery-dl truncaria em 5.000 itens (fingindo fim de perfil), recusa o filtro de conteúdo, não
# expõe contagem de visualizações e não encerra a listagem pelo período. Nunca são atendidos pelo legado, nem por
# fallback.
WEB_ONLY_MESSAGE = ("\"Todos\", \"Publicações do feed\", quantidades acima de 5.000 e os filtros de visualizações "
                    "mínimas e de período de postagem exigem o método de importação web; o método gallery-dl não "
                    "lista o perfil até o fim, para em 5.000 mídias e não conhece esses filtros.")


def web_only(content: str, limit: int | None, min_views: int | None = None, posted_after: str | None = None, *,
             covers: bool = False) -> bool:
    # `covers` ("Somente capas", migration 0021): a capa do vídeo sozinha vem só do provider web (`thumbnail_url`).
    return (limit is None or limit > LEGACY_MAX_ITEMS or content == "photos_carousels" or min_views is not None
            or posted_after is not None or covers)


def _no_fallback(exc: InstagramError) -> bool:
    diagnostic = getattr(exc, "diagnostic", None)
    return isinstance(diagnostic, dict) and diagnostic.get("detail") in NO_FALLBACK_DETAILS

T = TypeVar("T")


class CompositeInstagramDownloader(InstagramDownloader):
    def __init__(self, primary: InstagramDownloader, fallback: InstagramDownloader, strategy: str = "web") -> None:
        self.primary = primary
        self.fallback = fallback
        self.strategy = strategy if strategy in INSTAGRAM_PROVIDERS else DEFAULT_INSTAGRAM_PROVIDER
        self._listing_source: InstagramDownloader | None = None

    @property
    def last_listing_stats(self) -> dict | None:
        """Métricas seguras da última listagem do provider que a atendeu (None se ele não as expõe)."""
        return getattr(self._listing_source, "last_listing_stats", None)

    def _call(self, operation: str, call: Callable[[InstagramDownloader], T]) -> T:
        if self.strategy == "gallery_dl":
            return call(self.fallback)
        if self.strategy == "web":
            return call(self.primary)
        try:
            return call(self.primary)
        except InstagramError as exc:
            if exc.code not in FALLBACK_CODES or _no_fallback(exc):
                raise
            log.warning("Instagram: provider web falhou em %s com %s; usando gallery-dl (fallback único desta "
                        "operação)", operation, exc.code)
        return call(self.fallback)

    def test_session(self) -> SessionTestResult:
        # O teste valida o provider em uso; em `auto` é o web (válido só se a listagem moderna funcionar), sem
        # trocar o resultado pelo do gallery-dl.
        if self.strategy == "gallery_dl":
            return self.fallback.test_session()
        return self.primary.test_session()

    def get_profile(self, username: str) -> ProfileInfo:
        return self._call("get_profile", lambda d: d.get_profile(username))

    @property
    def resolve_post_authenticated(self) -> bool:
        """O provider que atende `resolve_post` nesta estratégia consulta com a sessão do dono?"""
        source = self.fallback if self.strategy == "gallery_dl" else self.primary
        return bool(getattr(source, "resolve_post_authenticated", True))

    def resolve_post(self, shortcode: str, *, cancel: threading.Event | None = None,
                     path_kind: str | None = None, use_session: bool = True) -> list[RemoteMedia]:
        # cancel/path_kind só são repassados quando informados (providers antigos não os conhecem).
        extra: dict = {} if cancel is None else {"cancel": cancel}
        if path_kind is not None:
            extra["path_kind"] = path_kind
        # Em `auto` não há fallback: NENHUMA falha do documento público (lido sem sessão) vira uma segunda consulta
        # pelo gallery-dl com a sessão do dono. Só a estratégia `gallery_dl` explícita usa o legado.
        source = self.fallback if self.strategy == "gallery_dl" else self.primary
        if not use_session and source is self.primary:
            # Só o provider web conhece a 2a tentativa com a sessão (documento da publicação); o legado não.
            extra["use_session"] = False
        return source.resolve_post(shortcode, **extra)

    def download(
        self,
        item: RemoteMedia,
        dest_dir: Path,
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        return self._call("download", lambda d: d.download(item, dest_dir, cancel=cancel, progress=progress))

    def download_url(
        self,
        url: str,
        dest_dir: Path,
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        # Fluxo separado (DASH): mesmo provider que atende `resolve_post` (quem entregou as URLs), sem fallback.
        source = self.fallback if self.strategy == "gallery_dl" else self.primary
        return source.download_url(url, dest_dir, cancel=cancel, progress=progress)

    def refresh_page(
        self, profile: str, ref: ListingRef, *, cancel: threading.Event | None = None
    ) -> list[RemoteMedia]:
        return self._call("refresh_page", lambda d: d.refresh_page(profile, ref, cancel=cancel))

    def iter_reels_post_times(self, username: str, *, limit: int | None, posted_after: str | None = None,
                              cancel: threading.Event | None = None) -> Iterator[dict]:
        """Horários dos Reels da aba Reels (relatório de padrões): só o provider web lê; sem fallback em `auto`. Com a
        estratégia `gallery_dl` levanta `invalid_input` (o runner registra como aviso do lote)."""
        if self.strategy == "gallery_dl":
            raise InstagramError("invalid_input", REELS_TAB_WEB_ONLY_MESSAGE)
        return self.primary.iter_reels_post_times(username, limit=limit, posted_after=posted_after, cancel=cancel)

    def iter_profile_media(
        self,
        username: str,
        content: ContentType,
        limit: int | None,
        *,
        cancel: threading.Event | None = None,
        resume: ListingCheckpoint | None = None,
        on_checkpoint: Callable[[ListingCheckpoint], None] | None = None,
        min_views: int | None = None,
        posted_after: str | None = None,
        max_views: int | None = None,
        covers: bool = False,
        on_post: Callable[[dict], None] | None = None,
    ) -> Iterator[RemoteMedia]:
        # resume/on_checkpoint/min_views/max_views/posted_after só são repassados quando informados (providers antigos
        # não os conhecem). `covers` não é repassado: só decide que a listagem é do provider web.
        extra: dict = {}
        if resume is not None:
            extra["resume"] = resume
        if on_checkpoint is not None:
            extra["on_checkpoint"] = on_checkpoint
        if min_views is not None:
            extra["min_views"] = min_views
        if posted_after is not None:
            extra["posted_after"] = posted_after
        if max_views is not None:
            extra["max_views"] = max_views
        views = min_views if min_views is not None else max_views
        if self.strategy == "gallery_dl":
            if web_only(content, limit, views, posted_after, covers=covers):
                # Erro claro em vez de truncar em 5.000 ou falhar de forma obscura (a API já recusa na criação).
                raise InstagramError("invalid_input", WEB_ONLY_MESSAGE)
            self._listing_source = self.fallback
            return self.fallback.iter_profile_media(username, content, limit, cancel=cancel, **extra)
        self._listing_source = self.primary
        # `on_post` (horário dos posts, migration 0026): só o provider web fornece; o gallery-dl não recebe.
        primary_extra = extra if on_post is None else {**extra, "on_post": on_post}
        primary = self.primary.iter_profile_media(username, content, limit, cancel=cancel, **primary_extra)
        if self.strategy == "web" or resume is not None or web_only(content, limit, views, posted_after,
                                                                       covers=covers):
            # Seção 9 (M5): com resume não há fallback (o gallery-dl recomeçaria do início, fora do checkpoint).
            # "Todos"/`photos_carousels`/filtro de visualizações: sem fallback em `auto`; o erro do web fica visível.
            return primary
        return self._iter_auto(primary, username, content, limit, cancel, extra)

    def _iter_auto(
        self,
        primary: Iterator[RemoteMedia],
        username: str,
        content: ContentType,
        limit: int | None,
        cancel: threading.Event | None,
        extra: dict | None = None,
    ) -> Iterator[RemoteMedia]:
        yielded = 0
        try:
            for item in primary:
                yield item
                yielded += 1
            return
        except InstagramError as exc:
            # Depois de entregar itens o fallback repetiria a listagem do início; o erro sobe (lote parcial).
            if exc.code not in FALLBACK_CODES or yielded or _no_fallback(exc):
                raise
            log.warning("Instagram: provider web falhou em iter_profile_media com %s; usando gallery-dl (fallback "
                        "único desta operação)", exc.code)
        self._listing_source = self.fallback
        yield from self.fallback.iter_profile_media(username, content, limit, cancel=cancel, **(extra or {}))
