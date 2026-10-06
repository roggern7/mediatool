"""Validação de entradas do Instagram: perfil, shortcode e URL."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

MAX_INPUT_LENGTH = 512

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")
_PRINTABLE_ASCII_RE = re.compile(r"^[\x21-\x7e]+$")

ALLOWED_HOSTS = frozenset({"instagram.com", "www.instagram.com"})

# Primeiros segmentos de caminho que não são perfis.
RESERVED_PATHS = frozenset({
    "about", "accounts", "api", "ar", "blog", "challenge", "developer", "direct", "directory",
    "emails", "explore", "graphql", "help", "igtv", "invites", "jobs", "legal", "lite",
    "locations", "login", "logout", "nametag", "oauth", "p", "press", "privacy", "reel",
    "reels", "s", "session", "share", "static", "stories", "tags", "terms", "topics", "tv",
    "web",
})


def _check_text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} deve ser texto")
    value = value.strip()
    if not value:
        raise ValueError(f"{what} vazio")
    if len(value) > MAX_INPUT_LENGTH:
        raise ValueError(f"{what} longo demais")
    if not _PRINTABLE_ASCII_RE.match(value):
        raise ValueError(f"{what} contém caracteres não permitidos")
    return value


def _check_username(username: str) -> str:
    if not _USERNAME_RE.match(username):
        raise ValueError("nome de usuário inválido")
    if ".." in username:
        raise ValueError("nome de usuário inválido")
    if not any(ch.isalnum() for ch in username):
        raise ValueError("nome de usuário inválido")
    lowered = username.lower()
    if lowered in RESERVED_PATHS:
        raise ValueError("nome de usuário reservado")
    return lowered


def validate_instagram_url(url: str) -> str:
    """Valida uma URL do Instagram e devolve a forma normalizada (https://www.instagram.com/<path>).

    Query e fragmento são descartados. Só https, sem userinfo, porta 443 ou ausente.
    """
    url = _check_text(url, "URL")
    if "\\" in url:
        raise ValueError("URL inválida")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("URL inválida") from exc
    if parts.scheme.lower() != "https":
        raise ValueError("a URL precisa usar https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("URL inválida")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError("a URL não é do instagram.com")
    if port not in (None, 443):
        raise ValueError("URL inválida")
    path = parts.path or "/"
    segments = path.split("/")
    if any(seg in (".", "..") for seg in segments) or "%" in path:
        raise ValueError("URL inválida")
    return f"https://www.instagram.com{path}"


def parse_profile_input(value: str) -> str:
    """Converte "@user", "user" ou uma URL de perfil em "user" (minúsculo). ValueError se inválido."""
    value = _check_text(value, "perfil")
    lowered = value.lower()
    if lowered.startswith(("instagram.com/", "www.instagram.com/")):
        value = "https://" + value
        lowered = value.lower()
    if "://" in lowered or "/" in value:
        url = validate_instagram_url(value)
        segments = [seg for seg in urlsplit(url).path.split("/") if seg]
        if not segments:
            raise ValueError("a URL não aponta para um perfil")
        if segments[0].lower() in RESERVED_PATHS:
            raise ValueError("a URL não aponta para um perfil")
        return _check_username(segments[0])
    if value.startswith("@"):
        value = value[1:]
    return _check_username(value)


def validate_shortcode(code: str) -> str:
    """Valida o shortcode de um post (o trecho após /p/ ou /reel/)."""
    code = _check_text(code, "shortcode")
    if not _SHORTCODE_RE.match(code):
        raise ValueError("shortcode inválido")
    return code
