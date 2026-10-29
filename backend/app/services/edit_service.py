"""Edição de vídeos da biblioteca: um lote de jobs `process` sem preset (preset_id NULL, preset_name "Edição").

A saída de cada job é uma rendition normal da mídia; o original nunca é alterado. Mesma mídia com as mesmas
configurações reaproveita a rendition existente (dedupe por mídia + preset_id NULL + params, seção 9).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.errors import AppError
from app.schemas.edits import EditSettings
from app.schemas.presets import PresetParams
from app.services.context import AppContext

logger = logging.getLogger("media_tool.edits")

EDIT_PRESET_NAME = "Edição"
# Margem somada ao limite de duração escolhido: com corte, a saída fica em limite + 0,03 s.
EDIT_DURATION_MARGIN = 0.03
DURATION_LIMITS = {"15": 15.0, "30": 30.0, "60": 60.0}


def duration_limit(settings: EditSettings) -> float | None:
    """Limite escolhido em segundos (sem a margem), ou None para manter a duração original."""
    if settings.duration_mode == "custom":
        return settings.custom_seconds
    return DURATION_LIMITS.get(settings.duration_mode)


def edit_params(settings: EditSettings) -> dict[str, Any]:
    """Params do processor para a edição. Sem limpeza de metadados, espelho, corte inicial nem auto_adjust
    (preserva dimensões, SAR e rotação); ajustes neutros são omitidos como nos presets."""
    limit = duration_limit(settings)
    return PresetParams(
        metadata_cleanup=False,
        trim_start=0.0,
        mirror=False,
        speed=float(settings.speed),
        auto_adjust=False,
        reencode=settings.output_type == "mp4",
        max_duration=0.0 if limit is None else round(limit + EDIT_DURATION_MARGIN, 6),
        sharpen=settings.sharpen,
        audio_sync_start=True,  # áudio que começa depois do vídeo continua sincronizado com velocidade
    ).model_dump()


def start(ctx: AppContext, media_ids: list[int], settings: EditSettings) -> int:
    """Cria o lote de edição (um job por mídia) e acorda o worker. Devolve o id do lote."""
    if (settings.output_type == "auto" and duration_limit(settings) is None and settings.speed == 1.0
            and not settings.sharpen):
        raise AppError(422, "edit_no_changes", "Nenhuma edição selecionada.")
    unique_ids = list(dict.fromkeys(media_ids))
    items = [ctx.media.get(media_id) for media_id in unique_ids]
    missing = [media_id for media_id, media in zip(unique_ids, items) if media is None]
    if missing:
        raise AppError(404, "media_not_found", f"Mídias não encontradas: {', '.join(map(str, missing[:20]))}.")
    not_video = [media["id"] for media in items if media["media_type"] != "video"]
    if not_video:
        raise AppError(
            422, "edit_requires_video",
            f"Só vídeos podem ser editados. Mídias que não são vídeo: {', '.join(map(str, not_video[:20]))}.",
        )
    params = edit_params(settings)
    with ctx.db.transaction():
        batch_id = ctx.batches.create(
            source="library", status="running", preset_id=None, preset_name=EDIT_PRESET_NAME, params=params,
            process=True,
        )
        for media in items:
            ctx.jobs.create_process(
                media=media, batch_id=batch_id, preset_id=None, preset_name=EDIT_PRESET_NAME, params=params,
            )
        ctx.batches.sync_found(batch_id)
    logger.info("Lote %s criado: edição de %s vídeos", batch_id, len(items))
    ctx.wake_worker()
    return batch_id
