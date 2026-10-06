"""Criação e controle de lotes: importação de perfil e processamento em lote da biblioteca."""

from __future__ import annotations

import logging
from typing import Any

from app.core.errors import AppError, not_found
from app.db.connection import utc_now
from app.integrations.instagram.validation import parse_profile_input
from app.services.context import AppContext
from app.services.instagram_pause import ensure_not_paused

logger = logging.getLogger("media_tool.imports")

# Teto de jobs criados por um "processar tudo do filtro" da Biblioteca (seção 6).
MAX_BULK_PROCESS = 10_000

SESSION_MISSING_MESSAGE = (
    "Sessão do Instagram não configurada. Informe o sessionid em Configurações antes de importar."
)
PROFILE_ALREADY_IMPORTING_MESSAGE = (
    "Já existe uma importação deste perfil em andamento. Aguarde ela terminar ou cancele-a antes de importar de novo."
)


def resolve_preset(ctx: AppContext, preset_id: int | None) -> dict[str, Any]:
    """Preset pedido, ou o padrão configurado, ou o primeiro builtin."""
    if preset_id is not None:
        preset = ctx.presets.get(preset_id)
        if preset is None:
            raise not_found("Preset", "preset_not_found")
        return preset
    default_id = ctx.app_settings.get("default_preset_id")
    preset = ctx.presets.get(default_id) if default_id else None
    if preset is None:
        builtin = ctx.presets.first_builtin_id()
        preset = ctx.presets.get(builtin) if builtin else None
    if preset is None:
        raise AppError(409, "preset_not_found", "Nenhum preset disponível para o processamento.")
    return preset


def create_import(
    ctx: AppContext, *, profile: str, content_type: str, limit: int | None, preset_id: int | None, process: bool
) -> int:
    if limit is None:
        # "Todos" fica desabilitado até ter política própria (docs/BATCH_ROBUSTNESS.md, seção 1).
        raise AppError(422, "limit_all_disabled",
                       "A opção \"Todos\" está desativada por enquanto. Escolha uma quantidade de mídias.")
    try:
        username = parse_profile_input(profile)
    except ValueError as exc:
        raise AppError(422, "invalid_input", f"Perfil inválido: {exc}") from exc
    if not ctx.session_provider.status().configured:
        raise AppError(409, "session_missing", SESSION_MISSING_MESSAGE)
    ensure_not_paused(ctx)
    preset = resolve_preset(ctx, preset_id) if (process or preset_id is not None) else None
    # Conferência e criação na mesma transação: duas requisições simultâneas não abrem duas listagens do perfil.
    with ctx.db.transaction():
        if ctx.batches.open_listing_for_profile(username) is not None:
            raise AppError(409, "profile_already_importing", PROFILE_ALREADY_IMPORTING_MESSAGE)
        batch_id = ctx.batches.create(
            source="instagram_import",
            status="listing",
            source_profile=username,
            content_type=content_type,
            requested_limit=limit,
            preset_id=preset["id"] if preset else None,
            preset_name=preset["name"] if preset else None,
            params=preset["params"] if (preset and process) else None,
            process=process,
        )
    logger.info("Lote %s criado: importação de %s (%s, limite %s)", batch_id, username, content_type, limit)
    ctx.wake_import_runner()
    return batch_id


def cancel_job_ids(ctx: AppContext, job_ids: list[int]) -> None:
    for job_id in job_ids:
        ctx.jobs.request_cancel(job_id)
        if not (ctx.worker is not None and ctx.worker.signal_cancel(job_id)):
            ctx.jobs.cancel_if_idle(job_id)


def cancel_batch(ctx: AppContext, batch_id: int) -> None:
    batch = ctx.batches.get(batch_id)
    if batch is None:
        raise not_found("Lote", "batch_not_found")
    if batch["status"] in ("completed", "failed", "cancelled"):
        raise AppError(409, "batch_finished", "O lote já foi finalizado.")
    # Uma transação: lote cancelado, jobs ociosos cancelados num UPDATE único, URLs/cursores apagados.
    with ctx.db.transaction():
        ctx.batches.update(batch_id, status="cancelled", finished_at=utc_now())
        if batch["status"] == "listing":
            ctx.batches.update_listing(batch_id, listing_cursor=None, listing_state="cancelled", listing_retry_at=None)
        else:
            ctx.batches.update_listing(batch_id, listing_cursor=None)
        running = ctx.jobs.cancel_batch_jobs(batch_id)
        # Descartes da página que estava aberta (sem checkpoint): a página não será refeita, então são perda real do
        # que já tinha sido listado; somam uma única vez (o parcial é apagado na mesma instrução).
        ctx.batches.add_listing_dropped(batch_id, merge_partial=True)
    # Sem jobs em execução o lote já está sem job aberto: congela os contadores finais agora.
    ctx.batches.refresh_status(batch_id)
    if ctx.import_runner is not None:
        ctx.import_runner.cancel(batch_id)
    # Só os jobs em execução são sinalizados individualmente; sem executor vivo, a manutenção conclui o cancelamento.
    for job_id in running:
        if ctx.worker is not None:
            ctx.worker.signal_cancel(job_id)
    logger.info("Lote %s cancelado (%s jobs em execução sinalizados)", batch_id, len(running))


def resume_listing(ctx: AppContext, batch_id: int) -> None:
    """Retoma a listagem `paused`/`needs_attention` do checkpoint salvo (seção 9, C1)."""
    batch = ctx.batches.get(batch_id)
    if batch is None:
        raise not_found("Lote", "batch_not_found")
    if not ctx.batches.resume_listing(batch_id):
        raise AppError(409, "listing_not_resumable", "A listagem deste lote não está pausada nem aguardando ação.")
    logger.info("Lote %s: listagem retomada manualmente", batch_id)
    ctx.wake_import_runner()


RESTART_LISTING_WARNING = (
    "Recomeçar da página 1 pede de novo ao Instagram todas as páginas já listadas ({pages} até agora): é lento, conta "
    "no limite de páginas por hora e só depois chega a itens novos. Os itens já criados não são duplicados. Confirme "
    "para recomeçar."
)


def restart_listing(ctx: AppContext, batch_id: int, *, confirm: bool) -> None:
    """Recomeça a listagem `paused`/`needs_attention` da página 1, só por ação explícita do usuário (seção 9)."""
    batch = ctx.batches.get(batch_id)
    if batch is None:
        raise not_found("Lote", "batch_not_found")
    if not confirm:
        raise AppError(409, "restart_confirmation_required",
                       RESTART_LISTING_WARNING.format(pages=int(batch["listing_pages"] or 0)))
    if not ctx.batches.resume_listing(batch_id, from_start=True):
        raise AppError(409, "listing_not_resumable", "A listagem deste lote não está pausada nem aguardando ação.")
    logger.info("Lote %s: listagem recomeçada da página 1 por ação do usuário", batch_id)
    ctx.wake_import_runner()


def retry_failed(ctx: AppContext, batch_id: int) -> None:
    batch = ctx.batches.get(batch_id)
    if batch is None:
        raise not_found("Lote", "batch_not_found")
    if batch["status"] == "listing":
        raise AppError(409, "batch_listing", "O lote ainda está listando o perfil.")
    failed = ctx.jobs.failed_ids(batch_id)
    if not failed:
        raise AppError(409, "nothing_to_retry", "O lote não tem jobs falhos.")
    with ctx.db.transaction():
        if ctx.jobs.reset_for_retry(failed) == 0:
            raise AppError(409, "nothing_to_retry", "Nenhum job foi reenfileirado (estado mudou).")
        if batch["status"] != "running":
            ctx.batches.reopen(batch_id)
            ctx.batches.update(batch_id, error_code=None, error_message=None)
    ctx.wake_worker()


def retry_job(ctx: AppContext, job_id: int) -> None:
    job = ctx.jobs.get(job_id)
    if job is None:
        raise not_found("Job", "job_not_found")
    if job["status"] not in ("failed", "cancelled"):
        raise AppError(409, "job_not_retryable", "Só jobs falhos ou cancelados podem ser repetidos.")
    with ctx.db.transaction():
        if ctx.jobs.reset_for_retry([job_id]) == 0:
            raise AppError(409, "job_not_retryable", "O job mudou de estado e não foi reenfileirado.")
        if job["batch_id"] is not None:
            ctx.batches.reopen(job["batch_id"])
    ctx.wake_worker()


def cancel_job(ctx: AppContext, job_id: int) -> None:
    job = ctx.jobs.get(job_id)
    if job is None:
        raise not_found("Job", "job_not_found")
    if job["status"] in ("completed", "failed", "cancelled"):
        raise AppError(409, "job_finished", "O job já foi finalizado.")
    cancel_job_ids(ctx, [job_id])
    if job["batch_id"] is not None:
        ctx.batches.refresh_status(job["batch_id"])


def process_media(ctx: AppContext, media_id: int, preset_id: int) -> int:
    media = ctx.media.get(media_id)
    if media is None:
        raise not_found("Mídia", "media_not_found")
    preset = resolve_preset(ctx, preset_id)
    job_id = ctx.jobs.create_process(
        media=media, batch_id=None, preset_id=preset["id"], preset_name=preset["name"], params=preset["params"]
    )
    ctx.wake_worker()
    return job_id


def process_media_by_filter(
    ctx: AppContext, *, view: str, media_type: str | None, q: str | None, profile: str | None, preset_id: int
) -> int:
    """Cria no servidor, numa transação, um job de process por mídia do filtro da Biblioteca."""
    preset = resolve_preset(ctx, preset_id)
    # Idempotente (seção 9, M3): ignora mídias com processamento aberto ou já processadas com o mesmo preset/params.
    where, params = ctx.media.process_filter_where(
        view=view, media_type=media_type, profile=profile, q=q, preset_id=preset["id"], params=preset["params"]
    )
    with ctx.db.transaction():
        total = ctx.media.count(view=view, media_type=media_type, profile=profile, q=q)
        if total == 0:
            raise AppError(422, "no_matching_media", "Nenhuma mídia corresponde ao filtro.")
        if total > MAX_BULK_PROCESS:
            raise AppError(
                422, "bulk_limit_exceeded",
                f"O filtro tem {total} mídias; o limite por operação é {MAX_BULK_PROCESS}. Refine o filtro.",
            )
        if ctx.media.count_where(where, params) == 0:
            raise AppError(
                409, "nothing_to_process",
                "Todas as mídias do filtro já foram processadas com este preset ou já estão na fila de processamento.",
            )
        batch_id = ctx.batches.create(
            source="library",
            status="running",
            preset_id=preset["id"],
            preset_name=preset["name"],
            params=preset["params"],
            process=True,
        )
        ctx.jobs.create_process_for_filter(
            batch_id=batch_id, preset_id=preset["id"], preset_name=preset["name"], params=preset["params"],
            media_where=where, media_params=params,
        )
        ctx.batches.sync_found(batch_id)
    logger.info("Lote %s criado: processamento do filtro da Biblioteca (%s mídias no filtro)", batch_id, total)
    ctx.wake_worker()
    return batch_id


def process_media_bulk(ctx: AppContext, media_ids: list[int], preset_id: int) -> int:
    unique_ids = list(dict.fromkeys(media_ids))
    preset = resolve_preset(ctx, preset_id)
    items = [ctx.media.get(media_id) for media_id in unique_ids]
    missing = [media_id for media_id, media in zip(unique_ids, items) if media is None]
    if missing:
        raise AppError(404, "media_not_found", f"Mídias não encontradas: {', '.join(map(str, missing[:20]))}.")
    with ctx.db.transaction():
        batch_id = ctx.batches.create(
            source="library",
            status="running",
            preset_id=preset["id"],
            preset_name=preset["name"],
            params=preset["params"],
            process=True,
        )
        for media in items:
            ctx.jobs.create_process(
                media=media, batch_id=batch_id, preset_id=preset["id"], preset_name=preset["name"],
                params=preset["params"],
            )
        ctx.batches.sync_found(batch_id)
    ctx.wake_worker()
    return batch_id
