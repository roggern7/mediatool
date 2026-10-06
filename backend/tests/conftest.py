from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from media_fakes import FakeInstagramDownloader, FakeProcessor, MemorySessionProvider

CLIENT_HEADERS = {"X-Media-Tool-Client": "pytest"}
BASE_URL = "http://127.0.0.1:8765"


def make_settings(root: Path, **overrides) -> Settings:
    settings = Settings(
        root=root,
        data_dir=root / "data",
        storage_dir=root / "storage",
        log_dir=root / "logs",
        secrets_dir=root / "data" / "secrets",
        frontend_dist=root / "frontend" / "dist",
        port=8765,
        env={},
        retry_delays=(30.0, 120.0, 600.0),
        rate_limit_pause_seconds=600.0,
        poll_interval=0.05,
        heartbeat_interval=0.2,
        instagram_request_interval=(0.0, 0.0),  # portão sem espera nos testes (relógio real)
    )
    return replace(settings, **overrides)


class Harness:
    """App de teste com fakes injetados; `client` já envia o header de mutação."""

    def __init__(self, root: Path, *, downloader=None, processor=None, session_provider=None,
                 start_worker: bool = False, **settings_overrides):
        self.root = root
        self.settings = make_settings(root, **settings_overrides)
        # Job repetido perde a URL ao terminar (seção 2): o fake padrão renova a página para baixar de novo.
        self.downloader = downloader if downloader is not None else FakeInstagramDownloader(refreshable=True)
        self.processor = processor if processor is not None else FakeProcessor()
        self.session_provider = session_provider if session_provider is not None else MemorySessionProvider()
        self.app = create_app(
            self.settings,
            session_provider=self.session_provider,
            downloader_factory=lambda: self.downloader,
            processor=self.processor,
            start_worker=start_worker,
        )
        self.ctx = self.app.state.ctx
        self.client = TestClient(self.app, base_url=BASE_URL, headers=CLIENT_HEADERS)

    def __enter__(self) -> Harness:
        self.client.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.client.__exit__(*exc)

    def run_imports(self) -> None:
        while self.ctx.import_runner.run_next():
            pass

    def run_jobs(self) -> int:
        return self.ctx.worker.run_pending()

    def run_all(self) -> None:
        self.run_imports()
        self.run_jobs()

    def import_profile(self, **body) -> dict:
        payload = {"profile": "perfil_teste", "content_type": "all", "limit": 100, "preset_id": None,
                   "process": False, **body}
        response = self.client.post("/api/imports", json=payload)
        assert response.status_code == 202, response.text
        return response.json()

    def batch(self, batch_id: int) -> dict:
        # Sem include_jobs o lote vem sem jobs (seção 6); os testes pequenos conferem os jobs embutidos.
        response = self.client.get(f"/api/batches/{batch_id}", params={"include_jobs": "true"})
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def harness_factory(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    created: list[Harness] = []

    def factory(root: Path | None = None, **kwargs) -> Harness:
        """Cria um app de teste. Passar `root` de um app anterior simula um reinício sobre os mesmos dados."""
        if root is None:
            root = tmp_path / f"app{len(created)}"
            root.mkdir()
        harness = Harness(root, **kwargs).__enter__()
        created.append(harness)
        return harness

    yield factory
    for harness in reversed(created):
        harness.__exit__(None, None, None)


@pytest.fixture
def harness(harness_factory) -> Harness:
    return harness_factory()


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture(autouse=True)
def _no_real_desktop(monkeypatch):
    """Nenhum teste abre o diálogo nativo de pasta, o Explorer ou toca a pasta Documentos real: quem precisa injeta
    `ctx.folder_picker`/`ctx.documents_dir` e troca `export_service.open_folder` (ou injeta o próprio falso de
    `subprocess.Popen`/`os.startfile` sobre as funções reais guardadas na importação do módulo de teste)."""
    from app.services import export_destination, export_service, system_open

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Teste tentou usar o desktop real (diálogo de pasta, Explorer ou Documentos).")

    monkeypatch.setattr(export_destination, "pick_folder_native", forbidden)
    monkeypatch.setattr(export_destination, "user_documents_dir", forbidden)
    monkeypatch.setattr(export_service, "open_folder", forbidden)
    # Chamadas diretas aos métodos do seletor (Popen real de tkinter/PowerShell) e à abertura real de pastas.
    for name in ("_pick_with_tk", "_pick_with_powershell", "_run_picker_child"):
        monkeypatch.setattr(export_destination, name, forbidden)
    monkeypatch.setattr(system_open, "open_folder", forbidden)
    monkeypatch.setattr(system_open.os, "startfile", forbidden, raising=False)


@pytest.fixture(autouse=True)
def _isolate_registered_secrets():
    """O registro de segredos do redator é global no processo; cada teste começa e termina com o mesmo conjunto,
    para que valores FAKE curtos registrados por um teste não redijam texto de outro (dependência de ordem)."""
    from app.core import logging as mt_logging

    with mt_logging._lock:
        saved = (set(mt_logging._secrets), mt_logging._secrets_sorted)
    yield
    with mt_logging._lock:
        mt_logging._secrets.clear()
        mt_logging._secrets.update(saved[0])
        mt_logging._secrets_sorted = saved[1]
