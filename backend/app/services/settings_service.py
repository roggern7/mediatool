"""Configurações do app e status da sessão do Instagram (sem nunca expor o segredo)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from media_processor import MediaProcessor
from pydantic import SecretStr

from app.core.config import INSTAGRAM_PROVIDERS, MAX_CONCURRENCY, MAX_DOWNLOAD_CONCURRENCY, MIN_CONCURRENCY, Settings
from app.core.errors import AppError
from app.db.connection import utc_now
from app.integrations.instagram.downloader import InstagramError
from app.integrations.instagram.session import SessionStorageError, SessionValidationError
from app.jobs.errors import safe_message
from app.services.context import AppContext
from app.services.deadline import CallInProgress, DeadlineExceeded, run_with_deadline
from app.services.instagram_gate import InstagramPaused
from app.services.instagram_pause import raise_paused
from app.services.instagram_recovery import pause_scope

logger = logging.getLogger("media_tool.settings")

LAST_TEST_KEY = "instagram_session_last_test"
SESSION_EXTRA_KEYS = ("csrftoken", "ds_user_id", "mid", "ig_did")
# Nomes de cookies extras que podem aparecer no status (só nomes, nunca valores).
_KNOWN_EXTRA_KEYS = ("csrftoken", "ds_user_id", "mid", "ig_did", "rur")
SESSION_TEST_RESULTS = (
    "valid", "invalid", "expired", "checkpoint_required", "rate_limited", "restricted", "api_refused", "timeout",
    "network_error", "instagram_error", "session_missing",
)
LISTING_API_STATES = ("ok", "refused", "rate_limited", "unknown")
WEB_STAGE_STATES = ("ok", "failed", "unknown")
DOC_IDS_KEY = "instagram_web_doc_ids"
# `detail` do adapter é um identificador curto (ex.: redirect_home); qualquer outra coisa é descartada.
_DETAIL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Código de erro do adapter -> `result` estruturado (usado quando o adapter ainda não informa `result`).
_RESULT_BY_CODE = {
    "ok": "valid",
    "session_invalid": "invalid",
    "session_expired": "expired",
    "checkpoint_required": "checkpoint_required",
    "rate_limited": "rate_limited",
    "action_blocked": "restricted",
    "access_refused": "api_refused",
    "timeout": "timeout",
    "network_error": "network_error",
    "session_missing": "session_missing",
}


def _pause_reason_for(record: dict[str, Any]) -> str | None:
    """Motivo de pausa para o resultado de um teste de sessão (o teste em si é sempre permitido).

    Listagem recusada (`api_refused`, redirect para a página inicial) não pausa. `session_invalid` só chega aqui
    com evidência explícita do adapter (require_login, 401, sessionid apagado).
    """
    if record["result"] == "checkpoint_required" or record["code"] == "checkpoint_required":
        return "checkpoint_required"
    if record["code"] in ("session_invalid", "session_expired"):
        return "session_invalid"
    if record["code"] == "request_auth_failed":
        return "request_auth_failed"
    if record["result"] == "rate_limited":
        return "rate_limited"
    if record["result"] == "restricted" or record["code"] == "action_blocked":
        return "action_blocked"
    return None


def effective_concurrency(ctx: AppContext) -> int:
    """Concorrência do pool de processamento FFmpeg (1..2; valores antigos maiores são limitados)."""
    value = int(ctx.app_settings.get("concurrency", ctx.settings.concurrency))
    return max(MIN_CONCURRENCY, min(MAX_CONCURRENCY, value))


def effective_download_concurrency(ctx: AppContext) -> int:
    """Concorrência do pool de downloads (1..4)."""
    value = int(ctx.app_settings.get("download_concurrency", ctx.settings.download_concurrency))
    return max(MIN_CONCURRENCY, min(MAX_DOWNLOAD_CONCURRENCY, value))


def disk_status(ctx: AppContext) -> dict[str, Any]:
    """Pausa local disk_full, espaço livre e mínimo exigido (GET /api/settings e /api/settings/diagnostics)."""
    if ctx.worker is not None:
        return ctx.worker.disk_status()
    from app.services.disk import disk_ok

    ok, free, required = disk_ok()
    return {"disk_full": False, "ok": ok, "free_bytes": free, "required_bytes": required}


def execution_diagnostics(ctx: AppContext) -> dict[str, Any]:
    """Pools, disco (pausa local disk_full, espaço livre e mínimo exigido) e lotes pausados pelo disjuntor."""
    worker = ctx.worker
    disk_info = disk_status(ctx)
    paused = ctx.db.fetchall(
        "SELECT id, paused_reason, paused_code, paused_until FROM batches WHERE paused_until > ? ORDER BY id",
        (utc_now(),),
    )
    return {
        "download_concurrency": worker.download_concurrency if worker else effective_download_concurrency(ctx),
        "processing_concurrency": worker.concurrency if worker else effective_concurrency(ctx),
        "active_downloads": worker.active_count_for("download") if worker else 0,
        "active_processing": worker.active_count_for("process") if worker else 0,
        "disk": disk_info,
        "paused_batches": [
            {"batch_id": row["id"], "paused_reason": row["paused_reason"], "paused_code": row["paused_code"],
             "paused_until": row["paused_until"]}
            for row in paused
        ],
    }


def instagram_provider(app_settings: Any, settings: Settings) -> str:
    """Estratégia efetiva: setting gravado > env MEDIA_TOOL_INSTAGRAM_PROVIDER > `web`."""
    stored = app_settings.get("instagram_provider")
    if stored in INSTAGRAM_PROVIDERS:
        return stored
    return settings.instagram_provider if settings.instagram_provider in INSTAGRAM_PROVIDERS else "web"


def instagram_doc_id_overrides(app_settings: Any) -> dict[str, str]:
    """Overrides de doc_id gravados (nome -> doc_id). O env MEDIA_TOOL_IG_DOC_<NAME> é aplicado por queries.py."""
    stored = app_settings.get(DOC_IDS_KEY)
    if not isinstance(stored, dict):
        return {}
    return {str(key): value for key, value in stored.items() if isinstance(value, str)}


def _result_for(ok: bool, code: str, declared: object = None) -> str:
    if isinstance(declared, str) and declared in SESSION_TEST_RESULTS:
        return declared
    if ok:
        return "valid"
    return _RESULT_BY_CODE.get(code, "instagram_error")


def _normalize_last_test(record: dict[str, Any]) -> dict[str, Any]:
    """Completa registros antigos (anteriores aos campos result/http_status/duration_ms)."""
    ok = bool(record.get("ok"))
    code = str(record.get("code") or ("ok" if ok else "instagram_error"))
    http_status = record.get("http_status")
    duration = record.get("duration_ms")
    detail = record.get("detail")
    recognized = record.get("session_recognized")
    listing = record.get("listing_api")
    present = record.get("session_present")
    bootstrap = record.get("bootstrap")
    graphql = record.get("graphql")
    return {
        "ok": ok,
        "result": _result_for(ok, code, record.get("result")),
        "code": code,
        "message": str(record.get("message") or ""),
        "username": record.get("username"),
        "http_status": http_status if isinstance(http_status, int) else None,
        "duration_ms": duration if isinstance(duration, int) and duration >= 0 else 0,
        "detail": detail if isinstance(detail, str) and _DETAIL_RE.match(detail) else None,
        "session_recognized": recognized if isinstance(recognized, bool) else None,
        "listing_api": listing if listing in LISTING_API_STATES else None,
        "session_present": present if isinstance(present, bool) else None,
        "bootstrap": bootstrap if bootstrap in WEB_STAGE_STATES else None,
        "graphql": graphql if graphql in WEB_STAGE_STATES else None,
        "tested_at": str(record.get("tested_at") or ""),
    }


def instagram_status(ctx: AppContext) -> dict[str, Any]:
    status = ctx.session_provider.status()
    paused_until, pause_reason = ctx.worker.pause_state() if ctx.worker is not None else (None, None)
    recovery = ctx.worker.recovery.state() if ctx.worker is not None else None
    raw = ctx.app_settings.get_state(LAST_TEST_KEY)
    last_test = None
    if raw:
        try:
            last_test = _normalize_last_test(json.loads(raw))
        except (ValueError, TypeError, AttributeError):
            last_test = None
    return {
        "session_configured": bool(status.configured),
        "source": status.source,
        "updated_at": status.updated_at,
        "extra_keys": [key for key in _KNOWN_EXTRA_KEYS if key in set(getattr(status, "extra_keys", None) or ())],
        "paused_until": paused_until,
        "pause_reason": pause_reason,
        "pause_scope": pause_scope(pause_reason) if paused_until else None,
        "next_attempt_at": recovery.next_attempt_at if paused_until and recovery is not None else None,
        "action_required": bool(paused_until and recovery is not None and recovery.action_required),
        "last_test": last_test,
    }


def get_settings(ctx: AppContext) -> dict[str, Any]:
    stored = ctx.app_settings.all()
    ffmpeg = ctx.processors.ffmpeg_info()
    ffprobe = ctx.processors.ffprobe_info()
    return {
        "ffmpeg_path": stored.get("ffmpeg_path"),
        "ffprobe_path": stored.get("ffprobe_path"),
        "concurrency": effective_concurrency(ctx),
        "download_concurrency": effective_download_concurrency(ctx),
        "processing_concurrency": effective_concurrency(ctx),
        "disk": disk_status(ctx),
        "default_preset_id": stored.get("default_preset_id"),
        "instagram_provider": instagram_provider(ctx.app_settings, ctx.settings),
        "instagram_web_doc_ids": instagram_doc_id_overrides(ctx.app_settings),
        "effective": {
            "ffmpeg_path": ffmpeg.path if ffmpeg.found else None,
            "ffprobe_path": ffprobe.path if ffprobe.found else None,
        },
        "storage": ctx.storage.dirs(),
        "database_path": str(ctx.settings.database_path),
        "instagram": instagram_status(ctx),
    }


_DRIVE_REMOTE = 4  # GetDriveTypeW: unidade de rede


def _is_network_drive(path: Path) -> bool:
    if os.name != "nt" or not path.drive:
        return False
    import ctypes

    return ctypes.windll.kernel32.GetDriveTypeW(f"{path.drive}\\") == _DRIVE_REMOTE


def _validate_tool_path(value: str, field: str, tool: str) -> str:
    """Aceita só o executável local do FFmpeg/ffprobe: caminho absoluto, fora de rede, nome esperado e que
    responde a `-version` como a ferramenta certa."""
    raw = value.strip()
    if raw.startswith(("\\\\", "//")):
        raise AppError(422, "invalid_path", f"{field}: caminhos de rede (UNC) não são permitidos.")
    path = Path(raw)
    if not path.is_absolute():
        raise AppError(422, "invalid_path", f"{field}: informe o caminho absoluto do executável.")
    resolved = path.resolve()
    if str(resolved).startswith(("\\\\", "//")) or _is_network_drive(resolved):
        raise AppError(422, "invalid_path", f"{field}: o executável precisa estar em um disco local.")
    if not resolved.is_file():
        raise AppError(422, "invalid_path", f"{field}: arquivo não encontrado.")
    if resolved.name.lower() not in (tool, f"{tool}.exe"):
        raise AppError(422, "invalid_path", f"{field}: o arquivo precisa se chamar {tool}.exe.")
    processor = MediaProcessor(**{f"{tool}_path": str(resolved)})
    info = processor.ffmpeg_info() if tool == "ffmpeg" else processor.ffprobe_info()
    if not info.found or not (info.version or "").lower().startswith(f"{tool} version"):
        raise AppError(422, "invalid_tool", f"{field}: o arquivo não respondeu como {tool} ({tool} -version).")
    return str(resolved)


def update_settings(ctx: AppContext, changes: dict[str, Any]) -> None:
    """Aplica só as chaves enviadas (tudo validado antes de gravar). `null`/texto vazio volta ao padrão."""
    writes: dict[str, Any] = {}
    for field, tool in (("ffmpeg_path", "ffmpeg"), ("ffprobe_path", "ffprobe")):
        if field in changes:
            value = changes[field]
            writes[field] = None if value is None or not value.strip() else _validate_tool_path(value, field, tool)
    if "processing_concurrency" in changes:
        changes = {**changes, "concurrency": changes["processing_concurrency"]}
    if "concurrency" in changes:
        writes["concurrency"] = None if changes["concurrency"] is None else int(changes["concurrency"])
    if "download_concurrency" in changes:
        value = changes["download_concurrency"]
        writes["download_concurrency"] = None if value is None else int(value)
    if "default_preset_id" in changes:
        value = changes["default_preset_id"]
        if value is not None and ctx.presets.get(value) is None:
            raise AppError(404, "preset_not_found", "Preset não encontrado.")
        writes["default_preset_id"] = None if value is None else int(value)
    if "instagram_provider" in changes:
        # O formato já foi validado pelo schema; null volta ao padrão (env ou `web`).
        writes["instagram_provider"] = changes["instagram_provider"]
    if DOC_IDS_KEY in changes:
        writes[DOC_IDS_KEY] = changes[DOC_IDS_KEY] or None

    with ctx.db.transaction():
        for key, value in writes.items():
            if value is None:
                ctx.app_settings.delete(key)
            else:
                ctx.app_settings.set(key, value)
    if "ffmpeg_path" in writes or "ffprobe_path" in writes:
        ctx.processors.invalidate()
    if "concurrency" in writes and ctx.worker is not None:
        ctx.worker.set_concurrency(effective_concurrency(ctx))
    if "download_concurrency" in writes and ctx.worker is not None:
        ctx.worker.set_download_concurrency(effective_download_concurrency(ctx))
    logger.info("Configurações atualizadas: %s", ", ".join(sorted(writes)) or "nenhuma")


def save_session(ctx: AppContext, sessionid: SecretStr, extra: dict[str, SecretStr | None]) -> None:
    """Grava a sessão pelo provider. Os valores só são revelados (`get_secret_value`) na chamada ao provider."""
    cleaned = {key: value for key, value in extra.items() if key in SESSION_EXTRA_KEYS and value is not None}
    try:
        ctx.session_provider.save(
            sessionid.get_secret_value(),
            {key: value.get_secret_value() for key, value in cleaned.items()} or None,
        )
    except SessionValidationError as exc:
        raise AppError(422, "invalid_session", safe_message(getattr(exc, "message", None) or "Sessão inválida.")) from None
    except SessionStorageError:
        logger.error("Falha ao gravar a sessão do Instagram no armazenamento protegido")
        raise AppError(500, "session_storage_error", "Não foi possível gravar a sessão no armazenamento protegido.") from None
    ctx.app_settings.set_state(LAST_TEST_KEY, None)
    _release_session_invalid_pause(ctx)
    logger.info("Sessão do Instagram atualizada")


def update_session_cookies(ctx: AppContext, changes: dict[str, SecretStr | None]) -> None:
    """Altera só os cookies auxiliares da sessão salva em arquivo. Vazio/None remove a chave."""
    provider = ctx.session_provider
    if provider.status().source != "file":
        raise AppError(409, "session_not_editable", "Configure primeiro o sessionid.")
    update_extra = getattr(provider, "update_extra", None)
    if update_extra is None:
        raise AppError(501, "not_supported", "O armazenamento da sessão não permite editar cookies.")
    extra = {
        key: (None if value is None or not value.get_secret_value() else value.get_secret_value())
        for key, value in changes.items()
        if key in SESSION_EXTRA_KEYS
    }
    try:
        update_extra(extra)
    except SessionValidationError as exc:
        raise AppError(422, "invalid_session", safe_message(getattr(exc, "message", None) or "Cookie inválido.")) from None
    except SessionStorageError:
        logger.error("Falha ao gravar os cookies da sessão no armazenamento protegido")
        raise AppError(500, "session_storage_error", "Não foi possível gravar a sessão no armazenamento protegido.") from None
    # O resultado do teste anterior não vale para a sessão alterada.
    ctx.app_settings.set_state(LAST_TEST_KEY, None)
    _release_session_invalid_pause(ctx)
    logger.info("Cookies da sessão do Instagram atualizados: %s", ", ".join(sorted(extra)))


def _release_session_invalid_pause(ctx: AppContext) -> None:
    """Sessão ou cookies novos (ou teste de sessão válido) encerram as pausas de escopo `session` (auth_check,
    session_invalid, checkpoint_required) e zeram o contador de recusas de autenticação (rate_limited e
    action_blocked continuam)."""
    if ctx.worker is None:
        return
    ctx.worker.note_auth_success()
    reason = ctx.worker.pause_state()[1]
    if pause_scope(reason) == "session" and ctx.worker.clear_pause(only_reason=reason):
        ctx.wake_import_runner()


def clear_instagram_pause(ctx: AppContext) -> None:
    if ctx.worker is not None:
        ctx.worker.clear_pause()
    ctx.wake_import_runner()


def clear_session(ctx: AppContext) -> None:
    try:
        ctx.session_provider.clear()
    except SessionStorageError:
        raise AppError(500, "session_storage_error", "Não foi possível remover a sessão salva.") from None
    ctx.app_settings.set_state(LAST_TEST_KEY, None)
    logger.info("Sessão do Instagram removida")


def test_session(ctx: AppContext) -> None:
    """Testa a sessão com prazo duro (`session_test_timeout`). Um teste por vez: se já houver um em andamento
    (inclusive um que estourou o prazo e ainda termina em segundo plano), responde 409 `session_test_in_progress`.

    Ação explícita do dono: continua disponível durante a pausa (seções 7 e 8) com passagem própria no portão,
    presa à thread desta chamada e à época da pausa vigente agora; uma pausa publicada no meio recusa as próximas
    requisições do teste (409 `instagram_paused`). Nenhuma outra operação usa esta passagem.
    """
    timeout = ctx.settings.session_test_timeout
    started = time.monotonic()
    gate = getattr(ctx, "instagram_gate", None)
    epoch_of = getattr(gate, "pause_epoch", None)
    epoch = epoch_of() if epoch_of is not None else None

    def call():
        logger.info("Teste de sessão do Instagram iniciado (prazo %ss)", int(timeout))
        grant = nullcontext() if epoch is None else gate.verification_pass(epoch)
        with grant:
            return ctx.downloader_factory().test_session()

    try:
        result = run_with_deadline(call, timeout, name="instagram-session-test", lock=ctx.session_test_lock)
        ok = bool(result.ok)
        code = str(result.code or ("ok" if ok else "instagram_error"))
        record = {
            "ok": ok,
            "result": _result_for(ok, code, getattr(result, "result", None)),
            "code": code,
            "message": safe_message(result.message),
            "username": result.username if ok else None,
            "http_status": getattr(result, "http_status", None),
            "duration_ms": getattr(result, "duration_ms", None),
            "detail": getattr(result, "detail", None),
            "session_recognized": getattr(result, "session_recognized", None),
            "listing_api": getattr(result, "listing_api", None),
            "session_present": getattr(result, "session_present", None),
            "bootstrap": getattr(result, "bootstrap", None),
            "graphql": getattr(result, "graphql", None),
        }
    except CallInProgress:
        raise AppError(409, "session_test_in_progress", "Já existe um teste de sessão em andamento. Aguarde.") from None
    except InstagramPaused:
        raise_paused(ctx)
    except DeadlineExceeded:
        record = {
            "ok": False, "result": "timeout", "code": "timeout", "username": None, "http_status": None,
            "duration_ms": None, "session_recognized": None, "listing_api": "unknown",
            "message": f"O Instagram não respondeu em {int(timeout)} s. Tente de novo em alguns minutos.",
        }
    except InstagramError as exc:
        record = {
            "ok": False, "result": _result_for(False, exc.code, getattr(exc, "result", None)), "code": exc.code,
            "message": safe_message(exc.message), "username": None,
            "http_status": getattr(exc, "http_status", None), "duration_ms": None,
            "detail": getattr(exc, "detail", None), "session_recognized": None, "listing_api": "unknown",
        }
    except Exception:  # noqa: BLE001 - o teste nunca derruba a rota
        logger.exception("Erro inesperado no teste de sessão do Instagram")
        record = {
            "ok": False, "result": "instagram_error", "code": "internal_error", "username": None,
            "http_status": None, "duration_ms": None, "session_recognized": None, "listing_api": "unknown",
            "message": "Erro inesperado ao testar a sessão. Consulte o log do aplicativo.",
        }
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not isinstance(record["duration_ms"], int):
        record["duration_ms"] = elapsed_ms
    if ctx.worker is not None:
        ctx.worker.pause_for(_pause_reason_for(record))
    if record["ok"] and record["result"] == "valid":
        # Resposta autenticada com sucesso: zera o contador e libera session_invalid/auth_check (seção 7).
        _release_session_invalid_pause(ctx)
    record["tested_at"] = utc_now()
    record = _normalize_last_test(record)
    ctx.app_settings.set_state(LAST_TEST_KEY, json.dumps(record, ensure_ascii=False))
    logger.info(
        "Teste de sessão do Instagram concluído: result=%s code=%s detail=%s http=%s em %sms",
        record["result"], record["code"], record["detail"], record["http_status"], elapsed_ms,
    )
