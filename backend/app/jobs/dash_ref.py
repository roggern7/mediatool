"""`job_remote_refs.dash_streams`: os fluxos separados (DASH) de um vídeo do lote por link, em JSON.

Contém URLs assinadas: tão sensível quanto `media_url` (nunca vai para log, erro, diagnóstico ou API).
"""

from __future__ import annotations

import json
import math
from typing import Any

from app.db.repositories.common import canonical_json
from app.integrations.instagram.downloader import RemoteMedia
from app.integrations.instagram.web.dash_manifest import MAX_CONTENT_LENGTH, MAX_DIMENSION, MAX_DURATION
from app.services.cdn_downloader import validate_cdn_url

DASH_KEYS = ("dash_video_url", "dash_audio_url", "dash_video_bytes", "dash_audio_bytes", "dash_width", "dash_height",
             "dash_duration")


def _int(value: object, limit: int, *, required: bool) -> bool:
    if value is None:
        return not required
    return type(value) is int and (0 < value if required else 0 <= value) and value <= limit


def _validated(data: dict[str, Any]) -> dict[str, Any] | None:
    """As 7 chaves com tipos e faixas válidos (URLs das CDNs permitidas, inteiros plausíveis, duração finita), ou
    None. Nunca levanta exceção, nem com números gigantes."""
    try:
        for key in ("dash_video_url", "dash_audio_url"):
            validate_cdn_url(data.get(key))
        if not (_int(data.get("dash_width"), MAX_DIMENSION, required=True)
                and _int(data.get("dash_height"), MAX_DIMENSION, required=True)
                and _int(data.get("dash_video_bytes"), MAX_CONTENT_LENGTH, required=False)
                and _int(data.get("dash_audio_bytes"), MAX_CONTENT_LENGTH, required=False)):
            return None
        duration = data.get("dash_duration")
        if duration is not None:
            if type(duration) not in (int, float) or not math.isfinite(duration) \
                    or not 0 < duration <= MAX_DURATION:
                return None
            duration = float(duration)
    except Exception:  # noqa: BLE001 - malformado (inclusive OverflowError de inteiro gigante): sem fluxos separados
        return None
    return {**{key: data.get(key) for key in DASH_KEYS}, "dash_duration": duration}


def dash_json(item: RemoteMedia) -> str | None:
    """JSON das 7 chaves quando o item tem os dois fluxos com valores válidos; senão None (baixa o MP4 direto).
    Nunca produz JSON inválido: valor não finito ou fora de faixa -> None."""
    if not item.dash_video_url or not item.dash_audio_url:
        return None
    fields = _validated({key: getattr(item, key) for key in DASH_KEYS})
    return None if fields is None else canonical_json(fields)


def dash_fields_from_json(text: object) -> dict[str, Any] | None:
    """Campos `dash_*` do RemoteMedia a partir do JSON gravado; None se ausente ou malformado (URL fora das CDNs
    permitidas, tipo errado, dimensão ausente, número não finito ou absurdo): o job baixa o MP4 direto."""
    if not isinstance(text, str) or not text:
        return None
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001 - malformado: sem fluxos separados
        return None
    return _validated(data) if isinstance(data, dict) else None
