from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.schemas.common import PositiveId, SafeText, StrictModel

ExportStatus = Literal["pending", "running", "completed", "cancelled", "failed", "expired"]
ExportMode = Literal["folder", "zip"]
ExportContent = Literal["originals", "edited", "both"]


class BatchSource(StrictModel):
    kind: Literal["batch"]
    id: PositiveId


class GroupSource(StrictModel):
    kind: Literal["group"]
    id: PositiveId


class IdsSelection(StrictModel):
    # Mesmo formato de POST /api/media/process.
    # Sem teto de quantidade: o limite real é o corpo de 1 MB da API. Seleção ampla usa o filtro (sem ids).
    media_ids: Annotated[list[PositiveId], Field(min_length=1)]


class LibraryFilter(StrictModel):
    # Mesmo filtro de GET /api/media e de POST /api/media/process-by-filter.
    view: Literal["all", "originals", "processed"] = "all"
    media_type: Literal["video", "image"] | None = None
    role: Literal["original", "cover"] | None = None   # migration 0021: "cover" = só capas
    q: Annotated[SafeText | None, Field(max_length=200)] = None
    profile: Annotated[SafeText | None, Field(max_length=64)] = None


class FilterSelection(StrictModel):
    # "Todos os resultados do filtro", resolvido no servidor; `exclude_ids` tira mídias desmarcadas.
    filter: LibraryFilter
    exclude_ids: list[PositiveId] = Field(default_factory=list)


class LibrarySource(StrictModel):
    kind: Literal["library"]
    selection: IdsSelection | FilterSelection


class TokenDestination(StrictModel):
    # Token devolvido por POST /api/exports/pick-folder. Nenhum caminho é aceito do cliente.
    token: str = Field(min_length=16, max_length=128)


class DefaultDestination(StrictModel):
    default: Literal[True]


class ZipDestination(StrictModel):
    # Baixar ZIP: o servidor prepara um .zip plano num temporário próprio (nenhuma pasta do usuário envolvida).
    zip: Literal[True]


class ExportRequest(StrictModel):
    source: Annotated[BatchSource | GroupSource | LibrarySource, Field(discriminator="kind")]
    destination: TokenDestination | DefaultDestination | ZipDestination
    # O que exportar: originais (padrão), versões editadas/processadas, ou os dois.
    content: ExportContent = "originals"


class PickFolderOut(BaseModel):
    cancelled: bool
    token: str | None = None
    path_display: str | None = None


class ExportOut(BaseModel):
    id: int
    kind: Literal["batch", "group", "library"]
    batch_id: int | None
    group_id: int | None
    dest_path: str
    status: ExportStatus
    total: int
    # Arquivos já tratados: copied + skipped_existing + missing + failed.
    processed: int
    # Gravados na pasta (inclui os `renamed`).
    copied: int
    # Arquivo idêntico já estava na pasta: não foi copiado de novo.
    skipped_existing: int
    # Gravados com outro nome (` (2)`, ` (3)`...) porque havia um arquivo DIFERENTE com o mesmo nome.
    renamed: int
    # Arquivo (original ou versão editada) ausente no storage.
    missing: int
    failed: int
    error_code: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    mode: ExportMode = "folder"
    content: ExportContent = "originals"
    # Só em mode = "zip". `download_url` (com o token de download) e `zip_bytes` só com o ZIP pronto.
    zip_name: str | None = None
    zip_bytes: int | None = None
    zip_ready: bool = False
    zip_expires_at: str | None = None
    download_url: str | None = None


class ExportList(BaseModel):
    items: list[ExportOut]


class LastExport(BaseModel):
    # Última exportação CONCLUÍDA do lote/grupo: define o "Abrir pasta" da importação.
    id: int
    dest_path: str
    status: ExportStatus
    total: int
    copied: int
    skipped_existing: int
    renamed: int
    missing: int
    failed: int
    finished_at: str | None
    mode: ExportMode = "folder"
