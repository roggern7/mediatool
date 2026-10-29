"""Nomes de arquivo da exportação para pasta: amigáveis, estáveis e válidos no Windows (funções puras).

Formato: `{perfil}_{AAAA-MM-DD}_{codigo_do_post}[_{n}].{ext}`. A data é a da publicação (`taken_at`) no fuso do
Media Tool (`app.services.export_time`; sem zona informada, UTC); `n` (1-based) só aparece em item de carrossel. Sem código do post: `{perfil}_{id da mídia}`; sem perfil nem código: prefixo do SHA-256.
A extensão é sempre a do arquivo guardado no storage. Versão editada/processada: o nome base ganha a marca da versão
(`rendition_tag`, ex.: `_editado_15s_1.5x`) antes do sufixo de colisão e da extensão.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from typing import Any

from app.services.export_time import Zone, local_date_text

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
# Versões editadas: nome do "preset" da Edição e margem somada ao limite de duração (app.services.edit_service; repetidos
# aqui para manter este módulo puro e sem dependências).
EDIT_PRESET_NAME = "Edição"
EDIT_DURATION_MARGIN = 0.03
MAX_TAG_LENGTH = 40


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


def _local_stem(media: dict[str, Any]) -> str:
    """Vídeo enviado do PC (`source_platform = 'local'`): o nome original enviado, sem a extensão, sanitizado."""
    name = sanitize_component(media.get("original_filename"))
    base, dot, _ = name.rpartition(".")
    return sanitize_component(base if dot and base else name)


def export_stem(media: dict[str, Any], *, carousel: bool = False, zone: Zone | None = None) -> str:
    if media.get("source_platform") == "local":
        local = _local_stem(media)
        if local:
            return safe_stem(local)
    profile = sanitize_component(media.get("source_profile"))
    code = sanitize_component(media.get("source_post_id"))
    date = local_date_text(media.get("taken_at"), zone)
    parts = [profile or FALLBACK_STEM]
    if date:
        parts.append(date)
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


def _seconds(value: float) -> str:
    """Segundos do recorte no nome: até 3 casas (milissegundo), sem zeros à direita ("2.5", "7.125", "10")."""
    return f"{value:.3f}".rstrip("0").rstrip(".")


def rendition_tag(rendition: dict[str, Any]) -> str:
    """Marca da versão no nome exportado (vem antes do sufixo de colisão). Edição (preset_id NULL e nome "Edição"):
    `_editado` + `_{início}-{fim}` (recorte manual, segundos com até 3 casas, sem zeros à direita) ou `_{N}s` (limite
    de duração, sem a margem) + `_{V}x` (velocidade diferente de 1) + `_nitido`.
    Preset (inclusive preset excluído, que fica com preset_id NULL): `_{nome do preset}` (sem nome: `_processado`).
    Sempre segura para nome de arquivo e com até
    MAX_TAG_LENGTH caracteres."""
    params = rendition.get("params") or {}
    if rendition.get("preset_id") is None and rendition.get("preset_name") == EDIT_PRESET_NAME:
        parts = ["_editado"]
        limit = float(params.get("max_duration") or 0)
        cut_end = float(params.get("cut_end") or 0)
        if cut_end > 0:
            parts.append(f"_{_seconds(float(params.get('trim_start') or 0))}-{_seconds(cut_end)}")
        elif limit > 0:
            seconds = round(limit - EDIT_DURATION_MARGIN, 3) if limit > EDIT_DURATION_MARGIN else limit
            parts.append(f"_{seconds:g}s")
        speed = float(params.get("speed") or 1)
        if speed != 1:
            parts.append(f"_{speed:g}x")
        if params.get("sharpen"):
            parts.append("_nitido")
        tag = "".join(parts)
    else:
        tag = "_" + (sanitize_component(rendition.get("preset_name")) or "processado")
    return sanitize_component(tag)[:MAX_TAG_LENGTH].rstrip(" .")


def file_name(stem: str, ext: str, attempt: int = 1, *, tag: str = "") -> str:
    """Nome final. `tag` (versão editada/processada) vem depois do nome base; `attempt` > 1 acrescenta ` (2)`,
    ` (3)`... (colisão com arquivo diferente). O nome inteiro cabe em MAX_NAME_LENGTH: o corte acontece no nome base,
    nunca na marca, na extensão nem no sufixo."""
    suffix = "" if attempt <= 1 else f" ({attempt})"
    room = max(1, MAX_NAME_LENGTH - len(ext) - len(suffix) - len(tag))
    base = stem[:room].rstrip(" .") or FALLBACK_STEM
    return f"{base}{tag}{suffix}{ext}"
