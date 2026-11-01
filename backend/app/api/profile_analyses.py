from __future__ import annotations

from typing import IO, Annotated, Any

from fastapi import APIRouter, Query
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.api.deps import Ctx, IdPath
from app.schemas.profile_analyses import AnalysisCreate, AnalysisItemList, AnalysisList, AnalysisOut
from app.services import profile_analysis_service

router = APIRouter(tags=["análise de perfis"])

Offset = Annotated[int, Query(ge=0, le=2**31)]
REPORT_BLOCK_BYTES = 64 * 1024


class ReportResponse(StreamingResponse):
    """Entrega um arquivo temporário em blocos e o fecha em qualquer saída. O `finally` fica aqui porque uma tarefa de
    fundo não roda na desconexão do cliente, e um gerador que não começou não executa o próprio `finally`."""

    def __init__(self, file: IO[bytes], **kwargs: Any) -> None:
        super().__init__(iter(lambda: file.read(REPORT_BLOCK_BYTES), b""), **kwargs)
        self.file = file

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.file.close()


@router.post("/profile-analyses", response_model=AnalysisOut, status_code=202)
def create_analysis(ctx: Ctx, body: AnalysisCreate) -> dict[str, Any]:
    """Cria uma análise a partir do texto colado (um @ ou link por linha) e a coloca na fila."""
    return profile_analysis_service.create_analysis(ctx, body.text)


@router.get("/profile-analyses", response_model=AnalysisList)
def list_analyses(ctx: Ctx, limit: Annotated[int, Query(ge=1, le=100)] = 20, offset: Offset = 0) -> dict[str, Any]:
    """Histórico (id DESC)."""
    return profile_analysis_service.list_analyses(ctx, limit=limit, offset=offset)


@router.get("/profile-analyses/{analysis_id}", response_model=AnalysisOut)
def get_analysis(ctx: Ctx, analysis_id: IdPath) -> dict[str, Any]:
    return profile_analysis_service.get_analysis(ctx, analysis_id)


@router.get("/profile-analyses/{analysis_id}/items", response_model=AnalysisItemList)
def list_items(
    ctx: Ctx, analysis_id: IdPath, limit: Annotated[int, Query(ge=1, le=200)] = 50, offset: Offset = 0,
) -> dict[str, Any]:
    """Resultados por perfil, na ordem colada."""
    return profile_analysis_service.list_items(ctx, analysis_id, limit=limit, offset=offset)


@router.post("/profile-analyses/{analysis_id}/cancel", response_model=AnalysisOut)
def cancel_analysis(ctx: Ctx, analysis_id: IdPath) -> dict[str, Any]:
    """Idempotente em análise aberta; terminal é devolvida como está. Os resultados já obtidos ficam."""
    return profile_analysis_service.cancel_analysis(ctx, analysis_id)


@router.post("/profile-analyses/{analysis_id}/resume", response_model=AnalysisOut)
def resume_analysis(ctx: Ctx, analysis_id: IdPath) -> dict[str, Any]:
    """Interrompida ou cancelada com perfis não consultados: eles voltam à fila. Os demais casos: 409."""
    return profile_analysis_service.resume_analysis(ctx, analysis_id)


@router.get("/profile-analyses/{analysis_id}/report", response_class=Response)
def download_report(ctx: Ctx, analysis_id: IdPath) -> Response:
    """ZIP com XLSX + CSV + HTML, gerado sob demanda a partir do banco (só dados públicos). Análise aberta: 409."""
    name, file, size = profile_analysis_service.report_zip(ctx, analysis_id)
    return ReportResponse(file, media_type="application/zip", headers={
        "content-disposition": f'attachment; filename="{name}"', "cache-control": "no-store",
        "content-length": str(size)})
