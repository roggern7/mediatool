"""InstagramWebDownloader: provider moderno (bootstrap + GraphQL persistido) que implementa InstagramDownloader.

Contrato: docs/INSTAGRAM_WEB_CONTRACT.md (web/provider.py). Tokens (cookies, fb_dtsg, lsd, jazoest) só existem em
memória, dentro dos objetos http/bootstrap/graphql criados por operação e descartados ao fim dela; nada é gravado.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.integrations.instagram.downloader import (
    CONTENT_TYPES,
    ContentType,
    InstagramDownloader,
    InstagramError,
    ListingCheckpoint,
    ListingRef,
    ProfileInfo,
    RemoteMedia,
    SessionTestResult,
)
from app.integrations.instagram.validation import parse_profile_input, validate_shortcode

if TYPE_CHECKING:
    from app.integrations.instagram.session import InstagramSession, InstagramSessionProvider

log = logging.getLogger(__name__)

DEFAULT_MAX_DOWNLOAD_BYTES = 2147483648
DEFAULT_PAGE_SIZE = 12
# Perfil público consultado (count=1) no teste de sessão.
TEST_PROFILE = "instagram"
VALID_SESSION_MESSAGE = "Sessão funcionando: o Media Tool conseguiu consultar publicações."
# Consulta funcionou, mas a resposta não trouxe xdt_viewer.user.id (ou não há ds_user_id para comparar).
UNCONFIRMED_SESSION_MESSAGE = ("A consulta de publicações funcionou, mas o Instagram não confirmou a conta da "
                               "sessão.")
# Consulta funcionou, mas xdt_viewer.user.id difere do ds_user_id da sessão salva.
OTHER_ACCOUNT_MESSAGE = ("A sessão salva pertence a outra conta: a consulta de publicações funcionou, mas o Instagram "
                         "identificou uma conta diferente da registrada na sessão. Salve a sessão da conta desejada "
                         "em Configurações.")
_EXPIRED_CODES = ("forbidden", "not_found")
# Código de erro -> `result` do teste de sessão.
_TEST_RESULTS = {
    "session_missing": "session_missing",
    "session_invalid": "invalid",
    "session_expired": "expired",
    "checkpoint_required": "checkpoint_required",
    "rate_limited": "rate_limited",
    "action_blocked": "restricted",
    "access_refused": "api_refused",
    "timeout": "timeout",
    "network_error": "network_error",
}
_NOT_RECOGNIZED = ("session_invalid", "session_expired")
# Falhas em que a resposta GraphQL veio 200 com o campo raiz, mas a listagem (edges/page_info) não serviu.
_LISTING_SHAPE_DETAILS = ("edges_not_list", "page_info_missing")


def _default_http(session: InstagramSession, config: Any, transport: Any, gate: Any = None) -> Any:
    from app.integrations.instagram.web.http import InstagramWebHttp, WebHttpConfig

    return InstagramWebHttp(session, config or WebHttpConfig(), transport=transport, gate=gate)


def _default_bootstrap(http: Any) -> Any:
    from app.integrations.instagram.web.bootstrap import WebBootstrap

    return WebBootstrap(http)


def _default_graphql(http: Any, bootstrap: Any, overrides: dict[str, str], env: Mapping[str, str] | None,
                     page_delay: tuple[float, float] | None) -> Any:
    from app.integrations.instagram.web.graphql import InstagramWebGraphQL

    extra = {} if page_delay is None else {"page_delay": page_delay}  # None = intervalo padrão do graphql
    return InstagramWebGraphQL(http, bootstrap, overrides=overrides, env=env, **extra)


BOOTSTRAP_CACHE_TTL = 600.0   # seção 9 (M2): WebState reaproveitado por até 10 min
ENV_MAX_PAGES_PER_HOUR = "MEDIA_TOOL_IG_MAX_PAGES_PER_HOUR"
# Configuração técnica para POC de escala (sem botão na interface): teto de páginas de listagem por hora. Ausente
# ou inválida = padrão (600); ``0`` = sem teto horário. Só o teto por hora muda: intervalo entre requisições, 1 em
# voo, backpressure, pausas, retries e checkpoint continuam iguais. Quando definida, prevalece sobre a de cima.
ENV_PAGE_RATE_LIMIT = "INSTAGRAM_PAGE_RATE_LIMIT"
_rate_limit_disabled_logged = False


def page_rate_limit(env: Any) -> int:
    """Teto de páginas por hora em vigor: ``0`` = desabilitado (só por ``INSTAGRAM_PAGE_RATE_LIMIT=0``)."""
    from app.integrations.instagram.web.budget import _env_number
    from app.services.instagram_gate import DEFAULT_MAX_PAGES_PER_HOUR

    raw = str((env or {}).get(ENV_PAGE_RATE_LIMIT) or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = -1
        if 0 <= value <= 100000:
            return value
        log.warning("%s inválido; usando o padrão de %d páginas por hora", ENV_PAGE_RATE_LIMIT,
                       DEFAULT_MAX_PAGES_PER_HOUR)
        return DEFAULT_MAX_PAGES_PER_HOUR
    return int(_env_number(env, ENV_MAX_PAGES_PER_HOUR, DEFAULT_MAX_PAGES_PER_HOUR, 1, 100000))


class WebStateCache:
    """Cache do WebState do bootstrap compartilhado no app (só memória, nunca gravado), por sessão salva.

    Evita um GET do documento por trecho de listagem/renovação. Invalidado quando o GraphQL recusa o token."""

    def __init__(self, *, ttl: float = BOOTSTRAP_CACHE_TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = float(ttl)
        self._clock = clock
        self._lock = threading.Lock()
        self._entry: tuple[str, Any, float] | None = None   # (chave da sessão, WebState, momento)

    def __repr__(self) -> str:
        return f"WebStateCache(cached={self._entry is not None})"

    def get(self, key: str) -> Any:
        with self._lock:
            entry = self._entry
            if entry is None or entry[0] != key or self._clock() - entry[2] >= self._ttl:
                return None
            return entry[1]

    def put(self, key: str, state: Any) -> None:
        with self._lock:
            self._entry = (key, state, self._clock())

    def invalidate(self, key: str | None = None) -> None:
        with self._lock:
            if key is None or (self._entry is not None and self._entry[0] == key):
                self._entry = None


def _session_key(session: InstagramSession) -> str:
    import hashlib

    return hashlib.sha256((session.cookies().get("sessionid") or "").encode("utf-8")).hexdigest()


class _CachedBootstrap:
    """Bootstrap da operação servido pelo ``WebStateCache``; ``force``/``invalidate`` passam ao bootstrap real."""

    def __init__(self, inner: Any, cache: WebStateCache, key: str) -> None:
        self._inner = inner
        self._cache = cache
        self._key = key

    def __repr__(self) -> str:
        return "_CachedBootstrap(<redacted>)"

    def get(self, *, force: bool = False) -> Any:
        if not force:
            state = self._cache.get(self._key)
            if state is not None:
                return state
        state = self._inner.get(force=force)
        self._cache.put(self._key, state)
        return state

    def invalidate(self) -> None:
        self._cache.invalidate(self._key)
        self._inner.invalidate()


def _classify(resp: Any, stage: str) -> InstagramError | None:
    from app.integrations.instagram.web.errors import classify_response

    return classify_response(resp, stage=stage)


class _Ops:
    """Objetos de uma operação (http, bootstrap, graphql). Vivem só durante a operação."""

    def __init__(self, http: Any, bootstrap: Any, graphql: Any) -> None:
        self.http = http
        self.bootstrap = bootstrap
        self.graphql = graphql

    def __repr__(self) -> str:
        return "_Ops(<redacted>)"


class InstagramWebDownloader(InstagramDownloader):
    def __init__(
        self,
        session_provider: InstagramSessionProvider,
        *,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        cdn_downloader: Callable[..., Any] | None = None,
        doc_id_overrides: dict[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        page_delay: tuple[float, float] | None = None,
        http_config: Any = None,
        transport: Any = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        http_factory: Callable[[InstagramSession], Any] | None = None,
        bootstrap_factory: Callable[[Any], Any] | None = None,
        graphql_factory: Callable[[Any, Any], Any] | None = None,
        on_page: Callable[[dict], None] | None = None,
        max_pages: int | None = None,
        request_gate: Any = None,
        bootstrap_cache: WebStateCache | None = None,
    ) -> None:
        self._provider = session_provider
        self._max_download_bytes = int(max_download_bytes)
        self._cdn_downloader = cdn_downloader
        self._overrides = dict(doc_id_overrides or {})
        self._env = env   # MEDIA_TOOL_IG_* (doc_id e orçamento de paginação); nunca os.environ direto
        self._page_size = int(page_size)
        # Portão global (seção 8): injetado no cliente HTTP, que ocupa uma vaga por requisição ao instagram.com.
        self._http_factory = http_factory or (
            lambda session: _default_http(session, http_config, transport, request_gate))
        self._bootstrap_factory = bootstrap_factory or _default_bootstrap
        self._gate = request_gate
        # WebState compartilhado entre trechos de listagem e renovações (seção 9, M2); None = 1 bootstrap por operação.
        self._bootstrap_cache = bootstrap_cache
        # Ritmo entre páginas decidido por operação: lento (6-12 s) enquanto o portão estiver em ritmo lento pós-429.
        self._graphql_factory = graphql_factory or (
            lambda http, bootstrap: _default_graphql(http, bootstrap, self._overrides, env,
                                                     self._listing_delay(page_delay))
        )
        # Repassado a iter_profile_posts na listagem (metadados seguros por página; ver graphql.py).
        self._on_page = on_page
        # Teto de páginas por TRECHO de listagem (só testes/POC; None = sem teto, o padrão do app). Atingido, o
        # trecho termina com checkpoint `has_next=True`: não é fim do perfil.
        self._max_pages = max_pages
        # Métricas seguras da última listagem (pages, posts_seen, items_compatible, items_skipped_filter, duplicates,
        # stop_reason); None antes de qualquer listagem. Nunca cursor, URL ou id.
        self.last_listing_stats: dict | None = None
        # Totais seguros do último trecho de listagem: pages_with_errors, errors, dropped_unexpected e
        # skipped_expected (motivo -> quantidade); None antes do fim de um trecho. Nunca mensagem, id ou cursor.
        self.last_listing_anomalies: dict | None = None
        # Estado da conta na 1a resposta da última listagem: "confirmed", "different_account" ou "not_confirmed"
        # (None antes de qualquer listagem). Nunca contém ids.
        self.last_account_confirmation: str | None = None

    def __repr__(self) -> str:
        return "InstagramWebDownloader(<redacted>)"

    # ------------------------------------------------------------------ API pública

    def get_profile(self, username: str) -> ProfileInfo:
        username = self._username(username)
        with self._ops() as ops:
            resp = ops.http.get_api("/web/search/topsearch/", {"query": username}, referer="/")
            error = _classify(resp, "topsearch")
            if error is not None:
                raise error
            try:
                data = resp.json()
            except ValueError:
                raise InstagramError("contract_changed", "A busca de perfis do Instagram respondeu num formato "
                                     "inesperado.") from None
            users = data.get("users") if isinstance(data, dict) else None
            if not isinstance(users, list):
                raise InstagramError("contract_changed", "A busca de perfis do Instagram mudou de formato.")
            for entry in users:
                user = entry.get("user") if isinstance(entry, dict) else None
                if not isinstance(user, dict):
                    continue
                name = user.get("username")
                if isinstance(name, str) and name.lower() == username.lower():
                    return _profile_from_user(user)
        raise InstagramError("profile_not_found")

    def iter_profile_media(
        self,
        username: str,
        content: ContentType,
        limit: int | None,
        *,
        cancel: threading.Event | None = None,
        resume: ListingCheckpoint | None = None,
        on_checkpoint: Callable[[ListingCheckpoint], None] | None = None,
    ) -> Iterator[RemoteMedia]:
        username = self._username(username)
        if content not in CONTENT_TYPES:
            raise InstagramError("invalid_input", "Tipo de conteúdo inválido.")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
            raise InstagramError("invalid_input", "Limite inválido.")
        if resume is not None and not isinstance(resume, ListingCheckpoint):
            raise InstagramError("invalid_input", "Ponto de retomada da listagem inválido.")
        # ``limit=None`` ("Todos"): até o fim real do perfil, sem teto de quantidade nem de páginas.
        return self._iter_media(username, content, limit, cancel, resume, on_checkpoint)

    def resolve_post(self, shortcode: str, *, cancel: threading.Event | None = None) -> list[RemoteMedia]:
        """Bootstrap (cache do app) + 1 POST da consulta de post isolado. A consulta NÃO foi verificada no
        Instagram real (queries.POST_BY_SHORTCODE; doc_id e campo raiz trocáveis por setting/env).

        Devolve TODAS as mídias da publicação (carrossel inteiro, na ordem), sem filtro de conteúdo, com
        ``listing_ref=None`` e ``source_profile`` = dono da publicação ("" se ausente). Cada post conta como uma
        página no teto global por hora. Cancelado antes do POST -> lista vazia (quem chama confere o cancelamento).
        Sem mídia: só tipo não suportado -> ``unsupported_media``; objeto sem código de publicação ->
        ``contract_changed``; demais (itens sem URL utilizável) -> ``content_unavailable``.
        Resposta cujo shortcode não é EXATAMENTE o pedido (outra publicação, ou só maiúsculas diferentes) ->
        ``contract_changed`` com detail ``post_mismatch``, sem devolver mídia nenhuma."""
        try:
            shortcode = validate_shortcode(shortcode)
        except ValueError as exc:
            raise InstagramError("invalid_input", str(exc)) from None
        from app.integrations.instagram.web.errors import InstagramWebError
        from app.integrations.instagram.web.normalize import NormalizeStats, normalize_post, post_code

        if cancel is not None and cancel.is_set():
            return []
        with self._ops(cancel, shared=True) as ops:
            ops.bootstrap.get()
            if cancel is not None and cancel.is_set():
                return []
            wait_page = getattr(self._gate, "wait_listing_page", None)
            if wait_page is not None:
                per_hour = page_rate_limit(self._env)
                if per_hour > 0:
                    wait_page(cancel, max_per_hour=per_hour)
                    if cancel is not None and cancel.is_set():
                        return []
            root = ops.graphql.post_by_shortcode(shortcode)
        answered = post_code(root)
        if answered is not None and answered != shortcode:
            # Resposta de OUTRA publicação (comparação exata, diferenciando maiúsculas): nunca vira mídia nem renova
            # URL. Resposta sem shortcode nenhum segue para normalize_post, que não devolve mídia (abaixo:
            # `post_shape_unknown`). Nenhum dos dois shortcodes vai para o log nem para o diagnóstico.
            del root
            log.warning("Publicação resolvida (web): a resposta é de outra publicação; descartada")
            raise InstagramWebError(
                "contract_changed", "O Instagram respondeu com outra publicação, diferente da pedida. Nada foi "
                "importado deste link.", stage="normalize", diagnostic={"detail": "post_mismatch"})
        stats = NormalizeStats()
        items = normalize_post(root, stats=stats)
        del root
        log.info("Publicação resolvida (web): %d mídias, %d sem URL, %d não suportadas", len(items),
                 stats.skipped_no_url, stats.skipped_unsupported)
        if items:
            return items
        diagnostic = {"skipped_unsupported": stats.skipped_unsupported, "skipped_no_url": stats.skipped_no_url}
        if stats.skipped_unsupported and not stats.skipped_no_url:
            raise InstagramWebError(
                "unsupported_media", "A publicação só tem mídia de um tipo que o Media Tool ainda não sabe baixar.",
                stage="normalize", diagnostic={**diagnostic, "detail": "unsupported_media"})
        if stats.skipped_invalid_node:
            # Objeto sem código de publicação: a resposta veio num formato que o Media Tool não reconhece.
            raise InstagramWebError(
                "contract_changed", "O Instagram mudou a forma de responder e o Media Tool precisa ser atualizado.",
                stage="normalize", diagnostic={**diagnostic, "detail": "post_shape_unknown"})
        raise InstagramWebError(
            "content_unavailable", "A publicação não trouxe nenhuma foto ou vídeo que possa ser baixado.",
            stage="normalize", diagnostic={**diagnostic, "detail": "post_no_media"})

    def download(
        self,
        item: RemoteMedia,
        dest_dir: Path,
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        from app.services.cdn_downloader import DownloadError

        cdn = self._cdn_downloader
        if cdn is None:
            from app.services.cdn_downloader import cdn_download as cdn

        # Sem renovação interna: URL recusada pelo CDN (403/404) -> media_url_expired; a pipeline decide renovar a
        # página (refresh_page). Cancelamento (DownloadError "cancelled") sobe como está.
        try:
            result = cdn(item.media_url, Path(dest_dir), max_bytes=self._max_download_bytes, cancel=cancel,
                         progress=progress)
        except DownloadError as exc:
            if exc.code in _EXPIRED_CODES and cancel is not None and cancel.is_set():
                raise   # cancelado: sobe como está (sem media_url_expired, que levaria a renovar a página)
            raise _download_error(exc) from None
        return Path(result.path)

    def refresh_page(
        self, profile: str, ref: ListingRef, *, cancel: threading.Event | None = None
    ) -> list[RemoteMedia]:
        """Bootstrap (cache da operação) + 1 POST da página ``ref`` (after=ref.cursor, count=ref.page_size).

        Devolve as mídias normalizadas da página (sem filtro de conteúdo), cada uma com ``listing_ref=ref``.
        Cancelado antes do POST -> lista vazia (quem chama confere o cancelamento)."""
        profile = self._username(profile)
        if not isinstance(ref, ListingRef) or isinstance(ref.page_size, bool) or not isinstance(ref.page_size, int) \
                or ref.page_size <= 0 or not (ref.cursor is None or isinstance(ref.cursor, str)):
            raise InstagramError("invalid_input", "Referência de listagem inválida.")
        from app.integrations.instagram.web.normalize import normalize_node

        if cancel is not None and cancel.is_set():
            return []
        items: list[RemoteMedia] = []
        with self._ops(cancel, shared=True) as ops:
            ops.bootstrap.get()
            if cancel is not None and cancel.is_set():
                return []
            for node in ops.graphql.profile_page(profile, after=ref.cursor, count=ref.page_size):
                for item in normalize_node(node, profile=profile):
                    item.listing_ref = ref
                    items.append(item)
        log.info("Página %d da listagem renovada: %d mídias", ref.page_index, len(items))
        return items

    def test_session(self) -> SessionTestResult:
        """Sessão presente -> bootstrap (1 GET) -> GraphQL count=1 do perfil público TEST_PROFILE. Sem download;
        nunca lança. Mensagens sem jargão; detalhe técnico só em `detail` e nos logs.

        O viewer do HTML inicial não conta (pode vir "0" com a sessão logada). A conta é confirmada pela resposta
        GraphQL: ``xdt_viewer.user.id`` igual ao ``ds_user_id`` dos cookies -> reconhecida; diferente -> outra conta
        (``session_recognized`` False); ausente -> não confirmada (None). Com a listagem funcionando, ``result`` é
        ``valid`` nos três casos (a operação pode ser feita); a mensagem diz o que não foi confirmado."""
        from app.services.instagram_gate import InstagramPaused

        started = time.monotonic()

        def finish(result: str, code: str, message: str, *, present: bool, bootstrap: str, graphql: str,
                   recognized: bool | None, listing: str, username: str | None = None,
                   error: InstagramError | None = None) -> SessionTestResult:
            diagnostic = getattr(error, "diagnostic", None) or {}
            status = diagnostic.get("status") if isinstance(diagnostic, dict) else None
            detail = diagnostic.get("detail") if isinstance(diagnostic, dict) else None
            return SessionTestResult(
                ok=result == "valid", code=code, message=message, username=username, result=result,
                http_status=status if isinstance(status, int) and not isinstance(status, bool) else None,
                duration_ms=int((time.monotonic() - started) * 1000),
                detail=detail if isinstance(detail, str) else None,
                session_recognized=recognized, listing_api=listing,
                session_present=present, bootstrap=bootstrap, graphql=graphql,
            )

        session = self._provider.get()
        if session is None or not session.cookies().get("sessionid"):
            return finish("session_missing", "session_missing",
                          "Nenhuma sessão do Instagram configurada. Salve um sessionid em Configurações e teste de "
                          "novo.", present=False, bootstrap="unknown", graphql="unknown", recognized=None,
                          listing="unknown")
        ops = None
        stage = "bootstrap"
        try:
            ops = self._build_ops(session)
            ops.bootstrap.get()
            stage = "graphql"
            posts = ops.graphql.iter_profile_posts(TEST_PROFILE, max_media=1, page_size=1, max_pages=1)
            try:
                next(iter(posts), None)
            finally:
                close = getattr(posts, "close", None)
                if close is not None:
                    close()
            viewer = _norm_id(getattr(ops.graphql, "last_viewer_id", None))
            account_id = getattr(ops.http, "account_id", None)
            account = _norm_id(account_id() if callable(account_id) else None)
            confirmation = _account_confirmation(viewer, account)
        except InstagramError as exc:
            return self._test_failure(finish, stage, exc)
        except InstagramPaused:
            raise   # recusa do portão (pausa publicada): quem chamou trata como pausa, não como erro do teste
        except Exception:  # noqa: BLE001 - o teste nunca lança
            log.exception("Erro inesperado no teste de sessão (provider web, etapa %s)", stage)
            return finish("instagram_error", "internal_error",
                          "Erro inesperado ao testar a sessão. Consulte o log do aplicativo.", present=True,
                          bootstrap="failed" if stage == "bootstrap" else "ok",
                          graphql="unknown" if stage == "bootstrap" else "failed", recognized=None,
                          listing="unknown")
        finally:
            if ops is not None:
                _close(ops)
        if confirmation == "confirmed":
            recognized: bool | None = True
            message = VALID_SESSION_MESSAGE
        elif confirmation == "different_account":
            recognized = False
            message = OTHER_ACCOUNT_MESSAGE
            log.warning("Teste de sessão (web): a resposta identificou uma conta diferente da sessão salva")
        else:
            recognized = None
            message = UNCONFIRMED_SESSION_MESSAGE
            log.info("Teste de sessão (web): conta não confirmada pela resposta (viewer %s, ds_user_id %s)",
                     "presente" if viewer else "ausente", "presente" if account else "ausente")
        return finish("valid", "ok", message, present=True, bootstrap="ok", graphql="ok",
                      recognized=recognized, listing="ok")

    # ------------------------------------------------------------------ internos

    @staticmethod
    def _test_failure(finish: Callable[..., SessionTestResult], stage: str, exc: InstagramError) -> SessionTestResult:
        code = exc.code
        result = _TEST_RESULTS.get(code, "instagram_error")
        # False só com evidência confiável (login/require_login/sessionid apagado/token recusado após o re-bootstrap);
        # nas demais falhas a conta não foi confirmada (o viewer do HTML inicial não prova nada).
        recognized: bool | None = False if code in _NOT_RECOGNIZED else None
        if code == "rate_limited":
            listing = "rate_limited"
        elif code in ("access_refused", "action_blocked") and stage == "graphql":
            listing = "refused"
        else:
            listing = "unknown"
        if stage == "bootstrap":
            if code in _NOT_RECOGNIZED:
                prefix = "Sessão presente, mas o Instagram não a reconheceu ao abrir a página inicial."
            else:
                prefix = ("Sessão presente, mas não foi possível abrir a página inicial do Instagram para confirmar "
                          "se ela é reconhecida; a consulta de publicações não foi testada.")
            return finish(result, code, f"{prefix} Motivo: {exc.message}", present=True, bootstrap="failed",
                          graphql="unknown", recognized=recognized, listing=listing, error=exc)
        if code in _NOT_RECOGNIZED:
            prefix = "Sessão presente, mas o Instagram recusou a autenticação ao consultar publicações."
        else:
            prefix = "Sessão presente, mas a consulta de publicações falhou."
        diagnostic = getattr(exc, "diagnostic", None) or {}
        graphql = "ok" if isinstance(diagnostic, dict) and diagnostic.get("detail") in _LISTING_SHAPE_DETAILS             else "failed"
        return finish(result, code, f"{prefix} Motivo: {exc.message}", present=True, bootstrap="ok",
                      graphql=graphql, recognized=recognized, listing=listing, error=exc)

    @staticmethod
    def _username(username: str) -> str:
        try:
            return parse_profile_input(username)
        except ValueError as exc:
            raise InstagramError("invalid_input", str(exc)) from None

    def _listing_delay(self, page_delay: tuple[float, float] | None) -> tuple[float, float] | None:
        delay_for = getattr(self._gate, "listing_page_delay", None)
        return delay_for(page_delay) if delay_for is not None else page_delay

    def _build_ops(self, session: InstagramSession, cancel: threading.Event | None = None, *,
                   shared: bool = False) -> _Ops:
        http = self._http_factory(session)
        if cancel is not None and hasattr(http, "cancel"):
            http.cancel = cancel   # a espera pela vaga no portão respeita o cancelamento da operação
        try:
            bootstrap = self._bootstrap_factory(http)
            if shared and self._bootstrap_cache is not None:
                bootstrap = _CachedBootstrap(bootstrap, self._bootstrap_cache, _session_key(session))
            graphql = self._graphql_factory(http, bootstrap)
        except BaseException:
            _close_http(http)
            raise
        return _Ops(http, bootstrap, graphql)

    @contextmanager
    def _ops(self, cancel: threading.Event | None = None, *, shared: bool = False) -> Iterator[_Ops]:
        """``shared``: bootstrap pelo cache do app (listagem e renovação); teste de sessão e prévia não usam."""
        session = self._provider.get()
        if session is None or not session.cookies().get("sessionid"):
            raise InstagramError("session_missing")
        ops = self._build_ops(session, cancel, shared=shared)
        try:
            yield ops
        finally:
            _close(ops)

    def _iter_media(
        self, username: str, content: str, limit: int | None, cancel: threading.Event | None,
        resume: ListingCheckpoint | None, on_checkpoint: Callable[[ListingCheckpoint], None] | None,
    ) -> Iterator[RemoteMedia]:
        """Lista página a página (gerador; nada de respostas/nós/itens acumulados).

        Parada (ordem): quantidade atingida do tipo pedido (imediata; ``limit=None`` nunca para por quantidade) >
        cancelamento > erro (inclusive fim anômalo da paginação, que NÃO é fim do perfil) > fim real do perfil
        (``has_next_page=false``). Páginas sem item compatível com o filtro e rendimento baixo NÃO encerram a
        listagem: ela segue até a quantidade ou o fim real. ``on_checkpoint`` é chamado depois que todos os itens de
        uma página foram entregues; ``resume`` continua de um checkpoint (contadores totais continuam valendo)."""
        from app.integrations.instagram.web.errors import InstagramWebError
        from app.integrations.instagram.web.normalize import (
            DEGRADED_REASONS,
            EXPECTED_REASONS,
            NormalizeStats,
            filter_by_content,
            normalize_node,
        )

        st: dict[str, Any] = {
            "pages": resume.pages if resume else 0,
            "posts_seen": resume.posts_seen if resume else 0,
            "items_compatible": resume.items_compatible if resume else 0,
            "items_skipped_filter": 0,
            "duplicates": 0,
            "stop_reason": None,
        }
        self.last_listing_stats = st
        self.last_listing_anomalies = None
        if cancel is not None and cancel.is_set():
            st["stop_reason"] = "cancelled"
            return
        if resume is not None and not resume.has_next:
            st["stop_reason"] = "nothing_to_resume"
            return
        max_pages = self._max_pages
        stats = NormalizeStats()
        count = 0
        streak = resume.unproductive_streak if resume else 0
        page_compatible = 0
        closed_page = 0   # índice da última página que chegou ao fim (on_page_end): os descartes dela já saíram
        self.last_account_confirmation = None

        def on_page(meta: dict) -> None:
            st["pages"] += 1
            st["duplicates"] += int(meta.get("duplicate_nodes") or 0)
            if self._on_page is not None:
                self._on_page(meta)

        def on_page_end(reason: str | None) -> bool:
            nonlocal streak, page_compatible, closed_page
            streak = 0 if page_compatible else streak + 1
            page_compatible = 0
            st["stop_reason"] = reason
            page = ops.graphql.current_page
            if page is not None:
                closed_page = page.index
            if on_checkpoint is not None and page is not None:
                on_checkpoint(ListingCheckpoint(
                    resume_cursor=page.end_cursor, next_page_index=page.index + 1, pages=st["pages"],
                    posts_seen=st["posts_seen"], items_compatible=st["items_compatible"],
                    # Só o fim real do perfil zera `has_next`; o teto de páginas do trecho (testes/POC) não.
                    unproductive_streak=streak, has_next=reason != "end_of_profile",
                    page_counts=_page_counts(getattr(page, "report", None), DEGRADED_REASONS),
                ))
            return reason is None

        # Teto global de páginas de listagem por hora (seção 9, A2), aplicado no portão antes de cada POST.
        extra: dict[str, Any] = {}
        wait_page = getattr(self._gate, "wait_listing_page", None)
        if wait_page is not None:
            per_hour = page_rate_limit(self._env)
            if per_hour > 0:
                extra["before_page"] = lambda page_cancel: wait_page(page_cancel, max_per_hour=per_hour)
            else:
                global _rate_limit_disabled_logged
                if not _rate_limit_disabled_logged:
                    _rate_limit_disabled_logged = True
                    log.warning("Teto de páginas por hora DESABILITADO por %s=0 (configuração de teste); as demais "
                                   "proteções continuam ativas", ENV_PAGE_RATE_LIMIT)

        completed = False
        with self._ops(cancel, shared=True) as ops:
            ops.bootstrap.get()
            posts = ops.graphql.iter_profile_posts(
                username, max_media=limit, page_size=self._page_size, max_pages=max_pages, cancel=cancel,
                on_page=on_page, start_cursor=resume.resume_cursor if resume else None,
                start_page=resume.next_page_index if resume else 1, on_page_end=on_page_end, **extra,
            )
            ref: ListingRef | None = None
            node_index = 0   # posição do nó entre os nós novos da página corrente
            try:
                for node in posts:
                    if cancel is not None and cancel.is_set():
                        st["stop_reason"] = "cancelled"
                        log.info("Listagem do Instagram (web) cancelada")
                        completed = True
                        return
                    page = getattr(ops.graphql, "current_page", None)
                    if page is not None and (ref is None or ref.page_index != page.index):
                        ref = ListingRef(page_index=page.index, cursor=page.after, page_size=page.count)
                        node_index = 0
                    before = dict(stats.reasons)
                    normalized = normalize_node(node, profile=username, stats=stats)
                    items = filter_by_content(normalized, content)
                    st["posts_seen"] += 1
                    st["items_skipped_filter"] += len(normalized) - len(items)
                    report = getattr(page, "report", None)
                    dropped_so_far: dict[str, int] | None = None
                    if report is not None:
                        # Contadores da página para a linha de log do GraphQL (motivos fixos, sem valores).
                        delta = {k: v - before.get(k, 0) for k, v in stats.reasons.items() if v != before.get(k, 0)}
                        skipped = {k: v for k, v in delta.items() if k in EXPECTED_REASONS}
                        skipped["content_filter"] = len(normalized) - len(items)
                        edge = report.fresh_edges[node_index] if node_index < len(report.fresh_edges) else None
                        report.add(len(normalized), {k: v for k, v in delta.items() if k not in EXPECTED_REASONS},
                                   skipped, edge)
                        if items and report.events:
                            dropped_so_far = _dropped_until(report.events, edge, DEGRADED_REASONS)
                    node_index += 1
                    del normalized
                    for item in items:
                        item.listing_ref = ref
                        item.listing_dropped = dropped_so_far
                        count += 1   # antes do yield: conta o item mesmo se o consumidor fechar o gerador nele
                        st["items_compatible"] += 1
                        page_compatible += 1
                        yield item
                        if limit is not None and count >= limit:
                            st["stop_reason"] = "limit_reached"
                            completed = True
                            return
                if cancel is not None and cancel.is_set():
                    st["stop_reason"] = "cancelled"
                # Só quando NENHUM item suportado foi normalizado (antes do filtro de conteúdo); se o filtro
                # esvaziou a lista de itens suportados, a listagem vazia é o resultado normal.
                if stats.items == 0 and stats.skipped_unsupported and not (cancel is not None and cancel.is_set()):
                    raise InstagramWebError(
                        "unsupported_media",
                        "O perfil só trouxe publicações de um tipo que o Media Tool ainda não sabe baixar.",
                        stage="normalize",
                        diagnostic={"detail": "unsupported_media", "skipped_unsupported": stats.skipped_unsupported,
                                    "nodes": stats.nodes},
                    )
                completed = True
            except GeneratorExit:
                if st["stop_reason"] is None:
                    st["stop_reason"] = "cancelled" if cancel is not None and cancel.is_set() else "consumer_closed"
                raise
            except BaseException as exc:
                st["stop_reason"] = "error"
                # Erro numa página que não chegou ao checkpoint (ex.: paginação quebrada depois de entregar os nós):
                # os descartes inesperados dela, até o último nó consumido, vão junto do erro (`listing_dropped`)
                # para quem lista registrar; sem isso um descarte sem item entregue depois dele ficaria invisível.
                page = getattr(ops.graphql, "current_page", None)
                report = getattr(page, "report", None)
                if report is not None and report.events and page.index != closed_page:
                    if ref is None or ref.page_index != page.index:
                        consumed = 0   # nenhum nó desta página chegou aqui
                    else:
                        consumed = node_index
                    if consumed >= len(report.fresh_edges):
                        edge = None    # página consumida até o fim: todos os descartes dela
                    else:
                        edge = report.fresh_edges[consumed - 1] if consumed else -1
                    pending = _dropped_until(report.events, edge, DEGRADED_REASONS)
                    if pending:
                        try:
                            exc.listing_dropped = pending
                        except Exception:  # noqa: BLE001 - exceção sem atributos: fica só no log da página
                            pass
                raise
            finally:
                if completed and st["stop_reason"] is None:
                    st["stop_reason"] = "end_of_profile"
                close = getattr(posts, "close", None)
                if close is not None:
                    close()
                self.last_account_confirmation = _listing_confirmation(ops)
                log.info(
                    "Listagem web de %s: %s posts, %s mídias normalizadas, %s entregues, %s sem URL, "
                    "%s não suportadas, conta %s", username, stats.nodes, stats.items, count, stats.skipped_no_url,
                    stats.skipped_unsupported, self.last_account_confirmation,
                )
                log.info(
                    "Listagem web: pages=%s posts_seen=%s items_compatible=%s items_skipped_filter=%s duplicates=%s "
                    "stop_reason=%s", st["pages"], st["posts_seen"], st["items_compatible"],
                    st["items_skipped_filter"], st["duplicates"], st["stop_reason"],
                )
                self.last_listing_anomalies = anomalies = _listing_anomalies(ops, stats.reasons, EXPECTED_REASONS,
                                                                                 st)
                if anomalies["pages_with_errors"] or anomalies["dropped_unexpected"]:
                    log.warning(
                        "Listagem web (este trecho): pages_with_errors=%s errors=%s "
                        "dropped_unexpected=%s skipped_expected=%s",
                        anomalies["pages_with_errors"], anomalies["errors"],
                        _counts(anomalies["dropped_unexpected"]), _counts(anomalies["skipped_expected"]),
                    )


def _counts(counts: Mapping[str, int]) -> str:
    return "{" + ",".join(f"{reason}:{counts[reason]}" for reason in sorted(counts)) + "}"


def _page_counts(report: Any, degraded: frozenset[str]) -> dict[str, dict[str, int]] | None:
    """Descartes da página para o checkpoint (ListingCheckpoint.page_counts): ``unexpected`` (item perdido),
    ``degraded`` (entregue em versão menor) e ``expected`` (filtro de propósito). None se não houve nenhum."""
    if report is None:
        return None
    counts = {
        "unexpected": {k: v for k, v in report.dropped.items() if v and k not in degraded},
        "degraded": {k: v for k, v in report.dropped.items() if v and k in degraded},
        "expected": {k: v for k, v in report.skipped.items() if v},
    }
    return {name: values for name, values in counts.items() if values} or None


def _dropped_until(events: list[tuple[int, str, int]], edge: int | None,
                   degraded: frozenset[str]) -> dict[str, int] | None:
    """Itens perdidos (motivo -> quantidade) da página até o edge ``edge``, inclusive (RemoteMedia.listing_dropped)."""
    out: dict[str, int] = {}
    for position, reason, amount in events:
        if reason not in degraded and (edge is None or position <= edge):
            out[reason] = out.get(reason, 0) + amount
    return out or None


def _listing_anomalies(ops: _Ops, reasons: Mapping[str, int], expected: frozenset[str],
                       st: Mapping[str, Any]) -> dict:
    """Totais seguros do trecho: páginas com `errors`, erros e descartes por motivo (GraphQL + normalização)."""
    totals = getattr(ops.graphql, "listing_totals", None)
    totals = totals if isinstance(totals, dict) else {}
    dropped = dict(totals.get("dropped") or {})
    skipped: dict[str, int] = {}
    for reason, amount in reasons.items():
        target = skipped if reason in expected else dropped
        target[reason] = target.get(reason, 0) + amount
    for reason, key in (("duplicate", "duplicates"), ("content_filter", "items_skipped_filter")):
        if st.get(key):
            skipped[reason] = int(st[key])
    return {"pages_with_errors": int(totals.get("pages_with_errors") or 0), "errors": int(totals.get("errors") or 0),
            "dropped_unexpected": dropped, "skipped_expected": skipped}


def _norm_id(value: object) -> str | None:
    """Id de conta como texto sem espaços; vazio/bool/não escalar -> None (comparação viewer x ds_user_id)."""
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def _account_confirmation(viewer: object, account: object) -> str:
    from app.integrations.instagram.web.graphql import account_confirmation

    return account_confirmation(viewer, account)


def _listing_confirmation(ops: _Ops) -> str:
    """Estado da conta na 1a resposta da listagem; sem ela (falha na 1a página ou graphql de teste), usa o
    ``last_viewer_id`` da última resposta."""
    state = getattr(ops.graphql, "listing_account_confirmation", None)
    if isinstance(state, str):
        return state
    account_id = getattr(ops.http, "account_id", None)
    return _account_confirmation(getattr(ops.graphql, "last_viewer_id", None),
                                 account_id() if callable(account_id) else None)


def _profile_from_user(user: dict[str, Any]) -> ProfileInfo:
    def opt_bool(key: str) -> bool | None:
        value = user.get(key)
        return value if isinstance(value, bool) else None

    user_id = user.get("pk") or user.get("pk_id") or user.get("id")
    full_name = user.get("full_name")
    avatar = user.get("profile_pic_url")
    count = user.get("media_count")
    return ProfileInfo(
        username=str(user["username"]),
        user_id=str(user_id) if user_id not in (None, "") else None,
        full_name=full_name if isinstance(full_name, str) and full_name else None,
        avatar_url=avatar if isinstance(avatar, str) and avatar.startswith("https://") else None,
        post_count=count if isinstance(count, int) and not isinstance(count, bool) else None,
        is_private=opt_bool("is_private"),
        is_verified=opt_bool("is_verified"),
    )


def _download_error(exc: Exception) -> Exception:
    code = getattr(exc, "code", None)
    if code == "cancelled":
        return exc
    if code == "network_error":
        return InstagramError("download_failed", "Falha de rede ao baixar a mídia.", retryable=True)
    if code in _EXPIRED_CODES:
        from app.integrations.instagram.web.errors import InstagramWebError

        return InstagramWebError("media_url_expired", "O servidor de mídia recusou o link temporário (expirado).",
                                 retryable=False, stage="download",
                                 diagnostic={"detail": "http_403" if code == "forbidden" else "http_404"})
    if code == "too_large":
        return InstagramError("download_failed", "Arquivo de mídia maior que o limite permitido.", retryable=False)
    if code == "invalid_media":
        return InstagramError("download_failed", "O arquivo baixado não é uma mídia válida.", retryable=False)
    if code == "invalid_url":
        return InstagramError("download_failed", "Link de mídia recusado pela validação de segurança.", retryable=False)
    return InstagramError("download_failed", retryable=False)


def _close_http(http: Any) -> None:
    close = getattr(http, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001
            log.debug("Falha ao fechar o cliente HTTP do Instagram Web", exc_info=True)


def _close(ops: _Ops) -> None:
    _close_http(ops.http)
