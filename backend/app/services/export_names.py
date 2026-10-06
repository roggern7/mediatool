"""Nomes de arquivo da exportação para pasta: amigáveis, estáveis e válidos no Windows (funções puras).

Formato: `{perfil}_{AAAA-MM-DD}_{codigo_do_post}[_{n}].{ext}`. A data é a de `taken_at` (UTC); `n` (1-based) só
aparece em item de carrossel. Sem código do post: `{perfil}_{id da mídia}`; sem perfil nem código: prefixo do SHA-256.
A extensão é sempre a do original guardado no storage.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from typing import Any

# Comprimento máximo do NOME do arquivo (com extensão e sufixo de colisão).
MAX_NAME_LENGTH = 120
FALLBACK_STEM = "midia"
_INVALID_CHARS = frozenset('<>:"/\\|?*')
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    | {f"COM{i}" for i in range(10)}
    | {f"LPT{i}" for i in range(10)}
    | {f"COM{c}" for c in "¹²³"}
    | {f"LPT{c}" for c in "¹²³"}
)
_EXTENSION = re.compile(r"^\.[a-z0-9]{1,5}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def sanitize_component(value: Any) -> str:
    """Texto seguro para compor um nome: NFC, sem `<>:"/\\|?*` nem caracteres de controle, sem ponto/espaço nas
    pontas."""
    text = unicodedata.normalize("NFC", "" if value is None else str(value))
    text = "".join(
        "_" if char in _INVALID_CHARS or unicodedata.category(char).startswith("C") else char for char in text
    )
    return re.sub(r"\s+", " ", text).strip(" .")


def safe_stem(stem: str) -> str:
    """Nome (sem extensão) nunca vazio e nunca reservado do Windows (CON, NUL, COM1..., com ou sem extensão)."""
    stem = sanitize_component(stem) or FALLBACK_STEM
    if stem.split(".", 1)[0].rstrip(" ").upper() in _RESERVED:
        stem = f"_{stem}"
    return stem


def export_stem(media: dict[str, Any], *, carousel: bool = False) -> str:
    profile = sanitize_component(media.get("source_profile"))
    code = sanitize_component(media.get("source_post_id"))
    date = _DATE.match(str(media.get("taken_at") or ""))
    parts = [profile or FALLBACK_STEM]
    if date:
        parts.append(date.group(0))
    if code:
        parts.append(code)
        index = int(media.get("source_media_index") or 0)
        if carousel or index > 0:
            parts.append(str(index + 1))
    elif profile:
        parts.append(str(media.get("id")))
    else:
        parts.append(str(media.get("original_sha256") or media.get("id"))[:12])
    return safe_stem("_".join(parts))


def export_extension(original_path: str | None) -> str:
    """Extensão do original no storage (minúscula); vazia se não for uma extensão simples."""
    ext = PurePosixPath(original_path or "").suffix.lower()
    return ext if _EXTENSION.match(ext) else ""


def file_name(stem: str, ext: str, attempt: int = 1) -> str:
    """Nome final. `attempt` > 1 acrescenta ` (2)`, ` (3)`... (colisão com arquivo diferente). O nome inteiro cabe em
    MAX_NAME_LENGTH: o corte acontece no nome base, nunca na extensão nem no sufixo."""
    suffix = "" if attempt <= 1 else f" ({attempt})"
    room = max(1, MAX_NAME_LENGTH - len(ext) - len(suffix))
    base = stem[:room].rstrip(" .") or FALLBACK_STEM
    return f"{base}{suffix}{ext}"
