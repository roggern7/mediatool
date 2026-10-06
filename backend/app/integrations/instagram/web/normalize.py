"""Nó do PolarisProfilePostsQuery -> RemoteMedia (contrato INSTAGRAM_WEB_CONTRACT.md, web/normalize.py)."""

from __future__ import annotations

import logging
import posixpath
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..downloader import ContentType, RemoteMedia

log = logging.getLogger(__name__)

POST_BASE = "https://www.instagram.com"

MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_CAROUSEL = 8


@dataclass
class NormalizeStats:
    """Contadores seguros para log (sem URLs nem ids)."""

    nodes: int = 0
    items: int = 0
    skipped_no_url: int = 0          # item sem URL utilizável (content_unavailable)
    skipped_unsupported: int = 0     # media_type desconhecido (unsupported_media)
    skipped_invalid_node: int = 0    # nó sem `code` (não dá para montar source_url)
    # Cada descarte acima também é contado por motivo (nomes fixos; ver EXPECTED_REASONS).
    reasons: dict[str, int] = field(default_factory=dict)

    def _drop(self, reason: str, amount: int = 1) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + amount


# Motivos de descarte "de propósito" (tipo que o Media Tool não baixa). Os demais são inválido/nulo inesperado:
# node_no_code, media_type_missing, carousel_no_children, carousel_child_invalid, carousel_children_missing (filhos
# a menos que `carousel_media_count`; soma a diferença), video_no_url, image_no_url e rendition_candidates_invalid
# (item entregue numa versão menor porque uma versão maior veio sem URL utilizável; não é item perdido).
EXPECTED_REASONS = frozenset({"unsupported_type"})
# Inesperado que NÃO é item perdido (o item foi entregue numa versão menor): fica fora dos contadores de mídias
# descartadas do lote.
DEGRADED_REASONS = frozenset({"rendition_candidates_invalid"})


def _type_reason(media_type: object) -> str:
    return "media_type_missing" if media_type is None else "unsupported_type"


def _usable_url(value: object) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://") else None


def _area(entry: Mapping) -> int:
    try:
        return int(entry.get("width") or 0) * int(entry.get("height") or 0)
    except (TypeError, ValueError):
        return 0


def _largest(entries: object) -> Mapping | None:
    """Maior por largura*altura entre as entradas com URL utilizável; empate fica com a primeira."""
    best: Mapping | None = None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, Mapping) or not _usable_url(entry.get("url")):
            continue
        if best is None or _area(entry) > _area(best):
            best = entry
    return best


def _larger_invalid(entries: object, best: Mapping) -> bool:
    """Há entrada sem URL utilizável que declara área maior que a escolhida? (Entrada nula não declara tamanho e
    não é detectável.)"""
    return isinstance(entries, list) and any(
        isinstance(entry, Mapping) and not _usable_url(entry.get("url")) and _area(entry) > _area(best)
        for entry in entries)


def _image_candidates(item: Mapping) -> object:
    versions = item.get("image_versions2")
    return versions.get("candidates") if isinstance(versions, Mapping) else None


def _best_image(item: Mapping) -> Mapping | None:
    versions = item.get("image_versions2")
    return _largest(versions.get("candidates")) if isinstance(versions, Mapping) else None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extension(url: str, default: str) -> str:
    ext = posixpath.splitext(urllib.parse.urlsplit(url).path)[1].lstrip(".").lower()
    return ext if ext.isalnum() and 2 <= len(ext) <= 5 else default


def _taken_at(value: object) -> str | None:
    try:
        ts = int(value)
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):   # ausente ou absurdo: sem data, item mantido
        return None


def _str_id(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def normalize_node(node: Mapping, *, profile: str, from_reels: bool = False,
                   stats: NormalizeStats | None = None) -> list[RemoteMedia]:
    stats = stats if stats is not None else NormalizeStats()
    stats.nodes += 1
    code = node.get("code")
    if not isinstance(code, str) or not code:
        stats.skipped_invalid_node += 1
        stats._drop("node_no_code")
        return []
    post_is_reel = from_reels or node.get("product_type") == "clips"
    source_url = f"{POST_BASE}/reel/{code}/" if post_is_reel else f"{POST_BASE}/p/{code}/"
    post_id = _str_id(node.get("pk"))
    taken_at = _taken_at(node.get("taken_at"))

    media_type = node.get("media_type")
    if media_type == MEDIA_CAROUSEL:
        children = node.get("carousel_media")
        entries = list(enumerate(children)) if isinstance(children, list) else []
        if not entries:
            stats.skipped_no_url += 1
            stats._drop("carousel_no_children")
        declared = node.get("carousel_media_count")
        if entries and isinstance(declared, int) and not isinstance(declared, bool) and declared > len(entries):
            stats._drop("carousel_children_missing", declared - len(entries))
    elif media_type in (MEDIA_IMAGE, MEDIA_VIDEO):
        entries = [(0, node)]
    else:
        stats.skipped_unsupported += 1
        stats._drop(_type_reason(media_type))
        return []

    result: list[RemoteMedia] = []
    for index, item in entries:
        if not isinstance(item, Mapping):
            stats.skipped_no_url += 1
            stats._drop("carousel_child_invalid")
            continue
        item_type = item.get("media_type")
        image = _best_image(item)
        if item_type == MEDIA_VIDEO:
            video = _largest(item.get("video_versions"))
            if video is None:
                stats.skipped_no_url += 1
                stats._drop("video_no_url")
                continue
            if _larger_invalid(item.get("video_versions"), video):
                stats._drop("rendition_candidates_invalid")
            media_url = video["url"]
            kind = "video"
            thumbnail = image["url"] if image is not None else None
            width, height = _int_or_none(video.get("width")), _int_or_none(video.get("height"))
            duration = item.get("video_duration")
            duration = float(duration) if isinstance(duration, (int, float)) else None
            extension = _extension(media_url, "mp4")
        elif item_type == MEDIA_IMAGE:
            if image is None:
                stats.skipped_no_url += 1
                stats._drop("image_no_url")
                continue
            if _larger_invalid(_image_candidates(item), image):
                stats._drop("rendition_candidates_invalid")
            media_url = image["url"]
            kind = "image"
            thumbnail = None
            width, height = _int_or_none(image.get("width")), _int_or_none(image.get("height"))
            duration = None
            extension = _extension(media_url, "jpg")
        else:
            stats.skipped_unsupported += 1
            stats._drop(_type_reason(item_type))
            continue
        if width is None:
            width = _int_or_none(item.get("original_width"))
        if height is None:
            height = _int_or_none(item.get("original_height"))
        result.append(RemoteMedia(
            source_profile=profile,
            source_post_id=code,
            post_id=post_id,
            media_id=_str_id(item.get("pk")) or _str_id(item.get("id")),
            source_media_index=index,
            media_type=kind,
            is_reel=post_is_reel and kind == "video",
            source_url=source_url,
            media_url=media_url,
            thumbnail_url=thumbnail,
            width=width,
            height=height,
            duration=duration,
            taken_at=taken_at,
            extension=extension,
            in_carousel=media_type == MEDIA_CAROUSEL,
        ))
    stats.items += len(result)
    return result


def filter_by_content(items: list[RemoteMedia], content: ContentType) -> list[RemoteMedia]:
    if content == "reels":
        return [item for item in items if item.is_reel and not item.in_carousel]
    if content == "photos_carousels":
        # Fotos simples + TODOS os itens de carrossel (inclusive vídeos de carrossel); Reels e vídeos avulsos fora.
        return [item for item in items if item.in_carousel or item.media_type == "image"]
    if content == "videos":
        return [item for item in items if item.media_type == "video"]
    if content == "images":
        return [item for item in items if item.media_type == "image"]
    if content in ("posts", "all"):
        return list(items)
    raise ValueError(f"tipo de conteúdo desconhecido: {content!r}")
