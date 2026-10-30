"""Proteções HTTP locais e validadores de caminho.

``install_security`` aplica, de fora para dentro: cabeçalhos de segurança -> validação de Host -> acesso da
extensão do Chrome (origem ``chrome-extension://`` só em ``/api/ext/*``, com CORS próprio) -> CORS restrito -> guarda
de mutações (Origin permitido + header ``X-Media-Tool-Client``) -> aplicação. O token da extensão é conferido nas
próprias rotas ``/api/ext/*`` (app/api/extension.py).
Também registra um handler genérico de exceções que nunca devolve stack trace nem segredos.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("media_tool.security")

CLIENT_HEADER = "X-Media-Tool-Client"
EXTENSION_TOKEN_HEADER = "X-Media-Tool-Token"
EXTENSION_PATH_PREFIX = "/api/ext/"
# Origem de uma extensão do Chrome: o ID tem 32 letras de "a" a "p".
EXTENSION_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}$")
EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")
# ID fixo da NOSSA extensão (derivado da `key` de extension/manifest.json). IDs extras (ex.: outra cópia sem
# compactação) só pela variável MEDIA_TOOL_EXTENSION_IDS (separados por vírgula; docs/EXTENSION.md).
EXTENSION_ID = "lnpealhnkmjaohdlkpapjmdealokkdbf"
EXTENSION_IDS_ENV = "MEDIA_TOOL_EXTENSION_IDS"
EXTENSION_CORS_METHODS = "GET, POST, OPTIONS"
EXTENSION_CORS_HEADERS = f"Authorization, Content-Type, {CLIENT_HEADER}, {EXTENSION_TOKEN_HEADER}"
DEFAULT_CORS_ORIGINS: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173")
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
)

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data: blob: https://*.cdninstagram.com https://*.fbcdn.net; "
    "media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
    "font-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
# A UI do Swagger/ReDoc carrega scripts de CDN e scripts inline; essas rotas ficam sem CSP.
_CSP_EXEMPT_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})

_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    | {f"COM{i}" for i in range(10)}
    | {f"LPT{i}" for i in range(10)}
    | {f"COM{c}" for c in "¹²³"}
    | {f"LPT{c}" for c in "¹²³"}
)
_INVALID_NAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class UnsafePathError(ValueError):
    """Caminho rejeitado (fora da raiz permitida ou com componente perigoso)."""


# --------------------------------------------------------------------------- caminhos


def safe_join(root: Path, relative: str) -> Path:
    """Junta ``relative`` a ``root`` e garante que o resultado resolvido fica dentro de ``root``.

    Rejeita: vazio, caminhos absolutos, letras de unidade, UNC/raiz (``/``, ``\\``), ``..``, ADS do NTFS
    (``:``), caracteres inválidos no Windows, nomes reservados (CON, NUL, COM1...) e componentes terminados
    em ponto ou espaço. Links/junções que escapem da raiz também são rejeitados (via ``resolve``).
    """
    if not isinstance(relative, str) or not relative:
        raise UnsafePathError("Caminho vazio ou inválido.")
    if relative[0] in "/\\":
        raise UnsafePathError("Caminho absoluto não permitido.")
    if _DRIVE_RE.match(relative):
        raise UnsafePathError("Letra de unidade não permitida.")
    parts: list[str] = []
    for part in re.split(r"[\\/]+", relative):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafePathError("Componente '..' não permitido.")
        if _INVALID_NAME_CHARS.search(part):
            raise UnsafePathError("Caractere não permitido no caminho.")
        if part.endswith((".", " ")):
            raise UnsafePathError("Componente terminado em ponto ou espaço não permitido.")
        stem = part.split(".", 1)[0].rstrip(" ").upper()
        if stem in _WINDOWS_RESERVED:
            raise UnsafePathError("Nome reservado do Windows não permitido.")
        parts.append(part)
    root_resolved = Path(root).resolve()
    candidate = root_resolved.joinpath(*parts).resolve()
    if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
        raise UnsafePathError("Caminho fora da pasta permitida.")
    return candidate


def ensure_within(path: Path, roots: Iterable[Path]) -> Path:
    """Resolve ``path`` (seguindo links) e garante que está dentro de alguma das ``roots``."""
    resolved = Path(path).resolve()
    for root in roots:
        root_resolved = Path(root).resolve()
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            return resolved
    raise UnsafePathError("Caminho fora das pastas permitidas.")


# --------------------------------------------------------------------------- middlewares ASGI


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def is_extension_origin(origin: str | None) -> bool:
    """Parece origem de QUALQUER extensão do Chrome (usado para barrar extensões fora de ``/api/ext/*``)."""
    return origin is not None and EXTENSION_ORIGIN_RE.match(origin.strip().lower().rstrip("/")) is not None


def extension_origins(extra_ids: str | None = None) -> frozenset[str]:
    """Origens aceitas em ``/api/ext/*``: o ID fixo da nossa extensão + IDs extras válidos de
    ``MEDIA_TOOL_EXTENSION_IDS`` (separados por vírgula; entradas inválidas são ignoradas)."""
    ids = {EXTENSION_ID}
    for part in (extra_ids or "").split(","):
        value = part.strip().lower()
        if EXTENSION_ID_RE.match(value):
            ids.add(value)
        elif value:
            logger.warning("ID de extensão inválido em %s ignorado", EXTENSION_IDS_ENV)
    return frozenset(f"chrome-extension://{value}" for value in ids)


def is_extension_path(path: str) -> bool:
    return path.startswith(EXTENSION_PATH_PREFIX) or path == EXTENSION_PATH_PREFIX.rstrip("/")


async def _send_json(send: Send, status: int, detail: str, code: str) -> None:
    body = json.dumps({"detail": detail, "code": code}, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        add_csp = path not in _CSP_EXEMPT_PATHS and not path.startswith("/docs/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for key, value in SECURITY_HEADERS:
                    if key not in present:
                        headers.append((key, value))
                if add_csp and b"content-security-policy" not in present:
                    headers.append((b"content-security-policy", CONTENT_SECURITY_POLICY.encode("ascii")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class HostValidationMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_hosts: Iterable[str]) -> None:
        self.app = app
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = (_header(scope, b"host") or "").strip().lower()
        if host not in self.allowed_hosts:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await _send_json(send, 400, "Host não permitido.", "invalid_host")
            return
        await self.app(scope, receive, send)


class ExtensionAccessMiddleware:
    """Origem ``chrome-extension://<id>``: aceita só em ``/api/ext/*`` (qualquer outra rota: 403, em qualquer
    método). Em ``/api/ext/*`` responde o preflight (com ``Access-Control-Allow-Private-Network`` quando pedido) e
    devolve os cabeçalhos CORS para a própria origem da extensão; outras origens que não sejam do próprio app
    recebem 403. Só a NOSSA extensão (``extension_origins``) é aceita; outra extensão recebe 403 também em
    ``/api/ext/*``. O token é exigido pelas rotas (app/api/extension.py), não aqui."""

    def __init__(self, app: ASGIApp, *, app_origins: Iterable[str], extension_origins: Iterable[str]) -> None:
        self.app = app
        self.app_origins = frozenset(o.lower().rstrip("/") for o in app_origins)
        self.extension_origins = frozenset(o.lower().rstrip("/") for o in extension_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = _header(scope, b"origin")
        from_extension = origin is not None and origin.strip().lower().rstrip("/") in self.extension_origins
        if not is_extension_path(scope.get("path", "")):
            if from_extension or is_extension_origin(origin):
                await _send_json(send, 403, "Origem não permitida.", "origin_not_allowed")
                return
            await self.app(scope, receive, send)
            return
        if not from_extension:
            if origin is not None and origin.strip().lower().rstrip("/") not in self.app_origins:
                await _send_json(send, 403, "Origem não permitida.", "origin_not_allowed")
                return
            await self.app(scope, receive, send)
            return
        cors = [
            (b"access-control-allow-origin", origin.strip().encode("latin-1")),
            (b"vary", b"Origin"),
        ]
        if scope.get("method", "").upper() == "OPTIONS":
            headers = [
                *cors,
                (b"access-control-allow-methods", EXTENSION_CORS_METHODS.encode()),
                (b"access-control-allow-headers", EXTENSION_CORS_HEADERS.encode()),
                (b"access-control-max-age", b"600"),
                (b"content-length", b"0"),
            ]
            if (_header(scope, b"access-control-request-private-network") or "").strip().lower() == "true":
                headers.append((b"access-control-allow-private-network", b"true"))
            await send({"type": "http.response.start", "status": 204, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"vary"]
                message["headers"] = [*headers, *cors]
            await send(message)

        await self.app(scope, receive, send_with_cors)


class MutationGuardMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_origins: Iterable[str],
                 extension_origins: Iterable[str] = ()) -> None:
        self.app = app
        self.allowed_origins = frozenset(o.lower().rstrip("/") for o in allowed_origins)
        self.extension_origins = frozenset(o.lower().rstrip("/") for o in extension_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "").upper() not in MUTATING_METHODS:
            await self.app(scope, receive, send)
            return
        origin = _header(scope, b"origin")
        # A origem da NOSSA extensão só chega aqui em /api/ext/* (ExtensionAccessMiddleware barra o resto).
        extension_ok = (origin is not None and origin.strip().lower().rstrip("/") in self.extension_origins
                        and is_extension_path(scope.get("path", "")))
        if origin is not None and not extension_ok and origin.strip().lower().rstrip("/") not in self.allowed_origins:
            await _send_json(send, 403, "Origem não permitida.", "origin_not_allowed")
            return
        client = _header(scope, CLIENT_HEADER.lower().encode())
        if client is None or not client.strip():
            await _send_json(
                send, 403, f"Header {CLIENT_HEADER} obrigatório em requisições de alteração.",
                "client_header_required",
            )
            return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- instalação


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Erro não tratado em %s %s", request.method, request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(status_code=500, content={"detail": "Erro interno.", "code": "internal_error"})


def install_security(
    app,
    *,
    port: int,
    extra_hosts: Sequence[str] = (),
    cors_origins: Sequence[str] = DEFAULT_CORS_ORIGINS,
    allow_testserver: bool = False,
    extension_ids: str | None = None,
) -> None:
    """Instala middlewares de segurança e o handler genérico de exceções em ``app`` (FastAPI/Starlette).

    Deve ser chamado uma vez, durante a montagem do app (antes de iniciar o servidor).
    ``allow_testserver=True`` aceita o Host ``testserver`` do TestClient; use somente em testes.
    ``extension_ids``: IDs extras de extensão aceitos em ``/api/ext/*`` (valor de MEDIA_TOOL_EXTENSION_IDS).
    """
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", *extra_hosts}
    if allow_testserver:
        allowed_hosts.add("testserver")
    same_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    if allow_testserver:
        same_origins.add("http://testserver")
    mutation_origins = same_origins | set(cors_origins)

    # Starlette: o último add_middleware é o mais externo.
    ext_origins = extension_origins(extension_ids)
    app.add_middleware(MutationGuardMiddleware, allowed_origins=mutation_origins, extension_origins=ext_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", CLIENT_HEADER],
        allow_credentials=False,
        max_age=600,
    )
    app.add_middleware(ExtensionAccessMiddleware, app_origins=mutation_origins, extension_origins=ext_origins)
    app.add_middleware(HostValidationMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
