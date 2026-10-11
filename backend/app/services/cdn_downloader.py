"""Download seguro de mídia a partir das CDNs do Instagram/Facebook.

Regras: só ``https`` na porta 443; host com sufixo ``.cdninstagram.com`` ou ``.fbcdn.net``; sem userinfo nem IP
literal; DNS resolvido e IPs não públicos rejeitados; redirects seguidos manualmente e revalidados (máx. 3); sem
cookies; streaming para ``.part`` em ``dest_dir`` com limite de bytes e cancelamento; tipo validado por magic bytes
e sufixo definido pelo conteúdo (nunca pelo nome remoto). O ``.part`` é apagado em qualquer falha.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

DownloadErrorCode = Literal[
    "invalid_url", "forbidden", "not_found", "too_large", "network_error", "invalid_media", "cancelled"
]

ALLOWED_HOST_SUFFIXES: tuple[str, ...] = (".cdninstagram.com", ".fbcdn.net")
MAX_REDIRECTS = 3
CHUNK_SIZE = 64 * 1024
_SNIFF_BYTES = 64
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_USER_AGENT = "media-tool/1.0"


class DownloadError(Exception):
    def __init__(self, code: DownloadErrorCode, message: str, *, http_status: int | None = None) -> None:
        self.code = code
        self.message = message
        # Status HTTP inesperado da CDN (só no `network_error` de `_raise_for_status`); None nos demais erros.
        self.http_status = http_status
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size: int
    content_type: str  # MIME detectado pelos magic bytes
    suffix: str  # ".mp4", ".mov", ".webm", ".jpg", ".png", ".webp", ".heic"


def validate_cdn_url(url: str) -> str:
    """Valida a URL de CDN sem rede. Retorna a própria URL ou levanta DownloadError('invalid_url')."""
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise DownloadError("invalid_url", "URL ausente ou longa demais.")
    if any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in url):
        raise DownloadError("invalid_url", "URL contém caracteres inválidos.")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise DownloadError("invalid_url", "URL malformada.") from None
    if parts.scheme.lower() != "https":
        raise DownloadError("invalid_url", "Somente https é permitido.")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise DownloadError("invalid_url", "URL com credenciais não é permitida.")
    if port not in (None, 443):
        raise DownloadError("invalid_url", "Somente a porta 443 é permitida.")
    host = (parts.hostname or "").lower()
    if not host:
        raise DownloadError("invalid_url", "URL sem host.")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise DownloadError("invalid_url", "IP literal não é permitido.")
    if not _HOST_RE.match(host) or not any(
        host.endswith(suffix) and len(host) > len(suffix) for suffix in ALLOWED_HOST_SUFFIXES
    ):
        raise DownloadError("invalid_url", "Host fora da lista de CDNs permitidas.")
    return url


def _check_dns(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise DownloadError("network_error", "Falha ao resolver o host da CDN.") from None
    if not infos:
        raise DownloadError("network_error", "Host da CDN sem endereços.")
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise DownloadError("invalid_url", "Endereço resolvido inválido.") from None
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local:
            raise DownloadError("invalid_url", "Host da CDN resolve para endereço não público.")


def _sniff(head: bytes) -> tuple[str, str] | None:
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if head.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in head:
        return ".webm", "video/webm"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"qt  ":
            return ".mov", "video/quicktime"
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"):
            return ".heic", "image/heic"
        if brand in (b"avif", b"avis"):
            return None
        if all(32 <= b < 127 for b in brand):
            return ".mp4", "video/mp4"
    return None


def _build_client(timeout: float) -> httpx.Client:
    """Cliente sem cookies persistentes, sem proxies/.netrc do ambiente e sem redirects automáticos."""
    return httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(timeout),
        trust_env=False,
        headers={"User-Agent": _USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"},
    )


def _raise_for_status(status: int) -> None:
    if status in (401, 403):
        raise DownloadError("forbidden", f"CDN recusou o acesso (HTTP {status}).")
    if status in (404, 410):
        raise DownloadError("not_found", f"Mídia não encontrada na CDN (HTTP {status}).")
    if status != 200:
        raise DownloadError("network_error", f"Resposta inesperada da CDN (HTTP {status}).", http_status=status)


def cdn_download(
    url: str,
    dest_dir: Path,
    *,
    max_bytes: int,
    timeout: float = 60,
    cancel: threading.Event | None = None,
    progress: Callable[[int, int | None], None] | None = None,
) -> DownloadResult:
    """Baixa ``url`` para um arquivo novo em ``dest_dir`` e retorna o resultado verificado."""
    if max_bytes <= 0:
        raise ValueError("max_bytes deve ser positivo")
    current = validate_cdn_url(url)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    part_path = dest_dir / f"cdn-{token}.part"

    def check_cancel() -> None:
        if cancel is not None and cancel.is_set():
            raise DownloadError("cancelled", "Download cancelado.")

    try:
        with _build_client(timeout) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                check_cancel()
                _check_dns((urllib.parse.urlsplit(current).hostname or "").lower())
                client.cookies.clear()
                with client.stream("GET", current) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadError("network_error", "Redirect sem destino.")
                        current = validate_cdn_url(urllib.parse.urljoin(current, location))
                        continue
                    _raise_for_status(response.status_code)
                    return _stream_to_file(response, part_path, dest_dir, token, max_bytes, check_cancel, progress)
            raise DownloadError("invalid_url", "Redirecionamentos demais.")
    except DownloadError:
        _discard(part_path)
        raise
    except httpx.TimeoutException:
        _discard(part_path)
        raise DownloadError("network_error", "Tempo esgotado ao baixar da CDN.") from None
    except httpx.HTTPError as exc:
        _discard(part_path)
        raise DownloadError("network_error", f"Falha de rede ao baixar da CDN ({type(exc).__name__}).") from None
    except BaseException:
        _discard(part_path)
        raise


def _stream_to_file(
    response: httpx.Response,
    part_path: Path,
    dest_dir: Path,
    token: str,
    max_bytes: int,
    check_cancel: Callable[[], None],
    progress: Callable[[int, int | None], None] | None,
) -> DownloadResult:
    total_expected: int | None = None
    length_header = response.headers.get("content-length")
    if length_header is not None:
        try:
            total_expected = int(length_header)
        except ValueError:
            total_expected = None
        if total_expected is not None and total_expected > max_bytes:
            raise DownloadError("too_large", "Arquivo excede o tamanho máximo permitido.")
    size = 0
    head = b""
    with open(part_path, "xb") as fh:
        for chunk in response.iter_bytes(CHUNK_SIZE):
            check_cancel()
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise DownloadError("too_large", "Arquivo excede o tamanho máximo permitido.")
            if len(head) < _SNIFF_BYTES:
                head += chunk[: _SNIFF_BYTES - len(head)]
            fh.write(chunk)
            if progress is not None:
                progress(size, total_expected)
        fh.flush()
        os.fsync(fh.fileno())
    check_cancel()
    if size == 0:
        raise DownloadError("invalid_media", "Resposta vazia.")
    if total_expected is not None and size != total_expected and not response.headers.get("content-encoding"):
        raise DownloadError("network_error", "Download incompleto.")
    sniffed = _sniff(head)
    if sniffed is None:
        raise DownloadError("invalid_media", "Conteúdo não é uma mídia suportada.")
    suffix, mime = sniffed
    final_path = dest_dir / f"cdn-{token}{suffix}"
    os.replace(part_path, final_path)
    return DownloadResult(path=final_path, size=size, content_type=mime, suffix=suffix)


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
