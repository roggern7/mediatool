"""Relatório de padrões de postagem (migration 0026): ZIP gerado na hora a partir dos horários gravados na listagem."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query
from starlette.responses import Response

from app.api.deps import MAX_ID, Ctx
from app.core.errors import AppError, not_found
from app.schemas.post_patterns import PatternReport
from app.services import export_time

router = APIRouter(tags=["importação"])

OptionalId = Annotated[int | None, Query(ge=1, le=MAX_ID)]
POST_TIMES_NOT_FOUND_MESSAGE = (
    "Esta importação não tem horários de posts gravados. Marque \"Puxar horário dos posts\" (ou use a aba "
    "\"Consultar padrões\") ao importar perfis e espere a listagem avançar."
)


def _within_limit(rows: list[dict], limits: dict[int, int | None]) -> list[dict]:
    """Lote com "Últimas N": por lote (um perfil), só as N publicações NÃO fixadas mais recentes (por `taken_at`) e os
    fixados. A grade e a aba Reels juntas podem passar de N; o relatório continua sendo das N mais recentes."""
    kept: list[dict] = []
    for batch_id, limit in limits.items():
        own = [row for row in rows if row["batch_id"] == batch_id]
        if limit is not None:
            unpinned = sorted((row for row in own if not row["pinned"]), key=lambda row: (
                export_time.publication_moment(row["taken_at"]) or datetime.min.replace(tzinfo=UTC), row["code"]),
                reverse=True)[:limit]
            chosen = {id(row) for row in unpinned}
            own = [row for row in own if row["pinned"] or id(row) in chosen]
        kept += own
    return kept


def _report_posts(ctx: Ctx, batch_id: int | None, group_id: int | None) -> tuple[list, str]:
    """Publicações (com o corte das N mais recentes) e título de um lote OU de um grupo; erros 422/404."""
    if (batch_id is None) == (group_id is None):
        raise AppError(422, "invalid_input", "Informe batch_id ou group_id (um dos dois).")
    if batch_id is not None:
        batch = ctx.batches.get(batch_id)
        if batch is None:
            raise not_found("Lote", "batch_not_found")
        limits = {batch_id: batch["requested_limit"]}
        title = f"@{batch['source_profile']}" if batch.get("source_profile") else f"lote {batch_id}"
    else:
        if ctx.batches.get_group(group_id) is None:
            raise not_found("Grupo de importação", "group_not_found")
        limits = {row["id"]: row["requested_limit"] for row in ctx.db.fetchall(
            "SELECT id, requested_limit FROM batches WHERE group_id = ?", (group_id,))}
        title = f"grupo {group_id}"
    # Publicação sem data (o Instagram não trouxe `taken_at`) não entra na análise.
    rows = _within_limit([row for row in ctx.batches.post_times_rows(list(limits)) if row["taken_at"]], limits)
    if not rows:
        raise AppError(404, "post_times_not_found", POST_TIMES_NOT_FOUND_MESSAGE)
    from app.services.post_patterns import PostTime

    posts = [PostTime(username=row["username"], code=row["code"], taken_at=row["taken_at"], kind=row["kind"],
                      pinned=None if row["pinned"] is None else bool(row["pinned"]), source=row["source"])
             for row in rows]
    return posts, title


@router.get("/post-times/report.zip", response_class=Response)
def post_times_report(ctx: Ctx, batch_id: OptionalId = None, group_id: OptionalId = None) -> Response:
    """ZIP com uma pasta por perfil (e comparativo com 2+ perfis) de um lote (`batch_id`) OU de um grupo
    (`group_id`). Sem horários gravados: 404 `post_times_not_found`. Nunca consulta o Instagram."""
    from app.services.post_patterns import build_report_zip, report_filename

    posts, title = _report_posts(ctx, batch_id, group_id)
    zone = export_time.resolve_zone(ctx.settings.timezone)   # o mesmo fuso dos nomes da exportação
    content = build_report_zip(posts, title=title, zone=zone)
    name = report_filename(title, zone(datetime.now(UTC)))
    return Response(content, media_type="application/zip", headers={
        "content-disposition": f'attachment; filename="{name}"', "cache-control": "no-store"})


@router.get("/post-times/report", response_model=PatternReport)
def post_times_report_data(ctx: Ctx, batch_id: OptionalId = None, group_id: OptionalId = None) -> PatternReport:
    """Os mesmos dados do ZIP em JSON (tela de padrões): mesmas contas, cortes, fuso e erros do `.zip`."""
    from app.services.post_patterns import report_data

    posts, title = _report_posts(ctx, batch_id, group_id)
    data = report_data(posts, title=title, zone=export_time.resolve_zone(ctx.settings.timezone))
    query = f"batch_id={batch_id}" if batch_id is not None else f"group_id={group_id}"
    return PatternReport(**data, zip_url=f"/api/post-times/report.zip?{query}")
