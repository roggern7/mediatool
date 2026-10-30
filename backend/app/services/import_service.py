"""Criação e controle de lotes: importação de perfil e processamento em lote da biblioteca."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from app.core.errors import AppError, not_found
from app.db.connection import utc_now
from app.integrations.instagram.composite import web_only
from app.integrations.instagram.validation import parse_post_link, parse_profile_input, post_path_kind
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
    "\"Todos\", \"Publicações do feed\", quantidades acima de 5.000 e os filtros de visualizações mínimas e de "
    "período de postagem exigem o método de importação web. O método configurado (gallery-dl) não lista o perfil até o "
    "fim, para em 5.000 mídias e não conhece esses filtros. Troque o método em Configurações ou escolha outra opção."
)
# O filtro de visualizações mínimas é de VÍDEOS: só com conteúdo "Reels" ou "Vídeos" (fotos nunca entram nele).
MIN_VIEWS_CONTENT_TYPES = ("reels", "videos")
MIN_VIEWS_CONTENT_MESSAGE = (
    "O filtro de visualizações mínimas só vale para vídeos: escolha o conteúdo \"Reels\" ou \"Vídeos\", ou "
    "desligue o filtro."
)


# Interruptor ÚNICO do filtro de visualizações (mínimo e máximo; docs/INSTAGRAM_WEB_CONTRACT.md, "Contagem de
# visualizações"): desligado, a API recusa `min_views`/`max_views` com 422 e nenhum lote novo usa o filtro. LIGADO desde
# 2026-10-29: a aba Reels SEM login paginou 5 páginas seguidas (60 Reels, `play_count` em todos, sem 429). Desligar =
# False aqui e VIEWS_FILTER_ENABLED no frontend (frontend/src/lib/viewsFilter.ts).
MIN_VIEWS_ENABLED = True
MIN_VIEWS_UNAVAILABLE_MESSAGE = "O filtro de visualizações ainda não está disponível."
COVERS_WEB_ONLY_MESSAGE = (
    "\"Somente capas\" exige o método de importação web (o gallery-dl não entrega a capa do vídeo sozinha). Troque o "
    "método em Configurações ou escolha outro tipo de mídia."
)


def ensure_provider_supports(ctx: AppContext, content_type: str, limit: int | None,
                             min_views: int | None = None, posted_within_days: int | None = None,
                             max_views: int | None = None, covers: bool = False) -> None:
    """ "Todos" (`limit=None`), N acima de 5.000, `photos_carousels` e os filtros de visualizações mínimas e de período
    de postagem só existem no provider web: com o método `gallery_dl` configurado a criação é recusada com erro claro
    (o legado truncaria em 5.000 itens, fingindo fim de perfil, recusa o filtro de conteúdo, não expõe contagem de
    visualizações e não encerra pelo período). O filtro de visualizações também exige conteúdo de vídeo. "Somente
    capas" (`covers`) também é só do método web."""
    views = min_views if min_views is not None else max_views
    if views is not None and not MIN_VIEWS_ENABLED:
        raise AppError(422, "min_views_unavailable", MIN_VIEWS_UNAVAILABLE_MESSAGE)
    if views is not None and content_type not in MIN_VIEWS_CONTENT_TYPES:
        raise AppError(422, "min_views_requires_video", MIN_VIEWS_CONTENT_MESSAGE)
    period = None if posted_within_days is None else "on"
    if (web_only(content_type, limit, views, period, covers=covers)
            and instagram_provider(ctx.app_settings, ctx.settings) == "gallery_dl"):
        raise AppError(422, "not_supported_by_provider", WEB_ONLY_MESSAGE)


def filter_fields(min_views: int | None, posted_within_days: int | None,
                  max_views: int | None = None) -> dict[str, Any]:
    """Colunas dos filtros para `create`/`create_group`. O corte do período é calculado AQUI, uma vez (agora - N
    dias, UTC) e gravado: retomada, reinício do app e "recomeçar listagem" usam sempre o mesmo corte. Sem filtro, nenhuma
    chave (as chamadas de criação ficam idênticas às de antes)."""
    fields: dict[str, Any] = {}
    if min_views is not None:
        fields["min_views"] = min_views
    if max_views is not None:
        fields["max_views"] = max_views
    if posted_within_days is not None:
        cutoff = utc_now(-posted_within_days * 86400)
        fields["posted_within_days"] = posted_within_days
        fields["posted_after"] = cutoff[:19] + "Z"   # "AAAA-MM-DDTHH:MM:SSZ" (sem milissegundos)
    return fields


def _filters_log(min_views: int | None, posted_within_days: int | None, max_views: int | None = None,
                 media_role: str | None = None) -> str:
    return ("" if min_views is None else f", min_views {min_views}") + (
        "" if max_views is None else f", max_views {max_views}") + (
        "" if posted_within_days is None else f", últimos {posted_within_days} dias") + (
        ", só capas" if media_role == "cover" else "")


def create_import(
    ctx: AppContext, *, profile: str, content_type: str, limit: int | None, preset_id: int | None, process: bool,
    edit: EditSettings | None = None, min_views: int | None = None, posted_within_days: int | None = None,
    max_views: int | None = None, media_role: str = "original", media_set: str | None = None,
    auto_export_content: str | None = None,
) -> int:
    # `limit=None` = "Todos": a listagem vai até o fim real do perfil (docs/BATCH_ROBUSTNESS.md, seção 0.1).
    # `min_views`: filtro de visualizações mínimas (só vídeos; None = desligado). `posted_within_days`: período de
    # postagem (últimos N dias; None = qualquer data); o corte é calculado AGORA e gravado no lote.
    # `max_views` (migration 0021): máximo de visualizações; `media_role="cover"`: "Somente capas".
    # `media_set` (migration 0023): tipo de mídia da importação unificada; `auto_export_content`: exportação ZIP
    # automática ao concluir.
    check_edit(edit, process=process, preset_id=preset_id)
    ensure_provider_supports(ctx, content_type, limit, min_views, posted_within_days, max_views,
                             covers=media_role == "cover" or media_set in COVER_SETS)
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
            media_role=media_role,
            media_set=media_set,
            auto_export_content=auto_export_content,
            **fields,
            **filter_fields(min_views, posted_within_days, max_views),
        )
    logger.info("Lote %s criado: importação de %s (%s, limite %s%s)", batch_id, username, content_type, limit,
                _filters_log(min_views, posted_within_days, max_views, media_role))
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
    process: bool, edit: EditSettings | None = None, min_views: int | None = None,
    posted_within_days: int | None = None, max_views: int | None = None, media_role: str = "original",
    media_set: str | None = None, auto_export_content: str | None = None,
) -> tuple[int, list[dict[str, str]], int]:
    """Uma importação pai e um lote filho por perfil (na ordem da lista; `limit`, `min_views` e o período valem POR
    PERFIL). Retorna (id do grupo, perfis pulados por já terem importação ativa, repetições removidas). Com `edit`, o
    grupo e todos os filhos são lotes de Edição. O corte do período é calculado UMA vez, na criação, e o mesmo vale
    para todos os filhos."""
    check_edit(edit, process=process, preset_id=preset_id)
    ensure_provider_supports(ctx, content_type, limit, min_views, posted_within_days, max_views,
                             covers=media_role == "cover" or media_set in COVER_SETS)
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
        "media_role": media_role,
        "media_set": media_set,
        **post_processing(ctx, preset_id=preset_id, process=process, edit=edit),
    }
    # Um único corte para o grupo e todos os filhos (calculado uma vez, aqui).
    fields.update(filter_fields(min_views, posted_within_days, max_views))
    # Conferência e criação na mesma transação (como em create_import): um perfil nunca ganha duas listagens.
    with ctx.db.transaction():
        importing = ctx.batches.open_listing_profiles()
        skipped = [{"profile": name, "reason": "profile_already_importing"} for name in valid if name in importing]
        pending = [name for name in valid if name not in importing]
        if not pending:
            raise AppError(409, "profile_already_importing",
                           "Todos os perfis da lista já têm uma importação em andamento.", extra={"skipped": skipped})
        # A exportação automática de um grupo é UMA, do grupo inteiro (os filhos não exportam sozinhos).
        group_id = ctx.batches.create_group(**fields, auto_export_content=auto_export_content)
        for position, username in enumerate(pending, start=1):
            ctx.batches.create(source="instagram_import", status="listing", source_profile=username,
                               group_id=group_id, group_position=position, **fields)
    logger.info("Grupo %s criado: %s perfis (%s, limite por perfil %s%s; %s pulados)", group_id, len(pending),
                content_type, limit, _filters_log(min_views, posted_within_days, max_views, media_role), len(skipped))
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
    content_type: str = "all", media_role: str = "original", media_set: str | None = None,
    auto_export_content: str | None = None,
) -> tuple[int, int]:
    """Um lote de importação por link (`import_mode = 'links'`) com todos os links da lista, na ordem colada.
    Retorna (id do lote, repetições removidas). Nenhuma requisição ao Instagram aqui: o import runner resolve um
    link por vez. `content_type="videos"` (importação unificada): só os vídeos das publicações resolvidas viram job
    (os demais itens contam como filtrados); `media_role="cover"`: a capa de cada vídeo (só método web)."""
    check_edit(edit, process=process, preset_id=preset_id)
    if ((media_role == "cover" or media_set in COVER_SETS)
            and instagram_provider(ctx.app_settings, ctx.settings) == "gallery_dl"):
        raise AppError(422, "not_supported_by_provider", COVERS_WEB_ONLY_MESSAGE)
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
            content_type=content_type,
            import_mode="links",
            media_role=media_role,
            media_set=media_set,
            auto_export_content=auto_export_content,
            **fields,
        )
        ctx.batches.add_links(batch_id, [(link["shortcode"], link["url"]) for link in valid])
    logger.info("Lote %s criado: importação por link (%s links, %s%s)", batch_id, len(valid), content_type,
                ", só capas" if media_role == "cover" else "")
    ctx.wake_import_runner()
    return batch_id, duplicates


# Importação unificada (docs/CONTRACTS.md, seção 8.6): uma entrada com publicações e/ou perfis. Separadores: quebra
# de linha, vírgula e ponto e vírgula. Espaço só separa quando TODOS os pedaços do trecho são URLs do Instagram ou
# `@usuario` (ex.: links colados lado a lado); senão o trecho inteiro é inválido ("lixo com espaço" nunca vira os
# perfis "lixo" e "com"). Cada entrada é publicação (/p/, /reel/, /reels/, /tv/) ou perfil (URL, `@usuario` ou
# `usuario`); o resto é inválido. Tudo reaproveita create_import/create_import_group/create_import_links (mesmas
# validações, tetos e transações).
_UNIFIED_SPLIT_RE = re.compile(r"[\r\n,;]+")
_UNIFIED_SPACE_RE = re.compile(r"\s+")
SPACED_ENTRY_REASON = "entrada com espaço: use uma por linha"
# Tipo de mídia -> (content_type do lote, papel do lote). O conjunto pedido fica em `media_set` (migration 0023) e
# decide os jobs de cada item (import_runner): "videos" = vídeos (original); "photos" = fotos, inclusive de carrossel
# (original); "covers" = só a capa de cada vídeo; "videos_covers" = vídeo + capa de cada vídeo; "all" = todos os itens
# originais (carrossel inteiro, na ordem) + a capa de cada vídeo. Um vídeo pode gerar dois jobs (original e capa).
MEDIA_CHOICES: dict[str, tuple[str, str]] = {
    "videos": ("videos", "original"),
    "photos": ("images", "original"),
    "covers": ("videos", "cover"),
    "videos_covers": ("videos", "original"),
    "all": ("all", "original"),
}
# Conjuntos com capa: só o método web entrega a capa do vídeo sozinha (como "Somente capas" na 0021).
COVER_SETS = frozenset({"covers", "videos_covers", "all"})
# Filtro de visualizações (só vídeos): permitido com estes tipos quando há perfis.
VIEWS_MEDIA = frozenset({"videos", "covers", "videos_covers"})
CONFIRM_ALL_MESSAGE = (
    "\"Todas\" pede ao Instagram todas as publicações de cada perfil: pode demorar e fazer muitas requisições. "
    "Confirme para continuar."
)
VIEWS_FILTER_MEDIA_MESSAGE = (
    "O filtro de visualizações só vale para vídeos: escolha \"Somente vídeos\", \"Somente capas\" ou \"Vídeos e "
    "capas\", ou desligue o filtro."
)

AUTO_EXPORT_NEEDS_PROCESSING_MESSAGE = (
    "Exportar \"editados\" ou \"originais e editados\" exige processar ou editar depois de baixar. Escolha um "
    "pós-processamento ou exporte só os originais."
)

LINK_FILTERS_IGNORED_NOTE = (
    "Os filtros de visualizações e de período valem só para perfis; as publicações por link foram importadas sem eles."
)


def _tv_as_post(token: str) -> str:
    """`/tv/<código>/` (IGTV antigo) é a mesma publicação de `/p/<código>/`: só a importação unificada aceita."""
    lowered = token.lower()
    for marker in ("instagram.com/tv/",):
        at = lowered.find(marker)
        if at >= 0:
            start = at + len("instagram.com/")
            return token[:start] + "p/" + token[start + 3:]
    return token


def _unified_tokens(raw: str) -> list[tuple[str, str | None]]:
    """Um texto colado -> [(entrada, motivo de inválida já conhecido ou None)]. Quebra de linha, vírgula e ponto e
    vírgula separam; espaço interno só separa se todos os pedaços forem URL do Instagram ou `@usuario`."""
    tokens: list[tuple[str, str | None]] = []
    for chunk in _UNIFIED_SPLIT_RE.split(raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        pieces = _UNIFIED_SPACE_RE.split(chunk)
        if len(pieces) == 1:
            tokens.append((chunk, None))
        elif all(piece.startswith("@") or "instagram.com/" in piece.lower() for piece in pieces):
            tokens.extend((piece, None) for piece in pieces)
        else:
            tokens.append((chunk, SPACED_ENTRY_REASON))
    return tokens


def classify_inputs(entries: list[str]) -> dict[str, Any]:
    """Entradas coladas -> {"profiles": [usernames], "links": [{shortcode, url, kind}], "invalid": [{entry, reason}],
    "duplicates": n}. Sem efeitos e sem nenhuma requisição ao Instagram. Ordem preservada; repetições (mesmo perfil,
    mesmo código de publicação ou mesma entrada inválida) contadas e removidas."""
    profiles: list[str] = []
    links: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    seen_profiles: set[str] = set()
    seen_links: set[str] = set()
    seen_invalid: set[str] = set()
    duplicates = 0
    for raw in entries:
        for token, spaced_reason in _unified_tokens(raw):
            if spaced_reason is not None:
                if token in seen_invalid:
                    duplicates += 1
                else:
                    seen_invalid.add(token)
                    invalid.append({"entry": token[:_MAX_ENTRY_ECHO], "reason": spaced_reason})
                continue
            try:
                shortcode, url = parse_post_link(_tv_as_post(token))
            except ValueError as link_error:
                try:
                    username = normalize_profile_entry(token)
                except ValueError as profile_error:
                    if token in seen_invalid:
                        duplicates += 1
                        continue
                    seen_invalid.add(token)
                    # Parece um link de publicação (caminho com o tipo): o motivo do link é o mais útil.
                    looks_post = any(f"/{kind}/" in token.lower() for kind in ("p", "reel", "reels", "tv"))
                    reason = str(link_error if looks_post else profile_error)
                    invalid.append({"entry": token[:_MAX_ENTRY_ECHO], "reason": reason})
                    continue
                if username in seen_profiles:
                    duplicates += 1
                else:
                    seen_profiles.add(username)
                    profiles.append(username)
                continue
            if shortcode in seen_links:
                duplicates += 1
            else:
                seen_links.add(shortcode)
                links.append({"shortcode": shortcode, "url": url, "kind": post_path_kind(url) or "p"})
    if len(profiles) > MAX_GROUP_PROFILES:
        raise AppError(422, "too_many_profiles",
                       f"A lista tem mais de {MAX_GROUP_PROFILES} perfis; divida em mais de uma importação.")
    if len(links) > MAX_BATCH_LINKS:
        raise AppError(422, "too_many_links",
                       f"A lista tem mais de {MAX_BATCH_LINKS} links; divida em mais de uma importação.")
    return {"profiles": profiles, "links": links, "invalid": invalid, "duplicates": duplicates}


def start_unified(ctx: AppContext, req: Any) -> dict[str, Any]:
    """Importação unificada (`UnifiedImportRequest`): 1 perfil -> lote avulso; 2+ perfis -> grupo; publicações ->
    um lote de links. Período, visualizações, quantidade e `confirm_all` valem só para perfis; o tipo de mídia vale
    para perfis e links. Tudo é conferido ANTES de criar qualquer lote (nada fica pela metade por erro de validação).
    Também usada pelo endpoint da extensão (`/api/ext/start`)."""
    classified = classify_inputs(list(req.inputs) if req.inputs is not None else [req.text or ""])
    profiles, links = classified["profiles"], classified["links"]
    if not profiles and not links:
        raise AppError(422, "no_valid_inputs", "Nenhuma entrada é um link de publicação ou um perfil válido.",
                       extra={"invalid": classified["invalid"], "duplicates": classified["duplicates"]})
    content_type, media_role = MEDIA_CHOICES[req.media]
    media_set = req.media
    views = req.min_views is not None or req.max_views is not None
    # Visualizações com fotos ("Somente fotos", "Todas") só são recusadas quando há perfis: para links os filtros de
    # perfil são ignorados (avisado em `notes`).
    if views and req.media not in VIEWS_MEDIA and profiles:
        raise AppError(422, "views_filter_media", VIEWS_FILTER_MEDIA_MESSAGE)
    if profiles and req.limit is None and not req.confirm_all:
        raise AppError(422, "confirm_all_required", CONFIRM_ALL_MESSAGE)
    # Pós-processamento: só vídeos originais são processados/editados; "Somente capas" e "Somente fotos" só baixam
    # (nos conjuntos mistos, fotos e capas são só baixadas: import_runner._add_item).
    covers = media_role == "cover" or media_set == "photos"
    with_covers = media_set in COVER_SETS
    auto_export = req.auto_export.content if getattr(req, "auto_export", None) is not None else None
    # Exportar "editados"/"ambos" sem pós-processamento: não haveria versão editada para exportar.
    # "Somente capas"/"Somente fotos" nunca são processadas: também não haveria versão editada.
    if auto_export in ("edited", "both") and (req.post_processing == "none" or covers):
        raise AppError(422, "auto_export_needs_processing", AUTO_EXPORT_NEEDS_PROCESSING_MESSAGE)
    process = req.post_processing == "preset" and not covers
    preset_id = req.preset_id if process else None
    edit = req.edit if req.post_processing == "edit" and not covers else None
    filters = ({"min_views": req.min_views, "max_views": req.max_views, "posted_within_days": req.posted_within_days}
               if profiles else {})
    # Conferências de todos os lotes antes de criar o primeiro.
    check_edit(edit, process=process, preset_id=preset_id)
    if profiles:
        ensure_provider_supports(ctx, content_type, req.limit, req.min_views, req.posted_within_days, req.max_views,
                                 covers=with_covers)
    if links and with_covers and instagram_provider(ctx.app_settings, ctx.settings) == "gallery_dl":
        raise AppError(422, "not_supported_by_provider", COVERS_WEB_ONLY_MESSAGE)
    if not ctx.session_provider.status().configured:
        raise AppError(409, "session_missing", SESSION_MISSING_MESSAGE)
    ensure_not_paused(ctx)
    if process or preset_id is not None:
        resolve_preset(ctx, preset_id)
    # Perfis E links: UM ZIP por pedido (migration 0024). A exportação automática fica no pedido, não nas partes.
    multipart = bool(profiles) and bool(links) and auto_export is not None
    common = {"preset_id": preset_id, "process": process, "edit": edit, "media_set": media_set,
              "auto_export_content": None if multipart else auto_export}
    batch_ids: list[int] = []
    group_id: int | None = None
    links_batch_id: int | None = None
    skipped: list[dict[str, str]] = []
    # Tudo numa transação só (a do banco é reentrante: as criações abaixo entram nela): se a criação do lote de
    # links falhar, o lote/grupo de perfis criado antes é desfeito junto (nada fica pela metade).
    with ctx.db.transaction():
        if len(profiles) == 1:
            batch_ids.append(create_import(ctx, profile=profiles[0], content_type=content_type, limit=req.limit,
                                           media_role=media_role, **common, **filters))
        elif profiles:
            group_id, skipped, _ = create_import_group(ctx, profiles=profiles, content_type=content_type,
                                                       limit=req.limit, media_role=media_role, **common, **filters)
        if links:
            links_batch_id, _ = create_import_links(ctx, links=[link["url"] for link in links],
                                                    content_type=content_type, media_role=media_role, **common)
            batch_ids.append(links_batch_id)
        if multipart:
            ctx.batches.create_request(auto_export_content=auto_export, batch_ids=batch_ids, group_id=group_id)
    # As criações acordam o runner ainda dentro da transação (ele pode não ter visto os lotes): acorda de novo.
    ctx.wake_import_runner()
    notes: list[str] = []
    if links and (views or req.posted_within_days is not None):
        notes.append(LINK_FILTERS_IGNORED_NOTE)
    logger.info("Importação unificada: %s perfis, %s publicações, %s inválidas, mídia %s", len(profiles), len(links),
                len(classified["invalid"]), req.media)
    return {"batch_ids": batch_ids, "group_id": group_id, "links_batch_id": links_batch_id,
            "profiles": len(profiles), "links": len(links), "invalid": classified["invalid"],
            "duplicates": classified["duplicates"], "skipped": skipped, "notes": notes}


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
    # Idem para os vereditos do filtro de visualizações (lote sem filtro não é tocado).
    ctx.batches.add_listing_views(batch_id, merge_partial=True)
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
    ctx: AppContext, *, view: str, media_type: str | None, q: str | None, profile: str | None, preset_id: int,
    role: str | None = None,
) -> int:
    """Cria no servidor, numa transação, um job de process por mídia do filtro da Biblioteca."""
    preset = resolve_preset(ctx, preset_id)
    # Idempotente (seção 9, M3): ignora mídias com processamento aberto ou já processadas com o mesmo preset/params.
    where, params = ctx.media.process_filter_where(
        view=view, media_type=media_type, profile=profile, q=q, preset_id=preset["id"], params=preset["params"],
        role=role,
    )
    with ctx.db.transaction():
        total = ctx.media.count(view=view, media_type=media_type, profile=profile, q=q, role=role)
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
