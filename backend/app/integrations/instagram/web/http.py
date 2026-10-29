"""Cliente HTTP do Instagram Web (contrato: web/http.py).

Host fixo ``https://www.instagram.com``, sem redirect automático, sem proxy/variáveis de ambiente, sem retries e com
prazo total por requisição. Os cookies da ``InstagramSession`` ficam só no jar em memória (domínio
``.instagram.com``, path ``/``); Set-Cookie das respostas atualiza esse jar. Nunca registra corpo, cookies ou
valores de headers sensíveis.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from app.core.logging import register_secret
from app.integrations.instagram.session import InstagramSession
from app.integrations.instagram.web.errors import InstagramWebError, loads_json

try:
    from gallery_dl.util import USERAGENT_CHROME as _GDL_USERAGENT_CHROME
except Exception:  # gallery-dl ausente ou sem a constante
    _GDL_USERAGENT_CHROME = None

_FALLBACK_USERAGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)
DEFAULT_USER_AGENT: str = _GDL_USERAGENT_CHROME if isinstance(_GDL_USERAGENT_CHROME, str) else _FALLBACK_USERAGENT

COOKIE_DOMAIN = ".instagram.com"
COOKIE_PATH = "/"
_HOST = "www.instagram.com"
_CHROME_RE = re.compile(r"Chrome/(\d+)")
_log = logging.getLogger("media_tool.instagram.web.http")


@dataclass(frozen=True)
class WebHttpConfig:
    timeout: float = 15.0  # prazo total por requisição; retries 0
    user_agent: str = DEFAULT_USER_AGENT
    app_id: str = "936619743392459"
    asbd_id: str = "359341"  # valor do frontend atual; configurável
    accept_language: str = "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"


class WebResponse:
    """Resposta em memória. Classe com ``__slots__`` (sem ``__dict__``): ``vars``/``dataclasses.asdict`` não a
    expõem; repr redigido; pickle/copy bloqueados. ``text`` NUNCA deve ser logado."""

    __slots__ = ("status", "location_path", "content_type", "elapsed_ms", "set_cookie_names",
                 "sessionid_deleted", "text", "path", "location_origin")

    def __init__(self, status: int, location_path: str | None, content_type: str, elapsed_ms: int,
                 set_cookie_names: list[str], sessionid_deleted: bool, text: str, path: str = "",
                 location_origin: str = "") -> None:
        self.status = status
        self.location_path = location_path
        self.content_type = content_type
        self.elapsed_ms = elapsed_ms
        self.set_cookie_names = set_cookie_names
        self.sessionid_deleted = sessionid_deleted
        self.text = text  # NUNCA logar; repr não mostra
        self.path = path  # path da requisição, sem query (para diagnósticos)
        # "esquema://host" do Location em minúsculas ("" = relativo); nunca logado (não entra no repr).
        self.location_origin = location_origin

    def json(self) -> object:
        """Corpo como JSON (aceita o prefixo ``for (;;);``); ``ValueError`` se inválido."""
        return loads_json(self.text)

    def __repr__(self) -> str:
        return (f"WebResponse(status={self.status}, path={self.path!r}, location_path={self.location_path!r}, "
                f"content_type={self.content_type!r}, size={len(self.text or '')}, "
                f"set_cookie_names={self.set_cookie_names!r}, sessionid_deleted={self.sessionid_deleted})")

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return repr(self)

    def __getstate__(self) -> object:
        raise TypeError("WebResponse não é serializável")

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("WebResponse não é serializável")

    def __reduce__(self) -> object:
        raise TypeError("WebResponse não é serializável")

    def __copy__(self) -> object:
        raise TypeError("WebResponse não é copiável")

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError("WebResponse não é copiável")


# --------------------------------------------------------------------------- auxiliares


def validate_path(path: object) -> str:
    """Aceita só caminho absoluto no host fixo: começa com "/", sem "//", sem esquema, sem "\\" nem controle."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("path inválido: deve começar com '/'")
    if "//" in path or "\\" in path:
        raise ValueError("path inválido: '//' e '\\' não são permitidos")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        raise ValueError("path inválido: caracteres de controle (CR/LF) não são permitidos")
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        raise ValueError("path inválido: esquema/host não são permitidos")
    return path


def _referer_url(referer: str | None) -> str | None:
    if referer is None:
        return None
    if referer.startswith(InstagramWebHttp.BASE + "/"):
        validate_path(referer[len(InstagramWebHttp.BASE):])
        return referer
    return InstagramWebHttp.BASE + validate_path(referer)


def _cookie_deletes(header: str) -> tuple[str, bool]:
    """(nome, apaga?) de um Set-Cookie. Apaga = valor vazio/"" , Max-Age <= 0 ou Expires no passado."""
    parts = [part.strip() for part in header.split(";")]
    name, _, value = parts[0].partition("=")
    deleted = value.strip() in ("", '""')
    for attr in parts[1:]:
        key, _, attr_value = attr.partition("=")
        key = key.strip().lower()
        if key == "max-age":
            try:
                if int(attr_value.strip()) <= 0:
                    deleted = True
            except ValueError:
                pass
        elif key == "expires":
            try:
                moment = parsedate_to_datetime(attr_value.strip())
            except (TypeError, ValueError, IndexError):
                continue
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            if moment <= datetime.now(timezone.utc):
                deleted = True
    return name.strip(), deleted


def _sec_ch_headers(user_agent: str) -> dict[str, str]:
    match = _CHROME_RE.search(user_agent)
    if not match:
        return {}
    major = match.group(1)
    return {
        "sec-ch-ua": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


# --------------------------------------------------------------------------- cliente


class InstagramWebHttp:
    BASE = "https://www.instagram.com"

    def __init__(self, session: InstagramSession, config: WebHttpConfig = WebHttpConfig(), *,
                 transport: httpx.BaseTransport | None = None, gate: object | None = None) -> None:
        self._config = config
        # Portão global (seção 8 de docs/BATCH_ROBUSTNESS.md): cada requisição ao instagram.com ocupa UMA vaga
        # (``gate.slot(cancel)``) só durante o envio/leitura. Ninguém mais segura o portão para este cliente
        # (o portão não é reentrante). ``cancel`` é o evento da operação corrente (definido pelo provider).
        self._gate = gate
        self.cancel: threading.Event | None = None
        jar = httpx.Cookies()
        for name, value in session.cookies().items():
            register_secret(value)
            jar.set(name, value, domain=COOKIE_DOMAIN, path=COOKIE_PATH)
        self._client = httpx.Client(
            cookies=jar,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(config.timeout),
            transport=transport,
            headers={"User-Agent": config.user_agent},
        )

    @classmethod
    def anonymous(cls, config: WebHttpConfig = WebHttpConfig(), *, transport: httpx.BaseTransport | None = None,
                  gate: object | None = None) -> InstagramWebHttp:
        """Cliente SEM cookies do dono (jar vazio): nenhuma sessão salva viaja nas requisições dele. Usado para o
        documento público de uma publicação; descartado ao fim da operação."""
        return cls(InstagramSession({}), config, transport=transport, gate=gate)

    def __repr__(self) -> str:
        return "InstagramWebHttp(<redacted>)"

    __str__ = __repr__

    def __getstate__(self) -> object:
        raise TypeError("InstagramWebHttp não é serializável")

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("InstagramWebHttp não é serializável")

    # ----- headers

    def _common_headers(self) -> dict[str, str]:
        headers = {"User-Agent": self._config.user_agent}
        headers.update(_sec_ch_headers(self._config.user_agent))
        headers["Accept-Language"] = self._config.accept_language
        return headers

    def _document_headers(self, referer: str | None) -> dict[str, str]:
        headers = self._common_headers()
        headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,"
                      "*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin" if referer else "none",
            "Sec-Fetch-User": "?1",
        })
        if referer:
            headers["Referer"] = referer
        return headers

    def _api_headers(self, referer: str | None) -> dict[str, str]:
        headers = self._common_headers()
        headers.update({
            "Accept": "*/*",
            "X-IG-App-ID": self._config.app_id,
            "X-ASBD-ID": self._config.asbd_id,
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Referer": referer or self.BASE + "/",
        })
        token = self.csrftoken()
        if token:
            headers["X-CSRFToken"] = token
        return headers

    # ----- requisições

    def get_document(self, path: str, *, referer: str | None = None, max_bytes: int | None = None,
                     diagnostic_path: str | None = None) -> WebResponse:
        """``max_bytes``: teto do corpo (None = sem teto, como o bootstrap); acima dele, ``contract_changed`` com
        detail ``document_too_large``. ``diagnostic_path``: token fixo que substitui o path real no log, nos
        diagnósticos e em ``WebResponse.path`` (para path que carrega o shortcode de uma publicação)."""
        validate_path(path)
        return self._send("GET", path, headers=self._document_headers(_referer_url(referer)), max_bytes=max_bytes,
                          diagnostic_path=diagnostic_path)

    def get_api(self, path: str, params: Mapping[str, str] | None = None, *,
                referer: str | None = None) -> WebResponse:
        validate_path(path)
        return self._send("GET", path, headers=self._api_headers(_referer_url(referer)), params=params)

    def post_form(self, path: str, form: Mapping[str, str], headers: Mapping[str, str]) -> WebResponse:
        validate_path(path)
        merged = httpx.Headers(self._api_headers(None))
        merged["Origin"] = self.BASE
        merged["Content-Type"] = "application/x-www-form-urlencoded"
        del merged["X-Requested-With"]  # o POST GraphQL do navegador não envia; só se o chamador passar
        for name, value in headers.items():  # sem duplicar nomes com outra caixa (x-csrftoken / X-CSRFToken)
            merged[name] = value
        return self._send("POST", path, headers=merged, data=dict(form))

    def csrftoken(self) -> str | None:
        for cookie in self._client.cookies.jar:
            if cookie.name == "csrftoken" and cookie.value:
                return cookie.value
        return None

    def account_id(self) -> str | None:
        """Cookie ``ds_user_id`` do jar (id da conta da sessão); nunca logado, registrado no redator."""
        for cookie in self._client.cookies.jar:
            if cookie.name == "ds_user_id" and cookie.value:
                register_secret(cookie.value)
                return cookie.value
        return None

    def close(self) -> None:
        self._client.close()

    # ----- núcleo

    def _send(self, method: str, path: str, *, headers: Mapping[str, str],
              params: Mapping[str, str] | None = None, data: Mapping[str, str] | None = None,
              max_bytes: int | None = None, diagnostic_path: str | None = None) -> WebResponse:
        url = httpx.URL(self.BASE + path)
        if url.scheme != "https" or url.host != _HOST:  # defesa extra; validate_path já garante
            raise ValueError("path inválido: host fora do permitido")
        safe_path = diagnostic_path if diagnostic_path is not None else urlsplit(path).path
        too_large = False
        request = self._client.build_request(method, url, params=params, data=data, headers=headers)
        timeout = self._config.timeout
        # GateCancelled (code "cancelled") sobe como está se a operação for cancelada enquanto espera a vez.
        slot = self._gate.slot(self.cancel) if self._gate is not None else nullcontext()
        with slot:
            start = time.monotonic()
            try:
                response = self._client.send(request, stream=True)
                try:
                    chunks: list[bytes] = []
                    # Prazo total checado entre chunks; um chunk parado é limitado pelo read timeout do httpx
                    # (mesmo valor), então o pior caso fica perto de 2x o timeout configurado.
                    received = 0
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if max_bytes is not None and received > max_bytes:
                            too_large = True   # para de acumular; o corpo é descartado
                            break
                        chunks.append(chunk)
                        if time.monotonic() - start > timeout:
                            raise httpx.ReadTimeout("prazo total excedido")
                finally:
                    response.close()
            except httpx.TimeoutException as exc:
                raise InstagramWebError(
                    "timeout", "O Instagram não respondeu a tempo.", retryable=True, stage="http",
                    diagnostic={"method": method, "path": safe_path, "exception": type(exc).__name__},
                ) from None
            except httpx.RequestError as exc:
                raise InstagramWebError(
                    "network_error", "Falha de rede ao acessar o Instagram.", retryable=True, stage="http",
                    diagnostic={"method": method, "path": safe_path, "exception": type(exc).__name__},
                ) from None
            elapsed_ms = int((time.monotonic() - start) * 1000)

        if too_large:
            del chunks
            raise InstagramWebError(
                "contract_changed", "O Instagram mudou a forma de responder e o Media Tool precisa ser atualizado.",
                stage="http", diagnostic={"detail": "document_too_large", "method": method, "path": safe_path})
        body = b"".join(chunks)
        try:
            text = body.decode(response.charset_encoding or "utf-8", errors="replace")
        except LookupError:
            # charset desconhecido no Content-Type: lê como UTF-8 em vez de estourar como erro interno
            text = body.decode("utf-8", errors="replace")
        set_cookie_names: list[str] = []
        sessionid_deleted = False
        for header in response.headers.get_list("set-cookie"):
            name, deleted = _cookie_deletes(header)
            if name:
                set_cookie_names.append(name)
            if name == "sessionid" and deleted:
                sessionid_deleted = True
        self._normalize_jar(sessionid_deleted)

        location = response.headers.get("location")
        location_parts = urlsplit(location) if location else None
        location_path = (location_parts.path or "/") if location_parts is not None else None
        location_origin = (f"{location_parts.scheme}://{location_parts.netloc}".lower()
                           if location_parts is not None and location_parts.netloc else "")
        result = WebResponse(
            status=response.status_code,
            location_path=location_path,
            content_type=response.headers.get("content-type", ""),
            elapsed_ms=elapsed_ms,
            set_cookie_names=set_cookie_names,
            sessionid_deleted=sessionid_deleted,
            text=text,
            path=safe_path,
            location_origin=location_origin,
        )
        _log.debug("instagram web %s %s -> %s (%d ms, %d bytes)", method, safe_path, result.status, elapsed_ms,
                   len(body))
        return result

    def _normalize_jar(self, sessionid_deleted: bool) -> None:
        """Mantém um cookie por nome no domínio ``.instagram.com`` path ``/`` e registra valores como segredo.

        Set-Cookie sem ``Domain`` cria cookie só do host; ele passa a valer para ``.instagram.com`` (o mais
        recente vence). Set-Cookie apagando o ``sessionid`` remove todas as cópias dele.
        """
        jar = self._client.cookies.jar
        moved: dict[str, str] = {}
        for cookie in list(jar):
            if sessionid_deleted and cookie.name == "sessionid":
                jar.clear(cookie.domain, cookie.path, cookie.name)
                continue
            if cookie.value:
                register_secret(cookie.value)
            if cookie.domain != COOKIE_DOMAIN or cookie.path != COOKIE_PATH:
                moved[cookie.name] = cookie.value or ""
                jar.clear(cookie.domain, cookie.path, cookie.name)
        for name, value in moved.items():
            self._client.cookies.set(name, value, domain=COOKIE_DOMAIN, path=COOKIE_PATH)
