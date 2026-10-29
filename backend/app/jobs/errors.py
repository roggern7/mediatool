"""Classificação de erros de job em `{code, message, retryable}` sem vazar segredos."""

from __future__ import annotations

import re
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


# Diagnóstico seguro de um erro do Instagram para o log: só os campos desta lista de permissão, cada um com formato
# restrito. Nada além disso sai do `diagnostic` (nem `location_path`, chaves do JSON, nomes de cookie, mensagens).
# `stage`/`detail`: começam por letra minúscula, até 64 caracteres e sem sequência de 6+ dígitos (pareceria um id).
_DIAG_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_DIAG_ID_LIKE_RE = re.compile(r"[0-9]{6}")
# Conjunto FECHADO dos valores de `redirect` (web/errors.redirect_kind).
_DIAG_REDIRECTS = frozenset({
    "redirect_login", "redirect_challenge", "redirect_checkpoint", "redirect_consent", "redirect_home",
    "redirect_other",
})
# Tipos de conteúdo reais aceitos (sem parâmetros); qualquer outro é omitido.
_DIAG_CONTENT_TYPES = frozenset({
    "text/html", "application/json", "text/javascript", "application/x-javascript", "text/plain",
    "application/octet-stream", "image/jpeg", "image/png", "image/webp", "video/mp4",
})
# Paths FIXOS das requisições do provider web (sem query, sem shortcode/usuário/id). Qualquer outro é omitido.
# `/p/` e `/reel/`: token do documento público de uma publicação (o provider já tirou o shortcode do path).
_DIAG_SAFE_PATHS = frozenset({"/", "/graphql/query", "/web/search/topsearch/", "/p/", "/reel/", "/reels/"})
_DIAG_STATUS_RANGE = (100, 599)
_DIAG_SIZE_RANGE = (0, 10**12)


def safe_diagnostic(exc: BaseException) -> str:
    """Resumo seguro do `stage`/`diagnostic` de um erro do Instagram: ``stage=... status=... redirect=...
    detail=... content_type=... size=... path=...`` (só os campos presentes e válidos; vazio se não houver nenhum).

    `stage`/`detail` só entram casando por inteiro ``[a-z][a-z0-9_]{0,63}`` e sem 6+ dígitos seguidos; `redirect`,
    o content-type (sem parâmetros) e o path só se forem um dos valores fixos conhecidos; `status` (100-599) e `size`
    (0 a 10**12) só como ``int`` exato (nem ``bool`` nem subclasse); os textos só como ``str`` exato (nunca
    subclasse, cujo ``__str__``/``__format__`` poderia devolver outra coisa). Valor fora do formato é omitido, nunca
    truncado. Única normalização: o content-type perde os parâmetros (``; charset=...``), os espaços em volta e as
    maiúsculas ANTES de ser comparado com a lista fixa; o que sai é sempre o valor da lista. O `stage` do erro
    (``exc.stage``) tem precedência sobre ``diagnostic["stage"]``. Nenhum valor aceito contém espaço nem quebra de
    linha: o resultado é sempre uma linha de ``chave=valor`` separados por espaço."""
    diagnostic = getattr(exc, "diagnostic", None)
    diag = diagnostic if isinstance(diagnostic, dict) else {}
    parts: list[str] = []

    def token(key: str, value: object) -> None:
        if type(value) is str and _DIAG_TOKEN_RE.fullmatch(value) and not _DIAG_ID_LIKE_RE.search(value):
            parts.append(f"{key}={value}")

    def number(key: str, value: object, bounds: tuple[int, int]) -> None:
        if type(value) is int and bounds[0] <= value <= bounds[1]:
            parts.append(f"{key}={value}")

    token("stage", getattr(exc, "stage", None) or diag.get("stage"))
    number("status", diag.get("status"), _DIAG_STATUS_RANGE)
    redirect = diag.get("redirect")
    if type(redirect) is str and redirect in _DIAG_REDIRECTS:
        parts.append(f"redirect={redirect}")
    token("detail", diag.get("detail"))
    content_type = diag.get("content_type")
    if type(content_type) is str:
        content_type = content_type.split(";", 1)[0].strip().lower()
        if content_type in _DIAG_CONTENT_TYPES:
            parts.append(f"content_type={content_type}")
    number("size", diag.get("size"), _DIAG_SIZE_RANGE)
    path = diag.get("path")
    if type(path) is str and path in _DIAG_SAFE_PATHS:
        parts.append(f"path={path}")
    return redact(" ".join(parts))


def message_with_diagnostic(message: str, diagnostic: str) -> str:
    """`message` + `` (diagnóstico: ...)`` respeitando `MAX_MESSAGE_LENGTH`. Se não couber, o diagnóstico perde
    campos INTEIROS do fim (nunca um valor cortado ao meio, que pareceria outro valor); sem nenhum campo que caiba,
    a mensagem vai sem sufixo. `message` e `diagnostic` já chegam seguros (`safe_message`/`safe_diagnostic`)."""
    message = message[:MAX_MESSAGE_LENGTH]
    parts = diagnostic.split()
    while parts:
        text = f"{message} (diagnóstico: {' '.join(parts)})"
        if len(text) <= MAX_MESSAGE_LENGTH:
            return text
        parts.pop()
    return message


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
