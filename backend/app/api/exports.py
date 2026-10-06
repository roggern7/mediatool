from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.api.deps import MAX_ID, Ctx, IdPath
from app.schemas.exports import ExportList, ExportOut, ExportRequest, PickFolderOut
from app.schemas.media import OpenResult
from app.services import export_service

router = APIRouter(tags=["exportação"])


@router.post("/exports/pick-folder", response_model=PickFolderOut)
def pick_folder(ctx: Ctx) -> dict[str, Any]:
    """Abre o diálogo nativo "Selecionar pasta" do Windows (um por vez) e devolve um token de uso único para a pasta
    escolhida. Sem corpo: nenhum caminho é aceito do cliente."""
    return export_service.pick_folder(ctx)


@router.post("/exports", response_model=ExportOut, status_code=202)
def create_export(ctx: Ctx, body: ExportRequest) -> dict[str, Any]:
    payload = body.model_dump()
    return export_service.create_export(ctx, payload["source"], payload["destination"])


@router.get("/exports", response_model=ExportList)
def list_exports(
    ctx: Ctx,
    batch_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    group_id: Annotated[int | None, Query(ge=1, le=MAX_ID)] = None,
    kind: Literal["batch", "group", "library"] | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Últimas exportações (id DESC), opcionalmente de um lote, de um grupo ou de um tipo de origem
    (`kind=library`: as da Biblioteca)."""
    rows = ctx.exports.list(limit=limit, batch_id=batch_id, group_id=group_id, kind=kind)
    return {"items": [export_service.export_out(row) for row in rows]}


@router.get("/exports/{export_id}", response_model=ExportOut)
def get_export(ctx: Ctx, export_id: IdPath) -> dict[str, Any]:
    return export_service.export_out(export_service.get_export(ctx, export_id))


@router.get("/exports/{export_id}/download", response_class=FileResponse)
def download_export(
    ctx: Ctx, request: Request, export_id: IdPath, token: Annotated[str | None, Query(max_length=128)] = None,
) -> FileResponse:
    """Download do ZIP pronto, em streaming direto do arquivo (com `Range`). Exige o token de download que só o
    painel recebe no objeto `Export`; sem token ou com token errado a resposta é 404. O ZIP só conta como baixado
    quando uma resposta que vai até o último byte termina de ser transmitida (resposta parcial ou interrompida não
    conta)."""
    path, name, complete = export_service.zip_download(ctx, export_id, token, request.headers.get("range"))
    return FileResponse(
        path, media_type="application/zip", filename=name,
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(export_service.zip_downloaded, ctx, export_id) if complete else None,
    )


@router.post("/exports/{export_id}/cancel", response_model=ExportOut)
def cancel_export(ctx: Ctx, export_id: IdPath) -> dict[str, Any]:
    return export_service.cancel_export(ctx, export_id)


@router.post("/exports/{export_id}/open", response_model=OpenResult)
def open_export(ctx: Ctx, export_id: IdPath) -> dict[str, bool]:
    """Abre no Explorer a pasta daquela exportação (caminho do registro no banco, revalidado)."""
    export_service.open_export_folder(ctx, export_id)
    return {"ok": True}


@router.post("/library/open-folder", response_model=OpenResult)
def open_library_folder(ctx: Ctx) -> dict[str, bool]:
    """Abre no Explorer a pasta física dos originais da Biblioteca (caminho do servidor)."""
    export_service.open_library_folder(ctx)
    return {"ok": True}
