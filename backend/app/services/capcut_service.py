"""Legendas CapCut: configurações, modelos de legenda, fila (criação, cancelamento, nova tentativa) e apresentação.

O trabalho fica em `app.jobs.capcut_runner`. O núcleo (`app.integrations.capcut`: transcrição, rascunho, exportação) é
importado só dentro das funções (`load_core`): sem as dependências instaladas o resto do Media Tool funciona e o status
responde "indisponível"."""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.errors import AppError, not_found
from app.core.security import UnsafePathError, ensure_within
from app.db.repositories.capcut import CapCutRepository
from app.services.settings_service import _is_network_drive
from app.services.system_open import SystemOpenError, open_path

if TYPE_CHECKING:
    from types import ModuleType

    from app.services.context import AppContext

logger = logging.getLogger("media_tool.capcut")

SETTINGS_KEY = "capcut"
PAUSE_STATE_KEY = "capcut_queue_paused"
PAUSE_REASON_KEY = "capcut_queue_pause_reason"
BUILTIN_STYLE_ID = 0
BUILTIN_STYLE_NAME = "Padrão (capcut-agent-skill)"
DEFAULTS: dict[str, Any] = {
    "output_dir": None,           # None = <storage_dir>/capcut
    "resolution": "original",
    "bitrate": "recommended",
    "fps": "60",  # pedido do dono: exportar sempre a 60 fps (configurável)
    "highlight": True,
    "highlight_color": None,      # None = cor do modelo ou amarelo
    "max_words": 3,
    "max_chars": 24,
    "whisper_model": "large-v3-turbo",
    "keep_original_audio": True,
    "keep_draft": False,
    "auto_after_download": False,
    "max_attempts": 3,
    "default_style_id": None,     # None = estilo embutido
    # 'isolated' = área de trabalho separada (sem mouse/teclado/tela); 'interactive' = usa a tela e precisa do PC livre.
    # Lido na hora em que o job/prévia começa (não entra no instantâneo do job) e gravado em `options.export_mode`.
    "export_mode": "isolated",
}
EXPORT_MODES = ("isolated", "interactive")
# Campos copiados para o job na criação (instantâneo: mudar as configurações depois não altera o job).
OPTION_KEYS = ("output_dir", "resolution", "bitrate", "fps", "highlight", "highlight_color", "max_words", "max_chars",
               "whisper_model", "keep_original_audio", "keep_draft")
_NULLABLE = frozenset({"output_dir", "highlight_color", "default_style_id"})
_UNAVAILABLE_STATUS: dict[str, Any] = {
    "installed": False, "version": None, "drafts_dir": None, "running_pids": [],
    "deps": {"faster_whisper": False, "uiautomation": False, "pywin32": False},
    "skill": {"installed": False, "path": "", "commit": None},
    "models": {"small": False, "medium": False, "large-v3-turbo": False},
    "isolated": {"available": False, "reason": None},
}
_OPEN_ERROR_STATUS = {"not_found": 404, "not_allowed": 403, "invalid_target": 422, "unsupported": 501}
MSG_UNAVAILABLE = ("Os componentes das Legendas CapCut não estão instalados neste computador"
                   " (veja docs/CAPCUT.md).")
# Serializa "conferir job aberto da mídia -> criar" entre requisições e o enfileiramento automático.
_create_lock = threading.Lock()


def repo(ctx: AppContext) -> CapCutRepository:
    return CapCutRepository(ctx.db)


# ---- núcleo ---------------------------------------------------------------------------------------------------------
def load_core() -> ModuleType:
    """O pacote do núcleo. Levanta ImportError (ou outra exceção) se ele ou as dependências não estão instalados."""
    from app.integrations import capcut

    return capcut


def _core_or_reason() -> tuple[ModuleType | None, str | None]:
    try:
        return load_core(), None
    except Exception as exc:  # noqa: BLE001 - dependência faltando ou núcleo quebrado: o painel mostra o motivo
        logger.info("Núcleo das Legendas CapCut indisponível: %s", type(exc).__name__)
        return None, MSG_UNAVAILABLE


def _require_core() -> ModuleType:
    core, reason = _core_or_reason()
    if core is None:
        raise AppError(409, "capcut_unavailable", reason or MSG_UNAVAILABLE)
    return core


def _core_error(exc: Exception, what: str) -> AppError:
    """Erro do núcleo (`CapCutError`: `code` + `message` em pt-BR) vira 409 com o mesmo código."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        message = getattr(exc, "message", None) or str(exc) or "Erro no CapCut."
        return AppError(409, code, str(message))
    logger.warning("Legendas CapCut: falha inesperada ao %s", what, exc_info=True)
    return AppError(409, "capcut_error", f"Não foi possível {what}.")


def core_status(ctx: AppContext) -> dict[str, Any]:
    core, reason = _core_or_reason()
    info = {key: (dict(value) if isinstance(value, dict) else value) for key, value in _UNAVAILABLE_STATUS.items()}
    available = False
    if core is not None:
        try:
            raw = core.status(ctx.settings.data_dir)
            for key in info:
                if key in raw:
                    info[key] = raw[key]
            info["deps"] = {str(k): bool(v) for k, v in dict(info["deps"] or {}).items()}
            info["models"] = {str(k): bool(v) for k, v in dict(info["models"] or {}).items()}
            info["skill"] = dict(info["skill"] or {})
            isolated = dict(info["isolated"] or {})
            info["isolated"] = {"available": bool(isolated.get("available")), "reason": isolated.get("reason")}
            info["running_pids"] = [int(pid) for pid in info["running_pids"] or []]
            info["installed"] = bool(info["installed"])
            available = True
        except Exception:  # noqa: BLE001 - status é só leitura; uma falha não derruba a tela
            logger.warning("Legendas CapCut: falha ao ler o status do núcleo", exc_info=True)
            info = {key: (dict(value) if isinstance(value, dict) else value)
                    for key, value in _UNAVAILABLE_STATUS.items()}
            reason = "Não foi possível ler o estado do CapCut."
    counts = repo(ctx).counts()
    queue = {**{key: counts[key] for key in ("pending", "running", "waiting", "failed", "completed", "cancelled")},
             "paused": queue_paused(ctx), "pause_reason": ctx.app_settings.get_state(PAUSE_REASON_KEY),
             "held": repo(ctx).held_count()}
    settings = get_settings(ctx)
    return {**info, "available": available, "reason": None if available else reason, "queue": queue,
            "settings": settings, "export_mode": settings["export_mode"]}


def list_drafts() -> list[dict[str, Any]]:
    core = _require_core()
    try:
        return list(core.list_caption_drafts())
    except Exception as exc:  # noqa: BLE001 - convertido em erro da API
        raise _core_error(exc, "listar os projetos do CapCut") from exc


# ---- configurações --------------------------------------------------------------------------------------------------
def default_output_dir(ctx: AppContext) -> Path:
    return ctx.settings.storage_dir / "capcut"


def _stored(ctx: AppContext) -> dict[str, Any]:
    stored = ctx.app_settings.get(SETTINGS_KEY) or {}
    if not isinstance(stored, dict):
        return dict(DEFAULTS)
    return {**DEFAULTS, **{key: value for key, value in stored.items() if key in DEFAULTS}}


def get_settings(ctx: AppContext) -> dict[str, Any]:
    values = _stored(ctx)
    return {**values, "output_dir": values["output_dir"] or str(default_output_dir(ctx)),
            "output_dir_is_default": not values["output_dir"], "export_mode": export_mode(ctx)}


def export_mode(ctx: AppContext) -> str:
    """Modo de exportação configurado (valor desconhecido gravado no banco: o padrão, isolado)."""
    mode = _stored(ctx)["export_mode"]
    return mode if mode in EXPORT_MODES else DEFAULTS["export_mode"]


def validate_output_dir(raw: str) -> Path:
    """Pasta de saída: caminho absoluto em disco local (sem rede/UNC, sem `..`, fora da raiz da unidade). É criada se
    não existir."""
    text = (raw or "").strip()
    if not text or "\x00" in text:
        raise AppError(422, "invalid_output_dir", "Informe a pasta de saída.")
    if text.startswith(("\\\\", "//")):
        raise AppError(422, "invalid_output_dir", "Pastas de rede (UNC) não são permitidas para a saída.")
    path = Path(text)
    if not path.is_absolute() or ".." in path.parts:
        raise AppError(422, "invalid_output_dir", "A pasta de saída precisa ser um caminho absoluto.")
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        raise AppError(422, "invalid_output_dir", "Pasta de saída inválida.") from None
    if str(resolved).startswith(("\\\\", "//")) or _is_network_drive(resolved):
        raise AppError(422, "invalid_output_dir", "A pasta de saída precisa estar em um disco local.")
    if resolved.parent == resolved:
        raise AppError(422, "invalid_output_dir", "Escolha uma subpasta, não a raiz da unidade.")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise AppError(422, "invalid_output_dir", "Não foi possível criar a pasta de saída.") from None
    if not resolved.is_dir():
        raise AppError(422, "invalid_output_dir", "O caminho de saída não é uma pasta.")
    return resolved


def update_settings(ctx: AppContext, changes: dict[str, Any]) -> dict[str, Any]:
    """Aplica só as chaves enviadas (tudo validado antes de gravar). `null` volta ao padrão."""
    values = _stored(ctx)
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        if key == "output_dir":
            values[key] = None if value is None or not value.strip() else str(validate_output_dir(value))
        elif key == "default_style_id":
            values[key] = _existing_style_id(ctx, value)
        elif value is None and key not in _NULLABLE:
            values[key] = DEFAULTS[key]
        else:
            values[key] = value
    ctx.app_settings.set(SETTINGS_KEY, values)
    repo(ctx).mark_default(values["default_style_id"])
    return get_settings(ctx)


def _existing_style_id(ctx: AppContext, style_id: int | None) -> int | None:
    if not style_id:
        return None
    if repo(ctx).get_style(style_id) is None:
        raise not_found("Modelo de legenda", "style_not_found")
    return style_id


def queue_paused(ctx: AppContext) -> bool:
    return ctx.app_settings.get_state(PAUSE_STATE_KEY) == "1"


def set_queue_paused(ctx: AppContext, paused: bool, reason: str | None = None) -> dict[str, bool]:
    ctx.app_settings.set_state(PAUSE_STATE_KEY, "1" if paused else None)
    ctx.app_settings.set_state(PAUSE_REASON_KEY, reason if paused else None)
    if not paused:
        _wake(ctx)
    return {"paused": paused}


# ---- modelos de legenda ---------------------------------------------------------------------------------------------
def _builtin_summary() -> dict[str, Any] | None:
    core, _ = _core_or_reason()
    if core is None:
        return None
    try:
        return dict(core.style_summary(core.default_style()))
    except Exception:  # noqa: BLE001 - só o resumo da tela
        logger.warning("Legendas CapCut: falha ao resumir o estilo embutido", exc_info=True)
        return None


def _builtin_design() -> dict[str, Any] | None:
    """Design equivalente ao embutido (ponto de partida do editor); None sem o núcleo."""
    core, _ = _core_or_reason()
    getter = getattr(core, "default_design", None) if core is not None else None
    if getter is None:
        return None
    try:
        return dict(getter())
    except Exception:  # noqa: BLE001 - só o ponto de partida do editor
        logger.warning("Legendas CapCut: falha ao montar o design do estilo embutido", exc_info=True)
        return None


def list_styles(ctx: AppContext) -> list[dict[str, Any]]:
    default_id = _stored(ctx)["default_style_id"]
    builtin = {"id": BUILTIN_STYLE_ID, "name": BUILTIN_STYLE_NAME, "builtin": True, "is_default": default_id is None,
               "source_draft": None, "summary": _builtin_summary(), "created_at": None, "design": _builtin_design()}
    saved = [{"id": style["id"], "name": style["name"], "builtin": False, "is_default": style["id"] == default_id,
              "source_draft": style["source_draft"], "summary": style["summary"], "created_at": style["created_at"],
              "design": style.get("design")}
             for style in repo(ctx).list_styles()]
    return [builtin, *saved]


def create_style(ctx: AppContext, name: str, draft_name: str, segment_index: int) -> dict[str, Any]:
    if name.casefold() == BUILTIN_STYLE_NAME.casefold() or repo(ctx).style_name_taken(name):
        raise AppError(409, "style_name_taken", "Já existe um modelo com este nome.")
    core = _require_core()
    try:
        style = core.extract_style(draft_name, segment_index)
    except Exception as exc:  # noqa: BLE001 - convertido em erro da API
        raise _core_error(exc, "copiar o estilo deste projeto") from exc
    try:
        summary = dict(core.style_summary(style))
    except Exception:  # noqa: BLE001 - o modelo vale sem o resumo
        logger.warning("Legendas CapCut: falha ao resumir o modelo salvo", exc_info=True)
        summary = None
    style_id = repo(ctx).create_style(name=name, source_draft=draft_name, style=style, summary=summary)
    return next(item for item in list_styles(ctx) if item["id"] == style_id)


def _compile_design(ctx: AppContext, design: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(design normalizado, estilo serializado). Fonte/animação não permitida: 422 `invalid_design`."""
    core = _require_core()
    try:
        return core.compile_design(ctx.settings.data_dir, design)
    except Exception as exc:  # noqa: BLE001 - convertido em erro da API
        if getattr(exc, "code", None) == "invalid_design":
            raise AppError(422, "invalid_design", str(getattr(exc, "message", None) or exc)) from exc
        raise _core_error(exc, "montar o estilo do editor") from exc


def _summary_of(core: ModuleType, style: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return dict(core.style_summary(style))
    except Exception:  # noqa: BLE001 - o modelo vale sem o resumo
        logger.warning("Legendas CapCut: falha ao resumir o modelo salvo", exc_info=True)
        return None


def design_warnings(design: dict[str, Any] | None) -> list[str]:
    """Avisos do design (não bloqueiam); vazio sem design ou sem o núcleo."""
    if not design:
        return []
    core, _ = _core_or_reason()
    getter = getattr(core, "design_warnings", None) if core is not None else None
    if getter is None:
        return []
    try:
        return [str(w) for w in getter(design)]
    except Exception:  # noqa: BLE001 - aviso é informativo
        logger.warning("Legendas CapCut: falha ao conferir os avisos do design", exc_info=True)
        return []


def create_design_style(ctx: AppContext, name: str, design: dict[str, Any]) -> dict[str, Any]:
    """Modelo novo criado pelo editor visual (com os avisos do design em `warnings`)."""
    if name.casefold() == BUILTIN_STYLE_NAME.casefold() or repo(ctx).style_name_taken(name):
        raise AppError(409, "style_name_taken", "Já existe um modelo com este nome.")
    normalized, style = _compile_design(ctx, design)
    style_id = repo(ctx).create_style(name=name, source_draft=None, style=style,
                                      summary=_summary_of(_require_core(), style), design=normalized)
    item = next(item for item in list_styles(ctx) if item["id"] == style_id)
    return {**item, "warnings": design_warnings(normalized)}


def update_design_style(ctx: AppContext, style_id: int, name: str | None, design: dict[str, Any]) -> dict[str, Any]:
    """Atualiza um modelo do editor; os copiados de rascunho não mudam (409 `style_not_editable`)."""
    current = repo(ctx).get_style(style_id)
    if current is None:
        raise not_found("Modelo de legenda", "style_not_found")
    if current.get("design") is None:
        raise AppError(409, "style_not_editable", "Só modelos criados no editor podem ser alterados.")
    new_name = name or current["name"]
    if new_name.casefold() == BUILTIN_STYLE_NAME.casefold() or repo(ctx).style_name_taken_by_other(new_name, style_id):
        raise AppError(409, "style_name_taken", "Já existe um modelo com este nome.")
    normalized, style = _compile_design(ctx, design)
    if not repo(ctx).update_design_style(style_id, name=new_name, style=style,
                                         summary=_summary_of(_require_core(), style), design=normalized):
        raise not_found("Modelo de legenda", "style_not_found")
    item = next(item for item in list_styles(ctx) if item["id"] == style_id)
    return {**item, "warnings": design_warnings(normalized)}


# ---- catálogo do editor ---------------------------------------------------------------------------------------------
def catalog_fonts(ctx: AppContext, **query: Any) -> dict[str, Any]:
    core = _require_core()
    try:
        return core.catalog_fonts(ctx.settings.data_dir, **query)
    except Exception as exc:  # noqa: BLE001 - convertido em erro da API
        raise _core_error(exc, "ler as fontes") from exc


def catalog_animations(ctx: AppContext, **query: Any) -> dict[str, Any]:
    core = _require_core()
    try:
        return core.catalog_animations(ctx.settings.data_dir, **query)
    except Exception as exc:  # noqa: BLE001 - convertido em erro da API
        raise _core_error(exc, "ler as animações") from exc


def font_file(key: str) -> Path:
    """Arquivo da fonte (só pastas permitidas); 404 se não houver arquivo local."""
    core = _require_core()
    try:
        path = core.font_file(key)
    except Exception as exc:  # noqa: BLE001 - convertido em erro da API
        raise _core_error(exc, "ler a fonte") from exc
    if path is None or not Path(path).is_file():
        raise AppError(404, "font_not_found", "Fonte sem arquivo local.")
    return Path(path)


# ---- prévias --------------------------------------------------------------------------------------------------------
def previews_dir(ctx: AppContext) -> Path:
    return ctx.settings.data_dir / "capcut" / "previews"


def _runner(ctx: AppContext) -> Any:
    if ctx.capcut_runner is None:
        raise AppError(409, "capcut_unavailable", MSG_UNAVAILABLE)
    return ctx.capcut_runner


def _preview_media(ctx: AppContext, media_id: int | None) -> dict[str, Any]:
    if media_id is None:
        row = ctx.db.fetchone("SELECT id FROM media WHERE media_type = 'video' AND role = 'original'"
                              " ORDER BY id DESC LIMIT 1")
        if row is None:
            raise AppError(409, "preview_no_media", "A Biblioteca não tem nenhum vídeo para a prévia.")
        media_id = int(row["id"])
    media = ctx.media.get(media_id)
    if media is None:
        raise not_found("Mídia", "media_not_found", feminine=True)
    if media["media_type"] != "video" or (media.get("role") or "original") == "cover":
        raise AppError(422, "not_video", "A prévia precisa de um vídeo original.")
    return media


def create_preview(ctx: AppContext, *, design: dict[str, Any] | None, style_id: int | None, media_id: int | None,
                   start_s: float, seconds: float) -> dict[str, Any]:
    """Prévia real no CapCut (mesma fila/executor, com prioridade e fora da lista de jobs)."""
    runner = _runner(ctx)
    settings = get_settings(ctx)
    if design is not None:
        normalized, style = _compile_design(ctx, design)
        highlight, highlight_color = bool(design["highlight"]["enabled"]), design["highlight"]["color"]
        warnings = design_warnings(normalized)
    else:
        stored = repo(ctx).get_style(style_id) if style_id else None
        if style_id and stored is None:
            raise not_found("Modelo de legenda", "style_not_found")
        style = stored["style"] if stored else None
        highlight, highlight_color = bool(settings["highlight"]), settings["highlight_color"]
        warnings = design_warnings(stored.get("design") if stored else None)
    media = _preview_media(ctx, media_id)
    try:
        video = ctx.storage.resolve(media["original_path"])
    except Exception:  # noqa: BLE001 - caminho inválido no banco
        video = None
    if video is None or not video.is_file():
        raise AppError(409, "source_missing", "O arquivo original deste vídeo não está mais na Biblioteca.")
    preview = runner.submit_preview({
        "media_id": media["id"], "video_path": str(video), "style": style, "highlight": highlight,
        "highlight_color": highlight_color, "start_s": float(start_s), "seconds": float(seconds),
        "max_words": int(settings["max_words"]), "max_chars": int(settings["max_chars"]),
        "whisper_model": settings["whisper_model"], "warnings": warnings})
    return {"id": preview["id"], "status": preview["status"], "warnings": warnings}


def preview_out(preview: dict[str, Any]) -> dict[str, Any]:
    done = preview["status"] == "completed" and preview.get("output_path") and Path(preview["output_path"]).is_file()
    return {**{key: preview.get(key) for key in ("id", "status", "stage", "detail", "error_code", "error_message",
                                                  "media_id", "start_s", "seconds", "export_mode")},
            "progress": int(preview.get("progress") or 0), "warnings": list(preview.get("warnings") or []),
            "file_url": f"/api/capcut/previews/{preview['id']}/file" if done else None}


def get_preview(ctx: AppContext, preview_id: str) -> dict[str, Any]:
    preview = _runner(ctx).get_preview(preview_id)
    if preview is None:
        raise not_found("Prévia", "preview_not_found", feminine=True)
    return preview_out(preview)


def cancel_preview(ctx: AppContext, preview_id: str) -> dict[str, Any]:
    if _runner(ctx).cancel_preview(preview_id) is None:
        raise not_found("Prévia", "preview_not_found", feminine=True)
    return get_preview(ctx, preview_id)


def preview_file(ctx: AppContext, preview_id: str) -> Path:
    preview = _runner(ctx).get_preview(preview_id)
    if preview is None:
        raise not_found("Prévia", "preview_not_found", feminine=True)
    if preview["status"] != "completed" or not preview.get("output_path"):
        raise AppError(404, "preview_file_not_found", "A prévia ainda não tem vídeo.")
    try:
        resolved = ensure_within(Path(preview["output_path"]), [previews_dir(ctx)])
    except UnsafePathError:
        raise AppError(403, "not_allowed", "Arquivo fora da pasta de prévias.") from None
    if not resolved.is_file():
        raise AppError(404, "preview_file_not_found", "O vídeo da prévia não existe mais.")
    return resolved


def delete_style(ctx: AppContext, style_id: int) -> None:
    if not repo(ctx).delete_style(style_id):
        raise not_found("Modelo de legenda", "style_not_found")
    if _stored(ctx)["default_style_id"] == style_id:
        update_settings(ctx, {"default_style_id": None})   # nunca deixa o padrão apontando para o nada


def set_default_style(ctx: AppContext, style_id: int) -> list[dict[str, Any]]:
    update_settings(ctx, {"default_style_id": style_id})
    return list_styles(ctx)


# ---- jobs -----------------------------------------------------------------------------------------------------------
def _job_options(ctx: AppContext, style_id: int | None, overrides: dict[str, Any] | None) -> dict[str, Any]:
    settings = get_settings(ctx)
    options = {key: settings[key] for key in OPTION_KEYS}
    for key, value in (overrides or {}).items():
        if key not in OPTION_KEYS:
            continue
        if key == "output_dir":
            options[key] = settings["output_dir"] if value is None or not value.strip() else value
        elif value is not None or key == "highlight_color":
            options[key] = value
    options["output_dir"] = str(validate_output_dir(options["output_dir"]))
    explicit = style_id is not None
    chosen = settings["default_style_id"] if style_id is None else (style_id or None)
    style = repo(ctx).get_style(chosen) if chosen else None
    if chosen and style is None:
        if explicit:
            raise not_found("Modelo de legenda", "style_not_found")
        chosen = None   # padrão salvo que sumiu: estilo embutido
    options.update(style_id=chosen, style_name=style["name"] if style else BUILTIN_STYLE_NAME,
                   style=style["style"] if style else None)
    return options


def create_jobs(ctx: AppContext, media_ids: list[int], *, style_id: int | None = None,
                overrides: dict[str, Any] | None = None, origin: str = "manual",
                batch_id: int | None = None) -> dict[str, Any]:
    """Um job por vídeo original. Pula mídia inexistente, que não é vídeo, capa e mídia com job aberto."""
    wanted = list(dict.fromkeys(media_ids))
    options = _job_options(ctx, style_id, overrides)
    max_attempts = int(_stored(ctx)["max_attempts"])
    created: list[int] = []
    skipped: list[dict[str, Any]] = []
    with _create_lock:
        medias = {media["id"]: media for media in ctx.media.get_many(wanted)}
        busy = repo(ctx).open_media_ids(wanted)
        for media_id in wanted:
            media = medias.get(media_id)
            reason = (
                "media_not_found" if media is None
                else "not_video" if media["media_type"] != "video"
                else "cover" if (media.get("role") or "original") == "cover"
                else "already_queued" if media_id in busy
                else None
            )
            if reason is not None:
                skipped.append({"media_id": media_id, "reason": reason})
                continue
            created.append(repo(ctx).create_job(
                media_id=media_id, source_path=media["original_path"], options=options, max_attempts=max_attempts,
                origin=origin, batch_id=batch_id))
    if created:
        logger.info("Legendas CapCut: %s job(s) na fila (%s)", len(created), origin)
        _wake(ctx)
    return {"created": [get_job(ctx, job_id) for job_id in created], "skipped": skipped}


def enqueue_after_download(ctx: AppContext, media_id: int, batch_id: int | None) -> int | None:
    """Mídia nova baixada: com "legendar automaticamente" ligado, um job `auto`. Retorna o id criado (ou None)."""
    if not _stored(ctx)["auto_after_download"]:
        return None
    result = create_jobs(ctx, [media_id], origin="auto", batch_id=batch_id)
    return result["created"][0]["id"] if result["created"] else None


EDIT_DONE_STATUSES = ("completed", "failed", "cancelled")   # estados finais da tabela `jobs`
MSG_EDITING = "Editando"


def job_out(job: dict[str, Any]) -> dict[str, Any]:
    options = dict(job["options"] or {})
    options.pop("style", None)
    output_path = job["output_path"]
    edit_status = job.get("edit_status")
    edit_raw = job.get("edit_progress_raw")
    edit_progress = (int(round(min(1.0, max(0.0, float(edit_raw))) * 100)) if edit_raw is not None
                     else 100 if edit_status == "completed" else None)
    waiting_edit = (edit_status is not None and edit_status not in EDIT_DONE_STATUSES
                    and job["status"] in ("pending", "waiting"))
    return {
        **{key: job[key] for key in (
            "id", "media_id", "media_profile", "media_type", "status", "stage", "stage_progress", "progress", "detail",
            "attempts", "max_attempts", "next_attempt_at", "wait_reason", "error_code", "error_message", "chunks",
            "draft_name", "output_path", "output_probe", "origin", "batch_id", "created_at", "started_at",
            "finished_at", "updated_at")},
        "media_filename": job["media_filename"] or Path(job["source_path"]).name,
        "thumbnail_url": f"/api/media/{job['media_id']}/thumbnail" if job["media_thumbnail"] else None,
        "cancel_requested": bool(job["cancel_requested"]),
        "style_id": options.get("style_id"),
        "style_name": options.get("style_name"),
        "export_mode": options.get("export_mode"),
        "options": options,
        "has_transcript": bool(job["has_transcript"]),
        "output_name": Path(output_path).name if output_path else None,
        "output_exists": bool(output_path) and Path(output_path).is_file(),
        "warnings": list(job["warnings"] or []),
        "detail": MSG_EDITING if waiting_edit else job["detail"],
        "capcut_batch_id": job.get("capcut_batch_id"),
        "mode": job.get("mode") or "caption",
        "held": bool(job.get("held")),
        "edit_job_id": job.get("edit_job_id"),
        "edit_status": edit_status,
        "edit_progress": edit_progress,
    }


def get_job(ctx: AppContext, job_id: int) -> dict[str, Any]:
    job = repo(ctx).get_job_view(job_id)
    if job is None:
        raise not_found("Job de legenda", "capcut_job_not_found")
    return job_out(job)


def list_jobs(ctx: AppContext, *, status: str | None, media_id: int | None, limit: int,
              offset: int) -> dict[str, Any]:
    items, total = repo(ctx).list_jobs(status=status, media_id=media_id, limit=limit, offset=offset)
    return {"items": [job_out(job) for job in items], "total": total, "counts": repo(ctx).counts()}


def cancel_job(ctx: AppContext, job_id: int) -> dict[str, Any]:
    previous = repo(ctx).request_cancel(job_id)
    if previous is None:
        raise not_found("Job de legenda", "capcut_job_not_found")
    if previous == "running":
        # Idempotente: `running` que nenhum executor está rodando (cancelamento pedido e perdido) termina cancelado.
        runner = ctx.capcut_runner
        if runner is None or not runner.cancel(job_id):
            repo(ctx).finish_orphan_cancel(job_id)
    view = get_job(ctx, job_id)
    if view["capcut_batch_id"] is not None:
        repo(ctx).refresh_batch_finished(view["capcut_batch_id"])
    return view


def retry_job(ctx: AppContext, job_id: int) -> dict[str, Any]:
    get_job(ctx, job_id)
    with ctx.db.transaction():
        job = repo(ctx).get_job(job_id)
        if job is None or not retry_with_edit(ctx, job):
            raise AppError(409, "capcut_job_not_retryable",
                           "Só é possível tentar de novo um job com erro ou cancelado.")
    _wake(ctx)
    ctx.wake_worker()
    return get_job(ctx, job_id)


def _edit_needs_redo(ctx: AppContext, edit: dict[str, Any] | None) -> bool:
    """A edição de um job de lote precisa ser refeita: não existe, falhou/foi cancelada, está aberta com cancelamento
    pedido ou concluiu mas a rendition (linha ou arquivo) sumiu."""
    if edit is None or edit["status"] in ("failed", "cancelled"):
        return True
    if edit["status"] != "completed":
        return bool(edit.get("cancel_requested"))
    rendition = ctx.media.get_rendition(edit["rendition_id"]) if edit.get("rendition_id") else None
    if rendition is None:
        return True
    try:
        return not ctx.storage.resolve(rendition["processed_path"]).is_file()
    except Exception:  # noqa: BLE001 - caminho inválido no banco: mesmo efeito de arquivo ausente
        return True


def retry_with_edit(ctx: AppContext, job: dict[str, Any]) -> bool:
    """Nova tentativa (`failed`/`cancelled` -> `pending`, mesma regra do `retry`). Job de lote com edição cuja edição
    falhou, foi cancelada, está sendo cancelada, não existe mais ou concluiu sem a rendition no disco ganha um novo job `process` com os mesmos params (no mesmo lote de
    edição, reaberto) e o `edit_job_id` aponta para ele. O lote do job deixa de estar cancelado. Chamar numa
    transação. False = o estado não permite."""
    if job["status"] not in ("failed", "cancelled"):
        return False
    if job.get("mode") in ("edit", "edit_caption") and job.get("media_id") is not None:
        edit = ctx.jobs.get(job["edit_job_id"]) if job.get("edit_job_id") else None
        params = (edit or {}).get("params") or (job["options"] or {}).get("edit_params")
        media = ctx.media.get(job["media_id"])
        if _edit_needs_redo(ctx, edit) and params is not None and media is not None:
            from app.services.edit_service import EDIT_PRESET_NAME

            edit_batch_id = (edit or {}).get("batch_id") or ctx.db.scalar(
                "SELECT edit_batch_id FROM capcut_batches WHERE id = ?", (job.get("capcut_batch_id"),))
            new_id = ctx.jobs.create_process(media=media, batch_id=edit_batch_id, preset_id=None,
                                             preset_name=EDIT_PRESET_NAME, params=params)
            if edit_batch_id is not None:
                ctx.batches.reopen(edit_batch_id)
                ctx.batches.sync_found(edit_batch_id)
            repo(ctx).update_job(job["id"], edit_job_id=new_id)
    if not repo(ctx).retry(job["id"]):
        return False
    if job.get("capcut_batch_id") is not None:
        repo(ctx).update_batch(job["capcut_batch_id"], cancel_requested=0)
        repo(ctx).refresh_batch_finished(job["capcut_batch_id"])
    return True


def work_dir(ctx: AppContext, job_id: int) -> Path:
    """Pasta de trabalho do job no núcleo (`<data_dir>/capcut/work/<id>`)."""
    return ctx.settings.data_dir / "capcut" / "work" / str(int(job_id))


def delete_job(ctx: AppContext, job_id: int) -> None:
    get_job(ctx, job_id)
    if not repo(ctx).delete_job(job_id):
        raise AppError(409, "capcut_job_active", "O job ainda está na fila ou em andamento. Cancele antes.")
    folder = work_dir(ctx, job_id)
    try:
        load_core().discard_job_work(folder)
    except Exception:  # noqa: BLE001 - sem núcleo: só a pasta de trabalho
        shutil.rmtree(folder, ignore_errors=True)


def output_file(ctx: AppContext, job_id: int) -> tuple[Path, Path]:
    """(arquivo de saída, pasta de saída registrada no job). Só arquivo dentro da pasta do job e que exista."""
    job = repo(ctx).get_job(job_id)
    if job is None:
        raise not_found("Job de legenda", "capcut_job_not_found")
    output_dir = (job["options"] or {}).get("output_dir")
    if not job["output_path"] or not output_dir:
        raise AppError(404, "capcut_output_not_found", "O vídeo legendado ainda não existe.")
    try:
        resolved = ensure_within(Path(job["output_path"]), [Path(output_dir)])
    except UnsafePathError:
        raise AppError(403, "not_allowed", "Arquivo fora da pasta de saída.") from None
    if not resolved.is_file():
        raise AppError(404, "capcut_output_not_found", "O vídeo legendado não está mais na pasta de saída.")
    return resolved, Path(output_dir)


def reveal_output(ctx: AppContext, job_id: int) -> None:
    path, output_dir = output_file(ctx, job_id)
    try:
        open_path(path, allowed_roots=[output_dir], target="folder")
    except SystemOpenError as exc:
        raise AppError(_OPEN_ERROR_STATUS.get(exc.code, 500), exc.code, exc.message) from exc


def _wake(ctx: AppContext) -> None:
    if ctx.capcut_runner is not None:
        ctx.capcut_runner.wake()
