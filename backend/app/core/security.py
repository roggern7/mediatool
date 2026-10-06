"""Proteções HTTP locais e validadores de caminho.

``install_security`` aplica, de fora para dentro: cabeçalhos de segurança -> validação de Host -> CORS
restrito -> guarda de mutações (Origin permitido + header ``X-Media-Tool-Client``) -> aplicação.
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


class MutationGuardMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(o.lower().rstrip("/") for o in allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "").upper() not in MUTATING_METHODS:
            await self.app(scope, receive, send)
            return
        origin = _header(scope, b"origin")
        if origin is not None and origin.strip().lower().rstrip("/") not in self.allowed_origins:
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
) -> None:
    """Instala middlewares de segurança e o handler genérico de exceções em ``app`` (FastAPI/Starlette).

    Deve ser chamado uma vez, durante a montagem do app (antes de iniciar o servidor).
    ``allow_testserver=True`` aceita o Host ``testserver`` do TestClient; use somente em testes.
    """
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", *extra_hosts}
    if allow_testserver:
        allowed_hosts.add("testserver")
    same_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    if allow_testserver:
        same_origins.add("http://testserver")
    mutation_origins = same_origins | set(cors_origins)

    # Starlette: o último add_middleware é o mais externo.
    app.add_middleware(MutationGuardMiddleware, allowed_origins=mutation_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", CLIENT_HEADER],
        allow_credentials=False,
        max_age=600,
    )
    app.add_middleware(HostValidationMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
