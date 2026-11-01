"""Aplicação FastAPI do Media Tool.

`create_app()` monta o contexto (banco, storage, sessão, adapter, processor, fila) e o lifespan:
logging -> migrations -> recovery -> worker + import runner (threads). FFmpeg e downloads nunca rodam no event loop:
endpoints são síncronos (threadpool) e o trabalho pesado fica nas threads do worker.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, Response
from media_processor import MediaProcessor
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import api_router
from app.api.errors import install_error_handlers
from app.api.limits import MAX_BODY_BYTES, BodySizeLimitMiddleware
from app.core.config import Settings, load_settings
from app.core.logging import configure_logging
from app.core.security import DEFAULT_CORS_ORIGINS, UnsafePathError, install_security, safe_join
from app.db.connection import Database
from app.db.migrate import apply_migrations
from app.db.repositories import (
    BatchRepository,
    ExportRepository,
    JobRepository,
    MediaRepository,
    PresetRepository,
    ProfileAnalysisRepository,
    SettingsRepository,
)
from app.integrations.instagram.composite import CompositeInstagramDownloader
from app.integrations.instagram.downloader import InstagramDownloader
from app.integrations.instagram.gallerydl import GalleryDlInstagramDownloader
from app.integrations.instagram.session import InstagramSessionProvider, LocalInstagramSessionProvider
from app.integrations.instagram.web.http import InstagramWebHttp, WebHttpConfig
from app.integrations.instagram.web.profile_snapshot import ProfileSnapshot, fetch_profile_snapshot
from app.integrations.instagram.web.provider import InstagramWebDownloader, WebStateCache
from app.jobs.export_runner import ExportRunner, recover_interrupted_exports
from app.jobs.import_runner import ImportRunner
from app.jobs.profile_analysis_runner import ProfileAnalysisRunner, recover_profile_analyses
from app.jobs.recovery import run_recovery
from app.jobs.worker import Worker
from app.services.context import AppContext
from app.services.instagram_gate import PAGES_STATE_KEY, InstagramRequestGate
from app.services.processor_provider import ProcessorProvider
from app.services.settings_service import (
    effective_concurrency,
    effective_download_concurrency,
    instagram_doc_id_overrides,
    instagram_provider,
)
from app.services.storage import Storage
from app.services.upload_service import UPLOAD_PATH

logger = logging.getLogger("media_tool")

FRONTEND_MISSING_PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Media Tool</title>
<style>body{background:#0f1113;color:#e7e9eb;font:13px Inter,"Segoe UI",system-ui,sans-serif;padding:40px}
code{background:#1d2125;padding:2px 6px;border-radius:6px}a{color:#6b8afd}</style></head>
<body><h1>Media Tool</h1>
<p>O painel ainda não foi compilado. Rode <code>npm install</code> e <code>npm run build</code> na pasta
<code>frontend</code> (ou <code>scripts\\setup.ps1</code>) e recarregue esta página.</p>
<p>A API está ativa: <a href="/docs">/docs</a>.</p></body></html>"""


def anonymous_profile_fetcher(
    gate: InstagramRequestGate, *, transport: httpx.BaseTransport | None = None,
) -> Callable[[str, threading.Event | None], ProfileSnapshot]:
    """Consulta da Análise de Perfis: por chamada, um cliente ANÔNIMO (jar vazio; nenhuma sessão é lida) e UM GET da
    aba Reels. Passa pelo portão global (1 em voo, intervalo, pausa publicada -> `InstagramPaused`). Não usa o teto de
    páginas por hora das importações: o limite vem do ritmo do executor. O cliente é descartado ao fim da consulta, e
    os cookies anônimos que ele recebe não viram segredo registrado (não são da conta do dono e seriam novos a cada
    consulta)."""

    def fetch(username: str, cancel: threading.Event | None) -> ProfileSnapshot:
        http = InstagramWebHttp.anonymous(WebHttpConfig(), transport=transport, gate=gate, register_cookies=False)
        http.cancel = cancel
        try:
            return fetch_profile_snapshot(http, username)
        finally:
            http.close()

    return fetch


def build_context(
    settings: Settings,
    *,
    session_provider: InstagramSessionProvider | None = None,
    downloader_factory: Callable[[], InstagramDownloader] | None = None,
    processor: MediaProcessor | None = None,
    profile_fetcher: Callable[[str, threading.Event | None], ProfileSnapshot] | None = None,
) -> AppContext:
    db = Database(settings.database_path)
    storage = Storage(settings.storage_dir)
    provider = session_provider or LocalInstagramSessionProvider(settings.secrets_dir, env=settings.env)
    app_settings = SettingsRepository(db)
    # Portão global (seção 8): único no app; os providers ocupam uma vaga por requisição/operação ao Instagram.
    # As reservas de página da última hora ficam em app_state (o teto por hora vale através de um reinício).
    gate = InstagramRequestGate(interval=settings.instagram_request_interval,
                                save_pages=lambda value: app_settings.set_state(PAGES_STATE_KEY, value))
    # WebState do bootstrap compartilhado entre trechos e renovações (seção 9, M2; só memória, até 10 min).
    bootstrap_cache = WebStateCache()

    if downloader_factory is None:

        def downloader_factory() -> InstagramDownloader:
            # Uma instância por operação: tokens do Instagram Web vivem só nela, em memória.
            web = InstagramWebDownloader(
                provider,
                max_download_bytes=settings.max_download_bytes,
                doc_id_overrides=instagram_doc_id_overrides(app_settings),
                env=settings.env,  # inclui o .env (MEDIA_TOOL_IG_DOC_<NAME>)
                request_gate=gate,
                bootstrap_cache=bootstrap_cache,
            )
            gallery = GalleryDlInstagramDownloader(
                provider, temp_dir=storage.temp, max_download_bytes=settings.max_download_bytes,
                request_gate=gate,
            )
            return CompositeInstagramDownloader(web, gallery, strategy=instagram_provider(app_settings, settings))

    ctx = AppContext(
        settings=settings,
        db=db,
        storage=storage,
        session_provider=provider,
        downloader_factory=downloader_factory,
        processors=ProcessorProvider(settings, app_settings, processor),
        presets=PresetRepository(db),
        media=MediaRepository(db),
        jobs=JobRepository(db),
        batches=BatchRepository(db),
        app_settings=app_settings,
        exports=ExportRepository(db),
        profile_analyses=ProfileAnalysisRepository(db),
        profile_fetcher=profile_fetcher or anonymous_profile_fetcher(gate),
    )
    ctx.instagram_gate = gate
    ctx.worker = Worker(ctx, concurrency=settings.concurrency)
    ctx.import_runner = ImportRunner(ctx)
    ctx.export_runner = ExportRunner(ctx)
    ctx.profile_runner = ProfileAnalysisRunner(ctx)
    return ctx


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str = "") -> Response:
        if path == "api" or path.startswith("api/"):
            raise StarletteHTTPException(status_code=404)
        index = dist / "index.html"
        if not index.is_file():
            return HTMLResponse(FRONTEND_MISSING_PAGE)
        if path:
            try:
                candidate = safe_join(dist, path)
            except UnsafePathError:
                raise StarletteHTTPException(status_code=404) from None
            if candidate.is_file():
                return FileResponse(candidate)
            if Path(path).suffix:
                raise StarletteHTTPException(status_code=404)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def create_app(
    settings: Settings | None = None,
    *,
    session_provider: InstagramSessionProvider | None = None,
    downloader_factory: Callable[[], InstagramDownloader] | None = None,
    processor: MediaProcessor | None = None,
    start_worker: bool = True,
    profile_fetcher: Callable[[str, threading.Event | None], ProfileSnapshot] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    ctx = build_context(
        settings, session_provider=session_provider, downloader_factory=downloader_factory, processor=processor,
        profile_fetcher=profile_fetcher,
    )
    run_threads = start_worker and not settings.disable_worker

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_dir)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        ctx.storage.ensure_dirs()
        apply_migrations(ctx.db)
        run_recovery(ctx)
        recover_interrupted_exports(ctx)
        recover_profile_analyses(ctx)
        ctx.worker.set_concurrency(effective_concurrency(ctx))
        ctx.worker.set_download_concurrency(effective_download_concurrency(ctx))
        ctx.worker.load_pause_state()
        ctx.import_runner.restore_rate_limit_wait()
        if run_threads:
            ctx.worker.start()
            ctx.import_runner.start()
            ctx.export_runner.start()
            ctx.profile_runner.start()
        logger.info("Media Tool %s pronto em http://%s:%s", __version__, settings.host, settings.port)
        try:
            yield
        finally:
            ctx.profile_runner.stop()
            ctx.export_runner.stop()
            ctx.import_runner.stop()
            ctx.worker.stop()
            ctx.db.close_all()
            logger.info("Media Tool encerrado")

    app = FastAPI(
        title="Media Tool",
        version=__version__,
        description="API local do Media Tool (importação do Instagram, fila, processamento e biblioteca).",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.ctx = ctx
    install_error_handlers(app)
    # O upload de vídeo local tem teto próprio (max_download_bytes) e lê o corpo em streaming.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES, exempt_paths=(UPLOAD_PATH,))
    # Origens do Vite (5173) só em modo de desenvolvimento; em produção só o próprio backend.
    # Extensão do Chrome: só o ID fixo da nossa + IDs extras de MEDIA_TOOL_EXTENSION_IDS (docs/EXTENSION.md).
    install_security(app, port=settings.port, cors_origins=DEFAULT_CORS_ORIGINS if settings.dev_mode else (),
                     extension_ids=settings.env.get("MEDIA_TOOL_EXTENSION_IDS"))
    app.include_router(api_router)
    _mount_frontend(app, settings.frontend_dist)
    return app


app = create_app()
