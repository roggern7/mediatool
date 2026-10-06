"""Respostas de erro no formato `{detail, code}`.

Erros de validação nunca ecoam o valor recebido nem chaves desconhecidas do JSON (um segredo enviado como nome de
campo não volta na resposta).
"""

from __future__ import annotations

import logging
import re

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError
from app.services.storage import FileBusyError, StorageError

logger = logging.getLogger("media_tool.api")

UNKNOWN_FIELD = "<campo desconhecido>"
_FIELD_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
_HTTP_CODES = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}


def _safe_loc(error: dict) -> list[str]:
    """`loc` sem nomes arbitrários: chave extra (extra_forbidden) ou fora do padrão de campo vira UNKNOWN_FIELD."""
    loc = list(error.get("loc", ()))
    safe: list[str] = []
    for index, part in enumerate(loc):
        if isinstance(part, int):
            safe.append(str(part))
        elif error.get("type") == "extra_forbidden" and index == len(loc) - 1:
            safe.append(UNKNOWN_FIELD)
        elif isinstance(part, str) and _FIELD_NAME.match(part):
            safe.append(part)
        else:
            safe.append(UNKNOWN_FIELD)
    return safe


def _location(loc: tuple) -> str:
    parts = [str(part) for part in loc if part not in ("body", "query", "path")]
    return ".".join(parts) or "corpo"


async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
    content = {**(getattr(exc, "extra", None) or {}), "detail": exc.detail, "code": exc.code}
    return JSONResponse(status_code=exc.status_code, content=content)


async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {"loc": _safe_loc(error), "msg": error.get("msg", ""), "type": error.get("type", "")}
        for error in exc.errors()
    ]
    first = errors[0] if errors else None
    detail = "Dados inválidos."
    if first is not None:
        detail = f"Dados inválidos em {_location(tuple(first['loc']))}: {first['msg']}"
    return JSONResponse(status_code=422, content={"detail": detail, "code": "validation_error", "errors": errors})


async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "Erro na requisição."
    if exc.status_code == 404 and detail == "Not Found":
        detail = "Recurso não encontrado."
    elif exc.status_code == 405 and detail == "Method Not Allowed":
        detail = "Método não permitido."
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": detail, "code": _HTTP_CODES.get(exc.status_code, f"http_{exc.status_code}")},
        headers=getattr(exc, "headers", None),
    )


async def _storage_error(_request: Request, exc: StorageError) -> JSONResponse:
    if isinstance(exc, FileBusyError):
        return JSONResponse(
            status_code=409,
            content={"detail": "Um arquivo está em uso por outro programa. Feche-o e tente de novo.",
                     "code": "file_in_use"},
        )
    logger.error("Falha de storage: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Falha ao acessar os arquivos do storage. Consulte o log do aplicativo.",
                 "code": "storage_error"},
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(StorageError, _storage_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
