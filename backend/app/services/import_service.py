"""Criação e controle de lotes: importação de perfil e processamento em lote da biblioteca."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from app.core.errors import AppError, not_found
from app.db.connection import utc_now
from app.integrations.instagram.composite import web_only
from app.integrations.instagram.validation import parse_post_link, parse_profile_input
from app.schemas.edits import EditSettings
from app.services import edit_service
from app.services.context import AppContext
from app.services.instagram_pause import ensure_not_paused
from app.services.settings_service import instagram_provider

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


EDIT_WITH_PRESET_MESSAGE = (
    "Escolha um só pós-processamento: \"Processar com preset\" ou \"Editar após baixar\" (não os dois)."
)


def check_edit(edit: EditSettings | None, *, process: bool, preset_id: int | None) -> None:
    """"Editar após baixar" é exclusivo com o preset (um lote tem um único pós-processamento) e recusa a edição sem
    nenhuma mudança, como POST /edits. Sem `edit`, não faz nada."""
    if edit is None:
        return
    if process or preset_id is not None:
        raise AppError(422, "edit_with_preset", EDIT_WITH_PRESET_MESSAGE)
    edit_service.ensure_changes(edit)


def post_processing(
    ctx: AppContext, *, preset_id: int | None, process: bool, edit: EditSettings | None
) -> dict[str, Any]:
    """Campos do lote para o pós-processamento. Com `edit`: lote de Edição (preset_id NULL, nome "Edição", params da
    edição, `process` ligado), igual ao de POST /edits; sem `edit`, o preset como sempre foi."""
    if edit is not None:
        return {"preset_id": None, "preset_name": edit_service.EDIT_PRESET_NAME,
                "params": edit_service.edit_params(edit), "process": True}
    preset = resolve_preset(ctx, preset_id) if (process or preset_id is not None) else None
    return {
        "preset_id": preset["id"] if preset else None,
        "preset_name": preset["name"] if preset else None,
        "params": preset["params"] if (preset and process) else None,
        "process": process,
    }


WEB_ONLY_MESSAGE = (
    "\"Todos\", \"Publicações do feed\" e quantidades acima de 5.000 exigem o método de importação web. O método "
    "configurado (gallery-dl) não lista o perfil até o fim, para em 5.000 mídias e não conhece esse filtro. Troque o "
    "método em Configurações ou escolha outra opção."
)


def ensure_provider_supports(ctx: AppContext, content_type: str, limit: int | None) -> None:
    """ "Todos" (`limit=None`), N acima de 5.000 e `photos_carousels` só existem no provider web: com o método
    `gallery_dl` configurado a criação é recusada com erro claro (o legado truncaria em 5.000 itens, fingindo fim de
    perfil, e recusa o filtro)."""
    if web_only(content_type, limit) and instagram_provider(ctx.app_settings, ctx.settings) == "gallery_dl":
        raise AppError(422, "not_supported_by_provider", WEB_ONLY_MESSAGE)


def create_import(
    ctx: AppContext, *, profile: str, content_type: str, limit: int | None, preset_id: int | None, process: bool,
    edit: EditSettings | None = None,
) -> int:
    # `limit=None` = "Todos": a listagem vai até o fim real do perfil (docs/BATCH_ROBUSTNESS.md, seção 0.1).
    check_edit(edit, process=process, preset_id=preset_id)
    ensure_provider_supports(ctx, content_type, limit)
    try:
        username = parse_profile_input(profile)
    except ValueError as exc:
        raise AppError(422, "invalid_input", f"Perfil inválido: {exc}") from exc
    if not ctx.session_provider.status().configured:
        raise AppError(409, "session_missing", SESSION_MISSING_MESSAGE)
    ensure_not_paused(ctx)
    fields = post_processing(ctx, preset_id=preset_id, process=process, edit=edit)
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
            **fields,
        )
    logger.info("Lote %s criado: importação de %s (%s, limite %s)", batch_id, username, content_type, limit)
    ctx.wake_import_runner()
    return batch_id


# Teto de sanidade de perfis por grupo (não é limite de produto) e separadores de uma lista colada: UM PERFIL POR
# LINHA (vírgula e ponto e vírgula também separam, por conveniência). Espaço NÃO separa: entrada com espaço interno
# é inválida.
MAX_GROUP_PROFILES = 10_000
_ENTRY_SPLIT_RE = re.compile(r"[\r\n,;]+")
_MAX_ENTRY_ECHO = 200
# Abas do próprio perfil aceitas depois do usuário numa URL (`/usuario/reels/`, `/usuario/tagged/`).
_PROFILE_TABS = frozenset({"reels", "tagged"})


def normalize_profile_entry(entry: str) -> str:
    """Uma entrada da lista -> username. Além de `parse_profile_input`: aceita `http://` e só URLs claramente de
    perfil (`/usuario`, `/usuario/`, `/usuario/reels/`, `/usuario/tagged/`); outros caminhos são inválidos."""
    value = "https://" + entry[7:] if entry[:7].lower() == "http://" else entry
    if value.lower().startswith(("instagram.com/", "www.instagram.com/")):
        value = "https://" + value
    if "://" in value:
        try:
            segments = [segment for segment in urlsplit(value).path.split("/") if segment]
        except ValueError:
            segments = []
        if len(segments) > 2 or (len(segments) == 2 and segments[1].lower() not in _PROFILE_TABS):
            raise ValueError("a URL não aponta para um perfil")
    return parse_profile_input(value)


def parse_profile_entries(entries: list[str]) -> tuple[list[str], list[dict[str, str]], int]:
    """Lista de perfis (`usuario`, `@usuario` ou URL do perfil; http/https, com/sem www, barra final, query ou
    fragmento) -> (usernames válidos em minúsculas, na ordem e sem repetição; inválidas `{entry, reason}`;
    quantas repetições foram removidas). Sem efeitos e sem nenhuma requisição ao Instagram. Um perfil por linha
    (vírgula e ponto e vírgula também separam); linhas vazias são ignoradas e espaços nas pontas aparados; entrada
    com espaço interno é inválida (espaço não separa perfis)."""
    valid: list[str] = []
    invalid: list[dict[str, str]] = []
    seen: set[str] = set()
    seen_invalid: set[str] = set()
    duplicates = 0
    count = 0
    for raw in entries:
        for token in _ENTRY_SPLIT_RE.split(raw):
            token = token.strip()
            if not token:
                continue
            count += 1
            if count > MAX_GROUP_PROFILES:
                raise AppError(422, "too_many_profiles",
                               f"A lista tem mais de {MAX_GROUP_PROFILES} perfis; divida em mais de uma importação.")
            try:
                username = normalize_profile_entry(token)
            except ValueError as exc:
                if token in seen_invalid:
                    duplicates += 1
                else:
                    seen_invalid.add(token)
                    invalid.append({"entry": token[:_MAX_ENTRY_ECHO], "reason": str(exc)})
                continue
            if username in seen:
                duplicates += 1
            else:
                seen.add(username)
                valid.append(username)
    return valid, invalid, duplicates


def create_import_group(
    ctx: AppContext, *, profiles: list[str], content_type: str, limit: int | None, preset_id: int | None,
    process: bool, edit: EditSettings | None = None,
) -> tuple[int, list[dict[str, str]], int]:
    """Uma importação pai e um lote filho por perfil (na ordem da lista; `limit` é POR PERFIL). Retorna
    (id do grupo, perfis pulados por já terem importação ativa, repetições removidas). Com `edit`, o grupo e todos
    os filhos são lotes de Edição."""
    check_edit(edit, process=process, preset_id=preset_id)
    ensure_provider_supports(ctx, content_type, limit)
    valid, invalid, duplicates = parse_profile_entries(profiles)
    if invalid:
        raise AppError(422, "invalid_profiles",
                       f"{len(invalid)} entrada(s) da lista não são perfis válidos; corrija ou remova antes de iniciar.",
                       extra={"invalid": invalid, "valid": valid, "duplicates": duplicates})
    if not valid:
        raise AppError(422, "no_profiles", "Informe ao menos um perfil.")
    if not ctx.session_provider.status().configured:
        raise AppError(409, "session_missing", SESSION_MISSING_MESSAGE)
    ensure_not_paused(ctx)
    fields: dict[str, Any] = {
        "content_type": content_type,
        "requested_limit": limit,
        **post_processing(ctx, preset_id=preset_id, process=process, edit=edit),
    }
    # Conferência e criação na mesma transação (como em create_import): um perfil nunca ganha duas listagens.
    with ctx.db.transaction():
        importing = ctx.batches.open_listing_profiles()
        skipped = [{"profile": name, "reason": "profile_already_importing"} for name in valid if name in importing]
        pending = [name for name in valid if name not in importing]
        if not pending:
            raise AppError(409, "profile_already_importing",
                           "Todos os perfis da lista já têm uma importação em andamento.", extra={"skipped": skipped})
        group_id = ctx.batches.create_group(**fields)
        for position, username in enumerate(pending, start=1):
            ctx.batches.create(source="instagram_import", status="listing", source_profile=username,
                               group_id=group_id, group_position=position, **fields)
    logger.info("Grupo %s criado: %s perfis (%s, limite por perfil %s; %s pulados)", group_id, len(pending),
                content_type, limit, len(skipped))
    ctx.wake_import_runner()
    return group_id, skipped, duplicates


# Importação por link (docs/CONTRACTS.md, seção 8.3). Teto de sanidade de links por lote (cada link é uma requisição
# ao Instagram) e separadores de uma lista colada: UM LINK POR LINHA; vírgula, ponto e vírgula e espaço em branco
# também separam (link não tem espaço).
MAX_BATCH_LINKS = 500
_LINK_SPLIT_RE = re.compile(r"[\s,;]+")


def parse_link_entries(entries: list[str]) -> tuple[list[dict[str, str]], list[dict[str, str]], int]:
    """Lista de links de publicações -> (válidos `{shortcode, url}` na ordem e sem repetição; inválidos
    `{entry, reason}`; quantas repetições foram removidas). Sem efeitos e sem nenhuma requisição ao Instagram. O
    shortcode identifica a publicação: `/p/X` e `/reel/X` são o mesmo link (fica o primeiro)."""
    valid: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    seen: set[str] = set()
    seen_invalid: set[str] = set()
    duplicates = 0
    count = 0
    for raw in entries:
        for token in _LINK_SPLIT_RE.split(raw):
            if not token:
                continue
            count += 1
            if count > MAX_BATCH_LINKS:
                raise AppError(422, "too_many_links",
                               f"A lista tem mais de {MAX_BATCH_LINKS} links; divida em mais de uma importação.")
            try:
                shortcode, url = parse_post_link(token)
            except ValueError as exc:
                if token in seen_invalid:
                    duplicates += 1
                else:
                    seen_invalid.add(token)
                    invalid.append({"entry": token[:_MAX_ENTRY_ECHO], "reason": str(exc)})
                continue
            if shortcode in seen:
                duplicates += 1
            else:
                seen.add(shortcode)
                valid.append({"shortcode": shortcode, "url": url})
    return valid, invalid, duplicates


def create_import_links(
    ctx: AppContext, *, links: list[str], preset_id: int | None, process: bool, edit: EditSettings | None = None,
) -> tuple[int, int]:
    """Um lote de importação por link (`import_mode = 'links'`) com todos os links da lista, na ordem colada.
    Retorna (id do lote, repetições removidas). Nenhuma requisição ao Instagram aqui: o import runner resolve um
    link por vez."""
    check_edit(edit, process=process, preset_id=preset_id)
    valid, invalid, duplicates = parse_link_entries(links)
    if invalid:
        raise AppError(422, "invalid_links",
                       f"{len(invalid)} entrada(s) da lista não são links de publicação válidos; corrija ou remova "
                       "antes de iniciar.",
                       extra={"invalid": invalid, "valid": valid, "duplicates": duplicates})
    if not valid:
        raise AppError(422, "no_links", "Informe ao menos um link.")
    if not ctx.session_provider.status().configured:
        raise AppError(409, "session_missing", SESSION_MISSING_MESSAGE)
    ensure_not_paused(ctx)
    fields = post_processing(ctx, preset_id=preset_id, process=process, edit=edit)
    with ctx.db.transaction():
        batch_id = ctx.batches.create(
            source="instagram_import",
            status="listing",
            content_type="all",
            import_mode="links",
            **fields,
        )
        ctx.batches.add_links(batch_id, [(link["shortcode"], link["url"]) for link in valid])
    logger.info("Lote %s criado: importação por link (%s links)", batch_id, len(valid))
    ctx.wake_import_runner()
    return batch_id, duplicates


def cancel_group(ctx: AppContext, group_id: int) -> None:
    """Cancela todos os filhos ainda abertos do grupo (os encerrados ficam como estão). A parte de banco é UMA
    transação: todos os filhos abertos (a fila inteira) ficam cancelados de uma vez, então nenhum filho da fila
    começa a listar depois. O filho que está listando/baixando é avisado em seguida (cancelamento cooperativo)."""
    if ctx.batches.get_group(group_id) is None:
        raise not_found("Grupo de importação", "group_not_found")
    with ctx.db.transaction():
        open_children = ctx.batches.group_open_children(group_id)
        if not open_children:
            raise AppError(409, "group_finished", "O grupo já foi finalizado.")
        running = {child["id"]: _cancel_batch_rows(ctx, child["id"], child["status"]) for child in open_children}
    for batch_id, jobs in running.items():
        _cancel_batch_signal(ctx, batch_id, jobs)
    logger.info("Grupo %s cancelado (%s lotes)", group_id, len(open_children))


def cancel_job_ids(ctx: AppContext, job_ids: list[int]) -> None:
    for job_id in job_ids:
        ctx.jobs.request_cancel(job_id)
        if not (ctx.worker is not None and ctx.worker.signal_cancel(job_id)):
            ctx.jobs.cancel_if_idle(job_id)


def _cancel_batch_rows(ctx: AppContext, batch_id: int, status: str) -> list[int]:
    """Parte de BANCO do cancelamento (chamar dentro de uma transação): lote cancelado, jobs ociosos cancelados num
    UPDATE único, URLs/cursores apagados. Retorna os jobs em execução, a sinalizar depois do commit."""
    ctx.batches.update(batch_id, status="cancelled", finished_at=utc_now())
    if status == "listing":
        ctx.batches.update_listing(batch_id, listing_cursor=None, listing_state="cancelled", listing_retry_at=None)
    else:
        ctx.batches.update_listing(batch_id, listing_cursor=None)
    running = ctx.jobs.cancel_batch_jobs(batch_id)
    # Lote por link com a recusa em espera: cancelado, a espera deixa de existir (fica só o motivo).
    ctx.batches.release_cancelled_links_hold(batch_id)
    # Descartes da página que estava aberta (sem checkpoint): a página não será refeita, então são perda real do
    # que já tinha sido listado; somam uma única vez (o parcial é apagado na mesma instrução).
    ctx.batches.add_listing_dropped(batch_id, merge_partial=True)
    return running


def _cancel_batch_signal(ctx: AppContext, batch_id: int, running: list[int]) -> None:
    """Parte COOPERATIVA do cancelamento (depois do commit): congela contadores e sinaliza quem está em execução."""
    # Sem jobs em execução o lote já está sem job aberto: congela os contadores finais agora.
    ctx.batches.refresh_status(batch_id)
    if ctx.import_runner is not None:
        ctx.import_runner.cancel(batch_id)
    # Só os jobs em execução são sinalizados individualmente; sem executor vivo, a manutenção conclui o cancelamento.
    for job_id in running:
        if ctx.worker is not None:
            ctx.worker.signal_cancel(job_id)


def cancel_batch(ctx: AppContext, batch_id: int) -> None:
    batch = ctx.batches.get(batch_id)
    if batch is None:
        raise not_found("Lote", "batch_not_found")
    if batch["status"] in ("completed", "failed", "cancelled"):
        raise AppError(409, "batch_finished", "O lote já foi finalizado.")
    with ctx.db.transaction():
        running = _cancel_batch_rows(ctx, batch_id, batch["status"])
    _cancel_batch_signal(ctx, batch_id, running)
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
    if batch.get("import_mode") == "links":
        # Lote por link não tem "página 1": os links pendentes continuam de onde pararam (resume-listing).
        raise AppError(409, "listing_not_resumable",
                       "Importação por link não recomeça do início: use \"Retomar importação\" para continuar dos links que "
                       "ainda faltam.")
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
        # Lote por link com a recusa em espera: esta ação do dono a desarma (uma nova consulta; recusada, rearma).
        ctx.batches.clear_links_hold(batch_id)
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
            # Recusa em espera de lote por link fora de `listing`: repetir um job também é ação do dono e desarma.
            ctx.batches.clear_links_hold(job["batch_id"])
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
        raise not_found("Mídia", "media_not_found", feminine=True)
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
