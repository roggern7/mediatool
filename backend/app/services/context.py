"""Contêiner de dependências do backend (um por aplicação)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.config import Settings
from app.db.connection import Database
from app.db.repositories import (
    BatchRepository,
    ExportRepository,
    JobRepository,
    MediaRepository,
    PresetRepository,
    ProfileAnalysisRepository,
    SettingsRepository,
)
from app.integrations.instagram.downloader import InstagramDownloader
from app.integrations.instagram.session import InstagramSessionProvider
from app.jobs.locks import KeyedLocks
from app.services.export_destination import DestinationTokens
from app.services.processor_provider import ProcessorProvider
from app.services.storage import Storage

if TYPE_CHECKING:
    from pathlib import Path

    from app.integrations.instagram.web.profile_snapshot import ProfileSnapshot
    from app.jobs.capcut_runner import CapCutRunner
    from app.jobs.export_runner import ExportRunner
    from app.jobs.import_runner import ImportRunner
    from app.jobs.profile_analysis_runner import ProfileAnalysisRunner
    from app.jobs.worker import Worker


@dataclass
class AppContext:
    settings: Settings
    db: Database
    storage: Storage
    session_provider: InstagramSessionProvider
    downloader_factory: Callable[[], InstagramDownloader]
    processors: ProcessorProvider
    presets: PresetRepository
    media: MediaRepository
    jobs: JobRepository
    batches: BatchRepository
    app_settings: SettingsRepository
    exports: ExportRepository
    profile_analyses: ProfileAnalysisRepository
    # Serializa "checar duplicata -> promover arquivo -> inserir mídia" entre threads do worker.
    ingest_lock: threading.Lock = field(default_factory=threading.Lock)
    # Serializa o processamento por (mídia, preset, params): jobs simultâneos geram uma única rendition.
    rendition_locks: KeyedLocks = field(default_factory=KeyedLocks)
    # Um teste de sessão por vez (liberado só quando a chamada ao adapter termina de fato).
    session_test_lock: threading.Lock = field(default_factory=threading.Lock)
    # Uma prévia de perfil por vez (mesma regra de liberação).
    profile_preview_lock: threading.Lock = field(default_factory=threading.Lock)
    # Exportação para pasta: destinos escolhidos no diálogo nativo (token de uso único) e um diálogo por vez.
    export_tokens: DestinationTokens = field(default_factory=DestinationTokens)
    picker_lock: threading.Lock = field(default_factory=threading.Lock)
    # Serializa "conferir exportação aberta/fila -> validar destino -> criar" entre requisições.
    export_create_lock: threading.Lock = field(default_factory=threading.Lock)
    # Injetáveis (testes): None = diálogo nativo do Windows e pasta Documentos reais (app.services.export_destination).
    folder_picker: Callable[[Path | None], Path | None] | None = None
    documents_dir: Callable[[], Path] | None = None
    # Lote por link: ids dos lotes em que a 2a tentativa de um link restrito com a sessão do dono foi recusada ou
    # falhou (`app.jobs.pipeline.note_links_session_refusal`); os links restritos seguintes deles não usam mais a
    # sessão. Só memória: um reinício do app libera de novo a 2a tentativa nesses lotes.
    links_session_off: set[int] = field(default_factory=set)
    worker: Worker | None = None
    import_runner: ImportRunner | None = None
    export_runner: ExportRunner | None = None
    # Análise de Perfis: (usuário, evento de cancelamento) -> retrato público de um perfil, sem sessão. Montado em
    # `app.main.build_context`; nunca passa pelo `session_provider`.
    profile_fetcher: Callable[[str, threading.Event | None], ProfileSnapshot] | None = None
    profile_runner: ProfileAnalysisRunner | None = None
    # Legendas CapCut: executor (um job por vez) e processador injetável (testes); None = `process_video` do núcleo
    # `app.integrations.capcut`, importado só na hora de usar.
    capcut_runner: CapCutRunner | None = None
    capcut_processor: Callable[..., Any] | None = None

    def wake_worker(self) -> None:
        if self.worker is not None:
            self.worker.wake()

    def wake_import_runner(self) -> None:
        if self.import_runner is not None:
            self.import_runner.wake()
