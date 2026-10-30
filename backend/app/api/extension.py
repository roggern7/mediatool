"""API da extensão do Chrome (``/api/ext/*``; docs/EXTENSION.md).

Toda rota exige o token do pareamento (``Authorization: Bearer <token>`` ou ``X-Media-Tool-Token``), exceto
``POST /api/ext/pair``, que exige um código de pareamento válido. A origem ``chrome-extension://`` só é aceita
aqui (core/security.py). Nenhuma rota devolve o token, exceto a resposta única do pareamento.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field

from app import __version__
from app.api.deps import MAX_ID, Ctx
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
from app.schemas.common import SafeText, StrictModel
from app.services import extension_auth, import_service
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
# Valores iniciais do popup (mesmos da tela Importar: só vídeos, Últimas 50 por perfil, qualquer data, só baixar;
# docs/EXTENSION.md).
POPUP_DEFAULTS = {"media": "videos", "limit": 50, "posted_within_days": None, "post_processing": "none"}


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
    }


@router.post("/page-info")
def page_info(_device: Device, body: UrlRequest) -> dict[str, Any]:
    return classify_page(body.url)


@router.post("/post-owner")
def post_owner(ctx: Ctx, _device: Device, body: UrlRequest) -> dict[str, Any]:
    """@ do dono de uma publicação (para "Coletar o perfil desta publicação"); uma consulta pelo portão do app."""
    page = classify_page(body.url)
    if page["kind"] not in ("post", "reel"):
        raise AppError(422, "invalid_input", "O link não aponta para uma publicação do Instagram.")
    if page.get("username"):
        return {"username": page["username"], "shortcode": page["shortcode"]}
    ensure_not_paused(ctx)
    shortcode = page["shortcode"]
    path_kind = post_path_kind(page["canonical_url"])
    timeout = ctx.settings.profile_preview_timeout
    try:
        items = run_with_deadline(
            lambda: ctx.downloader_factory().resolve_post(shortcode, path_kind=path_kind), timeout,
            name="extension-post-owner", lock=ctx.profile_preview_lock,
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
    owner = next((item.source_profile for item in items if item.source_profile), None)
    if not owner:
        raise AppError(404, "owner_not_found", "Não foi possível descobrir o perfil dono desta publicação.")
    return {"username": owner.lower(), "shortcode": shortcode}


@router.post("/start", status_code=202)
def start(ctx: Ctx, _device: Device, body: UnifiedImportRequest) -> dict[str, Any]:
    """Mesmo corpo e mesma resposta de ``POST /api/imports/start`` (importação unificada), mais ``open_url``."""
    result = import_service.start_unified(ctx, body)
    batch_ids = list(result.get("batch_ids") or [])
    group_id = result.get("group_id")
    return {**result, "open_url": _open_url(ctx, batch_ids, group_id)}


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
