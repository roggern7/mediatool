from __future__ import annotations

from pydantic import BaseModel

from app.schemas.media import MediaOut


class UploadOut(BaseModel):
    media: MediaOut
    duplicate: bool  # conteúdo idêntico (sha256) a uma mídia já existente: nenhuma linha nova
