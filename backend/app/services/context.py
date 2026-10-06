"""Contêiner de dependências do backend (um por aplicação)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.config import Settings
from app.db.connection import Database
from app.db.repositories import (
    BatchRepository,
    JobRepository,
    MediaRepository,
    PresetRepository,
    SettingsRepository,
)
from app.integrations.instagram.downloader import InstagramDownloader
from app.integrations.instagram.session import InstagramSessionProvider
from app.jobs.locks import KeyedLocks
from app.services.processor_provider import ProcessorProvider
from app.services.storage import Storage

if TYPE_CHECKING:
    from app.jobs.import_runner import ImportRunner
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
    # Serializa "checar duplicata -> promover arquivo -> inserir mídia" entre threads do worker.
    ingest_lock: threading.Lock = field(default_factory=threading.Lock)
    # Serializa o processamento por (mídia, preset, params): jobs simultâneos geram uma única rendition.
    rendition_locks: KeyedLocks = field(default_factory=KeyedLocks)
    # Um teste de sessão por vez (liberado só quando a chamada ao adapter termina de fato).
    session_test_lock: threading.Lock = field(default_factory=threading.Lock)
    # Uma prévia de perfil por vez (mesma regra de liberação).
    profile_preview_lock: threading.Lock = field(default_factory=threading.Lock)
    worker: Worker | None = None
    import_runner: ImportRunner | None = None

    def wake_worker(self) -> None:
        if self.worker is not None:
            self.worker.wake()

    def wake_import_runner(self) -> None:
        if self.import_runner is not None:
            self.import_runner.wake()
