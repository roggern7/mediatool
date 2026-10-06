"""Classificação de erros de job em `{code, message, retryable}` sem vazar segredos."""

from __future__ import annotations

from dataclasses import dataclass

from media_processor import ProcessorError

from app.core.logging import redact
from app.integrations.instagram.downloader import InstagramError

MAX_MESSAGE_LENGTH = 500

# Erros do cdn_downloader (contrato, seção 5): só falha de rede é transitória.
_CDN_ERRORS = {
    # código do cdn_downloader: (código do job, mensagem, retryable)
    "invalid_url": ("download_failed", "URL de mídia rejeitada pela validação de segurança.", False),
    "forbidden": ("download_failed", "O servidor de mídia recusou o download (link expirado ou sem acesso).", False),
    "not_found": ("download_failed", "Arquivo de mídia não encontrado no servidor.", False),
    "too_large": ("too_large", "Arquivo de mídia maior que o limite configurado.", False),
    "network_error": ("network_error", "Falha de rede ao baixar a mídia.", True),
    "invalid_media": ("invalid_media", "O arquivo baixado não é uma mídia válida.", False),
    "cancelled": ("cancelled", "Download cancelado.", False),
}
_PROCESSOR_MESSAGES = {
    "ffmpeg_not_found": "FFmpeg/ffprobe não encontrado. Configure o caminho em Configurações ou instale o FFmpeg.",
    "invalid_media": "O arquivo não é uma mídia válida ou não pôde ser lido.",
    "processing_failed": "O FFmpeg falhou ao processar a mídia.",
    "cancelled": "Processamento cancelado.",
}


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    retryable: bool = False


class JobFailure(Exception):
    """Falha controlada do pipeline (código estável e mensagem em português)."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class JobCancelled(Exception):
    pass


def safe_message(text: object) -> str:
    message = redact(text).strip()
    return message[:MAX_MESSAGE_LENGTH]


def is_cancellation(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    return isinstance(exc, JobCancelled) or code == "cancelled"


def classify(exc: BaseException) -> Failure:
    if isinstance(exc, JobFailure):
        return Failure(exc.code, safe_message(exc.message), exc.retryable)
    if isinstance(exc, InstagramError):
        return Failure(exc.code, safe_message(exc.message), bool(exc.retryable))
    if isinstance(exc, ProcessorError):
        base = _PROCESSOR_MESSAGES.get(exc.code, "Falha no processamento da mídia.")
        detail = safe_message(getattr(exc, "message", "") or "")
        message = base if not detail or detail == base else f"{base} Detalhe: {detail}"
        return Failure(exc.code, safe_message(message), False)
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _CDN_ERRORS:
        job_code, message, retryable = _CDN_ERRORS[code]
        return Failure(job_code, message, retryable)
    if isinstance(exc, OSError):
        reason = exc.strerror or exc.__class__.__name__
        return Failure("storage_error", safe_message(f"Falha de arquivo no storage: {reason}."), False)
    return Failure("internal_error", "Erro interno inesperado. Consulte o log do aplicativo.", False)
