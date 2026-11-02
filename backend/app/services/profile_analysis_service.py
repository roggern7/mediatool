"""Análise de Perfis: leitura da lista colada, criação, cancelamento e apresentação (fuso do servidor para tela e
relatórios). O trabalho fica em `app.jobs.profile_analysis_runner`; nada aqui fala com o Instagram."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import IO, TYPE_CHECKING, Any

from app.core.errors import AppError, not_found
from app.db.repositories.profile_analyses import OPEN_STATUSES, RESUMABLE_STATUSES, TERMINAL_STATUSES
from app.integrations.instagram.validation import parse_profile_input
from app.services import export_time, profile_report

if TYPE_CHECKING:
    from app.services.context import AppContext

MAX_PROFILES = 10000
MAX_INVALID_LINES = 50
REPORT_SPOOL_BYTES = 1024 * 1024
MAX_LINE_CHARS = 120
_HTTP_PROFILE_PREFIXES = ("http://instagram.com/", "http://www.instagram.com/")
SUMMARY_KEYS = ("active", "private", "restricted", "unavailable", "unverified", "cancelled", "stories_yes", "highlights_yes",
                "views_total")


@dataclass
class ParsedList:
    usernames: list[str] = field(default_factory=list)   # normalizados, na ordem da 1a aparição
    invalid: list[dict[str, str]] = field(default_factory=list)   # {line (truncada), reason}
    duplicate_count: int = 0


def parse_profile_list(text: str) -> ParsedList:
    """Uma entrada por linha (@, usuário ou link); linha em branco é ignorada; duplicata comparada depois de
    normalizar."""
    parsed = ParsedList()
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip(" \t﻿")
        if not line:
            continue
        https = f"https://{line[len('http://'):]}" if line.lower().startswith(_HTTP_PROFILE_PREFIXES) else line
        try:
            username = parse_profile_input(https)
        except ValueError as exc:
            parsed.invalid.append({"line": line[:MAX_LINE_CHARS], "reason": str(exc)})
            continue
        if username in seen:
            parsed.duplicate_count += 1
            continue
        seen.add(username)
        parsed.usernames.append(username)
    return parsed


def _require(ctx: AppContext, analysis_id: int) -> dict[str, Any]:
    analysis = ctx.profile_analyses.get(analysis_id)
    if analysis is None:
        raise not_found("Análise", "analysis_not_found", feminine=True)
    return analysis


def _zone(ctx: AppContext) -> export_time.Zone:
    return export_time.resolve_zone(ctx.settings.timezone)


def analysis_out(ctx: AppContext, analysis: dict[str, Any], stats: dict[str, Any] | None = None) -> dict[str, Any]:
    if stats is None:
        stats = ctx.profile_analyses.stats([analysis["id"]]).get(analysis["id"], {})
    zone = _zone(ctx)
    summary = {key: stats.get(key) or 0 for key in SUMMARY_KEYS}
    created_date, created_time = profile_report.local_parts(analysis["created_at"], zone)
    _, finished_time = profile_report.local_parts(analysis["finished_at"], zone)
    return {
        **{key: analysis[key] for key in (
            "id", "status", "total", "invalid_count", "duplicate_count", "invalid_lines", "waiting_until",
            "wait_reason", "error", "created_at", "started_at", "finished_at")},
        "done": stats.get("done") or 0,
        "pending": stats.get("pending") or 0,
        "running_username": stats.get("running_username"),
        "created_date": created_date,
        "created_time": created_time,
        "finished_time": finished_time,
        "report_ready": analysis["status"] in TERMINAL_STATUSES,
        "cautious_pace": bool(analysis["cautious_pace"]),
        "resumable": analysis["status"] in RESUMABLE_STATUSES and bool(stats.get("cancelled")),
        "summary": summary,
        "accounts": profile_report.account_totals(summary),
    }


def item_out(item: dict[str, Any], zone: export_time.Zone) -> dict[str, Any]:
    checked_date, checked_time = profile_report.local_parts(item["checked_at"], zone)
    return {**item, "checked_date": checked_date, "checked_time": checked_time}


def create_analysis(ctx: AppContext, text: str) -> dict[str, Any]:
    parsed = parse_profile_list(text)
    if not parsed.usernames:
        raise AppError(400, "no_profiles", "Nenhum perfil válido na lista.")
    if len(parsed.usernames) > MAX_PROFILES:
        raise AppError(400, "too_many_profiles", "A lista passa do limite de 10.000 perfis por análise.")
    analysis = ctx.profile_analyses.create(
        parsed.usernames, invalid_count=len(parsed.invalid), duplicate_count=parsed.duplicate_count,
        invalid_lines=parsed.invalid[:MAX_INVALID_LINES],
    )
    ctx.profile_runner.wake()
    return analysis_out(ctx, analysis)


def get_analysis(ctx: AppContext, analysis_id: int) -> dict[str, Any]:
    return analysis_out(ctx, _require(ctx, analysis_id))


def list_analyses(ctx: AppContext, *, limit: int, offset: int) -> dict[str, Any]:
    rows = ctx.profile_analyses.list(limit=limit, offset=offset)
    stats = ctx.profile_analyses.stats(row["id"] for row in rows)
    return {"items": [analysis_out(ctx, row, stats.get(row["id"], {})) for row in rows],
            "total": ctx.profile_analyses.count(), "open_id": ctx.profile_analyses.current_id()}


def list_items(ctx: AppContext, analysis_id: int, *, limit: int, offset: int) -> dict[str, Any]:
    analysis = _require(ctx, analysis_id)
    zone = _zone(ctx)
    rows = ctx.profile_analyses.items(analysis_id, limit=limit, offset=offset)
    return {"items": [item_out(row, zone) for row in rows], "total": analysis["total"]}


def cancel_analysis(ctx: AppContext, analysis_id: int) -> dict[str, Any]:
    _require(ctx, analysis_id)
    previous = ctx.profile_analyses.request_cancel(analysis_id)
    if previous in OPEN_STATUSES:
        ctx.profile_runner.cancel(analysis_id)
    return get_analysis(ctx, analysis_id)


def delete_analysis(ctx: AppContext, analysis_id: int) -> None:
    """Apaga do histórico uma análise encerrada. Aberta: 409, para cancelar antes."""
    _require(ctx, analysis_id)
    if not ctx.profile_analyses.delete_analysis(analysis_id):
        raise AppError(409, "analysis_running", "A análise ainda está em andamento. Cancele antes de apagar.")


def clear_history(ctx: AppContext) -> dict[str, int]:
    """Apaga todas as análises encerradas. As abertas continuam rodando."""
    deleted = ctx.profile_analyses.clear_finished()
    return {"deleted": deleted, "kept_open": len(ctx.profile_analyses.open_analyses())}


def resume_analysis(ctx: AppContext, analysis_id: int) -> dict[str, Any]:
    """Os perfis não consultados voltam à fila; o que já tem resultado não muda nem é consultado de novo."""
    _require(ctx, analysis_id)
    if not ctx.profile_analyses.resume_analysis(analysis_id):
        raise AppError(409, "analysis_not_resumable", "Esta análise não tem perfis para retomar.")
    ctx.profile_runner.wake()
    return get_analysis(ctx, analysis_id)


def report_zip(ctx: AppContext, analysis_id: int) -> tuple[str, IO[bytes], int]:
    """(nome do arquivo, ZIP aberto no início, tamanho), montado a partir do banco: reexportar nunca consulta o
    Instagram. O arquivo é temporário e sem memória proporcional ao total; quem recebe tem de fechá-lo."""
    analysis = _require(ctx, analysis_id)
    if analysis["status"] in OPEN_STATUSES:
        raise AppError(409, "analysis_running",
                       "A análise ainda está em andamento. O relatório sai quando ela terminar.")
    zone = _zone(ctx)

    def items() -> Iterator[dict[str, Any]]:
        return (item_out(row, zone) for row in ctx.profile_analyses.iter_items(analysis_id))

    target = tempfile.SpooledTemporaryFile(max_size=REPORT_SPOOL_BYTES)
    try:
        name = profile_report.write_report(target, analysis_out(ctx, analysis), items, zone)
        current = ctx.profile_analyses.get(analysis_id)
        if current is None or (current["status"], current["finished_at"]) != (analysis["status"],
                                                                                analysis["finished_at"]):
            # Retomada enquanto o ZIP era gerado: as passadas pelos itens não mostram o mesmo momento.
            raise AppError(409, "analysis_running",
                           "A análise foi retomada durante a geração do relatório. Baixe de novo quando ela terminar.")
        size = target.seek(0, os.SEEK_END)
        target.seek(0)
    except BaseException:
        target.close()
        raise
    return name, target, size
