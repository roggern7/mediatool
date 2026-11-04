"""Relatório de padrões de postagem (migration 0026): ZIP gerado na hora a partir dos horários gravados na listagem."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query
from starlette.responses import Response

from app.api.deps import MAX_ID, Ctx
from app.core.errors import AppError, not_found
from app.services import export_time

router = APIRouter(tags=["importação"])

OptionalId = Annotated[int | None, Query(ge=1, le=MAX_ID)]
POST_TIMES_NOT_FOUND_MESSAGE = (
    "Esta importação não tem horários de posts gravados. Marque \"Puxar horário dos posts\" (ou escolha \"Nada — só "
    "consultar padrões\") ao importar perfis e espere a listagem avançar."
)


@router.get("/post-times/report.zip", response_class=Response)
def post_times_report(ctx: Ctx, batch_id: OptionalId = None, group_id: OptionalId = None) -> Response:
    """ZIP com relatorio.txt, resumo.csv e posts.csv de um lote (`batch_id`) OU de um grupo (`group_id`). Sem
    horários gravados: 404 `post_times_not_found`. Nunca consulta o Instagram."""
    if (batch_id is None) == (group_id is None):
        raise AppError(422, "invalid_input", "Informe batch_id ou group_id (um dos dois).")
    if batch_id is not None:
        batch = ctx.batches.get(batch_id)
        if batch is None:
            raise not_found("Lote", "batch_not_found")
        batch_ids = [batch_id]
        title = f"@{batch['source_profile']}" if batch.get("source_profile") else f"lote {batch_id}"
    else:
        if ctx.batches.get_group(group_id) is None:
            raise not_found("Grupo de importação", "group_not_found")
        batch_ids = [row["id"] for row in ctx.db.fetchall("SELECT id FROM batches WHERE group_id = ?", (group_id,))]
        title = f"grupo {group_id}"
    # Publicação sem data (o Instagram não trouxe `taken_at`) não entra na análise.
    rows = [row for row in ctx.batches.post_times_rows(batch_ids) if row["taken_at"]]
    if not rows:
        raise AppError(404, "post_times_not_found", POST_TIMES_NOT_FOUND_MESSAGE)
    from app.services.post_patterns import PostTime, build_report_zip, report_filename

    posts = [PostTime(username=row["username"], code=row["code"], taken_at=row["taken_at"], kind=row["kind"],
                      pinned=None if row["pinned"] is None else bool(row["pinned"])) for row in rows]
    zone = export_time.resolve_zone(ctx.settings.timezone)   # o mesmo fuso dos nomes da exportação
    content = build_report_zip(posts, title=title, zone=zone)
    name = report_filename(title, zone(datetime.now(UTC)))
    return Response(content, media_type="application/zip", headers={
        "content-disposition": f'attachment; filename="{name}"', "cache-control": "no-store"})
