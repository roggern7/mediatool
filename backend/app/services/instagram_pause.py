"""Bloqueio do tráfego automático ao Instagram enquanto há pausa vigente (ver worker.PAUSE_REASONS)."""

from __future__ import annotations

from typing import NoReturn

from app.core.errors import AppError
from app.db.connection import parse_utc
from app.services.context import AppContext

REASON_LABELS = {
    "rate_limited": "limite de requisições do Instagram",
    "action_blocked": "proteção do Media Tool após aviso de restrição do Instagram",
    "checkpoint_required": "o Instagram pediu uma verificação de segurança; conclua a verificação no navegador "
    "e libere a pausa",
    "session_invalid": "a sessão precisa ser verificada; teste a sessão, salve a sessão de novo ou libere a pausa "
    "(a sessão salva continua guardada)",
    "auth_check": "verificando o acesso ao Instagram depois de uma recusa; a sessão salva continua guardada",
}


def local_time_label(iso: str) -> str:
    moment = parse_utc(iso)
    if moment is None:
        return iso
    return moment.astimezone().strftime("%d/%m/%Y às %H:%M")


def ensure_not_paused(ctx: AppContext) -> None:
    """409 `instagram_paused` se o acesso automático ao Instagram está pausado."""
    if ctx.worker is None:
        return
    until, reason = ctx.worker.pause_state()
    if until is None:
        return
    label = REASON_LABELS.get(reason or "", "pausa preventiva")
    raise AppError(
        409,
        "instagram_paused",
        f"O acesso automático ao Instagram está pausado até {local_time_label(until)} (motivo: {label}). "
        "Aguarde ou libere a pausa em Configurações.",
    )


def raise_paused(ctx: AppContext) -> NoReturn:
    """O portão recusou a requisição (``InstagramPaused``: pausa publicada durante a operação): 409
    `instagram_paused`, como ``ensure_not_paused``."""
    ensure_not_paused(ctx)
    raise AppError(409, "instagram_paused",
                   "O acesso automático ao Instagram está pausado. Aguarde ou libere a pausa em Configurações.")
