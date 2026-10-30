"""API da extensão do Chrome (``/api/ext/*``; docs/EXTENSION.md).

Toda rota exige o token do pareamento (``Authorization: Bearer <token>`` ou ``X-Media-Tool-Token``), exceto
``POST /api/ext/pair``, que exige um código de pareamento válido. A origem ``chrome-extension://`` só é aceita
aqui (core/security.py). Nenhuma rota devolve o token, exceto a resposta única do pareamento.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import Field

from app import __version__
from app.api.deps import MAX_ID, Ctx, IdPath
from app.api.imports import _PROFILE_ERROR_STATUS
from app.core.errors import AppError
from app.core.security import EXTENSION_TOKEN_HEADER
from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.validation import (
    parse_post_link,
    post_path_kind,
    validate_instagram_url,
    validate_shortcode,
)
from app.jobs.errors import safe_message
from app.schemas.batches import UnifiedImportRequest
from app.schemas.common import PositiveId, SafeText, StrictModel
from app.schemas.edits import EditSettings
from app.services import edit_service, export_service, extension_auth, extension_ops, import_service
from app.services.deadline import CallInProgress, DeadlineExceeded, run_with_deadline
from app.services.import_service import normalize_profile_entry
from app.services.instagram_gate import InstagramPaused
from app.services.instagram_pause import ensure_not_paused, raise_paused
from app.services.presenters import batches_out, groups_out
from app.services.settings_service import instagram_status

logger = logging.getLogger("media_tool.extension")

router = APIRouter(prefix="/ext", tags=["extensão do Chrome"])

TOKEN_INVALID_MESSAGE = (
    "A extensão não está conectada ou o acesso foi revogado. Pareie de novo nas opções da extensão."
)
MAX_PROGRESS_BATCHES = 50
_IDS_RE = re.compile(r"^\d{1,16}(,\d{1,16})*$")
# Valores iniciais do popup (mesmos da tela Importar: só vídeos, Últimas 50 por perfil, qualquer data, só baixar,
# sem exportação automática; docs/EXTENSION.md).
POPUP_DEFAULTS = {"media": "videos", "limit": 50, "posted_within_days": None, "post_processing": "none",
                  "auto_export": None, "activity_limit": 20, "library_limit": 50}
# Capacidades desta versão do backend (a extensão esconde o que o app ainda não oferece).
FEATURES = ["page_info_library", "post_preview", "media_sets", "auto_export", "client_request_id", "activity",
            "batch_items", "cancel", "library", "thumbnails", "edit", "process", "exports"]
# Tetos das rotas da central (além do corpo de 1 MB da API).
MAX_EXT_MEDIA_IDS = 500
MAX_EXT_EXPORT_IDS = 5000
# Cache da prévia de publicação (10 min por shortcode), por instância do app (guardado no contexto): é só cache, some
# num reinício sem efeito nenhum. As respostas do start por client_request_id NÃO ficam aqui: são persistidas em
# app_state (extension_ops.start_replay_*), sob a trava `start_lock`.
_CACHES_LOCK = threading.Lock()


def _caches(ctx: Ctx) -> dict[str, Any]:
    with _CACHES_LOCK:
        caches = getattr(ctx, "_extension_caches", None)
        if caches is None:
            caches = {"preview": extension_ops.TtlCache(), "start_lock": threading.Lock()}
            ctx._extension_caches = caches
        return caches


def _token_from(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    header = request.headers.get(EXTENSION_TOKEN_HEADER)
    return header.strip() if header and header.strip() else None


def require_token(request: Request, ctx: Ctx) -> dict[str, Any]:
    device = extension_auth.verify_token(ctx, _token_from(request))
    if device is None:
        raise AppError(401, "extension_unauthorized", TOKEN_INVALID_MESSAGE)
    return device


Device = Annotated[dict[str, Any], Depends(require_token)]


class PairRequest(StrictModel):
    code: str = Field(min_length=1, max_length=32)
    device_name: SafeText = Field(min_length=1, max_length=60)


class UrlRequest(StrictModel):
    url: str = Field(min_length=1, max_length=2048)


def _app_url(ctx: Ctx, path: str) -> str:
    return f"http://127.0.0.1:{ctx.settings.port}{path}"


def _https(url: str) -> str:
    url = url.strip()
    lowered = url.lower()
    if lowered.startswith("http://"):
        return "https://" + url[7:]
    if lowered.startswith(("instagram.com/", "www.instagram.com/")):
        return "https://" + url
    return url


def _tv_shortcode(url: str) -> str | None:
    """Código de um link `/tv/<código>/` (vídeo do antigo IGTV); None se não for um."""
    try:
        segments = [seg for seg in urlsplit(validate_instagram_url(_https(url))).path.split("/") if seg]
    except ValueError:
        return None
    if len(segments) != 2 or segments[0].lower() != "tv":
        return None
    try:
        return validate_shortcode(segments[1])
    except ValueError:
        return None


def classify_page(url: str) -> dict[str, Any]:
    """Classificação da URL da aba, sem nenhuma requisição: publicação, reel, perfil ou outra página."""
    try:
        shortcode, canonical = parse_post_link(url)
    except ValueError:
        shortcode = None
    if shortcode is not None:
        result: dict[str, Any] = {"kind": "reel" if post_path_kind(canonical) == "reel" else "post",
                                  "shortcode": shortcode, "canonical_url": canonical}
        segments = [seg for seg in urlsplit(validate_instagram_url(_https(url))).path.split("/") if seg]
        if len(segments) == 3:
            result["username"] = segments[0].lower()
        return result
    code = _tv_shortcode(url)
    if code is not None:
        # O shortcode identifica a publicação; `/p/<código>/` é a forma aceita pelos validadores de link do app.
        return {"kind": "reel", "shortcode": code, "canonical_url": f"https://www.instagram.com/p/{code}/"}
    try:
        validate_instagram_url(_https(url))
        username = normalize_profile_entry(_https(url))
    except (ValueError, AppError):
        return {"kind": "other"}
    return {"kind": "profile", "username": username, "canonical_url": f"https://www.instagram.com/{username}/"}


# --------------------------------------------------------------------------- rotas


@router.post("/pair")
def pair(ctx: Ctx, body: PairRequest, response: Response) -> dict[str, str]:
    """Troca o código mostrado em Configurações por um token (exibido só nesta resposta)."""
    response.headers["Cache-Control"] = "no-store"
    return extension_auth.pair(ctx, body.code, body.device_name.strip())


@router.get("/status")
def status(ctx: Ctx, _device: Device) -> dict[str, Any]:
    ig = instagram_status(ctx)
    message = None
    try:
        ensure_not_paused(ctx)
    except AppError as exc:
        message = exc.detail
    if message is None and not ig["session_configured"]:
        message = import_service.SESSION_MISSING_MESSAGE
    return {
        "ok": True,
        "app": {"version": __version__},
        "instagram": {"session_configured": ig["session_configured"], "paused": bool(ig["paused_until"]),
                      "pause_reason": ig["pause_reason"], "paused_until": ig["paused_until"], "message": message},
        "defaults": POPUP_DEFAULTS,
        "features": FEATURES,
    }


@router.post("/page-info")
def page_info(ctx: Ctx, _device: Device, body: UrlRequest) -> dict[str, Any]:
    """Classificação da aba (sem rede) + o que já está na biblioteca: itens da publicação ou contagens do perfil."""
    page = classify_page(body.url)
    if page["kind"] in ("post", "reel"):
        page["library"] = extension_ops.post_library(ctx, page["shortcode"])
    elif page["kind"] == "profile":
        page["library"] = extension_ops.profile_library(ctx, page["username"])
    return page


def _resolve_post_once(ctx: Ctx, page: dict[str, Any], name: str) -> list[Any]:
    """Uma leitura da publicação pelo `resolve_post` existente (portão, teto e pausa do app). Erro do Instagram é
    classificado e devolvido como está: nenhuma nova tentativa automática."""
    ensure_not_paused(ctx)
    shortcode = page["shortcode"]
    path_kind = post_path_kind(page["canonical_url"])
    timeout = ctx.settings.profile_preview_timeout
    try:
        return run_with_deadline(
            lambda: ctx.downloader_factory().resolve_post(shortcode, path_kind=path_kind), timeout,
            name=name, lock=ctx.profile_preview_lock,
        )
    except CallInProgress:
        raise AppError(409, "profile_preview_in_progress",
                       "Já existe uma consulta ao Instagram em andamento. Tente de novo em instantes.") from None
    except InstagramPaused:
        raise_paused(ctx)
    except DeadlineExceeded:
        raise AppError(504, "timeout", f"O Instagram não respondeu em {int(timeout)} s. Tente de novo em alguns "
                       "minutos.") from None
    except InstagramError as exc:
        if ctx.worker is not None:
            ctx.worker.pause_for(exc.code)
        raise AppError(_PROFILE_ERROR_STATUS.get(exc.code, 502), exc.code, safe_message(exc.message)) from None


@router.post("/post-owner")
def post_owner(ctx: Ctx, _device: Device, body: UrlRequest) -> dict[str, Any]:
    """@ do dono de uma publicação (para "Coletar o perfil desta publicação"); uma consulta pelo portão do app."""
    page = classify_page(body.url)
    if page["kind"] not in ("post", "reel"):
        raise AppError(422, "invalid_input", "O link não aponta para uma publicação do Instagram.")
    if page.get("username"):
        return {"username": page["username"], "shortcode": page["shortcode"]}
    shortcode = page["shortcode"]
    cached = _caches(ctx)["preview"].get(shortcode)
    if cached is not None and cached.get("username"):
        return {"username": cached["username"], "shortcode": shortcode}
    items = _resolve_post_once(ctx, page, "extension-post-owner")
    owner = next((item.source_profile for item in items if item.source_profile), None)
    if not owner:
        raise AppError(404, "owner_not_found", "Não foi possível descobrir o perfil dono desta publicação.")
    return {"username": owner.lower(), "shortcode": shortcode}


@router.post("/post-preview")
def post_preview(ctx: Ctx, _device: Device, body: UrlRequest) -> dict[str, Any]:
    """Tipo e itens de uma publicação (1 leitura pelo portão; cache de 10 min por shortcode). Sem URLs de mídia."""
    page = classify_page(body.url)
    if page["kind"] not in ("post", "reel"):
        raise AppError(422, "invalid_input", "O link não aponta para uma publicação do Instagram.")
    shortcode = page["shortcode"]
    cache = _caches(ctx)["preview"]
    cached = cache.get(shortcode)
    if cached is not None:
        return cached
    items = [item for item in _resolve_post_once(ctx, page, "extension-post-preview")
             if item.source_post_id == shortcode]
    if not items:
        raise AppError(404, "content_unavailable", "A publicação não tem mídias disponíveis.")
    result = extension_ops.preview_out(shortcode, items)
    cache.put(shortcode, result)
    return result


class ExtStartRequest(UnifiedImportRequest):
    # Idempotência (10 min): a mesma id devolve a mesma resposta sem criar de novo.
    client_request_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")


@router.post("/start", status_code=202)
def start(ctx: Ctx, device: Device, body: ExtStartRequest) -> dict[str, Any]:
    """Mesmo corpo e mesma resposta de ``POST /api/imports/start`` (importação unificada, com `auto_export`), mais
    ``open_url``. Com ``client_request_id`` repetido em até 10 min (mesmo dispositivo): a mesma resposta, sem criar
    nada de novo, inclusive depois de um reinício do app (persistido em app_state)."""
    device_id = str(device.get("id") or "")
    request_id = body.client_request_id if device_id else None
    with _caches(ctx)["start_lock"]:
        if request_id is not None:
            cached = extension_ops.start_replay_get(ctx, device_id, request_id)
            if cached is not None:
                return {**cached, "replayed": True}
        result = import_service.start_unified(ctx, body)
        batch_ids = list(result.get("batch_ids") or [])
        group_id = result.get("group_id")
        response = {**result, "open_url": _open_url(ctx, batch_ids, group_id)}
        if request_id is not None:
            extension_ops.start_replay_put(ctx, device_id, request_id, response)
    return response


def _open_url(ctx: Ctx, batch_ids: list[int], group_id: int | None) -> str:
    if group_id is not None or len(batch_ids) != 1:
        return _app_url(ctx, "/")  # grupos e vários lotes: tela Importar (lista de importações)
    return _app_url(ctx, f"/fila?lote={int(batch_ids[0])}")


def _parse_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    if not _IDS_RE.match(raw):
        raise AppError(422, "validation_error", "batch_ids deve ser uma lista de números separados por vírgula.")
    ids = list(dict.fromkeys(int(part) for part in raw.split(",")))
    if len(ids) > MAX_PROGRESS_BATCHES or any(not 1 <= value <= MAX_ID for value in ids):
        raise AppError(422, "validation_error", f"Informe de 1 a {MAX_PROGRESS_BATCHES} lotes válidos.")
    return ids


def _batch_summary(ctx: Ctx, batch: dict[str, Any]) -> dict[str, Any]:
    progress = batch["progress"]
    finished = batch["status"] in ("completed", "failed", "cancelled")
    return {
        "id": batch["id"],
        "status": batch["status"],
        "profile": batch.get("source_profile"),
        "found": batch["found"],
        "downloaded": int(progress.get("downloaded", 0)),
        "duplicates": int(progress.get("duplicates", 0)),
        "filtered": int(progress.get("filtered", 0)),
        "skipped": int(progress.get("skipped", 0)),
        "failed": int(progress.get("failed", 0)),
        "active": not finished,
        "finished": finished,
        "error_message": batch.get("error_message"),
        "open_url": _app_url(ctx, f"/fila?lote={batch['id']}"),
    }


@router.get("/progress")
def progress(
    ctx: Ctx,
    _device: Device,
    batch_ids: Annotated[str | None, Query(max_length=1024)] = None,
    group_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
) -> dict[str, Any]:
    """Resumo do andamento dos lotes/grupo iniciados pela extensão (mesmos presenters das telas do app)."""
    ids = _parse_ids(batch_ids)
    if not ids and group_id is None:
        raise AppError(422, "validation_error", "Informe batch_ids ou group_id.")
    rows = [row for row in (ctx.batches.get(batch_id) for batch_id in ids) if row is not None]
    batches = [_batch_summary(ctx, batch) for batch in batches_out(ctx, rows)]
    group = None
    if group_id is not None:
        row = ctx.batches.get_group(group_id)
        if row is not None:
            data = groups_out(ctx, [row])[0]
            prog = data["progress"]
            finished = data["status"] in ("completed", "cancelled")
            group = {
                "id": data["id"],
                "status": data["status"],
                "profiles": data["profiles"],
                "found": int(prog.get("found", 0)),
                "downloaded": int(prog.get("downloaded", 0)),
                "duplicates": int(prog.get("duplicates", 0)),
                "filtered": int(prog.get("filtered", 0)),
                "skipped": int(prog.get("skipped", 0)),
                "failed": int(prog.get("failed", 0)),
                "active": not finished,
                "finished": finished,
                "open_url": _app_url(ctx, "/"),
            }
    done = all(batch["finished"] for batch in batches) and (group is None or group["finished"])
    return {"batches": batches, "group": group, "finished": done,
            "missing": [batch_id for batch_id in ids if batch_id not in {b["id"] for b in batches}],
            "open_url": _open_url(ctx, ids, group_id), "library_url": _app_url(ctx, "/biblioteca")}


# --------------------------------------------------------------------------- central de operações (seção 8.7)


@router.get("/activity")
def activity(
    ctx: Ctx, _device: Device, limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Importações recentes de nível superior (grupos e lotes avulsos, inclusive Edição/processamento)."""
    return {"items": extension_ops.activity(ctx, limit)}


@router.get("/batches/{batch_id}/items")
def batch_items(
    ctx: Ctx, _device: Device, batch_id: IdPath,
    before_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    return extension_ops.batch_items(ctx, batch_id, before_id=before_id, limit=limit)


@router.get("/groups/{group_id}/children")
def group_children(
    ctx: Ctx, _device: Device, group_id: IdPath,
    after_position: Annotated[int | None, Query(ge=0, le=MAX_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Perfis (lotes filhos) de um grupo, paginados por `after_position`, no formato de item de lote da Atividade."""
    return extension_ops.group_children(ctx, group_id, after_position=after_position, limit=limit)


@router.post("/batches/{batch_id}/cancel")
def cancel_batch(ctx: Ctx, _device: Device, batch_id: IdPath) -> dict[str, bool]:
    import_service.cancel_batch(ctx, batch_id)
    return {"ok": True}


@router.post("/groups/{group_id}/cancel")
def cancel_group(ctx: Ctx, _device: Device, group_id: IdPath) -> dict[str, bool]:
    import_service.cancel_group(ctx, group_id)
    return {"ok": True}


MediaTypeQuery = Annotated[Literal["video", "image"] | None, Query()]
RoleQuery = Annotated[Literal["original", "cover"] | None, Query()]


@router.get("/library")
def library(
    ctx: Ctx, _device: Device,
    before_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
    media_type: MediaTypeQuery = None,
    role: RoleQuery = None,
    view: Literal["all", "originals", "processed"] = "all",
    batch_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    group_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    profile: Annotated[SafeText | None, Query(max_length=64)] = None,
    q: Annotated[SafeText | None, Query(max_length=200)] = None,
) -> dict[str, Any]:
    """Biblioteca em ordem id DESC (keyset `before_id`/`next_before_id`), com os filtros da tela Biblioteca."""
    items, total, next_before_id = ctx.media.list(
        view=view, media_type=media_type, role=role, profile=(profile or "").strip().lstrip("@") or None,
        q=(q or "").strip() or None, limit=limit, before_id=before_id,
        scope=extension_ops.library_scope(batch_id, group_id),
    )
    return {"items": extension_ops.library_items(ctx, items), "total": total, "next_before_id": next_before_id}


@router.get("/library/ids")
def library_ids(
    ctx: Ctx, _device: Device,
    batch_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    group_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    media_type: MediaTypeQuery = None,
    role: RoleQuery = None,
) -> dict[str, Any]:
    """Ids (até 5.000) de um lote ou grupo, para "selecionar todos"."""
    scope = extension_ops.library_scope(batch_id, group_id)
    if scope is None:
        raise AppError(422, "validation_error", "Informe batch_id ou group_id.")
    return extension_ops.library_ids(ctx, scope=scope, media_type=media_type, role=role)


@router.get("/media/{media_id}/thumbnail", response_class=FileResponse)
def media_thumbnail(ctx: Ctx, _device: Device, media_id: IdPath) -> FileResponse:
    """Miniatura JPEG (imagem: a miniatura ou a própria; vídeo: a miniatura, gerada se faltar). Nunca o vídeo."""
    media = ctx.media.get(media_id)
    if media is None:
        raise AppError(404, "media_not_found", "Mídia não encontrada.")
    path, mime = extension_ops.thumbnail_file(ctx, media)
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


@router.get("/edit/options")
def edit_options(ctx: Ctx, _device: Device) -> dict[str, Any]:
    """Os controles reais da página Editar (duração, velocidade, nitidez, saída MP4) e os presets existentes."""
    default_preset = ctx.app_settings.get("default_preset_id")
    return {
        "edit": {
            "duration_modes": [{"value": value, "label": label} for value, label in (
                ("original", "Original"), ("15", "15 s"), ("30", "30 s"), ("60", "60 s"),
                ("custom", "Personalizada"))],
            "custom_seconds_max": 7200,
            "duration_margin": edit_service.EDIT_DURATION_MARGIN,
            "speed": {"min": 1.0, "max": 1.5, "step": 0.01, "presets": list(EDIT_SPEED_PRESETS)},
            "sharpen": {"label": "Nitidez sutil", "description": "Realce leve para melhorar a aparência do vídeo."},
            "output": "mp4",
            "max_items": MAX_EXT_MEDIA_IDS,
        },
        "presets": extension_ops.presets_out(ctx),
        "defaults": {"duration_mode": "original", "custom_seconds": None, "speed": 1.0, "sharpen": False,
                     "output_type": "mp4", "preset_id": default_preset},
    }


# Atalhos de velocidade da página Editar (frontend/src/lib/editSettings.ts, SPEED_PRESETS).
EDIT_SPEED_PRESETS = (1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.5)
MediaIds = Annotated[list[PositiveId], Field(min_length=1, max_length=MAX_EXT_MEDIA_IDS)]


class ExtEditSettings(EditSettings):
    # A extensão usa a saída da página Editar (sempre MP4).
    output_type: Literal["mp4"] = "mp4"


class ExtEditRequest(StrictModel):
    # Sem recorte: o recorte manual é feito no Media Tool.
    media_ids: MediaIds
    settings: ExtEditSettings


class ExtProcessRequest(StrictModel):
    media_ids: MediaIds
    preset_id: PositiveId


def _skipped_only(skipped: list[dict[str, Any]], code: str, message: str) -> AppError:
    return AppError(422, code, message, extra={"skipped": skipped})


@router.post("/edits", status_code=202)
def start_edit(ctx: Ctx, _device: Device, body: ExtEditRequest) -> dict[str, Any]:
    """Edição em lote pelo serviço da página Editar. Só vídeos originais; os demais voltam em `skipped`."""
    videos, skipped = extension_ops.split_videos(ctx, body.media_ids)
    if not videos:
        raise _skipped_only(skipped, "edit_requires_video", "Nenhuma das mídias selecionadas é vídeo.")
    batch_id = edit_service.start(ctx, videos, body.settings)
    return {"batch_id": batch_id, "created": len(videos), "skipped": skipped,
            "open_url": _app_url(ctx, f"/fila?lote={batch_id}")}


@router.post("/process", status_code=202)
def process(ctx: Ctx, _device: Device, body: ExtProcessRequest) -> dict[str, Any]:
    """Processamento por preset existente (o mesmo de ``POST /api/media/process``). Só vídeos originais."""
    videos, skipped = extension_ops.split_videos(ctx, body.media_ids)
    if not videos:
        raise _skipped_only(skipped, "process_requires_video", "Nenhuma das mídias selecionadas é vídeo.")
    batch_id = import_service.process_media_bulk(ctx, videos, body.preset_id)
    return {"batch_id": batch_id, "created": len(videos), "skipped": skipped,
            "open_url": _app_url(ctx, f"/fila?lote={batch_id}")}


class ExtBatchSource(StrictModel):
    kind: Literal["batch", "group"]
    id: PositiveId


class ExtLibrarySource(StrictModel):
    kind: Literal["library"]
    media_ids: Annotated[list[PositiveId], Field(min_length=1, max_length=MAX_EXT_EXPORT_IDS)]


class ExtExportRequest(StrictModel):
    source: Annotated[ExtBatchSource | ExtLibrarySource, Field(discriminator="kind")]
    content: Literal["originals", "edited", "both"] = "originals"


@router.post("/exports", status_code=202)
def create_export(ctx: Ctx, _device: Device, body: ExtExportRequest) -> dict[str, Any]:
    """Sempre UM ZIP por pedido, pelo serviço de exportação existente."""
    if body.source.kind == "library":
        source = {"kind": "library", "selection": {"media_ids": list(dict.fromkeys(body.source.media_ids))}}
    else:
        source = {"kind": body.source.kind, "id": body.source.id}
    export = export_service.create_export(ctx, source, {"zip": True}, body.content)
    return {"id": export["id"], "status": export["status"]}


@router.get("/exports/{export_id}")
def get_export(ctx: Ctx, _device: Device, export_id: IdPath) -> dict[str, Any]:
    return extension_ops.export_status(ctx, export_service.get_export(ctx, export_id))
