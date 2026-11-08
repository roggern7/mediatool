"""Top vídeos de um perfil (aba Importar): busca em segundo plano dos Reels com mais visualizações e miniaturas.

Uma busca por vez, numa thread própria; o registro fica só em memória (as ``MAX_SEARCHES`` mais recentes; um reinício
do app apaga tudo). A leitura usa o cliente HTTP anônimo do app (jar vazio, nenhuma sessão é lida) com o portão global,
o teto de páginas por hora das listagens e o cancelamento (web/top_reels.py). Antes de começar, a pausa do Instagram é
conferida (409 ``instagram_paused``); uma pausa publicada no meio da busca a encerra como falha ``instagram_paused``.
Ritmo: 6 a 9 s entre páginas (a paginação sem login com ~7 s/página passou 5 páginas sem 429; a 2 s levou 429 em
2026-11-08). Bloqueio do Instagram (429/recusa) NÃO pausa o app: só o Top vídeos espera (15 min, dobrando até 1 h em
bloqueios seguidos; zera com uma busca concluída); downloads e importações seguem.

Miniaturas: a URL da CDN que veio no nó fica só no servidor. A rota ``/thumb/<código>`` baixa a imagem pelo
downloader de CDN do app (hosts permitidos, DNS público, tipo pelo conteúdo, limite de bytes) e guarda poucas em
memória. Nada de cookie, token, cursor, id, URL, usuário ou contagem vai para log.
"""

from __future__ import annotations

import logging
import random
import threading
import time
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
PAGE_GAP_S = (6.0, 9.0)              # espaço entre páginas da aba Reels sem login
COOLDOWN_S = (15 * 60, 60 * 60)      # espera só do Top vídeos depois de um bloqueio (1ª, teto)
COOLDOWN_CODES = frozenset({"rate_limited", "access_refused"})
# Sem sessão utilizável: a busca usa a aba sem login.
SESSION_FALLBACK_CODES = frozenset({"session_missing", "session_invalid", "session_expired", "request_auth_failed",
                                    "checkpoint_required"})


class _Done(Exception):
    """Controle interno: a busca com a sessão terminou (pula a leitura sem login)."""

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
                 thumb_fetcher: Callable[[str], tuple[bytes, str]] | None = None,
                 use_session: bool | None = None) -> None:
        self._ctx = ctx
        # Aba Reels com a sessão (padrão no app); testes que injetam o HTTP sem login a desligam, salvo pedido.
        self._use_session = (http_factory is None) if use_session is None else use_session
        self._lock = threading.Lock()
        self._searches: OrderedDict[str, TopSearch] = OrderedDict()
        self._http_factory = http_factory or self._anonymous_http
        self._thumb_fetcher = thumb_fetcher
        self._thumbs: OrderedDict[tuple[str, str], tuple[bytes, str]] = OrderedDict()
        self._cooldown_until = 0.0        # time.time() até quando o Top vídeos espera depois de um bloqueio
        self._cooldown_s = 0.0
        self._sleep_gap = True            # testes desligam o espaço entre páginas

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
            wait = self._cooldown_until - time.time()
            if wait > 0:
                minutes = max(1, int(round(wait / 60)))
                raise AppError(409, "top_videos_cooldown",
                               f"O Instagram limitou a leitura dos Reels há pouco. Tente o Top vídeos de novo em "
                               f"~{minutes} min (downloads e importações continuam normalmente).")
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

        last = {"t": None}

        def before_request() -> bool:
            if cancel.is_set():
                return False
            if last["t"] is not None and self._sleep_gap:
                gap = random.uniform(*PAGE_GAP_S) - (time.monotonic() - last["t"])
                if gap > 0 and cancel.wait(gap):
                    return False
            last["t"] = time.monotonic()
            per_hour = page_rate_limit(env)
            if gate is not None and per_hour > 0:
                gate.wait_listing_page(cancel, max_per_hour=per_hour)
            return not cancel.is_set()

        def on_progress(scanned: int, pages: int) -> None:
            with self._lock:
                search.scanned, search.pages = scanned, pages

        status, code, message, items = "failed", None, None, []
        http = None
        session_used = False
        try:
            session_result = self._scan_with_session(search, on_progress)
            if session_result is not None:
                session_used = True
                items = session_result.items
                status = "cancelled" if session_result.cancelled or cancel.is_set() else "completed"
                if status == "completed":
                    with self._lock:
                        self._cooldown_s = 0.0
                raise _Done()
            http = self._http_factory()
            if hasattr(http, "cancel"):
                http.cancel = cancel
            result = scan_top_reels(http, search.profile, top=search.top, scan_limit=search.scan_limit,
                                    before_request=before_request, overrides=instagram_doc_id_overrides(
                                        ctx.app_settings), env=env, on_progress=on_progress,
                                    cancel=cancel)
            items = result.items
            status = "cancelled" if result.cancelled or cancel.is_set() else "completed"
            if status == "completed":
                with self._lock:
                    self._cooldown_s = 0.0
        except _Done:
            pass
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
                    if (session_used or getattr(exc, "via_session", False)) and ctx.worker is not None:
                        # Bloqueio na leitura COM a sessão: a mesma pausa global das importações (protege a conta).
                        ctx.worker.pause_for(exc.code)
                    elif exc.code in COOLDOWN_CODES:
                        # Só o Top vídeos espera: a leitura sem login não usa a sessão, e pausar o app inteiro
                        # parava downloads e importações à toa (queixa do dono em 2026-11-08).
                        with self._lock:
                            self._cooldown_s = min(COOLDOWN_S[1], (self._cooldown_s * 2) or COOLDOWN_S[0])
                            self._cooldown_until = time.time() + self._cooldown_s
                        minutes = int(self._cooldown_s // 60)
                        message = (f"O Instagram limitou a leitura dos Reels. A busca parou e mostra o que já foi lido; "
                                   f"tente de novo em ~{minutes} min. Downloads e importações continuam normalmente.")
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

    def _scan_with_session(self, search: TopSearch, on_progress: Callable[[int, int], None]) -> Any:
        """Aba Reels COM a sessão salva (o mesmo leitor das importações: ``/graphql/query``, portão, ritmo e teto por
        hora). Verificado no real em 2026-11-08: 2 páginas seguidas sem 429 e ``play_count`` em todos os Reels, enquanto
        a paginação SEM login (``/api/graphql``) levou 429 na 2ª página mesmo espaçada. Sem sessão (ou recusada)
        devolve None e a busca usa a aba sem login. As miniaturas dos 12 mais recentes vêm da 1ª página sem login
        (HTML, nunca bloqueado); os demais ficam sem miniatura."""
        from app.integrations.instagram.web.top_reels import ScanResult, TopReel, rank

        factory = getattr(self._ctx, "downloader_factory", None)
        if factory is None or not self._use_session:
            return None
        downloader = factory()
        read = getattr(downloader, "iter_reels_post_times", None)
        if read is None:
            return None
        reels: list[TopReel] = []
        try:
            for index, item in enumerate(read(search.profile, limit=search.scan_limit, cancel=search.cancel)):
                views = item.get("play_count")
                reels.append(TopReel(code=str(item["code"]), views=views if isinstance(views, int) else None,
                                     posted_at=item.get("taken_at"), pinned=bool(item.get("pinned")), index=index,
                                     thumbnail=item.get("thumbnail_url")))
                on_progress(len(reels), (len(reels) + 11) // 12)
        except InstagramError as exc:
            if exc.code in SESSION_FALLBACK_CODES and not reels:
                logger.info("Top vídeos: sem sessão utilizável (%s); usando a aba sem login", exc.code)
                return None
            exc.partial = ScanResult(items=rank(reels, search.top), scanned=len(reels),   # type: ignore[attr-defined]
                                     pages=(len(reels) + 11) // 12, cancelled=False)
            exc.via_session = True   # type: ignore[attr-defined]
            raise
        finally:
            close = getattr(downloader, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        ranked = rank(reels, search.top)
        # Capa veio na resposta com a sessão: nada a buscar; senão, as dos 12 mais recentes pela 1ª página sem login.
        thumbs = {} if all(r.thumbnail for r in ranked) else self._first_page_thumbs(search)
        ranked = [TopReel(code=r.code, views=r.views, posted_at=r.posted_at, pinned=r.pinned,
                          thumbnail=r.thumbnail or thumbs.get(r.code), index=r.index) for r in ranked]
        return ScanResult(items=ranked, scanned=len(reels), pages=(len(reels) + 11) // 12,
                          cancelled=search.cancel.is_set())

    def _first_page_thumbs(self, search: TopSearch) -> dict[str, str]:
        """Miniaturas dos 12 mais recentes pela 1ª página SEM login (1 GET do HTML). Falha = sem miniaturas."""
        from app.integrations.instagram.web.top_reels import scan_top_reels

        http = None
        try:
            http = self._http_factory()
            budget = {"left": 1}   # UMA requisição: só a 1ª página (HTML)

            def one_request() -> bool:
                budget["left"] -= 1
                return budget["left"] >= 0

            result = scan_top_reels(http, search.profile, top=12, scan_limit=12, before_request=one_request,
                                    overrides={}, env=self._ctx.settings.env, on_progress=lambda *_: None,
                                    cancel=search.cancel)
            return {r.code: r.thumbnail for r in result.items if r.thumbnail}
        except Exception:  # noqa: BLE001 - miniatura é extra
            return {}
        finally:
            if http is not None:
                try:
                    http.close()
                except Exception:  # noqa: BLE001
                    pass

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
