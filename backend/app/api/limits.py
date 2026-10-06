"""Limite de tamanho do corpo das requisições (Content-Length e corpo em streaming)."""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 1024 * 1024


async def _reject(send: Send) -> None:
    body = json.dumps(
        {"detail": "Corpo da requisição maior que o limite de 1 MB.", "code": "payload_too_large"},
        ensure_ascii=False,
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """Rejeita com 413 corpos acima de `max_bytes`. O corpo (pequeno, por definição) é lido inteiro antes de
    chegar à aplicação, então o limite vale também para envio em streaming sem Content-Length."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for key, value in scope.get("headers", ()):
            if key.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = self.max_bytes + 1
                if declared > self.max_bytes:
                    await _reject(send)
                    return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # Desconexão antes do fim do corpo: repassa para a aplicação tratar.
                await self.app(scope, _replay([message], receive), send)
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await _reject(send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        buffered: Message = {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        await self.app(scope, _replay([buffered], receive), send)


def _replay(messages: list[Message], receive: Receive) -> Receive:
    pending = list(messages)

    async def replay() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay
