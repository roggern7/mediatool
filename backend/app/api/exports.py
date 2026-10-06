from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Query, Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from app.api.deps import MAX_ID, Ctx, IdPath
from app.schemas.exports import ExportList, ExportOut, ExportRequest, PickFolderOut
from app.schemas.media import OpenResult
from app.services import export_service

router = APIRouter(tags=["exportação"])

DOWNLOAD_BLOCK = 256 * 1024
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | Literal[False]:
    """`Range` de UM intervalo em bytes -> (início, fim) inclusivos. Sem cabeçalho: None (arquivo inteiro). Faixa
    inválida, fora do arquivo, múltipla ou em outra unidade: False (416)."""
    if not header:
        return None
    match = _RANGE.match(header.strip().lower().replace(" ", ""))
    if match is None or (not match.group(1) and not match.group(2)) or size <= 0:
        return False
    if not match.group(1):   # sufixo: os últimos N bytes
        length = int(match.group(2))
        return (max(0, size - length), size - 1) if length > 0 else False
    start = int(match.group(1))
    end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
    return (start, end) if start <= end and start < size else False


class ZipFileResponse(Response):
    """Envia o ZIP em blocos direto do arquivo (nunca inteiro na memória), com `Content-Length`, `Accept-Ranges` e
    `Range` (206/416). Conta os bytes realmente enviados e observa a desconexão do cliente: `on_complete` só é
    chamado quando a faixa pedida termina no ÚLTIMO byte do arquivo, todos os bytes dela foram enviados e o cliente
    não desconectou. Download interrompido não conta como baixado."""

    def __init__(self, path: Path, *, filename: str, range_header: str | None,
                 on_complete: Callable[[], None]) -> None:
        self.path = path
        self.size = path.stat().st_size
        self.on_complete = on_complete
        wanted = parse_range(range_header, self.size)
        headers = {"accept-ranges": "bytes", "cache-control": "no-store"}
        if wanted is False:
            self.span = None
            headers["content-range"] = f"bytes */{self.size}"
            super().__init__(status_code=416, headers=headers)
            return
        self.span = wanted or (0, self.size - 1)
        length = self.span[1] - self.span[0] + 1 if self.size else 0
        headers["content-length"] = str(length)
        headers["content-disposition"] = f'attachment; filename="{filename}"'
        if wanted:
            headers["content-range"] = f"bytes {wanted[0]}-{wanted[1]}/{self.size}"
        super().__init__(status_code=206 if wanted else 200, headers=headers, media_type="application/zip")
        self.raw_headers = [(k, v) for k, v in self.raw_headers if k != b"content-length" or v == str(length).encode()]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        if self.span is None or self.size == 0:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        start, end = self.span
        state = {"sent": 0, "disconnected": False, "last": False}

        async def stream() -> None:
            remaining = end - start + 1
            async with await anyio.open_file(self.path, "rb") as handle:
                await handle.seek(start)
                while remaining > 0:
                    chunk = await handle.read(min(DOWNLOAD_BLOCK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining == 0:
                        state["last"] = True   # último bloco a caminho: o fim da resposta não é desconexão
                    await send({"type": "http.response.body", "body": chunk, "more_body": remaining > 0})
                    state["sent"] += len(chunk)

        async with anyio.create_task_group() as group:
            async def listen() -> None:
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        if state["last"]:
                            return
                        state["disconnected"] = True
                        group.cancel_scope.cancel()   # cliente saiu: para de ler e enviar
                        return

            group.start_soon(listen)
            await stream()
            group.cancel_scope.cancel()
        complete = state["sent"] == end - start + 1 and end == self.size - 1 and not state["disconnected"]
        if complete:
            await anyio.to_thread.run_sync(self.on_complete)


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


@router.get("/exports/{export_id}/download", response_class=Response)
def download_export(
    ctx: Ctx, request: Request, export_id: IdPath, token: Annotated[str | None, Query(max_length=128)] = None,
) -> Response:
    """Download do ZIP pronto, em blocos direto do arquivo (com `Range`). Exige o token de download que só o painel
    recebe no objeto `Export`; sem token ou com token errado a resposta é 404. O ZIP só conta como baixado quando a
    transmissão entregou de fato até o último byte e o cliente não desconectou (`ZipFileResponse`)."""
    path, name, _ = export_service.zip_download(ctx, export_id, token)
    return ZipFileResponse(
        path, filename=name, range_header=request.headers.get("range"),
        on_complete=lambda: export_service.zip_downloaded(ctx, export_id),
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
