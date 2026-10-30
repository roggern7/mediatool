"""Pareamento e tokens da extensão do Chrome (docs/EXTENSION.md).

Tudo fica na tabela ``app_state`` (sem migração), sempre como hash: o código de pareamento (8 caracteres legíveis,
uso único, 5 min) e os tokens dos dispositivos (``sha256``). O token em claro só existe na resposta do pareamento.
Erros de código contam tentativas; ao atingir o limite o pareamento fica bloqueado por alguns minutos (gerar um
código novo no app libera); tentativa sem nenhum código ativo (nenhum gerado, expirado ou já usado) não conta.
Cada código tem um ``id`` público (não é segredo) para a tela de Configurações saber se ele já foi usado
(``pairing_code_status``). Nada aqui é registrado em log com valor de código ou token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
from datetime import timedelta
from typing import Any

from app.core.errors import AppError
from app.db.connection import parse_utc, utc_now
from app.services.context import AppContext

logger = logging.getLogger("media_tool.extension")

DEVICES_KEY = "extension_devices"
PAIRING_KEY = "extension_pairing"

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sem 0/O e 1/I, fáceis de confundir
CODE_LENGTH = 8
CODE_TTL_SECONDS = 5 * 60
MAX_FAILED_ATTEMPTS = 5
LOCK_SECONDS = 5 * 60
TOKEN_BYTES = 32
LAST_USED_RESOLUTION_SECONDS = 60  # o "último uso" é gravado no máximo uma vez por minuto
DEVICE_ID_RE = re.compile(r"^[a-f0-9]{16}$")
_CODE_CLEAN_RE = re.compile(r"[\s-]+")

PAIRING_INVALID_MESSAGE = "Código de pareamento inválido. Confira o código mostrado em Configurações do Media Tool."
PAIRING_EXPIRED_MESSAGE = "O código de pareamento expirou ou já foi usado. Gere um código novo no Media Tool."
PAIRING_LOCKED_MESSAGE = (
    "Tentativas erradas demais. O pareamento está bloqueado por alguns minutos; gere um código novo no Media Tool."
)

_lock = threading.Lock()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load(ctx: AppContext, key: str, default: Any) -> Any:
    raw = ctx.app_settings.get_state(key)
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _save(ctx: AppContext, key: str, value: Any) -> None:
    ctx.app_settings.set_state(key, json.dumps(value))


def _expired(iso: str | None) -> bool:
    moment = parse_utc(iso)
    return moment is None or moment <= parse_utc(utc_now())


def normalize_code(code: str) -> str:
    return _CODE_CLEAN_RE.sub("", code or "").upper()


# --------------------------------------------------------------------------- pareamento


def create_pairing_code(ctx: AppContext) -> dict[str, Any]:
    """Novo código (substitui o anterior e libera um bloqueio por tentativas). Só o hash fica guardado; o ``id``
    identifica o código na consulta de status."""
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    code_id = secrets.token_hex(8)
    expires_at = utc_now(CODE_TTL_SECONDS)
    with _lock:
        _save(ctx, PAIRING_KEY, {"code_hash": _sha256(code), "code_id": code_id, "expires_at": expires_at,
                                 "failed": 0, "locked_until": None})
    logger.info("Código de pareamento da extensão gerado (expira em %s min)", CODE_TTL_SECONDS // 60)
    return {"id": code_id, "code": code, "expires_at": expires_at, "ttl_seconds": CODE_TTL_SECONDS}


def pairing_code_status(ctx: AppContext, code_id: str) -> dict[str, Any]:
    """Estado do código ``code_id``: ``pending`` (ainda vale), ``used`` (pareou; com ``device_id``) ou ``expired``
    (expirou, foi substituído por outro, invalidado pelo bloqueio ou desconhecido)."""
    with _lock:
        state = _load(ctx, PAIRING_KEY, {})
    if state.get("used_code_id") == code_id:
        return {"status": "used", "device_id": state.get("used_device_id")}
    if state.get("code_id") == code_id and state.get("code_hash") and not _expired(state.get("expires_at")):
        return {"status": "pending", "device_id": None}
    return {"status": "expired", "device_id": None}


def pair(ctx: AppContext, code: str, device_name: str) -> dict[str, str]:
    """Troca um código válido por um token novo. O código é de uso único; erros contam para o bloqueio."""
    candidate = _sha256(normalize_code(code))
    with _lock:
        state = _load(ctx, PAIRING_KEY, {})
        locked_until = state.get("locked_until")
        if locked_until and not _expired(locked_until):
            raise AppError(429, "pairing_locked", PAIRING_LOCKED_MESSAGE)
        stored = state.get("code_hash")
        matches = isinstance(stored, str) and hmac.compare_digest(stored, candidate)
        if matches and _expired(state.get("expires_at")):
            _save(ctx, PAIRING_KEY, {**state, "code_hash": None, "expires_at": None})
            raise AppError(400, "pairing_code_expired", PAIRING_EXPIRED_MESSAGE)
        if not matches and (not stored or _expired(state.get("expires_at"))):
            # Sem código ativo não há o que adivinhar: a tentativa não conta para o bloqueio.
            raise AppError(400, "pairing_code_expired", PAIRING_EXPIRED_MESSAGE)
        if not matches:
            failed = int(state.get("failed") or 0) + 1
            if failed >= MAX_FAILED_ATTEMPTS:
                # Bloqueio temporário e o código vigente deixa de valer (o dono gera outro no app).
                _save(ctx, PAIRING_KEY, {"code_hash": None, "expires_at": None, "failed": 0,
                                         "locked_until": utc_now(LOCK_SECONDS)})
                logger.warning("Pareamento da extensão bloqueado após %s tentativas erradas", failed)
                raise AppError(429, "pairing_locked", PAIRING_LOCKED_MESSAGE)
            _save(ctx, PAIRING_KEY, {**state, "failed": failed, "locked_until": None})
            raise AppError(400, "pairing_code_invalid", PAIRING_INVALID_MESSAGE)
        # Uso único: o código some antes de o token existir (fica só o id, para a tela saber que foi usado).
        token = secrets.token_urlsafe(TOKEN_BYTES)
        device_id = secrets.token_hex(8)
        _save(ctx, PAIRING_KEY, {"code_hash": None, "expires_at": None, "failed": 0, "locked_until": None,
                                 "used_code_id": state.get("code_id"), "used_device_id": device_id})
        now = utc_now()
        devices = _load(ctx, DEVICES_KEY, [])
        devices.append({"id": device_id, "name": device_name, "token_hash": _sha256(token), "created_at": now,
                        "last_used_at": None})
        _save(ctx, DEVICES_KEY, devices)
    logger.info("Extensão do Chrome pareada (dispositivo %s)", device_id)
    return {"token": token, "device_id": device_id}


# --------------------------------------------------------------------------- tokens e dispositivos


def verify_token(ctx: AppContext, token: str | None) -> dict[str, Any] | None:
    """Dispositivo dono do token, ou None. Compara o hash com TODOS os dispositivos em tempo constante."""
    if not token:
        return None
    candidate = _sha256(token)
    found: dict[str, Any] | None = None
    with _lock:
        devices = _load(ctx, DEVICES_KEY, [])
        for device in devices:
            stored = device.get("token_hash")
            if isinstance(stored, str) and hmac.compare_digest(stored, candidate) and found is None:
                found = device
        if found is None:
            return None
        last = parse_utc(found.get("last_used_at"))
        now = utc_now()
        if last is None or parse_utc(now) - last >= timedelta(seconds=LAST_USED_RESOLUTION_SECONDS):
            found["last_used_at"] = now
            _save(ctx, DEVICES_KEY, devices)
    return {key: found.get(key) for key in ("id", "name", "created_at", "last_used_at")}


def list_devices(ctx: AppContext) -> list[dict[str, Any]]:
    """Extensões conectadas, sem o hash do token."""
    with _lock:
        devices = _load(ctx, DEVICES_KEY, [])
    return [{key: device.get(key) for key in ("id", "name", "created_at", "last_used_at")} for device in devices]


def revoke_device(ctx: AppContext, device_id: str) -> None:
    """Remove o dispositivo: o token dele passa a receber 401 na hora."""
    with _lock:
        devices = _load(ctx, DEVICES_KEY, [])
        remaining = [device for device in devices if device.get("id") != device_id]
        if len(remaining) == len(devices):
            raise AppError(404, "extension_device_not_found", "Extensão conectada não encontrada.")
        _save(ctx, DEVICES_KEY, remaining)
    logger.info("Extensão do Chrome revogada (dispositivo %s)", device_id)
