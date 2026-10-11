"""Nó do PolarisProfilePostsQuery -> RemoteMedia (contrato INSTAGRAM_WEB_CONTRACT.md, web/normalize.py).

Também o nó de UMA publicação (``normalize_post``): hoje o do documento público (web/post_document.py), que chega
no formato v1 (verificado no real para um Reel em 2026-10-10). O formato GraphQL "legado" nunca foi verificado.
"""

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


# --------------------------------------------------------------------------- post isolado

# `__typename` do formato GraphQL "legado" (com ou sem o prefixo XDT) -> media_type v1.
_LEGACY_TYPES = {"GraphImage": MEDIA_IMAGE, "GraphVideo": MEDIA_VIDEO, "GraphSidecar": MEDIA_CAROUSEL}
_OWNER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._")


def _legacy_media_type(node: Mapping) -> object:
    typename = node.get("__typename")
    if isinstance(typename, str) and typename:
        name = typename[3:] if typename.startswith("XDT") else typename
        return _LEGACY_TYPES.get(name, name)   # nome desconhecido: normalize_node conta como tipo não suportado
    if isinstance(node.get("edge_sidecar_to_children"), Mapping):
        return MEDIA_CAROUSEL
    is_video = node.get("is_video")
    return (MEDIA_VIDEO if is_video else MEDIA_IMAGE) if isinstance(is_video, bool) else None


def legacy_to_v1(node: Mapping) -> dict:
    """Nó do formato GraphQL "legado" (`shortcode`, `display_resources`, `video_url`, `edge_sidecar_to_children`)
    -> nó no formato v1 que ``normalize_node`` entende. Puro: só troca nomes de campo; a escolha de versão e a
    contagem de descartes continuam em ``normalize_node``. Formato NÃO verificado no Instagram real."""
    dimensions = node.get("dimensions")
    dimensions = dimensions if isinstance(dimensions, Mapping) else {}
    width, height = dimensions.get("width"), dimensions.get("height")
    candidates: list = []
    resources = node.get("display_resources")
    for entry in resources if isinstance(resources, list) else []:
        candidates.append({"url": entry.get("src"), "width": entry.get("config_width"),
                           "height": entry.get("config_height")} if isinstance(entry, Mapping) else entry)
    if node.get("display_url") is not None:
        candidates.append({"url": node.get("display_url"), "width": width, "height": height})
    media_type = _legacy_media_type(node)
    out: dict = {
        "code": node.get("shortcode"),
        "pk": node.get("id"),
        "media_type": media_type,
        "product_type": node.get("product_type"),
        "taken_at": node.get("taken_at_timestamp"),
        "image_versions2": {"candidates": candidates},
        "video_duration": node.get("video_duration"),
        "original_width": width,
        "original_height": height,
    }
    if node.get("video_url") is not None:
        out["video_versions"] = [{"url": node.get("video_url"), "width": width, "height": height}]
    if media_type == MEDIA_CAROUSEL:
        sidecar = node.get("edge_sidecar_to_children")
        edges = sidecar.get("edges") if isinstance(sidecar, Mapping) else None
        children: list = []
        for edge in edges if isinstance(edges, list) else []:
            child = edge.get("node") if isinstance(edge, Mapping) else None
            # Filho inválido fica na lista (None): normalize_node o conta e os índices dos demais não mudam.
            children.append(legacy_to_v1(child) if isinstance(child, Mapping) else None)
        out["carousel_media"] = children
        out["carousel_media_count"] = len(children)
    return out


def _post_owner(node: Mapping) -> str:
    """Username do dono da publicação em minúsculas (`user.username`, senão `owner.username`); "" se ausente."""
    for key in ("user", "owner"):
        holder = node.get(key)
        name = holder.get("username") if isinstance(holder, Mapping) else None
        if isinstance(name, str) and 0 < len(name) <= 30 and set(name.lower()) <= _OWNER_CHARS:
            return name.lower()
    return ""


def normalize_post(root: Mapping, *, stats: NormalizeStats | None = None) -> list[RemoteMedia]:
    """Nó de UM post -> todas as mídias dele (carrossel inteiro, na ordem), sem filtro de conteúdo.

    Aceita v1 (`{"items": [nó]}` ou um nó direto com `media_type` numérico; é o formato do documento público,
    verificado no real para um Reel) e GraphQL "legado" (convertido por ``legacy_to_v1``; NÃO verificado no real e
    nunca observado no documento público, mas ainda aceito se vier). `source_profile`
    é o dono da publicação. Lista vazia quando nada utilizável veio; ``stats`` diz por quê."""
    node: object = root
    items = root.get("items")
    if isinstance(items, list):
        node = next((item for item in items if isinstance(item, Mapping)), None)
        if node is None:
            return []
    media_type = node.get("media_type")
    v1 = isinstance(media_type, int) and not isinstance(media_type, bool)
    return normalize_node(node if v1 else legacy_to_v1(node), profile=_post_owner(node), stats=stats)


def post_code(root: Mapping) -> str | None:
    """Shortcode que a raiz da consulta de UM post declara (`code` no formato v1, `shortcode` no legado), escolhendo
    o nó como ``normalize_post``. None quando a resposta não traz nenhum (``normalize_post`` devolve lista vazia e
    conta `node_no_code`: nada vira mídia)."""
    node: object = root
    items = root.get("items")
    if isinstance(items, list):
        node = next((item for item in items if isinstance(item, Mapping)), None)
        if node is None:
            return None
    media_type = node.get("media_type")
    v1 = isinstance(media_type, int) and not isinstance(media_type, bool)
    code = node.get("code" if v1 else "shortcode")
    return code if isinstance(code, str) and code else None


def filter_by_content(items: list[RemoteMedia], content: ContentType) -> list[RemoteMedia]:
    if content == "reels":
        return [item for item in items if item.is_reel and not item.in_carousel]
    if content == "photos_carousels":
        # "Publicações do feed": tudo o que não é Reel — fotos simples, vídeos avulsos antigos (não-Reel) e TODOS
        # os itens de carrossel (inclusive vídeos de carrossel). É o complemento exato do filtro "reels".
        return [item for item in items if not item.is_reel or item.in_carousel]
    if content == "videos":
        return [item for item in items if item.media_type == "video"]
    if content == "images":
        return [item for item in items if item.media_type == "image"]
    if content in ("posts", "all"):
        return list(items)
    raise ValueError(f"tipo de conteúdo desconhecido: {content!r}")
