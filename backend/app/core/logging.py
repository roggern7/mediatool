"""Configuração de logging com redação de segredos.

Todo registro que passa pelo logger raiz ou pelos handlers configurados aqui tem mensagem, argumentos,
traceback e stack info redigidos: valores registrados via ``register_secret`` e padrões conhecidos de
cookies/cabeçalhos de autenticação (sessionid, csrftoken, ds_user_id, Cookie, Set-Cookie, Authorization).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import threading
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

REDACTED = "<redacted>"
LOG_FILENAME = "media-tool.log"
_MIN_SECRET_LENGTH = 6
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_secrets: set[str] = set()
_secrets_sorted: tuple[str, ...] = ()
_lock = threading.Lock()

MEDIA_URL_PLACEHOLDER = "<url-midia>"
_CDN_HOST_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")
_CDN_HOSTS = ("cdninstagram.com", "fbcdn.net")
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s\"'<>`]+")
# Token opaco (cursor, assinatura): 40+ caracteres com maiúscula, minúscula e dígito. Hexadecimal puro (SHA-256,
# UUID) e identificadores/nomes de pasta sem essa mistura não são tocados.
_OPAQUE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_\-=])[A-Za-z0-9_\-=]{40,}(?![A-Za-z0-9_\-=])")
_HEX_RE = re.compile(r"[0-9A-Fa-f]+")
# URL de CDN sem esquema (``scontent-gru1-1.cdninstagram.com/v/...``, ``//instagram.x.fna.fbcdn.net/o1/...``,
# também com barras escapadas de JSON ``\/``): host *.cdninstagram.com / *.fbcdn.net seguido de caminho ou query
# vira <url-midia> inteira. Host sozinho (sem ``/`` ou ``?`` depois) não é URL assinada e fica intacto, para não
# apagar menções ao domínio (ex.: constantes no código citadas por um traceback).
_SCHEMELESS_CDN_URL_RE = re.compile(
    r"(?i)(?<![\w.\-/\\@])(?:\\?/\\?/)?(?:[a-z0-9-]+\.)*(?:cdninstagram\.com|fbcdn\.net)(?::\d+)?"
    r"(?![\w-])\\?[/?][^\s\"'<>`]*"
)
# Cursor numérico isolado, sem chave: 16+ dígitos, opcionalmente seguidos de ``_dígitos`` (formato do Instagram
# ``3400000000000000000_25025320``). Só casa como token solto: não pode estar colado a letra/dígito/``_``, ``.``,
# ``-``, ``/`` ou ``\`` de nenhum dos lados (``.`` final de frase é aceito). Assim ficam intactos SHA-256/hex
# (têm letras ao redor), ids de job/media (até 12 dígitos), tamanhos em bytes e portas (menos de 16 dígitos),
# timestamps ISO (blocos curtos separados por ``-``/``:``), decimais de float (``0.30000000000000004``),
# endereços ``0x...`` e caminhos/nomes de arquivo por sha (``\`` ou ``/`` antes, ``.ext`` depois).
_NUMERIC_CURSOR_RE = re.compile(r"(?<![\w.\-/\\])\d{16,}(?:_\d+)?(?![\w\-/\\]|\.\w)")

_COOKIE_NAMES = r"(?:sessionid|csrftoken|ds_user_id|ig_did|mid|rur|fb_dtsg|lsd|jazoest|end_cursor|after|cursor)"
_HEADER_NAMES = r"(?:set-cookie|cookie|authorization|x-fb-lsd|x-csrftoken)"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Cabeçalhos inteiros: Cookie / Set-Cookie / Authorization / X-FB-LSD / X-CSRFToken (até o fim da linha).
    (re.compile(r"(?i)(?<![\w-])(" + _HEADER_NAMES + r")(\s*:\s*)[^\r\n]+"), r"\1\2" + REDACTED),
    # JSON / repr de dict: "sessionid": "valor"  ou  'lsd': 'valor'. Aspas podem vir escapadas
    # (\"lsd\": \"valor\"), como em repr de string JSON ou na linha de código de um traceback.
    (re.compile(r"(?i)(\\?[\"'])(" + _COOKIE_NAMES + r"|" + _HEADER_NAMES + r")\1(\s*:\s*)(\\?[\"'])(?:(?!\4).)*\4"),
     r"\1\2\1\3\4" + REDACTED + r"\4"),
    # JSON com valor sem aspas: "jazoest": 26123
    (re.compile(r"(?i)(\\?[\"'])(" + _COOKIE_NAMES + r"|" + _HEADER_NAMES + r")\1(\s*:\s*)(?![\"'\s\\])[^\s,}\]]+"),
     r"\1\2\1\3" + REDACTED),
    # sessionid=valor (cookies, query strings, linha de comando)
    (re.compile(r"(?i)\b(" + _COOKIE_NAMES + r")(\s*=\s*)(?:\"[^\"]*\"|[^\s;,&\"'}\]]+)"), r"\1\2" + REDACTED),
    # Bearer/Basic soltos
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 " + REDACTED),
)


def register_secret(value: str | None) -> None:
    """Registra um valor secreto para ser removido de qualquer log futuro.

    Valores curtos demais (menos de 6 caracteres) são ignorados para não destruir logs comuns.
    Também registra as variantes URL-decodificada e URL-codificada do valor.
    """
    global _secrets_sorted
    if not value or not isinstance(value, str):
        return
    variants = {value, urllib.parse.unquote(value), urllib.parse.quote(value, safe="")}
    with _lock:
        changed = False
        for variant in variants:
            if len(variant) >= _MIN_SECRET_LENGTH and variant not in _secrets:
                _secrets.add(variant)
                changed = True
        if changed:
            _secrets_sorted = tuple(sorted(_secrets, key=len, reverse=True))


def _reduce_url(match: re.Match[str]) -> str:
    """URL de CDN some por completo; as demais ficam só com esquema + host + path (sem userinfo/query/fragmento)."""
    url = match.group(0)
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return MEDIA_URL_PLACEHOLDER
    if host in _CDN_HOSTS or host.endswith(_CDN_HOST_SUFFIXES):
        return MEDIA_URL_PLACEHOLDER
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme.lower()}://{netloc}{parts.path}"


def _redact_opaque_token(match: re.Match[str]) -> str:
    token = match.group(0)
    if _HEX_RE.fullmatch(token):
        return token
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    has_digit = any(c.isdigit() for c in token)
    return REDACTED if has_upper and has_lower and has_digit else token


# A mesma URL de CDN percent-codificada (``scontent.cdninstagram.com%2Fv%2Fx.jpg%3Foh%3D...``, com ou sem
# ``https%3A%2F%2F``), como aparece dentro da query de outra URL ou num corpo de formulário.
_ENCODED_CDN_URL_RE = re.compile(
    r"(?i)(?:https?%3A)?(?:%2F%2F)?(?:[a-z0-9-]+\.)*(?:cdninstagram\.com|fbcdn\.net)(?:(?::|%3A)\d+)?"
    r"%(?:2F|3F)[^\s\"'<>`]*"
)
_REDACTED_REMAINDER_RE = re.compile(re.escape(REDACTED) + r"[A-Za-z0-9_%:\-]+")


def redact(text: object) -> str:
    """Retorna ``text`` (convertido em str) sem segredos registrados, credenciais, URLs assinadas e cursores."""
    if text is None:
        return ""
    result = text if isinstance(text, str) else str(text)
    # Formatos estruturais primeiro (URLs, cursores numéricos): se um segredo registrado fosse prefixo de um
    # cursor/URL e fosse trocado antes, o resto do valor escaparia das regras por não estar mais "solto".
    result = _URL_RE.sub(_reduce_url, result)
    result = _SCHEMELESS_CDN_URL_RE.sub(MEDIA_URL_PLACEHOLDER, result)
    result = _ENCODED_CDN_URL_RE.sub(MEDIA_URL_PLACEHOLDER, result)
    result = _NUMERIC_CURSOR_RE.sub(REDACTED, result)
    for secret in _secrets_sorted:
        if secret in result:
            result = result.replace(secret, REDACTED)
        lowered = secret.lower()   # hostname de URL é reduzido em minúsculas pela regra acima
        if lowered != secret and lowered in result:
            result = result.replace(lowered, REDACTED)
    # Um segredo registrado que seja só parte de um valor maior não deixa o resto do valor no texto.
    result = _REDACTED_REMAINDER_RE.sub(REDACTED, result)
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    result = _NUMERIC_CURSOR_RE.sub(REDACTED, result)
    return _OPAQUE_TOKEN_RE.sub(_redact_opaque_token, result)


def _redact_registered(text: str) -> str:
    """Remove só os segredos registrados (não aplica padrões, para não destruir placeholders %s)."""
    for secret in _secrets_sorted:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


class _RedactedArg:
    """Envolve um argumento de log não textual: str/repr/format saem redigidos."""

    __slots__ = ("_value",)

    def __init__(self, value: object) -> None:
        self._value = value

    def __str__(self) -> str:
        return redact(str(self._value))

    def __repr__(self) -> str:
        return redact(repr(self._value))

    def __format__(self, format_spec: str) -> str:
        return redact(format(self._value, format_spec))


def _redact_arg(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact(value)
    return _RedactedArg(value)


class RedactingFilter(logging.Filter):
    """Filtro que redige mensagem, argumentos, traceback e stack info do registro.

    Preserva a estrutura de ``record.args`` (tupla continua tupla, dict continua dict, números continuam
    números) para formatters que leem os argumentos diretamente, como o ``AccessFormatter`` do uvicorn.
    """

    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_media_tool_redacted", False):
            return True
        msg = record.msg if isinstance(record.msg, str) else str(record.msg)
        args = record.args
        if args:
            # Com argumentos, o template só perde segredos registrados; padrões são aplicados em cada
            # argumento e, na saída final, pelo RedactingFormatter.
            record.msg = _redact_registered(msg)
            if isinstance(args, Mapping):
                record.args = {key: _redact_arg(value) for key, value in args.items()}
            elif isinstance(args, tuple):
                record.args = tuple(_redact_arg(value) for value in args)
            else:
                record.args = _redact_arg(args)  # type: ignore[assignment]
            # Segredo que só aparece juntando template e argumentos (ex.: "%r" de um dict com sessionid):
            # nesse caso a segurança vence e a mensagem é pré-formatada (args=None).
            try:
                formatted = record.getMessage()
            except Exception:
                formatted = None
            if formatted is not None:
                safe = redact(formatted)
                if safe != formatted:
                    record.msg = safe
                    record.args = None
        else:
            record.msg = redact(msg)
        if record.exc_info:
            try:
                exc_text = self._formatter.formatException(record.exc_info)
            except Exception:
                exc_text = "<traceback indisponível>"
            record.exc_text = redact(exc_text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        record._media_tool_redacted = True
        return True


class RedactingFormatter(logging.Formatter):
    """Formatter que redige a saída final. Com ``inner``, delega a formatação a ele (ex.: formatter do uvicorn)."""

    def __init__(self, fmt: str | None = None, *, inner: logging.Formatter | None = None) -> None:
        super().__init__(fmt)
        self.inner = inner

    def format(self, record: logging.LogRecord) -> str:
        text = self.inner.format(record) if self.inner is not None else super().format(record)
        return redact(text)


def _wrap_handler_formatter(handler: logging.Handler) -> None:
    if isinstance(handler.formatter, RedactingFormatter):
        return
    handler.setFormatter(RedactingFormatter(inner=handler.formatter or logging.Formatter()))


_FILTER = RedactingFilter()

_JOB_LOGGER = "media_tool.jobs"
_CONSOLE_ENV = "MEDIA_TOOL_LOG_CONSOLE"


class JobDetailFilter(logging.Filter):
    """Rebaixa para DEBUG os detalhes de etapa de cada job (``Job %s: ...`` e ``Job %s iniciado``).

    Em INFO fica uma linha por job concluído/cancelado/falho; com nível DEBUG as etapas voltam a aparecer.
    Instalado só nos handlers do Media Tool, então não altera o registro visto por outros handlers.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != _JOB_LOGGER or record.levelno != logging.INFO:
            return True
        msg = record.msg if isinstance(record.msg, str) else ""
        if not (msg.startswith("Job %s: ") or msg.startswith("Job %s iniciado")):
            return True
        return logging.getLogger().isEnabledFor(logging.DEBUG)


_JOB_DETAIL_FILTER = JobDetailFilter()


def get_redacting_filter() -> RedactingFilter:
    """Filtro global (mesma instância instalada por ``configure_logging``)."""
    return _FILTER


def configure_logging(log_dir: Path, level: str | int = "INFO", *, console: bool | None = None) -> None:
    """Configura console + arquivo rotativo ``<log_dir>/media-tool.log`` com redação de segredos.

    ``console=None`` lê ``MEDIA_TOOL_LOG_CONSOLE`` (``0``/``false``/``no``/``off`` desliga o console); em segundo
    plano o ``start.ps1`` desliga, para o log não ser duplicado no stderr redirecionado.

    Idempotente: chamadas repetidas substituem os handlers criados anteriormente por esta função.
    O filtro também é instalado nos handlers já existentes de outros loggers (ex.: uvicorn), e o formatter
    deles é envolvido por ``RedactingFormatter`` para redigir a saída final sem alterar o formato.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(level, str):
        level_value = logging.getLevelName(level.upper())
        if not isinstance(level_value, int):
            level_value = logging.INFO
    else:
        level_value = level

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_media_tool_handler", False):
            root.removeHandler(handler)
            handler.close()

    if console is None:
        console = os.environ.get(_CONSOLE_ENV, "1").strip().lower() not in ("0", "false", "no", "off")

    formatter = RedactingFormatter(_LOG_FORMAT)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            log_dir / LOG_FILENAME, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
    ]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(_FILTER)
        handler.addFilter(_JOB_DETAIL_FILTER)
        handler._media_tool_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    root.setLevel(level_value)
    # httpx/httpcore registram a URL completa (URLs assinadas da CDN) em INFO/DEBUG.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if _FILTER not in root.filters:
        root.addFilter(_FILTER)
    install_filter_on_existing_handlers()


def install_filter_on_existing_handlers() -> None:
    """Adiciona o filtro de redação e o formatter redator a todos os handlers já registrados."""
    loggers: list[logging.Logger] = [logging.getLogger()]
    for obj in list(logging.root.manager.loggerDict.values()):
        if isinstance(obj, logging.Logger):
            loggers.append(obj)
    for logger in loggers:
        for handler in logger.handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)
            _wrap_handler_formatter(handler)
