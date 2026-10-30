"""Exportação automática ao concluir (docs/CONTRACTS.md, seção 8.6; docs/BATCH_ROBUSTNESS.md, "Exportação automática").

Um lote avulso ou um grupo criado com `auto_export` guarda o conteúdo pedido e `auto_export_state = 'waiting'`
(migration 0023). Quando ele termina (lote `completed`; grupo sem filho aberto e com algum filho concluído) e nenhuma
mídia dele tem processamento pendente, o backend cria UMA exportação ZIP pelo serviço existente
(`export_service.create_export`, fonte = o lote ou o grupo). A transição 'waiting' -> 'created'/'failed' acontece uma vez
só, sob a trava deste módulo: o disparo é idempotente (no máximo uma exportação por lote/grupo). Erros passageiros
(outra exportação da mesma origem em andamento, fila de exportações cheia) mantêm o disparo esperando; os demais
(nada para exportar, sem espaço) ficam registrados como falha, visível no painel do lote/grupo e na extensão.

Pedido com várias partes (perfis E links no mesmo start; migration 0024): UM ZIP por pedido. O disparo fica no pedido
(`import_requests`), espera TODAS as partes terminarem e exporta a união das mídias delas pela fonte `library`
(`media_ids`). As partes apresentam a exportação do pedido (`with_requests`). A exportação e o registro do id dela
nascem na MESMA transação (`on_created`): uma queda entre os dois não deixa um segundo ZIP nascer.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.core.errors import AppError
from app.services.context import AppContext

logger = logging.getLogger("media_tool.auto_export")

_LOCK = threading.Lock()
# Erros que só adiam o disparo (a próxima verificação tenta de novo).
_TRANSIENT_CODES = frozenset({"export_already_running", "export_queue_full"})


def run_due(ctx: AppContext) -> list[int]:
    """Cria as exportações automáticas devidas. Retorna os ids das exportações criadas nesta chamada. Nunca levanta
    (chamado do worker e da manutenção)."""
    created: list[int] = []
    try:
        with _LOCK:
            if not ctx.batches.auto_export_waiting():
                return created
            ctx.batches.auto_export_closed_without_result()
            batch_ids, group_ids = ctx.batches.auto_export_due()
            request_ids = ctx.batches.request_auto_export_due()
            for kind, row_id in [*(("batch", b) for b in batch_ids), *(("group", g) for g in group_ids),
                                 *(("request", r) for r in request_ids)]:
                export_id = _create_one(ctx, kind, row_id)
                if export_id is not None:
                    created.append(export_id)
    except Exception:  # noqa: BLE001 - a exportação automática nunca derruba o worker
        logger.exception("Falha inesperada ao verificar as exportações automáticas")
    return created


def _row(ctx: AppContext, kind: str, row_id: int) -> dict[str, Any] | None:
    if kind == "batch":
        return ctx.batches.get(row_id)
    if kind == "group":
        return ctx.batches.get_group(row_id)
    return ctx.batches.get_request(row_id)


def _source(ctx: AppContext, kind: str, row_id: int) -> dict[str, Any]:
    if kind != "request":
        return {"kind": kind, "id": row_id}
    # Pedido com várias partes (migration 0024): UM ZIP com a união das mídias das partes, pela Biblioteca.
    return {"kind": "library", "selection": {"media_ids": ctx.batches.request_media_ids(row_id)}}


def _create_one(ctx: AppContext, kind: str, row_id: int) -> int | None:
    from app.services import export_service

    row = _row(ctx, kind, row_id)
    if row is None or row.get("auto_export_state") != "waiting":
        return None
    content = row.get("auto_export_content") or "originals"

    def mark_created(export_id: int) -> None:
        # Chamado DENTRO da transação que grava a exportação (export_service -> ExportRepository.create): o ZIP e o
        # registro dele no lote/grupo/pedido nascem juntos. Se o app cair entre um e outro, nada fica gravado (o
        # disparo continua esperando e a próxima verificação cria a única exportação).
        if not ctx.batches.finish_auto_export(kind, row_id, export_id=export_id, error=None):
            raise RuntimeError("disparo da exportação automática já resolvido")

    try:
        export = export_service.create_export(ctx, _source(ctx, kind, row_id), {"zip": True}, content,
                                              on_created=mark_created)
    except AppError as exc:
        if exc.code in _TRANSIENT_CODES:
            return None
        ctx.batches.finish_auto_export(kind, row_id, export_id=None, error=exc.detail)
        logger.warning("Exportação automática do %s %s não criada (%s)", _label(kind), row_id, exc.code)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Exportação automática do %s %s falhou", _label(kind), row_id)
        ctx.batches.finish_auto_export(kind, row_id, export_id=None,
                                       error="Erro interno ao criar a exportação. Consulte o log do aplicativo.")
        return None
    logger.info("Exportação automática %s criada para o %s %s (%s)", export["id"], _label(kind), row_id, content)
    return int(export["id"])


def _label(kind: str) -> str:
    return {"batch": "lote", "group": "grupo"}.get(kind, "pedido")


def with_requests(ctx: AppContext, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """As linhas (lotes ou grupos) com a exportação automática apresentada: a parte de um pedido com várias partes
    (`request_id`, migration 0024) mostra a do PEDIDO (todas as partes apontam para o mesmo ZIP); as demais ficam como
    estão. Sem parte de pedido na página, nenhuma consulta."""
    requests = ctx.batches.requests_by_id(sorted({int(row["request_id"]) for row in rows if row.get("request_id")}))
    if not requests:
        return rows
    out = []
    for row in rows:
        request = requests.get(int(row["request_id"])) if row.get("request_id") else None
        if request is not None:
            row = {**row, **{key: request[key] for key in _AUTO_EXPORT_KEYS}, "auto_export_request_id": request["id"]}
        out.append(row)
    return out


_AUTO_EXPORT_KEYS = ("auto_export_content", "auto_export_state", "auto_export_id", "auto_export_error")


def auto_export_out(row: dict[str, Any], exports: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    """Estado apresentado da exportação automática de um lote/grupo; None sem exportação automática.

    `status`: "waiting" (aguardando o fim), "failed" (a criação falhou; motivo em `error_message`) ou o estado da
    exportação criada ("pending", "running", "completed", "failed", "cancelled", "expired")."""
    content = row.get("auto_export_content")
    if not content:
        return None
    state = row.get("auto_export_state") or "waiting"
    export_id = row.get("auto_export_id")
    out: dict[str, Any] = {"content": content, "status": state if state != "created" else "pending",
                           "export_id": export_id, "error_message": row.get("auto_export_error"),
                           "zip_ready": False, "download_url": None,
                           "request_id": row.get("auto_export_request_id")}
    export = exports.get(int(export_id)) if export_id is not None else None
    if state == "created":
        if export is None:
            out["status"] = "expired"   # registro removido (não deve acontecer): nada para baixar
        else:
            out["status"] = export["status"]
            out["error_message"] = export.get("error_message")
            ready = export.get("mode") == "zip" and export["status"] == "completed" and export.get("zip_token")
            out["zip_ready"] = bool(ready)
            out["download_url"] = (f"/api/exports/{export['id']}/download?token={export['zip_token']}"
                                   if ready else None)
    return out


def exports_for(ctx: AppContext, rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Exportações automáticas criadas das linhas (uma leitura por exportação; só linhas que têm uma)."""
    ids = sorted({int(row["auto_export_id"]) for row in rows if row.get("auto_export_id") is not None})
    if not ids:
        return {}
    found = {}
    for export_id in ids:
        export = ctx.exports.get(export_id)
        if export is not None:
            found[export_id] = export
    return found
