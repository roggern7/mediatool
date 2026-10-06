from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Path, Request

from app.services.context import AppContext

MAX_ID = 2**53


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]
IdPath = Annotated[int, Path(ge=1, le=MAX_ID)]
