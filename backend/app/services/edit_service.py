"""Edição de vídeos da biblioteca: um lote de jobs `process` sem preset (preset_id NULL, preset_name "Edição").

A saída de cada job é uma rendition normal da mídia; o original nunca é alterado. Mesma mídia com as mesmas
configurações reaproveita a rendition existente (dedupe por mídia + preset_id NULL + params, seção 9).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.errors import AppError
from app.schemas.edits import EditSettings, EditTrim
from app.schemas.presets import PresetParams
from app.services.context import AppContext

logger = logging.getLogger("media_tool.edits")

EDIT_PRESET_NAME = "Edição"
# Margem somada ao limite de duração escolhido: com corte, a saída fica em limite + 0,03 s.
EDIT_DURATION_MARGIN = 0.03
DURATION_LIMITS = {"15": 15.0, "30": 30.0, "60": 60.0}
# Folga (s) entre o fim do recorte e a duração conhecida do vídeo: o navegador mede a duração por conta própria
# e o recorte vem arredondado ao milissegundo.
TRIM_END_SLACK = 0.001


def duration_limit(settings: EditSettings) -> float | None:
    """Limite escolhido em segundos (sem a margem), ou None para manter a duração original."""
    if settings.duration_mode == "custom":
        return settings.custom_seconds
    return DURATION_LIMITS.get(settings.duration_mode)


def edit_params(settings: EditSettings, trim: EditTrim | None = None) -> dict[str, Any]:
    """Params do processor para a edição. Sem limpeza de metadados, espelho, corte inicial nem auto_adjust
    (preserva dimensões, SAR e rotação); ajustes neutros são omitidos como nos presets.

    Com recorte manual: `trim_start` = início e `cut_end` = fim do trecho (segundos do original, arredondados ao
    milissegundo) e a duração geral é ignorada (sem `max_duration`): o corte é exato, sem a margem de 0,03 s, e a
    velocidade vale sobre o trecho. `trim_start` passa do teto de 30 s dos presets, por isso é gravado depois da
    validação do PresetParams (o processor aceita qualquer início >= 0)."""
    limit = None if trim is not None else duration_limit(settings)
    params = PresetParams(
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
    if trim is not None:
        params["trim_start"] = round(trim.start, 3)
        params["cut_end"] = round(trim.end, 3)
    return params


def ensure_changes(settings: EditSettings, has_trims: bool = False) -> None:
    """422 `edit_no_changes` quando nada muda (Automático, duração original, 1,0x, sem nitidez e sem recorte)."""
    if (settings.output_type == "auto" and duration_limit(settings) is None and settings.speed == 1.0
            and not settings.sharpen and not has_trims):
        raise AppError(422, "edit_no_changes", "Nenhuma edição selecionada.")


def is_edit(row: dict[str, Any]) -> bool:
    """Lote ou job de Edição: sem preset e com o nome "Edição" (mesma regra da exportação e dos nomes)."""
    return row.get("preset_id") is None and row.get("preset_name") == EDIT_PRESET_NAME


def start(ctx: AppContext, media_ids: list[int], settings: EditSettings, trims: list[EditTrim] | None = None) -> int:
    """Cria o lote de edição (um job por mídia) e acorda o worker. Devolve o id do lote.

    `trims`: recorte manual por vídeo (já validado contra `media_ids` no schema). Os params do lote são os gerais;
    os do job de um vídeo recortado incluem o recorte."""
    by_media = {trim.media_id: trim for trim in trims or []}
    ensure_changes(settings, bool(by_media))
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
    too_long = [media["id"] for media in items if media["id"] in by_media and media.get("duration") is not None
                and by_media[media["id"]].end > float(media["duration"]) + TRIM_END_SLACK]
    if too_long:
        raise AppError(
            422, "edit_trim_out_of_range",
            f"O fim do recorte passa da duração do vídeo: {', '.join(map(str, too_long[:20]))}.",
        )
    params = edit_params(settings)
    with ctx.db.transaction():
        batch_id = ctx.batches.create(
            source="library", status="running", preset_id=None, preset_name=EDIT_PRESET_NAME, params=params,
            process=True,
        )
        for media in items:
            ctx.jobs.create_process(
                media=media, batch_id=batch_id, preset_id=None, preset_name=EDIT_PRESET_NAME,
                params=edit_params(settings, by_media[media["id"]]) if media["id"] in by_media else params,
            )
        ctx.batches.sync_found(batch_id)
    logger.info("Lote %s criado: edição de %s vídeos", batch_id, len(items))
    ctx.wake_worker()
    return batch_id
