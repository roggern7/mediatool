"""Processo filho que roda o gallery-dl in-process e fala JSON lines.

Uso (cwd = backend): ``python -m app.integrations.instagram.gallerydl_runner``

Entrada: UM objeto JSON no stdin::

    {"op": "test_session" | "profile" | "list" | "resolve_post",
     "cookies": {"sessionid": "...", ...},          # opcional; nunca em argv nem em arquivo
     "username": "...", "content": "reels", "limit": 10,   # conforme a operação
     "shortcode": "...", "sleep_request": [6, 12]}          # sleep_request opcional

Saída (stdout, uma linha JSON por evento)::

    {"type": "profile", ...ProfileInfo}
    {"type": "item", ...RemoteMedia}
    {"type": "error", "code": "...", "message": "..."}
    {"type": "done", "count": N}

Nenhum arquivo de mídia é baixado aqui; só metadados e URLs. Logs do gallery-dl vão para o stderr
(com os valores dos cookies redigidos); o processo pai redige de novo antes de registrar qualquer coisa.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from app.integrations.instagram.downloader import ERROR_CODES, SESSION_RESULT_CODES
from app.integrations.instagram.validation import parse_profile_input, validate_shortcode

MAX_REQUEST_BYTES = 64 * 1024
MAX_ITEMS = 5000
ALLOWED_COOKIES = ("sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "rur")
_COOKIE_VALUE_RE = re.compile(r"^[A-Za-z0-9%:_\-.]{1,512}$")
OPERATIONS = ("test_session", "profile", "list", "resolve_post", "inspect_request", "probe")
# Operações de diagnóstico: mesma configuração do teste de sessão (sem nova tentativa, sem dormir em 429).
DIAGNOSTIC_OPS = ("test_session", "inspect_request", "probe")
_PROBE_ENDPOINT_RE = re.compile(r"^/api/v1/[A-Za-z0-9_\-./]{1,200}$")
_PROBE_PARAM_RE = re.compile(r"^[A-Za-z0-9_\-.:,]{0,200}$")
CONTENT_TYPES = ("reels", "posts", "videos", "images", "all")
# Intervalo padrão do extractor do Instagram no gallery-dl (InstagramExtractor.request_interval).
DEFAULT_SLEEP_REQUEST = (6.0, 12.0)
# Tentativas extras após falha HTTP (inclusive 429). Listagem (background) mantém 2; perfil e post, 1.
RETRIES = 2
RETRIES_BY_OP = {"list": RETRIES, "profile": 1, "resolve_post": 1}
# Espera em 429 nas operações de background (o padrão do gallery-dl é 60 s por tentativa).
SLEEP_429 = 30.0
# Teste de sessão: nenhuma nova tentativa, nenhuma espera em 429, timeout HTTP curto.
TEST_HTTP_TIMEOUT = 15.0
TEST_SLEEP_REQUEST = 1.0
ROOT = "https://www.instagram.com"

Emit = Callable[[dict[str, Any]], None]


class RunnerError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class _LimitReached(Exception):
    pass


# --------------------------------------------------------------------------- normalização


def content_matches(content: str, media_type: str) -> bool:
    if content == "videos":
        return media_type == "video"
    if content == "images":
        return media_type == "image"
    return True


def _iso(value: Any) -> str | None:
    # dt.NONE do gallery-dl é um datetime(1, 1, 1) com __bool__ falso: significa "sem data".
    if isinstance(value, datetime) and value:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value) or None
    return None


def _str_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def normalize_file(url: str, kw: dict[str, Any], *, profile: str | None, from_reels: bool) -> dict[str, Any] | None:
    """Converte o kwdict de uma mensagem Url do gallery-dl em um item RemoteMedia (dict)."""
    shortcode = kw.get("post_shortcode")
    if not shortcode:
        return None
    video_url = kw.get("video_url")
    media_type = "video" if video_url else "image"
    media_url = video_url if video_url and not str(url).startswith("https://") else url
    if not isinstance(media_url, str) or not media_url.startswith("https://"):
        return None
    num = kw.get("num")
    index = num - 1 if isinstance(num, int) and num > 0 else 0
    # Reel = mídia "clips" ou vinda do endpoint de reels do perfil; vídeo único de feed não é reel.
    is_reel = media_type == "video" and (from_reels or kw.get("product_type") == "clips")
    source_url = f"{ROOT}/{'reel' if is_reel else 'p'}/{shortcode}/"
    duration = kw.get("video_duration")
    extension = kw.get("extension")
    return {
        "source_platform": "instagram",
        "source_profile": (kw.get("username") or profile or "").lower(),
        "source_post_id": str(shortcode),
        "post_id": _str_or_none(kw.get("post_id")),
        "media_id": _str_or_none(kw.get("media_id")),
        "source_media_index": index,
        "media_type": media_type,
        "is_reel": bool(is_reel),
        "source_url": source_url,
        "media_url": media_url,
        "thumbnail_url": kw.get("display_url") if media_type == "video" else None,
        "width": _int_or_none(kw.get("width")),
        "height": _int_or_none(kw.get("height")),
        "duration": float(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
        "taken_at": _iso(kw.get("date")) or _iso(kw.get("post_date")),
        "extension": extension.lower() if isinstance(extension, str) and extension else None,
    }


def profile_from_user(user: dict[str, Any]) -> dict[str, Any]:
    """Normaliza o usuário do topsearch, da página do perfil ou de web_profile_info em ProfileInfo (dict)."""
    post_count = user.get("media_count")
    if post_count is None:
        edge = user.get("edge_owner_to_timeline_media")
        if isinstance(edge, dict):
            post_count = edge.get("count")
    hd_info = user.get("hd_profile_pic_url_info")
    avatar = (
        user.get("profile_pic_url_hd")
        or (hd_info.get("url") if isinstance(hd_info, dict) else None)
        or user.get("profile_pic_url")
    )
    is_private = user.get("is_private")
    is_verified = user.get("is_verified")
    return {
        "username": str(user.get("username") or "").lower(),
        "user_id": _str_or_none(user.get("id") or user.get("pk")),
        "full_name": user.get("full_name") or None,
        "avatar_url": avatar or None,
        "post_count": post_count if isinstance(post_count, int) and not isinstance(post_count, bool) else None,
        "is_private": is_private if isinstance(is_private, bool) else None,
        "is_verified": is_verified if isinstance(is_verified, bool) else None,
    }


# --------------------------------------------------------------------------- classificação de erros

_QUERY_RE = re.compile(r"\?[^'\"\s)]*")

# Redirect para a página inicial nas operações: prova só que a chamada REST do gallery-dl foi recusada (o Instagram
# Web atual usa GraphQL com doc_id). Vira `access_refused` (não retryable, sem pausa).
SESSION_REFUSED_MESSAGE = ERROR_CODES["access_refused"][0]
# Teste de sessão: sessão reconhecida (sessionid não apagado) e listagem recusada com redirect para "/".
REDIRECT_HOME_TEST_MESSAGE = (
    "O Instagram reconheceu a sessão, mas recusou a chamada de listagem usada pelo Media Tool (gallery-dl, "
    "endpoint feed/user), redirecionando para a página inicial. O Instagram Web atual carrega perfis por outra API "
    "(GraphQL), então isso não indica sessão inválida nem restrição da conta. A listagem por perfil ainda não "
    "funciona com esta sessão."
)
SESSION_ENDED_MESSAGE = (
    "O Instagram encerrou a sessão nesta resposta (o cookie sessionid foi apagado ou expirado). Entre no Instagram "
    "pelo navegador, copie um sessionid novo e salve."
)
# Campos explícitos de restrição (feedback_required, spam true, restriction_enrollment_data ou chaves feedback_*):
# provam só que ESTA chamada foi recusada com aviso de restrição.
ACTION_BLOCKED_MESSAGE = ERROR_CODES["action_blocked"][0]
RESTRICTED_TEST_MESSAGE = "O Instagram respondeu a esta chamada com um aviso de restrição (campos feedback/restriction)."
_RESTRICTION_RE = re.compile(r'feedback_required|"spam"\s*:\s*true|"restriction_enrollment_data"\s*:|"feedback_\w+"\s*:')


def is_action_blocked(text: str) -> bool:
    """Detecção única de campos explícitos de restrição (teste de sessão e operações usam esta função).

    JSON: só campos de topo (``spam: true``, ``feedback_required`` em status/message/error_type, chave
    ``restriction_enrollment_data`` ou chaves ``feedback_*``); ``feedback_*`` aninhado não conta. Corpo que não é
    JSON: busca textual.
    """
    body = (text or "")[:65536]
    try:
        data = json.loads(body) if body.strip() else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        markers = " ".join(str(data.get(k) or "") for k in ("status", "message", "error_type")).lower()
        return (
            data.get("spam") is True or "feedback_required" in markers or "restriction_enrollment_data" in data
            or any(isinstance(k, str) and k.startswith("feedback_") for k in data)
        )
    if data is not None:
        return False
    return bool(_RESTRICTION_RE.search(body.lower()))
SESSION_LOGIN_MESSAGE = (
    "O Instagram não aceita esta sessão (pede login): ela expirou, foi encerrada ou o sessionid está errado. "
    "Entre no Instagram pelo navegador, copie um sessionid novo e salve."
)


# Seção 7 de docs/BATCH_ROBUSTNESS.md: recusa de autenticação de UM request sem Set-Cookie apagando o sessionid.
REQUEST_AUTH_MESSAGE = ERROR_CODES["request_auth_failed"][0]
REQUEST_AUTH_TEST_MESSAGE = (
    "O Instagram recusou esta chamada por autenticação, mas não encerrou a sessão (o sessionid não foi apagado). "
    "Isso não prova sessão inválida: o Media Tool aguarda e verifica de novo; a sessão salva continua guardada."
)


def _clean(text: str, limit: int = 300) -> str:
    return _QUERY_RE.sub("", text)[:limit]


def classify_exception(exc: BaseException, *, op: str, has_session: bool) -> tuple[str, str]:
    """Mapeia uma exceção do gallery-dl/requests para (code, message) do contrato."""
    from gallery_dl import exception as gexc

    if isinstance(exc, RunnerError):
        return exc.code, exc.message

    not_found_code = "content_unavailable" if op == "resolve_post" else "profile_not_found"
    # Seção 7: session_invalid só com Set-Cookie apagando o sessionid; demais recusas -> request_auth_failed.
    auth_code = "request_auth_failed" if has_session else "session_missing"

    if isinstance(exc, gexc.HttpError):
        status = exc.status
        raw = ""
        set_cookie = None
        if exc.response is not None:
            try:
                raw = exc.response.text[:65536]
            except Exception:
                raw = ""
            try:
                set_cookie = exc.response.headers.get("set-cookie")
            except Exception:
                set_cookie = None
        ended = has_session and bool(sessionid_deletion(set_cookie))
        body = raw[:8192].lower()
        detail = _clean(str(exc))
        if "checkpoint_required" in body or "challenge_required" in body:
            return "checkpoint_required", f"Instagram pediu verificação de segurança ({detail})"
        if status == 429:
            return "rate_limited", f"Instagram limitou as requisições ({detail})"
        # require_login vence "Please wait a few minutes": é o 401 que o Instagram dá a quem não está logado.
        if "login_required" in body or re.search(r'"require_login"\s*:\s*true', body):
            if not has_session:
                return auth_code, f"Instagram recusou o acesso sem uma sessão válida ({detail})"
            if ended:
                return "session_invalid", SESSION_ENDED_MESSAGE
            return auth_code, REQUEST_AUTH_MESSAGE
        if is_action_blocked(raw):
            return "action_blocked", ACTION_BLOCKED_MESSAGE
        if "please wait a few minutes" in body:
            return "rate_limited", f"Instagram limitou as requisições ({detail})"
        if status in (401, 403):
            if ended:
                return "session_invalid", SESSION_ENDED_MESSAGE
            if has_session:
                return auth_code, f"{REQUEST_AUTH_MESSAGE} ({detail})"
            return auth_code, f"Instagram recusou o acesso sem uma sessão válida ({detail})"
        if status == 404:
            return not_found_code, f"Não encontrado ({detail})"
        if status == 0 or status >= 500:
            return "network_error", f"Falha HTTP ({detail})"
        return "internal_error", f"Resposta HTTP inesperada ({detail})"

    if isinstance(exc, gexc.AbortExtraction):
        text = str(exc)
        if "redirect to challenge" in text:
            return "checkpoint_required", f"Instagram redirecionou para verificação de segurança ({_clean(text)})"
        if "redirect to login" in text:
            return auth_code, f"Instagram redirecionou para a página de login ({_clean(text)})"
        if "redirect to home" in text:
            if has_session:
                return "access_refused", SESSION_REFUSED_MESSAGE
            return auth_code, f"Instagram redirecionou para a página inicial ({_clean(text)})"
        if "are private" in text:
            return "profile_private", _clean(text)
        return "internal_error", _clean(text)

    if isinstance(exc, (gexc.AuthorizationError, gexc.AuthenticationError)):
        return auth_code, _clean(str(exc))
    if isinstance(exc, gexc.NotFoundError):
        return not_found_code, _clean(str(exc))
    if isinstance(exc, gexc.NoExtractorError):
        return "invalid_input", "URL não suportada pelo gallery-dl"

    if type(exc).__name__ == "JSONDecodeError":
        return auth_code, "Instagram respondeu com conteúdo que não é JSON (normalmente a página de login)"

    try:
        import requests
    except ImportError:  # pragma: no cover - requests é dependência do gallery-dl
        requests = None
    if requests is not None and isinstance(exc, requests.exceptions.RequestException):
        return "network_error", f"Falha de rede ({type(exc).__name__})"
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "network_error", f"Falha de rede ({type(exc).__name__})"
    if isinstance(exc, (KeyError, TypeError, IndexError)):
        return "internal_error", f"Resposta do Instagram em formato inesperado ({type(exc).__name__})"
    return "internal_error", f"Erro inesperado ({type(exc).__name__})"


# --------------------------------------------------------------------------- pedido


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise RunnerError("invalid_input", message)


def parse_request(raw: bytes) -> dict[str, Any]:
    _require(len(raw) <= MAX_REQUEST_BYTES, "pedido grande demais")
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RunnerError("invalid_input", "pedido não é JSON válido") from None
    _require(isinstance(request, dict), "pedido deve ser um objeto JSON")

    op = request.get("op")
    _require(op in OPERATIONS, "operação inválida")
    parsed: dict[str, Any] = {"op": op}

    cookies = request.get("cookies") or {}
    _require(isinstance(cookies, dict), "cookies inválidos")
    clean_cookies: dict[str, str] = {}
    for name, value in cookies.items():
        _require(name in ALLOWED_COOKIES, "cookie não permitido")
        _require(isinstance(value, str) and bool(_COOKIE_VALUE_RE.match(value)), "valor de cookie inválido")
        clean_cookies[name] = value
    parsed["cookies"] = clean_cookies

    sleep_request = request.get("sleep_request")
    if sleep_request is not None:
        _require(
            isinstance(sleep_request, list)
            and len(sleep_request) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in sleep_request),
            "sleep_request inválido",
        )
        # Só permite aumentar o intervalo padrão do gallery-dl, nunca reduzir.
        lower = max(float(sleep_request[0]), DEFAULT_SLEEP_REQUEST[0])
        upper = max(float(sleep_request[1]), lower, DEFAULT_SLEEP_REQUEST[1])
        parsed["sleep_request"] = [lower, upper]

    if op == "probe":
        method = request.get("method", "GET")
        endpoint = request.get("endpoint")
        params = request.get("params") or {}
        _require(method == "GET", "method inválido (só GET)")
        _require(isinstance(endpoint, str) and bool(_PROBE_ENDPOINT_RE.match(endpoint)) and ".." not in endpoint,
                 "endpoint inválido")
        _require(isinstance(params, dict) and len(params) <= 20, "parâmetros inválidos")
        for key, value in params.items():
            _require(bool(_PROBE_PARAM_RE.match(str(key))) and isinstance(value, str)
                     and bool(_PROBE_PARAM_RE.match(value)), "parâmetros inválidos")
        parsed.update(method=method, endpoint=endpoint, params=params)

    try:
        if op == "inspect_request":
            parsed["username"] = parse_profile_input(request.get("username") or "instagram")
        if op in ("profile", "list"):
            parsed["username"] = parse_profile_input(request.get("username"))
        if op == "resolve_post":
            parsed["shortcode"] = validate_shortcode(request.get("shortcode"))
    except ValueError as exc:
        raise RunnerError("invalid_input", str(exc)) from None

    if op == "list":
        content = request.get("content")
        _require(content in CONTENT_TYPES, "content inválido")
        limit = request.get("limit")
        _require(
            limit is None or (isinstance(limit, int) and not isinstance(limit, bool) and limit > 0),
            "limit inválido",
        )
        parsed["content"] = content
        parsed["limit"] = min(limit, MAX_ITEMS) if limit is not None else MAX_ITEMS
    return parsed


# --------------------------------------------------------------------------- gallery-dl


def _configure_gallery_dl(request: dict[str, Any]) -> None:
    from gallery_dl import config

    # Nunca carrega o config.json do usuário (que poderia apontar cookies do navegador).
    config.clear()
    config.set(("cache",), "file", ":memory:")
    config.set(("extractor",), "cookies-update", False)
    base = ("extractor", "instagram")
    config.set(base, "cookies", dict(request["cookies"]) or None)
    config.set(base, "api", "rest")
    # "merged": URL progressiva (vídeo + áudio) em vez do manifesto DASH via ytdl.
    config.set(base, "videos", "merged")
    config.set(base, "previews", False)
    config.set(base, "audio", False)
    if request["op"] in DIAGNOSTIC_OPS:
        # Sem nova tentativa e sem dormir em 429: o teste termina rápido e o 429 vira rate_limited na hora.
        # O intervalo curto só vale para as operações de diagnóstico com mais de uma requisição.
        config.set(base, "retries", 0)
        config.set(base, "sleep-429", 0)
        config.set(base, "sleep-request", TEST_SLEEP_REQUEST)
        config.set(base, "timeout", TEST_HTTP_TIMEOUT)
        return
    # Em 429 o gallery-dl espera SLEEP_429 s por tentativa (perfil/post: 1 nova tentativa; listagem: 2) e
    # depois o runner devolve rate_limited; a fila aplica o próprio backoff. O intervalo entre requisições
    # não muda.
    config.set(base, "retries", RETRIES_BY_OP[request["op"]])
    config.set(base, "sleep-429", SLEEP_429)
    if "sleep_request" in request:
        config.set(base, "sleep-request", request["sleep_request"])


def _make_job(url: str, on_url: Callable[[str, dict[str, Any]], None] | None = None, session=None):
    from gallery_dl import extractor as gextractor
    from gallery_dl import job as gjob

    class CollectJob(gjob.Job):
        def handle_url(self, url, kwdict):
            if on_url is not None:
                on_url(url, kwdict)

    extr = gextractor.find(url)
    if extr is None:
        raise RunnerError("internal_error", "URL não suportada pelo gallery-dl")
    if session is not None:
        extr.session = session
    job = CollectJob(extr)
    job._init()
    _use_session_csrftoken(extr)
    _attach_rest_extras(extr)
    return job


def _use_session_csrftoken(extr) -> None:
    """Usa o csrftoken da sessão do navegador, se informado, no cookie e no header X-CSRFToken.

    O ``_init()`` do extractor gera um token aleatório e sobrescreve o cookie ``csrftoken`` carregado da
    config; os outros cookies (ds_user_id, mid, ig_did, rur) já seguem como vieram.
    """
    from gallery_dl import config

    cookies = config.get(("extractor", "instagram"), "cookies")
    token = cookies.get("csrftoken") if isinstance(cookies, dict) else None
    if token and hasattr(extr, "csrf_token"):
        extr.csrf_token = token
        extr.cookies.set("csrftoken", token, domain=extr.cookies_domain)


def _attach_rest_extras(extr) -> None:
    """Acrescenta video_duration e product_type ao kwdict (o gallery-dl não os repassa)."""
    original = getattr(extr, "_parse_post_rest", None)
    if original is None:
        return

    def parse(post):
        data = original(post)
        sources = post.get("carousel_media") or (post,)
        by_pk = {str(item.get("pk")): item for item in sources if isinstance(item, dict)}
        for file in data.get("_files", ()):
            item = by_pk.get(str(file.get("media_id")))
            if item is not None and item.get("video_duration") is not None:
                file["video_duration"] = item.get("video_duration")
        data["product_type"] = post.get("product_type")
        return data

    extr._parse_post_rest = parse


def _session_user_id(cookies: dict[str, str]) -> str | None:
    if cookies.get("ds_user_id", "").isdigit():
        return cookies["ds_user_id"]
    match = re.match(r"^(\d+)(?:%3A|:)", cookies.get("sessionid", ""))
    return match.group(1) if match else None


def _fetch_profile(job, username: str) -> dict[str, Any]:
    """Resolve o perfil como o gallery-dl 1.32.14 (user-strategy padrão "search", "web").

    1. ``GET /web/search/topsearch/?query=`` (``user_by_search``); 2. página HTML do perfil, só ``profile_id``
    (``user_by_web``); 3. último recurso: ``/api/v1/users/web_profile_info/``, que o upstream abandonou por dar 429.
    O upstream converte qualquer falha em ``NotFoundError``; por isso a primeira falha HTTP/redirect (429, login,
    home, 404...) é relançada para a classificação normal. Só "200 sem o usuário" passa para a próxima estratégia.
    """
    from gallery_dl import exception as gexc

    extr = job.extractor
    extr.login()
    failures: list[BaseException] = []
    original_request = extr.request

    def tracked_request(url, **kwargs):
        try:
            return original_request(url, **kwargs)
        except Exception as exc:
            failures.append(exc)
            raise

    extr.request = tracked_request
    try:
        for strategy in (extr.api.user_by_search, extr.api.user_by_web):
            try:
                user = strategy(username)
            except gexc.NotFoundError:
                if failures:
                    raise failures[0] from None
                continue
            # user_by_web devolve só {"id": ...}.
            return {"username": username, **user}
    finally:
        extr.request = original_request
    data = extr.api._call("/v1/users/web_profile_info/", params={"username": username}, notfound="user")
    user = (data.get("data") or {}).get("user") if isinstance(data, dict) else None
    if not user:
        raise RunnerError("profile_not_found", "Perfil não encontrado")
    return user


_RETRY_LATER = "Aguarde alguns minutos antes de testar de novo."


def _session_outcome(result: str, message: str, http_status: int | None, username: str | None = None,
                     detail: str | None = None, *, recognized: bool | None = None,
                     listing: str = "unknown", code: str | None = None) -> dict:
    return {"result": result, "code": code or SESSION_RESULT_CODES[result], "message": message,
            "username": username, "http_status": http_status, "detail": detail,
            "session_recognized": recognized, "listing_api": listing}


_SET_COOKIE_SPLIT_RE = re.compile(r",\s*(?=[A-Za-z0-9_\-]+=)")


def sessionid_deletion(set_cookie: str | None) -> list[str]:
    """Motivos pelos quais o Set-Cookie apaga/expira o ``sessionid`` (lista vazia: não apaga).

    Só olha o nome do cookie e os atributos; o valor nunca é devolvido nem registrado. Motivos possíveis:
    ``empty_value`` (vazio ou ``""``), ``max_age_0`` (Max-Age <= 0) e ``expires_past``.
    """
    from email.utils import parsedate_to_datetime

    reasons: list[str] = []
    # Vários Set-Cookie chegam unidos por ", "; a vírgula de Expires ("Thu, 01 Jan") não é seguida de nome=.
    for cookie in _SET_COOKIE_SPLIT_RE.split(set_cookie or ""):
        parts = [part.strip() for part in cookie.split(";")]
        name, _, value = parts[0].partition("=")
        if name.strip().lower() != "sessionid":
            continue
        if value.strip() in ("", '""'):
            reasons.append("empty_value")
        for attr in parts[1:]:
            key, _, attr_value = attr.partition("=")
            key = key.strip().lower()
            if key == "max-age":
                try:
                    if int(attr_value.strip()) <= 0:
                        reasons.append("max_age_0")
                except ValueError:
                    pass
            elif key == "expires":
                try:
                    moment = parsedate_to_datetime(attr_value.strip())
                except (TypeError, ValueError, IndexError):
                    continue
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=UTC)
                if moment <= datetime.now(UTC):
                    reasons.append("expires_past")
    return reasons


def redirect_kind(location: str | None) -> str:
    """Classifica o destino de um redirect do Instagram (só o caminho; a query é ignorada).

    Sem ``Location`` não há destino conhecido: ``redirect_other``.
    """
    if not location:
        return "redirect_other"
    path = (urlsplit(location or "").path or "/").lower()
    if "/accounts/login" in path:
        return "redirect_login"
    if "/challenge" in path:
        return "redirect_challenge"
    if "/checkpoint" in path:
        return "redirect_checkpoint"
    if "consent" in path or "/privacy/checks" in path or "/terms/" in path:
        return "redirect_consent"
    if path == "/":
        return "redirect_home"
    return "redirect_other"


def classify_session_response(status: int, location: str | None, text: str,
                              set_cookie: str | None = None) -> dict[str, Any]:
    """Classifica a resposta do teste de sessão (pedido feito sem seguir redirect).

    Separa dois fatos: ``session_recognized`` (a resposta não encerrou a sessão: sem Set-Cookie apagando/expirando
    o ``sessionid``, sem redirect para login, sem ``require_login``) e ``listing_api`` (``ok`` = 200 com items,
    ``refused`` = redirect que não é login ou 400/403 com campos de restrição, ``rate_limited`` = 429,
    ``unknown``).

    200 com ``items`` (feed) ou objeto de usuário é ``valid`` antes de qualquer outra verificação (a bio pode
    conter qualquer texto); o username vem de ``user`` ou do primeiro item, se houver.
    Marcadores só são procurados nos campos estruturados do JSON (status, message, error_type, checkpoint_url,
    require_login, spam) ou, se o corpo não for JSON, no texto.
    Seção 7: só Set-Cookie apagando o sessionid vira ``invalid``. Redirect para /accounts/login/,
    ``require_login``/``login_required`` e 401/403 sem essa evidência viram ``instagram_error`` com código
    ``request_auth_failed`` (pausa de verificação). Redirect para "/" ou outro redirect que não é login, com a sessão
    mantida, vira ``api_refused``. ``require_login`` vence "Please wait a few minutes". Só HTTP 429, ou a frase sem ``require_login``, vira rate_limited. Campos de restrição
    (``feedback_required``, ``spam: true``, ``restriction_enrollment_data``, chaves ``feedback_*``) viram
    ``restricted`` (aviso nesta chamada, não restrição comprovada da conta inteira).
    """
    body = (text or "")[:65536]
    try:
        data = json.loads(body) if body.strip() else None
    except ValueError:
        data = None
    deleted = bool(sessionid_deletion(set_cookie))
    recognized = not deleted

    user = data.get("user") if isinstance(data, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    if status == 200 and (isinstance(items, list) or (isinstance(user, dict) and user.get("username"))):
        if not (isinstance(user, dict) and user.get("username")):
            user = next((i["user"] for i in items if isinstance(i, dict) and isinstance(i.get("user"), dict)), {})
        if user.get("username"):
            username = str(user["username"]).lower()
            return _session_outcome("valid", f"Sessão válida (conta @{username}).", status, username,
                                    recognized=recognized, listing="ok")
        return _session_outcome("valid", "Sessão válida.", status, recognized=recognized, listing="ok")

    if 300 <= status < 400:
        kind = redirect_kind(location)
        if kind in ("redirect_challenge", "redirect_checkpoint"):
            return _session_outcome(
                "checkpoint_required",
                "O Instagram pediu uma verificação de segurança. Abra o Instagram no navegador com esta conta, "
                "conclua a verificação e salve a sessão de novo.", status, detail=kind,
                recognized=recognized, listing="refused")
        if deleted:
            return _session_outcome("invalid", SESSION_ENDED_MESSAGE, status, detail=kind, recognized=False,
                                    listing="refused")
        if kind == "redirect_login":
            return _session_outcome("instagram_error", REQUEST_AUTH_TEST_MESSAGE, status, detail=kind,
                                    code="request_auth_failed")
        # Sessão mantida; só esta chamada de listagem foi recusada (redirect para "/" ou outra página).
        if kind == "redirect_home":
            return _session_outcome("api_refused", REDIRECT_HOME_TEST_MESSAGE, status, detail=kind,
                                    recognized=True, listing="refused")
        return _session_outcome(
            "api_refused",
            f"O Instagram reconheceu a sessão, mas recusou a chamada de listagem usada pelo Media Tool (gallery-dl, "
            f"endpoint feed/user), redirecionando para outra página ({kind}). Isso não indica sessão inválida nem "
            "restrição da conta. A listagem por perfil ainda não funciona com esta sessão.", status, detail=kind,
            recognized=True, listing="refused")

    if isinstance(data, dict):
        markers = " ".join(str(data.get(k) or "") for k in ("status", "message", "error_type")).lower()
        require_login = data.get("require_login") is True
        checkpoint_url = bool(data.get("checkpoint_url"))
    else:
        markers = body.lower()
        require_login = checkpoint_url = False
    restricted = is_action_blocked(body)

    if "checkpoint_required" in markers or "challenge_required" in markers or checkpoint_url:
        return _session_outcome(
            "checkpoint_required",
            "O Instagram pediu uma verificação de segurança. Abra o Instagram no navegador com esta conta, "
            "conclua a verificação e salve a sessão de novo.", status, recognized=recognized)
    if status == 429:
        return _session_outcome(
            "rate_limited", f"O Instagram limitou as requisições deste computador (HTTP 429). {_RETRY_LATER}", status,
            recognized=recognized, listing="rate_limited")
    if deleted:
        return _session_outcome("invalid", SESSION_ENDED_MESSAGE, status, recognized=False)
    if require_login or "login_required" in markers:
        return _session_outcome("instagram_error", REQUEST_AUTH_TEST_MESSAGE, status, detail="require_login",
                                code="request_auth_failed")
    if restricted:
        return _session_outcome("restricted", RESTRICTED_TEST_MESSAGE, status, recognized=True,
                                listing="refused" if status in (400, 403) else "unknown")
    if status in (401, 403):
        return _session_outcome("instagram_error", REQUEST_AUTH_TEST_MESSAGE, status, detail=f"http_{status}",
                                code="request_auth_failed")
    if "please wait a few minutes" in markers:
        return _session_outcome(
            "rate_limited", f"O Instagram pediu para esperar antes de novas requisições. {_RETRY_LATER}", status,
            recognized=True, listing="rate_limited")
    detail = f"HTTP {status}" if status != 200 else "resposta sem o usuário da sessão"
    return _session_outcome(
        "instagram_error", f"O Instagram respondeu de forma inesperada ({detail}). Tente de novo mais tarde.",
        status, recognized=None if status >= 500 else recognized)


def _probe_session(extr, endpoint: str, params: dict[str, str] | None, timeout: float) -> dict[str, Any]:
    """Uma requisição do teste de sessão: sem seguir redirect, qualquer status volta como resposta."""
    from gallery_dl import exception as gexc

    try:
        response = extr.api._call(endpoint, params=params, allow_redirects=False, expected=range(400, 600),
                                  timeout=timeout)
    except gexc.HttpError as exc:
        # Status 0: falha antes de qualquer resposta (DNS, conexão, SSL, timeout).
        text = str(exc)
        if "timed out" in text.lower() or "timeout" in text.lower():
            return _session_outcome(
                "timeout",
                f"O Instagram não respondeu em {timeout:.0f} s. Verifique a conexão e tente de novo "
                "em alguns minutos.", None)
        reason = re.search(r"\b(\w+Error)\b", text)
        return _session_outcome(
            "network_error",
            f"Não foi possível conectar ao Instagram ({reason.group(1) if reason else 'falha de rede'}). "
            "Verifique a internet, o DNS ou o proxy e tente de novo.", None)
    set_cookie = response.headers.get("set-cookie")
    deletion = sessionid_deletion(set_cookie)
    # Só nome do cookie e motivos; nunca valores.
    logging.getLogger("runner").info(
        "Teste de sessão: HTTP %s; Set-Cookie sessionid %s", response.status_code,
        f"apagado ({', '.join(deletion)})" if deletion else "não apagado")
    return classify_session_response(response.status_code, response.headers.get("location"), response.text,
                                     set_cookie)


def op_test_session(request: dict[str, Any], emit: Emit) -> int:
    """Uma requisição: a primeira chamada do download que exige login, o feed da própria conta.

    ``GET /api/v1/feed/user/<ds_user_id>/?count=1`` (sem ds_user_id, o prefixo numérico do sessionid), sem seguir
    redirect, sem nova tentativa e sem dormir em 429.
    """
    cookies = request["cookies"]
    if not cookies.get("sessionid"):
        raise RunnerError("session_missing", "Nenhum sessionid informado")
    uid = _session_user_id(cookies)
    if uid is None:
        emit({"type": "session_test", **_session_outcome(
            "invalid", "O sessionid não traz o id da conta. Copie o sessionid completo do navegador e salve.",
            None)})
        return 1
    # A URL só serve para instanciar o extractor do Instagram; ela não é requisitada.
    job = _make_job(f"{ROOT}/instagram/info/")
    extr = job.extractor
    extr.login()
    # Com isto api._call devolve a resposta crua (mesmos headers do gallery-dl) em vez de decodificar JSON.
    extr.request_json = extr.request
    outcome = _probe_session(extr, f"/v1/feed/user/{uid}/", {"count": "1"}, TEST_HTTP_TIMEOUT)
    emit({"type": "session_test", **outcome})
    return 1


def op_profile(request: dict[str, Any], emit: Emit) -> int:
    job = _make_job(f"{ROOT}/{request['username']}/info/")
    user = _fetch_profile(job, request["username"])
    emit({"type": "profile", **profile_from_user(user)})
    return 1


def op_list(request: dict[str, Any], emit: Emit) -> int:
    username = request["username"]
    content = request["content"]
    limit = request["limit"]

    info_job = _make_job(f"{ROOT}/{username}/info/")
    user = _fetch_profile(info_job, username)
    profile = profile_from_user(user)
    emit({"type": "profile", **profile})
    own_profile = profile["user_id"] is not None and profile["user_id"] == _session_user_id(request["cookies"])
    # web_profile_info traz followed_by_viewer; o topsearch traz friendship_status.following. Sem nenhum dos dois
    # não se sabe se a conta segue o perfil: a listagem tenta (o upstream só avisa) e o erro real é classificado.
    friendship = user.get("friendship_status")
    known = "followed_by_viewer" in user or (isinstance(friendship, dict) and "following" in friendship)
    followed = user.get("followed_by_viewer") or (isinstance(friendship, dict) and friendship.get("following"))
    if user.get("is_private") and known and not followed and not own_profile:
        raise RunnerError("profile_private", "O perfil é privado e a conta da sessão não o segue")
    if not profile["user_id"]:
        raise RunnerError("profile_not_found", "Perfil sem identificador")

    from_reels = content == "reels"
    section = "reels" if from_reels else "posts"
    count = 0

    def on_url(url: str, kwdict: dict[str, Any]) -> None:
        nonlocal count
        item = normalize_file(url, kwdict, profile=profile["username"], from_reels=from_reels)
        if item is None or not content_matches(content, item["media_type"]):
            return
        emit({"type": "item", **item})
        count += 1
        if count >= limit:
            raise _LimitReached()

    job = _make_job(f"{ROOT}/id:{profile['user_id']}/{section}/", on_url, session=info_job.extractor.session)
    try:
        job.dispatch(job.extractor)
    except _LimitReached:
        pass
    return count


def op_resolve_post(request: dict[str, Any], emit: Emit) -> int:
    count = 0

    def on_url(url: str, kwdict: dict[str, Any]) -> None:
        nonlocal count
        item = normalize_file(url, kwdict, profile=None, from_reels=False)
        if item is not None:
            emit({"type": "item", **item})
            count += 1

    job = _make_job(f"{ROOT}/p/{request['shortcode']}/", on_url)
    job.extractor.login()
    job.dispatch(job.extractor)
    if count == 0:
        raise RunnerError("content_unavailable", "Post sem mídias disponíveis")
    return count


# --------------------------------------------------------------------------- diagnóstico (sem segredos na saída)

_SAFE_HEADERS = frozenset({
    "user-agent", "accept", "accept-language", "accept-encoding", "referer", "origin", "x-ig-app-id", "x-asbd-id",
    "x-requested-with", "connection", "content-type", "content-length",
})
INSPECT_COOKIES = ("sessionid", "ds_user_id", "csrftoken", "mid", "ig_did")
_INSPECT_BODY = b'{"user": {"username": "inspect"}, "data": {"user": {"id": "0", "username": "inspect"}}}'


def _header_view(headers) -> dict[str, str]:
    """Valores só dos headers não sensíveis; os outros (Cookie, X-CSRFToken...) aparecem como <present>."""
    view = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower in _SAFE_HEADERS or lower.startswith("sec-fetch-"):
            view[name] = str(value)
        elif lower == "x-ig-www-claim" and value == "0":
            view[name] = "0"
        else:
            view[name] = "<present>"
    return view


def _cookie_names(header: str | None) -> list[str]:
    return [part.split("=", 1)[0].strip() for part in (header or "").split(";") if "=" in part]


class _CaptureAdapter:
    """Adapter requests que guarda a PreparedRequest e devolve uma resposta falsa; nada sai pela rede."""

    def __init__(self):
        self.captured = []

    def send(self, request, **kwargs):
        import requests

        self.captured.append(request)
        response = requests.Response()
        response.status_code = 200
        response.reason = "OK"
        response.headers["Content-Type"] = "application/json"
        response._content = _INSPECT_BODY
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass


def _capture_on(extr) -> tuple[_CaptureAdapter, list[bool]]:
    adapter = _CaptureAdapter()
    extr.session.mount(f"{ROOT}/", adapter)
    redirects: list[bool] = []
    original_send = extr.session.send

    def send(prepared, **kwargs):
        redirects.append(bool(kwargs.get("allow_redirects", True)))
        return original_send(prepared, **kwargs)

    extr.session.send = send
    return adapter, redirects


def _describe(flow: str, extr, prepared, allow_redirects: bool, uid: str | None) -> dict[str, Any]:
    import gallery_dl.version

    parts = urlsplit(prepared.url)
    query_keys = sorted({pair.split("=", 1)[0] for pair in parts.query.split("&") if pair})
    path = parts.path.replace(uid, "<own id>") if uid else parts.path
    names = _cookie_names(prepared.headers.get("Cookie"))
    jar_csrf = extr.session.cookies.get("csrftoken", domain=extr.cookies_domain)
    return {
        "type": "inspect",
        "flow": flow,
        "method": prepared.method,
        "url": f"{parts.scheme}://{parts.netloc}{path}",
        "query_keys": query_keys,
        "headers": _header_view(prepared.headers),
        # Listas de nomes (um dict nome->present seria mascarado pela redação de segredos do processo pai).
        "cookie_header_present": [name for name in INSPECT_COOKIES if name in names],
        "cookie_header_absent": [name for name in INSPECT_COOKIES if name not in names],
        "cookie_header_other": sorted(set(names) - set(INSPECT_COOKIES)),
        "jar": sorted(({"name": c.name, "domain": c.domain, "path": c.path} for c in extr.session.cookies),
                      key=lambda c: c["name"]),
        "x_csrftoken_matches_cookie": bool(jar_csrf) and prepared.headers.get("X-CSRFToken") == jar_csrf,
        "allow_redirects": allow_redirects,
        "gallery_dl_version": gallery_dl.version.__version__,
    }


def op_inspect_request(request: dict[str, Any], emit: Emit) -> int:
    """Monta as requisições do teste de sessão e da 1a chamada da listagem, captura e descreve; não envia."""
    from gallery_dl.extractor.common import Extractor

    username = request["username"]
    uid = _session_user_id(request["cookies"])

    job = _make_job(f"{ROOT}/instagram/info/")
    extr = job.extractor
    extr.login()
    extr.request_json = extr.request
    adapter, redirects = _capture_on(extr)
    _probe_session(extr, f"/v1/feed/user/{uid or '0'}/", {"count": "1"}, TEST_HTTP_TIMEOUT)
    emit(_describe("test_session", extr, adapter.captured[-1], redirects[-1], uid))

    Extractor.request_timestamp = 0.0  # nada foi enviado: sem espera entre as duas montagens
    info_job = _make_job(f"{ROOT}/{username}/info/")
    adapter, redirects = _capture_on(info_job.extractor)
    _fetch_profile(info_job, username)
    # 1a requisição da resolução do perfil (topsearch); as seguintes são fallbacks diante da resposta falsa.
    emit(_describe("list_profile", info_job.extractor, adapter.captured[0], redirects[0], uid))
    return 2


def op_probe(request: dict[str, Any], emit: Emit) -> int:
    """UMA requisição ao endpoint informado, sem seguir redirect e sem nova tentativa; só metadados seguros."""
    from gallery_dl import exception as gexc

    job = _make_job(f"{ROOT}/instagram/info/")
    extr = job.extractor
    extr.login()
    extr.request_json = extr.request
    kwargs: dict[str, Any] = {"method": request["method"], "params": request["params"] or None,
                              "allow_redirects": False, "expected": range(400, 600), "timeout": TEST_HTTP_TIMEOUT}
    started = time.monotonic()
    event: dict[str, Any] = {"type": "probe", "method": request["method"], "endpoint": request["endpoint"]}
    try:
        response = extr.api._call(request["endpoint"][len("/api"):], **kwargs)
    except gexc.HttpError as exc:
        text = str(exc).lower()
        event.update(error="timeout" if "timed out" in text or "timeout" in text else "network_error",
                     elapsed_ms=int((time.monotonic() - started) * 1000))
        emit(event)
        return 1
    location = response.headers.get("location")
    loc = urlsplit(location) if location else None
    try:
        data = json.loads(response.text) if response.text.strip() else None
    except ValueError:
        data = None
    event.update(
        status=response.status_code,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        location=f"{loc.scheme}://{loc.netloc}{loc.path}" if loc else None,
        redirect=redirect_kind(location) if 300 <= response.status_code < 400 else None,
        body_length=len(response.content or b""),
        content_type=response.headers.get("content-type"),
        json_keys=sorted(data.keys()) if isinstance(data, dict) else None,
    )
    emit(event)
    return 1


OPERATION_HANDLERS = {
    "test_session": op_test_session,
    "profile": op_profile,
    "list": op_list,
    "resolve_post": op_resolve_post,
    "inspect_request": op_inspect_request,
    "probe": op_probe,
}


# --------------------------------------------------------------------------- processo


class _RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if len(s) >= 4]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact(message, self._secrets)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


_COOKIE_NAMES = r"(?:sessionid|csrftoken|ds_user_id|mid|ig_did|rur)"
_PATTERNS = (
    # sessionid=valor / sessionid: valor / sessionid="valor" (limite de palavra evita "humid=")
    re.compile(r"(?i)\b(" + _COOKIE_NAMES + r")(\s*[=:]\s*[\"']?)[^;\s\"',}]+"),
    # "sessionid": "valor" (JSON) e 'sessionid': 'valor' (repr de dict)
    re.compile(r"(?i)([\"']" + _COOKIE_NAMES + r"[\"']\s*:\s*[\"'])[^\"']*"),
)


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    text = _PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    text = _PATTERNS[1].sub(lambda m: f"{m.group(1)}<redacted>", text)
    return text


def _setup_logging(secrets: list[str]) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(name)s][%(levelname)s] %(message)s"))
    handler.addFilter(_RedactFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> int:
    out = sys.stdout
    # Qualquer print acidental de bibliotecas vai para o stderr; o stdout é só do protocolo.
    sys.stdout = sys.stderr

    def emit(obj: dict[str, Any]) -> None:
        out.write(json.dumps(obj, ensure_ascii=True) + "\n")
        out.flush()

    _setup_logging([])
    try:
        request = parse_request(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
    except RunnerError as exc:
        emit({"type": "error", "code": exc.code, "message": exc.message})
        return 2

    cookies = request["cookies"]
    _setup_logging(list(cookies.values()))
    try:
        _configure_gallery_dl(request)
        count = OPERATION_HANDLERS[request["op"]](request, emit)
    except BaseException as exc:  # noqa: BLE001 - tudo vira uma linha de erro classificada
        if isinstance(exc, KeyboardInterrupt):
            return 130
        code, message = classify_exception(exc, op=request["op"], has_session=bool(cookies.get("sessionid")))
        logging.getLogger("runner").info("%s: %s", type(exc).__name__, redact(message, list(cookies.values())))
        emit({"type": "error", "code": code, "message": redact(message, list(cookies.values()))})
        return 1
    emit({"type": "done", "count": count})
    return 0


if __name__ == "__main__":
    sys.exit(main())
