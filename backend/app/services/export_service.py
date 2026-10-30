"""Exportação para pasta: escolha do destino (diálogo nativo + token), criação, cancelamento e "Abrir pasta".

Nenhuma função daqui recebe caminho vindo do cliente: o destino sai do registro de tokens (pasta escolhida no diálogo
nativo), da pasta padrão montada no servidor ou do registro da exportação no banco.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.errors import AppError, not_found
from app.db.connection import utc_now
from app.db.repositories.exports import export_content
from app.jobs.export_runner import CANCELLED_MESSAGE, cleanup_default_folder
from app.services import export_destination, export_zip
from app.services.context import AppContext
from app.services.export_destination import DestinationError, validate_destination
from app.services.export_names import EDIT_PRESET_NAME
from app.services.system_open import SystemOpenError, open_folder

logger = logging.getLogger("media_tool.exports")

LAST_FOLDER_STATE_KEY = "export_last_folder"
# Teto de exportações esperando na fila (uma roda por vez).
MAX_PENDING_EXPORTS = 20
_OPEN_ERROR_STATUS = {"not_found": 404, "not_allowed": 403, "invalid_target": 422, "unsupported": 501}
_COUNTS = ("total", "copied", "skipped_existing", "renamed", "missing", "failed")
_EMPTY_MESSAGE = "Não há mídias para exportar nesta seleção."
_EMPTY_EDITED_MESSAGE = {
    "batch": "Este lote não tem versões editadas ou processadas.",
    "group": "Esta importação não tem versões editadas ou processadas.",
    "library": "Nenhuma das mídias selecionadas tem versões editadas ou processadas.",
}


def _export_not_found() -> AppError:
    return AppError(404, "export_not_found", "Exportação não encontrada.")


def _app_error(exc: DestinationError) -> AppError:
    return AppError(exc.status, exc.code, exc.message)


def export_out(export: dict[str, Any]) -> dict[str, Any]:
    data = {key: export[key] for key in ("id", "kind", "batch_id", "group_id", "dest_path", "status", *_COUNTS,
                                         "error_code", "error_message", "created_at", "started_at", "finished_at")}
    # Arquivos já tratados (progresso `processed / total`): copiados (inclui renomeados) + já existentes + ausentes
    # + com falha.
    data["processed"] = export["copied"] + export["skipped_existing"] + export["missing"] + export["failed"]
    mode = export.get("mode") or "folder"
    ready = mode == "zip" and export["status"] == "completed"
    data["mode"] = mode
    data["content"] = export_content(export)
    if mode == "zip":
        data["dest_path"] = export.get("zip_name") or ""   # o caminho interno do temporário não sai pela API
    data["zip_name"] = export.get("zip_name") if mode == "zip" else None
    data["zip_bytes"] = export.get("zip_bytes") if ready else None
    data["zip_ready"] = ready
    data["zip_expires_at"] = export.get("zip_expires_at") if mode == "zip" else None
    data["download_url"] = (
        f"/api/exports/{export['id']}/download?token={export['zip_token']}" if ready and export.get("zip_token")
        else None
    )
    return data


def _documents_dir(ctx: AppContext) -> Path:
    return (ctx.documents_dir or export_destination.user_documents_dir)()


def _initial_dir(ctx: AppContext) -> Path | None:
    """Diretório inicial do diálogo: a última pasta usada ou a pasta padrão de exportações (criada sob demanda)."""
    last = ctx.app_settings.get_state(LAST_FOLDER_STATE_KEY)
    if last and Path(last).is_dir():
        return Path(last)
    default = export_destination.default_exports_dir(_documents_dir(ctx))
    try:
        default.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return default


def pick_folder(ctx: AppContext) -> dict[str, Any]:
    """Abre o diálogo nativo (um por vez) e guarda a pasta escolhida no servidor, atrás de um token de uso único."""
    if not ctx.picker_lock.acquire(blocking=False):
        raise AppError(
            409, "picker_busy",
            "Já existe uma janela de escolha de pasta aberta. Conclua ou feche essa janela do Windows.",
        )
    try:
        picker = ctx.folder_picker or export_destination.pick_folder_native
        chosen = picker(_initial_dir(ctx))
        if chosen is None:
            return {"cancelled": True, "token": None, "path_display": None}
        resolved = validate_destination(chosen, ctx.settings)
    except DestinationError as exc:
        raise _app_error(exc) from None
    finally:
        ctx.picker_lock.release()
    return {"cancelled": False, "token": ctx.export_tokens.issue(resolved), "path_display": str(resolved)}


def create_export(ctx: AppContext, source: dict[str, Any], destination: dict[str, Any],
                  content: str = "originals", on_created: Callable[[int], None] | None = None) -> dict[str, Any]:
    # `on_created(export_id)`: chamado dentro da MESMA transação que grava a exportação (exportação automática:
    # registra o id no lote/grupo/pedido atomicamente; se levantar, nada é gravado).
    kind = source["kind"]
    fields: dict[str, Any] = {"kind": kind}
    edit_batch = False
    if kind == "batch":
        batch = ctx.batches.get(source["id"])
        if batch is None:
            raise not_found("Lote", "batch_not_found")
        fields["batch_id"] = source["id"]
        # Lote de Edição da Biblioteca (`edicao-...zip`); importação com "Editar após baixar" continua `importacao-`.
        edit_batch = (batch["source"] == "library" and batch["preset_id"] is None
                      and batch["preset_name"] == EDIT_PRESET_NAME)
    elif kind == "group":
        if ctx.batches.get_group(source["id"]) is None:
            raise not_found("Grupo de importação", "group_not_found")
        fields["group_id"] = source["id"]
    else:
        selection = source["selection"]
        if selection.get("media_ids") is not None:
            fields["item_ids"] = selection["media_ids"]
            fields["selection"] = {"mode": "ids", "count": len(set(selection["media_ids"]))}
        else:
            chosen = selection["filter"]
            excluded = selection.get("exclude_ids") or []
            fields["item_ids"] = excluded   # na seleção por filtro, export_items guarda os EXCLUÍDOS
            fields["selection"] = {
                "mode": "filter",
                "view": chosen.get("view") or "all",
                "media_type": chosen.get("media_type"),
                "role": chosen.get("role"),
                "profile": (chosen.get("profile") or "").strip().lstrip("@") or None,
                "q": (chosen.get("q") or "").strip() or None,
                "max_id": int(ctx.db.scalar("SELECT COALESCE(MAX(id), 0) FROM media")),
                "excluded": len(set(excluded)),
            }
    if content != "originals":
        # Fica no JSON `selection` (sem migração); `originals` não grava nada: o registro continua como antes.
        fields["selection"] = {**(fields.get("selection") or {}), "content": content}
    empty_message = _EMPTY_EDITED_MESSAGE[kind] if content == "edited" else _EMPTY_MESSAGE

    use_default = bool(destination.get("default"))
    use_zip = bool(destination.get("zip"))
    mode = "zip" if use_zip else "folder"
    with ctx.export_create_lock:
        running = ctx.exports.open_for_source(kind=kind, batch_id=fields.get("batch_id"),
                                              group_id=fields.get("group_id"), mode=mode)
        if running is not None:
            raise AppError(
                409, "export_already_running",
                "Já existe uma exportação em andamento para esta origem. Aguarde ela terminar ou cancele-a.",
                extra={"export_id": running},
            )
        if ctx.exports.pending_count() >= MAX_PENDING_EXPORTS:
            raise AppError(
                409, "export_queue_full",
                f"Já há {MAX_PENDING_EXPORTS} exportações na fila. Aguarde alguma terminar antes de pedir outra.",
            )
        if use_zip:
            export = _create_zip(ctx, kind, fields, content, empty_message, edit_batch, on_created)
            logger.info("ZIP %s criado (%s, %s, %s arquivos)", export["id"], kind, content, export["total"])
            if ctx.export_runner is not None:
                ctx.export_runner.wake()
            return export_out(export)
        created: Path | None = None
        try:
            if use_default:
                # Importação: `Importacao-AAAA-MM-DD-HHMM`; seleção da Biblioteca: `Biblioteca-AAAA-MM-DD-HHMM`.
                prefix = (export_destination.LIBRARY_FOLDER_PREFIX if kind == "library"
                          else export_destination.DEFAULT_FOLDER_PREFIX)
                created = export_destination.create_default_folder(_documents_dir(ctx), prefix=prefix)
                dest = validate_destination(created, ctx.settings)
            else:
                # Só o token chega do cliente; o caminho sai do registro do servidor e é revalidado. O token só é
                # gasto depois que a exportação foi criada (pedido vazio ou recusado não obriga a escolher de novo).
                dest = validate_destination(ctx.export_tokens.peek(destination["token"]), ctx.settings)
        except DestinationError as exc:
            if created is not None:
                export_destination.remove_if_empty(created)   # pasta padrão recém-criada e recusada: não fica
            raise _app_error(exc) from None

        export = ctx.exports.create(dest_path=str(dest), dest_created=use_default, on_created=on_created, **fields)
        if export is None:
            if created is not None:
                export_destination.remove_if_empty(created)   # pasta padrão recém-criada e vazia: não fica
            raise AppError(409, "export_empty", empty_message)
        if not use_default:
            ctx.export_tokens.consume(destination["token"])
            ctx.app_settings.set_state(LAST_FOLDER_STATE_KEY, str(dest))
    logger.info("Exportação %s criada (%s, %s, %s arquivos)", export["id"], kind, content, export["total"])
    if ctx.export_runner is not None:
        ctx.export_runner.wake()
    return export_out(export)


def _create_zip(ctx: AppContext, kind: str, fields: dict[str, Any], content: str = "originals",
                empty_message: str = _EMPTY_MESSAGE, edit_batch: bool = False,
                on_created: Callable[[int], None] | None = None) -> dict[str, Any]:
    """Cria a exportação em modo ZIP depois de conferir o espaço livre para o temporário: soma agregada de
    `file_size` + margem, contra o espaço livre menos a reserva dos downloads e menos o que os ZIPs já na fila ainda
    vão escrever (reserva lógica)."""
    if os.path.lexists(export_zip.zip_dir(ctx.settings)) and export_zip.safe_zip_dir(ctx.settings) is None:
        raise AppError(500, "internal_error", "A pasta de ZIPs temporários não pôde ser usada.")
    folder = export_zip.zip_dir(ctx.settings)
    export = ctx.exports.create(dest_path=str(folder), mode="zip",
                                zip_fields=export_zip.new_zip_fields(kind, content=content, edit_batch=edit_batch),
                                on_created=on_created, **fields)
    if export is None:
        raise AppError(409, "export_empty", empty_message)
    problem = export_zip.space_problem(ctx, export)
    if problem is not None:
        ctx.exports.delete(export["id"])
        raise AppError(507, "export_no_space", problem)
    return export


def _range_reaches_end(range_header: str | None, size: int) -> bool:
    """A resposta vai entregar o arquivo até o ÚLTIMO byte? Sem `Range`: sim. Com `Range`: só se for um intervalo
    único que termina no fim (`bytes=N-`, `bytes=-N` ou `bytes=N-M` com M no último byte)."""
    if not range_header:
        return True
    unit, _, spec = range_header.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return False
    start, dash, end = spec.strip().partition("-")
    if not dash:
        return False
    if not end.strip():
        return True
    if not start.strip():
        return end.strip().isdigit() and int(end) > 0   # sufixo (`bytes=-N`): os últimos N bytes, até o fim
    try:
        return int(end) >= size - 1
    except ValueError:
        return False


def zip_download(ctx: AppContext, export_id: int, token: str | None,
                 range_header: str | None = None) -> tuple[Path, str, bool]:
    """Arquivo, nome de download e se esta resposta entrega o arquivo até o último byte (só então, ao terminar a
    transmissão, o ZIP conta como baixado). Token ausente/errado: 404 (como se não existisse)."""
    export = ctx.exports.get(export_id)
    expected = (export or {}).get("zip_token") or ""
    if export is None or export.get("mode") != "zip" or not token or not secrets.compare_digest(
            token.encode("utf-8"), expected.encode("utf-8")):
        raise _export_not_found()
    expired = AppError(410, "export_zip_expired", "Este ZIP já foi removido. Prepare o ZIP de novo.")
    if export["status"] == "expired":
        raise expired
    if export["status"] != "completed":
        raise AppError(409, "export_zip_not_ready", "O ZIP ainda não está pronto.")
    path = export_zip.servable_zip(ctx.settings, export)
    if path is None:
        if export_zip.remove_zip_files(ctx.settings, export):
            ctx.exports.set_expired(export_id)
        raise expired
    return path, export.get("zip_name") or "media-tool.zip", _range_reaches_end(range_header, path.stat().st_size)


def zip_downloaded(ctx: AppContext, export_id: int) -> None:
    """Chamado ao FIM de uma transmissão que entregou o arquivo até o último byte: a partir de agora o ZIP fica
    disponível por mais 1 h no máximo."""
    ctx.exports.mark_downloaded(export_id, utc_now(), utc_now(export_zip.ZIP_AFTER_DOWNLOAD_TTL_SECONDS))


def get_export(ctx: AppContext, export_id: int) -> dict[str, Any]:
    export = ctx.exports.get(export_id)
    if export is None:
        raise _export_not_found()
    return export


def cancel_export(ctx: AppContext, export_id: int) -> dict[str, Any]:
    previous = ctx.exports.request_cancel(export_id, CANCELLED_MESSAGE)
    if previous is None:
        raise _export_not_found()
    if previous not in ("pending", "running"):
        raise AppError(409, "export_finished", "Esta exportação já terminou; não há nada para cancelar.")
    if previous == "running" and ctx.export_runner is not None:
        ctx.export_runner.cancel(export_id)
    export = get_export(ctx, export_id)
    if previous == "pending":
        cleanup_default_folder(export)   # cancelada na fila: a pasta padrão criada para ela (vazia) não fica
    return export_out(export)


def _open(path: Path) -> None:
    try:
        open_folder(path, allowed_roots=[path])
    except SystemOpenError as exc:
        raise AppError(_OPEN_ERROR_STATUS.get(exc.code, 500), exc.code, exc.message) from exc


def open_export_folder(ctx: AppContext, export_id: int) -> None:
    """Abre no Explorer a pasta da exportação. O caminho vem do REGISTRO no banco e é revalidado."""
    export = get_export(ctx, export_id)
    if (export.get("mode") or "folder") != "folder":
        raise AppError(409, "export_not_folder", "Esta exportação é um ZIP: não há pasta para abrir.")
    try:
        dest = validate_destination(export["dest_path"], ctx.settings, check_writable=False)
    except DestinationError as exc:
        if exc.code == "export_dest_missing":
            raise AppError(
                409, "export_folder_missing",
                "A pasta desta exportação não existe mais (foi movida, renomeada ou apagada). Exporte de novo.",
            ) from None
        raise _app_error(exc) from None
    _open(dest)


def open_library_folder(ctx: AppContext) -> None:
    """Abre no Explorer a pasta física dos originais (caminho do servidor, respeitando a configuração)."""
    _open(ctx.storage.originals)
