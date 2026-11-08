"""Top vídeos de um perfil (aba Importar): busca em segundo plano dos Reels com mais visualizações e miniaturas.

Uma busca por vez, numa thread própria; o registro fica só em memória (as ``MAX_SEARCHES`` mais recentes; um reinício
do app apaga tudo). A leitura usa o cliente HTTP anônimo do app (jar vazio, nenhuma sessão é lida) com o portão global,
o teto de páginas por hora das listagens e o cancelamento (web/top_reels.py). Antes de começar, a pausa do Instagram é
conferida (409 ``instagram_paused``); uma pausa publicada no meio da busca a encerra como falha ``instagram_paused``.
Erro do Instagram aplica a mesma pausa global que a aba Reels aplica na importação (``worker.pause_for``: 429 pausa).

Miniaturas: a URL da CDN que veio no nó fica só no servidor. A rota ``/thumb/<código>`` baixa a imagem pelo
downloader de CDN do app (hosts permitidos, DNS público, tipo pelo conteúdo, limite de bytes) e guarda poucas em
memória. Nada de cookie, token, cursor, id, URL, usuário ou contagem vai para log.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.errors import AppError, not_found
from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.validation import parse_profile_input
from app.jobs.errors import safe_message
from app.services.instagram_gate import GateCancelled, InstagramPaused
from app.services.instagram_pause import ensure_not_paused

logger = logging.getLogger("media_tool.top_videos")

MAX_SEARCHES = 10
THUMB_MAX_BYTES = 2 * 1024 * 1024
THUMB_CACHE_ITEMS = 60
THUMB_TIMEOUT = 20.0
THUMB_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
REEL_URL = "https://www.instagram.com/reel/{code}/"

MSG_IN_PROGRESS = "Já existe uma busca de top vídeos em andamento. Aguarde o fim ou cancele a busca atual."
MSG_PAUSED = "O acesso automático ao Instagram foi pausado durante a busca. Aguarde ou libere a pausa em Configurações."
MSG_INTERNAL = "Erro inesperado ao buscar os vídeos do perfil."
MSG_THUMB_MISSING = "Este vídeo não tem miniatura."
MSG_THUMB_FAILED = "Não foi possível carregar a miniatura."


@dataclass
class TopSearch:
    id: str
    profile: str
    top: int
    scan_limit: int
    cancel: threading.Event = field(default_factory=threading.Event)
    status: str = "running"
    scanned: int = 0
    pages: int = 0
    items: list[Any] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    def __repr__(self) -> str:
        return f"TopSearch(status={self.status!r})"


def _default_thumb_fetcher(temp_dir: Path) -> Callable[[str], tuple[bytes, str]]:
    def fetch(url: str) -> tuple[bytes, str]:
        from app.services.cdn_downloader import cdn_download

        result = cdn_download(url, temp_dir, max_bytes=THUMB_MAX_BYTES, timeout=THUMB_TIMEOUT)
        try:
            return result.path.read_bytes(), result.content_type
        finally:
            result.path.unlink(missing_ok=True)

    return fetch


class TopVideosService:
    def __init__(self, ctx: Any, *, http_factory: Callable[[], Any] | None = None,
                 thumb_fetcher: Callable[[str], tuple[bytes, str]] | None = None) -> None:
        self._ctx = ctx
        self._lock = threading.Lock()
        self._searches: OrderedDict[str, TopSearch] = OrderedDict()
        self._http_factory = http_factory or self._anonymous_http
        self._thumb_fetcher = thumb_fetcher
        self._thumbs: OrderedDict[tuple[str, str], tuple[bytes, str]] = OrderedDict()

    def __repr__(self) -> str:
        return "TopVideosService(<redacted>)"

    def _anonymous_http(self) -> Any:
        from app.integrations.instagram.web.http import InstagramWebHttp, WebHttpConfig

        return InstagramWebHttp.anonymous(WebHttpConfig(), gate=getattr(self._ctx, "instagram_gate", None))

    # ---- rotas ---------------------------------------------------------------------------------------------------
    def start(self, profile: str, *, top: int, scan_limit: int) -> str:
        try:
            username = parse_profile_input(profile)
        except ValueError as exc:
            raise AppError(422, "invalid_input", f"Perfil inválido: {exc}") from None
        ensure_not_paused(self._ctx)
        with self._lock:
            if any(search.status == "running" for search in self._searches.values()):
                raise AppError(409, "top_videos_in_progress", MSG_IN_PROGRESS)
            search = TopSearch(id=uuid.uuid4().hex, profile=username, top=top, scan_limit=scan_limit)
            self._searches[search.id] = search
            while len(self._searches) > MAX_SEARCHES:
                oldest = next(iter(self._searches))
                self._forget(oldest)
        threading.Thread(target=self._run, args=(search,), name="top-videos", daemon=True).start()
        return search.id

    def get(self, search_id: str) -> dict[str, Any]:
        with self._lock:
            return self._out(self._require(search_id))

    def cancel(self, search_id: str) -> dict[str, Any]:
        with self._lock:
            search = self._require(search_id)
            search.cancel.set()
            return self._out(search)

    def stop(self) -> None:
        """Encerramento do app: cancela a busca em andamento (a thread termina na próxima requisição ou espera)."""
        with self._lock:
            for search in self._searches.values():
                search.cancel.set()

    def thumbnail(self, search_id: str, code: str) -> tuple[bytes, str]:
        with self._lock:
            search = self._require(search_id)
            url = next((item.thumbnail for item in search.items if item.code == code), None)
            cached = self._thumbs.get((search_id, code))
            if cached is not None:
                self._thumbs.move_to_end((search_id, code))
                return cached
        if not url:
            raise AppError(404, "thumbnail_not_found", MSG_THUMB_MISSING)
        fetcher = self._thumb_fetcher or _default_thumb_fetcher(self._ctx.storage.temp / "top-videos")
        try:
            content, content_type = fetcher(url)
        except Exception as exc:  # noqa: BLE001 - qualquer falha da CDN vira o mesmo 502, sem a URL
            logger.info("Top vídeos: miniatura indisponível (%s)", type(exc).__name__)
            raise AppError(502, "thumbnail_unavailable", MSG_THUMB_FAILED) from None
        if content_type not in THUMB_TYPES or len(content) > THUMB_MAX_BYTES:
            raise AppError(502, "thumbnail_unavailable", MSG_THUMB_FAILED)
        with self._lock:
            if search_id in self._searches:
                self._thumbs[(search_id, code)] = (content, content_type)
                while len(self._thumbs) > THUMB_CACHE_ITEMS:
                    self._thumbs.popitem(last=False)
        return content, content_type

    # ---- execução ------------------------------------------------------------------------------------------------
    def _run(self, search: TopSearch) -> None:
        from app.integrations.instagram.web.provider import page_rate_limit
        from app.integrations.instagram.web.top_reels import scan_top_reels
        from app.services.settings_service import instagram_doc_id_overrides

        ctx = self._ctx
        env = ctx.settings.env
        gate = getattr(ctx, "instagram_gate", None)
        cancel = search.cancel

        def before_request() -> bool:
            if cancel.is_set():
                return False
            per_hour = page_rate_limit(env)
            if gate is not None and per_hour > 0:
                gate.wait_listing_page(cancel, max_per_hour=per_hour)
            return not cancel.is_set()

        def on_progress(scanned: int, pages: int) -> None:
            with self._lock:
                search.scanned, search.pages = scanned, pages

        status, code, message, items = "failed", None, None, []
        http = None
        try:
            http = self._http_factory()
            if hasattr(http, "cancel"):
                http.cancel = cancel
            result = scan_top_reels(http, search.profile, top=search.top, scan_limit=search.scan_limit,
                                    before_request=before_request, overrides=instagram_doc_id_overrides(
                                        ctx.app_settings), env=env, on_progress=on_progress,
                                    cancel=cancel)
            items = result.items
            status = "cancelled" if result.cancelled or cancel.is_set() else "completed"
        except GateCancelled:
            status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - a busca nunca derruba o app
            partial = getattr(exc, "partial", None)
            items = partial.items if partial is not None else []
            if isinstance(exc, InstagramPaused):
                code, message = "instagram_paused", MSG_PAUSED
            elif isinstance(exc, InstagramError):
                if cancel.is_set():
                    status = "cancelled"
                else:
                    code, message = exc.code, safe_message(exc.message)
                    if ctx.worker is not None:
                        ctx.worker.pause_for(exc.code)   # 429: mesma pausa global da aba Reels na importação
            else:
                logger.exception("Top vídeos: erro inesperado na busca")
                code, message = "internal_error", MSG_INTERNAL
        finally:
            if http is not None:
                try:
                    http.close()
                except Exception:  # noqa: BLE001
                    pass
        with self._lock:
            search.items = items
            search.status = status
            search.error_code, search.error_message = code, message
        logger.info("Top vídeos: busca terminou (%s, %d páginas)", status, search.pages)

    # ---- auxiliares ----------------------------------------------------------------------------------------------
    def _require(self, search_id: str) -> TopSearch:
        search = self._searches.get(search_id)
        if search is None:
            raise not_found("Busca de top vídeos", "top_videos_not_found", feminine=True)
        return search

    def _forget(self, search_id: str) -> None:
        search = self._searches.pop(search_id)
        search.cancel.set()
        for key in [key for key in self._thumbs if key[0] == search_id]:
            del self._thumbs[key]

    @staticmethod
    def _out(search: TopSearch) -> dict[str, Any]:
        return {
            "id": search.id, "profile": search.profile, "status": search.status, "scanned": search.scanned,
            "pages": search.pages,
            "items": [{"code": item.code, "url": REEL_URL.format(code=item.code), "views": item.views,
                       "posted_at": item.posted_at, "pinned": item.pinned,
                       "thumbnail_url": f"/api/top-videos/{search.id}/thumb/{item.code}" if item.thumbnail else None}
                      for item in search.items],
            "error_code": search.error_code, "error_message": search.error_message,
        }
