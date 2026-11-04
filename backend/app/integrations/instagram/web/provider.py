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


def _default_public_http(config: Any, transport: Any, gate: Any = None) -> Any:
    """Cliente ANÔNIMO (sem cookies do dono) para o documento público de uma publicação."""
    from app.integrations.instagram.web.http import InstagramWebHttp, WebHttpConfig

    return InstagramWebHttp.anonymous(config or WebHttpConfig(), transport=transport, gate=gate)


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


POST_DOCUMENT_STAGE = "post_document"
# 2a tentativa de um link restrito: o MESMO documento da publicação aberto com a sessão do dono (nunca GraphQL/POST).
SESSION_DOCUMENT_STAGE = "session_document"
SESSION_DOCUMENT_VIA = "session_document"
POST_PATH_KINDS = ("p", "reel")
# Redirect seguido na 2a tentativa (só /p/): Location relativo ou exatamente nestas origens.
_SAME_SITE_ORIGINS = ("", "https://www.instagram.com", "https://instagram.com")
_MSG_POST_ENVELOPE_MISSING = ("O Instagram abriu a página da publicação, mas sem os dados dela no formato que o "
                              "Media Tool conhece. Nada foi importado deste link.")
_MSG_POST_GATED = ("A publicação exige login ou está restrita, e não há sessão do Instagram salva para tentar "
                   "abri-la. Salve a sessão em Configurações e importe este link de novo.")
_MSG_POST_GATED_WITH_SESSION = ("A publicação continua indisponível mesmo com a sessão: pode ser de conta privada que "
                                "você não segue, removida ou restrita por região/idade.")
_MSG_POST_GATED_SESSION_OFF = ("A publicação exige login ou está restrita. Nesta importação o Instagram recusou a "
                               "tentativa com a sessão salva (ou ela falhou), então o Media Tool não usa mais a "
                               "sessão para os links restritos deste lote. Para tentar de novo com a sessão, importe "
                               "este link em uma nova importação.")


class InstagramWebDownloader(InstagramDownloader):
    # `resolve_post` lê o documento público sem a sessão do dono; só um link restrito ganha a 2a tentativa com a
    # sessão. Quem chama não registra sucesso de autenticação por uma publicação entregue.
    resolve_post_authenticated: bool = False

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
        public_http_factory: Callable[[], Any] | None = None,
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
        # Cliente anônimo de `resolve_post` (jar vazio; mesmo portão): nenhum cookie da sessão salva viaja nele.
        self._public_http_factory = public_http_factory or (
            lambda: _default_public_http(http_config, transport, request_gate))
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
        min_views: int | None = None,
        posted_after: str | None = None,
        max_views: int | None = None,
        on_post: Callable[[dict], None] | None = None,
    ) -> Iterator[RemoteMedia]:
        username = self._username(username)
        if content not in CONTENT_TYPES:
            raise InstagramError("invalid_input", "Tipo de conteúdo inválido.")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
            raise InstagramError("invalid_input", "Limite inválido.")
        if resume is not None and not isinstance(resume, ListingCheckpoint):
            raise InstagramError("invalid_input", "Ponto de retomada da listagem inválido.")
        if min_views is not None and (isinstance(min_views, bool) or not isinstance(min_views, int) or min_views < 1):
            raise InstagramError("invalid_input", "Mínimo de visualizações inválido.")
        if max_views is not None and (isinstance(max_views, bool) or not isinstance(max_views, int) or max_views < 1
                                      or (min_views is not None and max_views < min_views)):
            raise InstagramError("invalid_input", "Máximo de visualizações inválido.")
        cutoff = None if posted_after is None else _cutoff_timestamp(posted_after)
        if posted_after is not None and cutoff is None:
            raise InstagramError("invalid_input", "Corte do período de postagem inválido.")
        # ``limit=None`` ("Todos"): até o fim real do perfil, sem teto de quantidade nem de páginas.
        return self._iter_media(username, content, limit, cancel, resume, on_checkpoint, min_views, cutoff,
                                max_views, on_post)

    def resolve_post(self, shortcode: str, *, cancel: threading.Event | None = None,
                     path_kind: str | None = None, use_session: bool = True) -> list[RemoteMedia]:
        """UM GET ANÔNIMO do documento público da publicação (``/p/<código>/`` ou ``/reel/<código>/``, conforme
        ``path_kind``, que vem do link de quem chama) + leitura do envelope embutido (web/post_document.py). O cliente
        é criado sem cookies e descartado ao fim. Redirect nunca é seguido.

        Publicação restrita no documento público (``gated``), e SÓ nesse caso: UMA 2a tentativa abrindo o MESMO
        documento com a sessão salva, pelo cliente autenticado da importação por perfil (``http_factory``: mesmos
        headers de navegador, mesmo portão, mesmo teto por hora), lido pelo MESMO parser. Nunca GraphQL/POST. Só
        acontece com ``use_session`` (quem chama passa False quando a sessão já foi recusada no lote), sessão salva
        com ``sessionid`` e portão sem pausa publicada (pausa -> ``InstagramPaused`` sem requisição). Erros dessa
        tentativa têm ``diagnostic["via"] = "session_document"``; os do GET (resposta ou transporte: recusa, 429,
        checkpoint, login, 401/403, rede) têm stage ``session_document`` e seguem ``classify_session_document`` (a
        classificação da importação por perfil, sem nova tentativa). ``session_off = True`` na exceção (quem chama
        desliga a sessão no resto do lote): esses erros do GET (menos o 404) e as falhas de formato da leitura
        (``post_envelope_missing``, ``post_gated_with_session``, ``post_mismatch``, ``post_shape_unknown``).

        VERIFICADO no Instagram real (2026-10-10, sem login): UM Reel público em ``/reel/<código>/``.
        NÃO VERIFICADO: foto, carrossel, o caminho ``/p/``, publicação restrita (forma de ``gating_ruling``), o
        documento aberto com a sessão (se traz o mesmo envelope; formato diferente falha como
        ``post_envelope_missing``), qual ``type`` de ``video_versions`` é a melhor qualidade (as versões vêm sem
        largura/altura; fica a primeira com URL https), o host do CDN das URLs sem login (fora de
        ``.cdninstagram.com``/``.fbcdn.net`` o download falha com ``invalid_url``) e a qualidade de foto sem login (a
        capa do Reel veio com no máximo 640 px de largura).

        Devolve TODAS as mídias da publicação (carrossel inteiro, na ordem), sem filtro de conteúdo, com
        ``listing_ref=None`` e ``source_profile`` = dono da publicação ("" se ausente). Cada GET conta como uma
        página no teto global por hora. Cancelado antes de um GET -> lista vazia (quem chama confere o cancelamento).
        ``path_kind`` fora de "p"/"reel" -> ``invalid_input`` sem requisição (nunca há tipo padrão).
        Erros do documento público (stage ``post_document``): redirect/401/403 -> ``access_refused``; 429 ->
        ``rate_limited``; 404 -> ``content_unavailable``; documento sem o envelope -> ``contract_changed`` com detail
        ``post_envelope_missing``; nunca código de sessão. Publicação restrita -> ``content_unavailable`` com
        ``post_gated_logged_out`` (sem sessão salva), ``post_gated_session_off`` (``use_session=False``) ou
        ``post_gated_with_session`` (continua restrita no documento com a sessão).
        Sem mídia: só tipo não suportado -> ``unsupported_media``; objeto sem código de publicação ->
        ``contract_changed``; demais (itens sem URL utilizável) -> ``content_unavailable``.
        Resposta cujo shortcode não é EXATAMENTE o pedido (outra publicação, ou só maiúsculas diferentes) ->
        ``contract_changed`` com detail ``post_mismatch``, sem devolver mídia nenhuma."""
        try:
            shortcode = validate_shortcode(shortcode)
        except ValueError as exc:
            raise InstagramError("invalid_input", str(exc)) from None
        if path_kind not in POST_PATH_KINDS:
            raise InstagramError("invalid_input", "Tipo do link da publicação desconhecido (esperado /p/ ou /reel/).")
        from app.integrations.instagram.web.errors import InstagramWebError, classify_public_document
        from app.integrations.instagram.web.normalize import NormalizeStats, normalize_post, post_code

        if cancel is not None and cancel.is_set():
            return []
        safe_path = f"/{path_kind}/"   # token fixo para log/diagnóstico: o path real carrega o shortcode
        fetched = self._fetch_post_document(self._public_http_factory(), shortcode, path_kind, cancel,
                                            classify_public_document, POST_DOCUMENT_STAGE)
        if fetched is None:
            return []
        root, parse, _ = fetched
        via = "public_document"
        stage = POST_DOCUMENT_STAGE
        log.info("Publicação (documento público): outcome=%s scripts=%d envelopes=%d preloader_observado=%s "
                 "typename=%s", parse.outcome, parse.scripts, parse.envelopes, parse.preloader_observed,
                 parse.typename or "-")
        if parse.outcome == "gated":
            # Forma NÃO verificada no real: `gating_ruling` não nulo ou a mídia ausente no envelope do shortcode.
            del root
            fetched = self._session_post_document(shortcode, path_kind, cancel, use_session)
            if fetched is None:
                return []
            root, parse, safe_path = fetched   # diagnóstico: o caminho realmente pedido (ex.: /reels/)
            via = SESSION_DOCUMENT_VIA
            stage = SESSION_DOCUMENT_STAGE
            log.info("Publicação (documento com a sessão): outcome=%s scripts=%d envelopes=%d "
                     "preloader_observado=%s typename=%s via=%s", parse.outcome, parse.scripts, parse.envelopes,
                     parse.preloader_observed, parse.typename or "-", via)
        extra_diag = {"via": via} if via == SESSION_DOCUMENT_VIA else {}

        def fail(error: InstagramWebError) -> InstagramWebError:
            # Falha de formato do documento aberto com a sessão: a sessão é desligada no resto do lote já na 1a.
            if extra_diag:
                error.session_off = True
            return error

        mismatch = parse.outcome == "mismatch"
        if parse.outcome in ("no_scripts", "envelope_missing"):
            raise fail(InstagramWebError(
                "contract_changed", _MSG_POST_ENVELOPE_MISSING, stage=stage,
                diagnostic={"detail": "post_envelope_missing", "path": safe_path, "scripts": parse.scripts,
                            "envelopes": parse.envelopes, **extra_diag}))
        if parse.outcome == "gated":
            # Só chega aqui o documento aberto com a sessão (o público restrito foi tratado acima).
            raise fail(InstagramWebError(
                "content_unavailable", _MSG_POST_GATED_WITH_SESSION, stage=stage,
                diagnostic={"detail": "post_gated_with_session", "path": safe_path, **extra_diag}))
        if not mismatch:
            answered = post_code(root)
            mismatch = answered is not None and answered != shortcode   # defesa dupla (já conferido na leitura)
        if mismatch:
            # Resposta de OUTRA publicação (comparação exata, diferenciando maiúsculas): nunca vira mídia nem renova
            # URL. Resposta sem shortcode nenhum segue para normalize_post, que não devolve mídia (abaixo:
            # `post_shape_unknown`). Nenhum dos dois shortcodes vai para o log nem para o diagnóstico.
            del root
            log.warning("Publicação resolvida (web): a resposta é de outra publicação; descartada")
            raise fail(InstagramWebError(
                "contract_changed", "O Instagram respondeu com outra publicação, diferente da pedida. Nada foi "
                "importado deste link.", stage="normalize", diagnostic={"detail": "post_mismatch", **extra_diag}))
        stats = NormalizeStats()
        items = normalize_post(root, stats=stats)
        del root
        log.info("Publicação resolvida (web): %d mídias, %d sem URL, %d não suportadas, dash=%s dash_escolhido=%d "
                 "via=%s", len(items), stats.skipped_no_url, stats.skipped_unsupported,
                 ",".join(f"{outcome}:{count}" for outcome, count in sorted(stats.dash.items())) or "-",
                 stats.dash_chosen, via)
        if items:
            return items
        diagnostic = {"skipped_unsupported": stats.skipped_unsupported, "skipped_no_url": stats.skipped_no_url,
                      **extra_diag}
        if stats.skipped_unsupported and not stats.skipped_no_url:
            raise InstagramWebError(
                "unsupported_media", "A publicação só tem mídia de um tipo que o Media Tool ainda não sabe baixar.",
                stage="normalize", diagnostic={**diagnostic, "detail": "unsupported_media"})
        if stats.skipped_invalid_node:
            # Objeto sem código de publicação: a resposta veio num formato que o Media Tool não reconhece.
            raise fail(InstagramWebError(
                "contract_changed", "O Instagram mudou a forma de responder e o Media Tool precisa ser atualizado.",
                stage="normalize", diagnostic={**diagnostic, "detail": "post_shape_unknown"}))
        raise InstagramWebError(
            "content_unavailable", "A publicação não trouxe nenhuma foto ou vídeo que possa ser baixado.",
            stage="normalize", diagnostic={**diagnostic, "detail": "post_no_media"})

    def _fetch_post_document(self, http: Any, shortcode: str, path_kind: str, cancel: threading.Event | None,
                             classify: Callable[..., Any], stage: str, parser: Callable[..., Any] | None = None,
                             follow_kinds: tuple[str, ...] = ()) -> tuple[Any, Any] | None:
        """GET de ``/<path_kind>/<shortcode>/`` pelo cliente ``http`` (fechado ao fim), depois do teto por hora,
        classificado por ``classify`` e lido por ``parser`` (padrão ``parse_post_document``) -> (nó, leitura, token do
        caminho realmente pedido). None = cancelado antes de um GET. ``follow_kinds``: redirect (sem ``sessionid`` apagado) cujo caminho é ``/<tipo>/<MESMO código>/``
        de um desses tipos, com ``Location`` relativo ou em ``https://www.instagram.com``/``https://instagram.com``, é
        seguido UMA vez (2o GET, de novo pelo teto e pelo portão); qualquer outro redirect vai
        para ``classify``. Vazio (o documento público) = redirect nunca é seguido."""
        from app.integrations.instagram.web.errors import InstagramWebError
        from app.integrations.instagram.web.post_document import MAX_DOCUMENT_BYTES, parse_post_document

        parser = parser or parse_post_document
        path = f"/{path_kind}/{shortcode}/"
        safe_path = f"/{path_kind}/"
        targets = {f"/{kind}/{shortcode}/": f"/{kind}/" for kind in follow_kinds}
        if cancel is not None and hasattr(http, "cancel"):
            http.cancel = cancel   # a espera pela vaga no portão respeita o cancelamento da operação
        try:
            for hop in range(2 if targets else 1):
                wait_page = getattr(self._gate, "wait_listing_page", None)
                if wait_page is not None:
                    per_hour = page_rate_limit(self._env)
                    if per_hour > 0:
                        wait_page(cancel, max_per_hour=per_hour)
                        if cancel is not None and cancel.is_set():
                            return None
                if hop and cancel is not None and cancel.is_set():
                    return None
                try:
                    resp = http.get_document(path, max_bytes=MAX_DOCUMENT_BYTES, diagnostic_path=safe_path)
                except InstagramWebError as exc:
                    if exc.diagnostic.get("detail") == "document_too_large":
                        exc.stage = stage
                    raise
                same_site = getattr(resp, "location_origin", "") in _SAME_SITE_ORIGINS
                target = (resp.location_path if 300 <= resp.status < 400 and not resp.sessionid_deleted
                          and same_site else None)
                if hop == 0 and target in targets and target != path:
                    log.info("Documento da publicação: redirect para a mesma publicação (%s -> %s) seguido uma vez",
                             safe_path, targets[target])
                    path, safe_path = target, targets[target]
                    del resp
                    continue
                break
            error = classify(resp, stage=stage)
            if error is not None:
                error.diagnostic["path"] = safe_path
                raise error
            root, parse = parser(resp.text, shortcode)
            del resp
            return root, parse, safe_path
        finally:
            _close_http(http)

    def _session_post_document(self, shortcode: str, path_kind: str, cancel: threading.Event | None,
                               use_session: bool) -> tuple[Any, Any] | None:
        """2a tentativa de um link restrito: o documento da publicação com a sessão salva, sem retry. Reel -> direto
        ``/reels/<código>/`` (VERIFICADO 2026-10-29: ``/reel/`` logado responde 302 para ``/reels/``); ``/p/`` (NÃO
        verificado) -> ``/p/<código>/``, seguindo UMA vez o redirect para ``/reels/``, ``/reel/`` ou ``/p/`` do mesmo
        código (no máximo 2 GETs). None = cancelado antes do GET."""
        from app.integrations.instagram.web.errors import InstagramWebError, classify_session_document
        from app.integrations.instagram.web.post_document import parse_session_post_document
        from app.services.instagram_gate import InstagramPaused

        safe_path = f"/{path_kind}/"
        if not use_session:
            log.info("Publicação restrita no documento público; a sessão já foi desligada neste lote e não é usada")
            error = InstagramWebError("content_unavailable", _MSG_POST_GATED_SESSION_OFF, stage=POST_DOCUMENT_STAGE,
                                      diagnostic={"detail": "post_gated_session_off", "path": safe_path})
            error.session_off = True   # consequência da sessão desligada: não conta no disjuntor `links_failing`
            raise error
        if cancel is not None and cancel.is_set():
            return None   # cancelado: a sessão salva nem é lida
        session = self._provider.get()
        if session is None or not session.cookies().get("sessionid"):
            log.info("Publicação restrita no documento público; sem sessão salva, nenhuma 2a tentativa")
            raise InstagramWebError("content_unavailable", _MSG_POST_GATED, stage=POST_DOCUMENT_STAGE,
                                    diagnostic={"detail": "post_gated_logged_out", "path": safe_path})
        paused_scope = getattr(self._gate, "paused_scope", None)
        scope = paused_scope() if paused_scope is not None else None
        if scope is not None:
            # Pausa publicada: nenhuma requisição (o link continua pendente e é pedido de novo depois da pausa).
            del session
            log.info("Publicação restrita no documento público; Instagram em pausa, 2a tentativa não feita")
            raise InstagramPaused(scope)
        log.info("Publicação restrita no documento público; 2a tentativa pelo documento com a sessão (via=%s)",
                 SESSION_DOCUMENT_VIA)
        http = self._http_factory(session)
        del session
        request_kind, follow = ("reels", ()) if path_kind == "reel" else ("p", ("reels", "reel", "p"))
        try:
            return self._fetch_post_document(http, shortcode, request_kind, cancel, classify_session_document,
                                             SESSION_DOCUMENT_STAGE, parse_session_post_document, follow)
        except InstagramWebError as exc:
            exc.stage = SESSION_DOCUMENT_STAGE
            exc.diagnostic["via"] = SESSION_DOCUMENT_VIA
            exc.diagnostic.setdefault("path", f"/{request_kind}/")
            if exc.code != "content_unavailable":
                # Recusa/limite/verificação do Instagram ou falha do transporte: sem nova tentativa; quem chama
                # aplica a política existente e para de usar a sessão no resto do lote.
                exc.session_off = True
            raise

    def download(
        self,
        item: RemoteMedia,
        dest_dir: Path,
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        return self._download_to(item.media_url, dest_dir, cancel, progress)

    def download_url(
        self,
        url: str,
        dest_dir: Path,
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        """UM fluxo separado (DASH) pelo mesmo `cdn_download` e com os mesmos erros de ``download``, menos um: status
        HTTP inesperado da CDN (nem 200 nem 403/404/410, ex.: 206/416) vira ``download_failed`` NÃO retryable, para
        a pipeline usar o MP4 direto em vez de insistir. NUNCA rodou contra o CDN real (o navegador pede ``Range``;
        não se sabe se a CDN entrega o arquivo inteiro num GET simples)."""
        return self._download_to(url, dest_dir, cancel, progress, separate_stream=True)

    def _download_to(self, url: str, dest_dir: Path, cancel: threading.Event | None,
                     progress: Callable[[int, int | None], None] | None, *, separate_stream: bool = False) -> Path:
        from app.services.cdn_downloader import DownloadError

        cdn = self._cdn_downloader
        if cdn is None:
            from app.services.cdn_downloader import cdn_download as cdn

        # Sem renovação interna: URL recusada pelo CDN (403/404) -> media_url_expired; a pipeline decide renovar a
        # página (refresh_page). Cancelamento (DownloadError "cancelled") sobe como está.
        try:
            result = cdn(url, Path(dest_dir), max_bytes=self._max_download_bytes, cancel=cancel, progress=progress)
        except DownloadError as exc:
            if exc.code in _EXPIRED_CODES and cancel is not None and cancel.is_set():
                raise   # cancelado: sobe como está (sem media_url_expired, que levaria a renovar a página)
            if separate_stream and exc.code == "network_error" and getattr(exc, "http_status", None) is not None:
                raise InstagramError("download_failed", "Resposta inesperada do servidor de mídia.",
                                     retryable=False) from None
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
        min_views: int | None = None, cutoff: int | None = None, max_views: int | None = None,
        on_post: Callable[[dict], None] | None = None,
    ) -> Iterator[RemoteMedia]:
        """Lista página a página (gerador; nada de respostas/nós/itens acumulados).

        Parada (ordem): quantidade atingida do tipo pedido (imediata; ``limit=None`` nunca para por quantidade) >
        cancelamento > erro (inclusive fim anômalo da paginação, que NÃO é fim do perfil) > fim real do perfil
        (``has_next_page=false``). Páginas sem item compatível com o filtro e rendimento baixo NÃO encerram a
        listagem: ela segue até a quantidade ou o fim real. ``on_checkpoint`` é chamado depois que todos os itens de
        uma página foram entregues; ``resume`` continua de um checkpoint (contadores totais continuam valendo).

        Filtros da importação por perfil (desligados = nada muda), depois do filtro de conteúdo e nesta ordem:

        ``cutoff`` (período de postagem; epoch UTC): publicação com ``taken_at`` anterior ao corte (ou sem data) não
        vira item ("out_of_period"). A listagem vem da mais nova para a mais antiga: a primeira publicação NÃO fixada
        mais antiga que o corte ENCERRA a listagem (checkpoint com ``has_next=False`` e ``period_end=True``). Fixada
        (``timeline_pinned_user_ids`` não vazio) é pulada sem encerrar. Sem esse campo no nó (não se sabe se é fixada)
        encerra só depois de ``PERIOD_STOP_STREAK`` publicações seguidas mais antigas que o corte (o Instagram fixa no
        máximo 3); publicação dentro do período zera a sequência.

        ``min_views`` (visualizações mínimas): só vídeos com ``play_count`` >= ``min_views`` na aba Reels SEM login
        (web/reels_tab.py, casado pelo código, páginas buscadas sob demanda) são entregues; abaixo ("below"), fora da
        aba Reels ou sem contagem ("no_count") não viram item, e item que não é vídeo também não ("no_count"). Erro da
        aba Reels sobe como erro da listagem (nunca entrega sem o filtro). ``limit`` conta só os aprovados.
        ``max_views`` (máximo, migration 0021; sozinho ou com o mínimo): ``play_count`` > ``max_views`` não vira item
        ("above"); as demais regras são as do mínimo (sem contagem nunca é aprovado).

        ``on_post`` (horário dos posts, migration 0026): chamado UMA vez por publicação vista dentro do período (antes
        do filtro de tipo de mídia e do de visualizações; carrossel conta uma vez) com ``_post_record(node)``."""
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
        # Vereditos dos filtros na página aberta (só totais; zerados no fim de cada página).
        views_on = min_views is not None or max_views is not None
        filters_on = views_on or cutoff is not None
        with_above = max_views is not None
        page_views: dict[str, int] | None = _views_zero(with_above) if filters_on else None
        if filters_on:
            st.update(views_approved=0, views_below=0, views_no_count=0, views_out_of_period=0, views_above=0)
        old_streak = 0   # publicações seguidas mais antigas que o corte sem saber se são fixadas
        reels: Any = None   # mapa de visualizações da aba Reels (criado na 1a consulta)
        reels_http: Any = None
        closed_page = 0   # índice da última página que chegou ao fim (on_page_end): os descartes dela já saíram
        self.last_account_confirmation = None

        def on_page(meta: dict) -> None:
            st["pages"] += 1
            st["duplicates"] += int(meta.get("duplicate_nodes") or 0)
            if self._on_page is not None:
                self._on_page(meta)

        def on_page_end(reason: str | None) -> bool:
            nonlocal streak, page_compatible, closed_page, page_views
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
                    page_views=None if page_views is None else dict(page_views),
                ))
            if page_views is not None:
                page_views = _views_zero(with_above)
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

        def before_reels_request() -> bool:
            """Teto de páginas por hora e cancelamento antes de cada requisição da aba Reels."""
            if cancel is not None and cancel.is_set():
                return False
            if wait_page is not None:
                per_hour = page_rate_limit(self._env)
                if per_hour > 0:
                    wait_page(cancel, max_per_hour=per_hour)
            return not (cancel is not None and cancel.is_set())

        def views_of(item: RemoteMedia) -> int | None:
            nonlocal reels, reels_http
            if reels is None:
                from app.integrations.instagram.web.reels_tab import ReelsViews

                reels_http = self._public_http_factory()   # anônimo (jar vazio), mesmo portão
                if cancel is not None and hasattr(reels_http, "cancel"):
                    reels_http.cancel = cancel
                reels = ReelsViews(reels_http, username, before_request=before_reels_request,
                                   overrides=self._overrides, env=self._env)
            return reels.lookup(item.source_post_id, item.post_id)

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
                    if cutoff is not None:
                        taken = _node_timestamp(node)
                        if taken is None or taken < cutoff:
                            # Fora do período (ou sem data): nenhuma mídia da publicação é baixada.
                            page_views["out_of_period"] += len(items)
                            st["views_out_of_period"] += len(items)
                            pinned = _node_pinned(node)
                            if taken is not None and pinned is not True:
                                old_streak = PERIOD_STOP_STREAK if pinned is False else old_streak + 1
                            if old_streak >= PERIOD_STOP_STREAK:
                                st["stop_reason"] = "period_end"
                                if on_checkpoint is not None and page is not None:
                                    closed_page = page.index
                                    on_checkpoint(ListingCheckpoint(
                                        resume_cursor=page.end_cursor, next_page_index=page.index + 1,
                                        pages=st["pages"], posts_seen=st["posts_seen"],
                                        items_compatible=st["items_compatible"], unproductive_streak=streak,
                                        has_next=False, page_counts=_page_counts(report, DEGRADED_REASONS),
                                        page_views=dict(page_views), period_end=True,
                                    ))
                                completed = True
                                return
                            continue
                        old_streak = 0
                    if on_post is not None:
                        record = _post_record(node)
                        if record is not None:
                            on_post(record)
                    for item in items:
                        if page_views is not None:
                            if not views_on:
                                verdict = "approved"
                            elif item.media_type != "video":
                                verdict = "no_count"   # foto/carrossel sem vídeo: nunca aprovado pelo filtro
                            else:
                                views = views_of(item)
                                if reels.cancelled or (cancel is not None and cancel.is_set()):
                                    st["stop_reason"] = "cancelled"
                                    completed = True
                                    return
                                if views is None:
                                    verdict = "no_count"
                                elif min_views is not None and views < min_views:
                                    verdict = "below"
                                elif max_views is not None and views > max_views:
                                    verdict = "above"
                                else:
                                    verdict = "approved"
                            page_views[verdict] += 1      # antes do yield, como `count`
                            st[f"views_{verdict}"] += 1
                            if verdict != "approved":
                                continue
                            item.listing_views = dict(page_views)
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
                # Vereditos do filtro de visualizações da página aberta (zerados no fim de cada página): vão junto do
                # erro como estado da página, igual aos descartes.
                if page_views is not None and any(page_views.values()):
                    try:
                        exc.listing_views = dict(page_views)
                    except Exception:  # noqa: BLE001 - exceção sem atributos
                        pass
                raise
            finally:
                if completed and st["stop_reason"] is None:
                    st["stop_reason"] = "end_of_profile"
                close = getattr(posts, "close", None)
                if close is not None:
                    close()
                if reels_http is not None:
                    _close_http(reels_http)
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
                if filters_on:
                    # Só totais do trecho; nenhuma contagem individual nem id.
                    log.info("Listagem web (filtros): views_approved=%s views_below=%s views_above=%s "
                             "views_no_count=%s out_of_period=%s reels_pages=%s", st["views_approved"],
                             st["views_below"], st["views_above"], st["views_no_count"], st["views_out_of_period"],
                             reels.pages if reels is not None else 0)
                self.last_listing_anomalies = anomalies = _listing_anomalies(ops, stats.reasons, EXPECTED_REASONS,
                                                                                 st)
                if anomalies["pages_with_errors"] or anomalies["dropped_unexpected"]:
                    log.warning(
                        "Listagem web (este trecho): pages_with_errors=%s errors=%s "
                        "dropped_unexpected=%s skipped_expected=%s",
                        anomalies["pages_with_errors"], anomalies["errors"],
                        _counts(anomalies["dropped_unexpected"]), _counts(anomalies["skipped_expected"]),
                    )


# Período de postagem sem sinal de "fixada" no nó: publicações seguidas mais antigas que o corte para encerrar (o
# Instagram fixa no máximo 3 no perfil; a 4a antiga seguida não pode ser fixada).
PERIOD_STOP_STREAK = 4
# Campo de fixação do nó v1 da listagem. NÃO verificado na resposta real do PolarisProfilePostsQuery (inferido do
# formato v1; a aba Reels traz o análogo `clips_tab_pinned_user_ids`, verificado). Ausente = não se sabe.
LISTING_PINNED_FIELD = "timeline_pinned_user_ids"


def _views_zero(with_above: bool = False) -> dict[str, int]:
    """Vereditos zerados da página; "above" só com o máximo de visualizações (os demais lotes mantêm o formato)."""
    zero = {"approved": 0, "below": 0, "no_count": 0, "out_of_period": 0}
    if with_above:
        zero["above"] = 0
    return zero


def _cutoff_timestamp(value: object) -> int | None:
    """``"AAAA-MM-DDTHH:MM:SSZ"`` (UTC, como o lote grava) -> epoch; None se inválido."""
    from datetime import datetime, timezone

    if not isinstance(value, str):
        return None
    try:
        return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _node_timestamp(node: Mapping) -> int | None:
    """``taken_at`` nos MESMOS formatos aceitos por ``normalize._taken_at`` (int, texto numérico, decimal...); None se
    ele não aceitar (sem data)."""
    from app.integrations.instagram.web.normalize import _taken_at

    value = node.get("taken_at")
    if _taken_at(value) is None:
        return None
    return int(value)


def _post_record(node: Mapping) -> dict | None:
    """Registro de horário de UMA publicação (nível de post): código, `taken_at` UTC ISO (None sem data), tipo
    ("photo", "video", "reel", "carousel" ou None) e fixado (True/False/None). None se o nó não tem código."""
    from app.integrations.instagram.web.normalize import MEDIA_CAROUSEL, MEDIA_IMAGE, MEDIA_VIDEO, _taken_at

    code = node.get("code")
    if not isinstance(code, str) or not code:
        return None
    media_type = node.get("media_type")
    if media_type == MEDIA_CAROUSEL:
        kind: str | None = "carousel"
    elif media_type == MEDIA_VIDEO:
        kind = "reel" if node.get("product_type") == "clips" else "video"
    elif media_type == MEDIA_IMAGE:
        kind = "photo"
    else:
        kind = None
    return {"code": code, "taken_at": _taken_at(node.get("taken_at")), "kind": kind, "pinned": _node_pinned(node)}


def _node_pinned(node: Mapping) -> bool | None:
    """True = fixada; False = o nó diz que não é; None = campo ausente (não se sabe)."""
    value = node.get(LISTING_PINNED_FIELD)
    if isinstance(value, list):
        return bool(value)
    return None


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
