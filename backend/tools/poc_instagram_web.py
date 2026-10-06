"""POC do provider web do Instagram sobre o app real (SQLite e storage reais do projeto).

Uso: ``.venv\\Scripts\\python.exe backend\\tools\\poc_instagram_web.py --profile <perfil> [--process]``

Passos (sem servidor, sem threads, sem nova tentativa):
1. monta o contexto real (build_context), aplica migrations e carrega a pausa do Instagram;
2. recusa rodar se o app estiver aberto ou se houver lote/job em andamento (a fila executada aqui é a real);
3. cria UM lote pelo serviço de importação com limit=1 e process=false, usando o provider web;
4. lista e executa o job de ingestão (download -> SHA-256 -> dedupe -> originals -> SQLite);
5. com ``--process``: cria o job de processamento com o preset "Anti-duplicidade", executa e confere que o
   SHA-256 do original não mudou.
Para imediatamente em qualquer erro (rate_limited, checkpoint_required, action_blocked, contract_changed,
session_invalid...), imprimindo só code/message/detail seguros. Relatório JSON sem segredos no stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from media_processor import sha256_file  # noqa: E402

from app.core.config import Settings, load_settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.db.connection import utc_now  # noqa: E402
from app.db.migrate import apply_migrations  # noqa: E402
from app.integrations.instagram.downloader import (  # noqa: E402
    CONTENT_TYPES,
    InstagramDownloader,
    InstagramError,
    RemoteMedia,
)
from app.integrations.instagram.web.provider import InstagramWebDownloader  # noqa: E402
from app.jobs.errors import safe_message  # noqa: E402
from app.main import build_context  # noqa: E402
from app.services import import_service  # noqa: E402
from app.services.context import AppContext  # noqa: E402
from app.services.settings_service import instagram_doc_id_overrides  # noqa: E402

PRESET_NAME = "Anti-duplicidade"
_DETAIL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TERMINAL = ("completed", "failed", "cancelled")


class _Recorder(InstagramDownloader):
    """Repassa ao provider e guarda o último erro (código, etapa e detail seguros) para o relatório."""

    def __init__(self, inner: InstagramDownloader, errors: list[dict[str, Any]]) -> None:
        self._inner = inner
        self._errors = errors

    def _record(self, operation: str, exc: InstagramError) -> None:
        self._errors.append(safe_error(exc, operation=operation))

    def test_session(self):
        return self._inner.test_session()

    def get_profile(self, username: str):
        return self._inner.get_profile(username)

    def resolve_post(self, shortcode: str):
        return self._inner.resolve_post(shortcode)

    def iter_profile_media(self, username, content, limit, *, cancel=None, **kwargs) -> Iterator[RemoteMedia]:
        try:
            yield from self._inner.iter_profile_media(username, content, limit, cancel=cancel, **kwargs)
        except InstagramError as exc:
            self._record("listing", exc)
            raise

    def refresh_page(self, profile, ref, *, cancel=None):
        try:
            return self._inner.refresh_page(profile, ref, cancel=cancel)
        except InstagramError as exc:
            self._record("refresh", exc)
            raise

    def download(self, item, dest_dir, *, cancel=None, progress=None):
        try:
            return self._inner.download(item, dest_dir, cancel=cancel, progress=progress)
        except InstagramError as exc:
            self._record("download", exc)
            raise


def safe_error(exc: BaseException, *, operation: str | None = None) -> dict[str, Any]:
    diagnostic = getattr(exc, "diagnostic", None)
    detail = diagnostic.get("detail") if isinstance(diagnostic, dict) else None
    stage = getattr(exc, "stage", None)
    return {
        "operation": operation,
        "code": str(getattr(exc, "code", None) or "internal_error"),
        "message": safe_message(getattr(exc, "message", None) or ""),
        "stage": stage if isinstance(stage, str) and _DETAIL_RE.match(stage) else None,
        "detail": detail if isinstance(detail, str) and _DETAIL_RE.match(detail) else None,
    }


def app_running(settings: Settings) -> bool:
    """True se o backend do Media Tool já escuta na porta configurada (a fila não pode ter dois executores)."""
    try:
        with socket.create_connection((settings.host, settings.port), timeout=0.5):
            return True
    except OSError:
        return False


def _web_factory(ctx: AppContext) -> InstagramDownloader:
    return InstagramWebDownloader(
        ctx.session_provider,
        max_download_bytes=ctx.settings.max_download_bytes,
        doc_id_overrides=instagram_doc_id_overrides(ctx.app_settings),
        env=ctx.settings.env,
    )


def _busy(ctx: AppContext) -> dict[str, int]:
    jobs = ctx.db.scalar(
        f"SELECT COUNT(*) FROM jobs WHERE status NOT IN ({', '.join('?' * len(_TERMINAL))})", _TERMINAL
    )
    batches = ctx.db.scalar("SELECT COUNT(*) FROM batches WHERE status = 'listing'")
    return {"open_jobs": int(jobs or 0), "listing_batches": int(batches or 0)}


def _job_error(job: dict[str, Any]) -> dict[str, Any]:
    return {"code": job.get("error_code") or job.get("status"), "message": safe_message(job.get("error_message") or "")}


def _finalize_on_error(ctx: AppContext, batch_id: int | None, job_ids: list[int], error: dict[str, Any]) -> None:
    """Em qualquer erro: jobs e lote da POC terminam como falhos, sem nova tentativa pendente (o worker do app
    não pode refazer o download depois)."""
    code = str(error.get("code") or "poc_stopped")
    message = safe_message(error.get("message") or "POC interrompida.")
    ids = set(job_ids)
    if batch_id is not None:
        ids.update(row["id"] for row in ctx.db.fetchall("SELECT id FROM jobs WHERE batch_id = ?", (batch_id,)))
    for job_id in sorted(ids):
        job = ctx.jobs.get(job_id)
        if job is not None and job["status"] not in _TERMINAL:
            ctx.jobs.update(job_id, status="failed", stage=None, progress=None, next_attempt_at=None,
                            finished_at=utc_now(), temp_path=None, error_code=code, error_message=message)
    if batch_id is not None:
        batch = ctx.batches.get(batch_id)
        if batch is not None and batch["status"] not in _TERMINAL:
            ctx.batches.update(batch_id, status="failed", error_code=code, error_message=message,
                               finished_at=utc_now())


def _stop(report: dict[str, Any], outcome: str, error: dict[str, Any] | None, code: int) -> tuple[int, dict]:
    report["outcome"] = outcome
    if error is not None:
        report["error"] = error
    return code, report


def run(
    args: argparse.Namespace,
    *,
    settings: Settings,
    session_provider: Any = None,
    web_factory: Callable[[AppContext], InstagramDownloader] = _web_factory,
    processor: Any = None,
    is_app_running: Callable[[Settings], bool] = app_running,
) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {"profile": args.profile, "content": args.content, "limit": 1, "provider": "web",
                              "process": bool(args.process), "steps": {}}
    if is_app_running(settings):
        return _stop(report, "app_running", {"code": "app_running", "message": (
            "O Media Tool está aberto. Feche o app (scripts\\stop.ps1) antes da POC: a fila é a mesma.")}, 2)

    errors: list[dict[str, Any]] = []
    owned: dict[str, Any] = {"batch_id": None, "job_ids": []}
    result: tuple[int, dict[str, Any]] | None = None
    try:
        result = _run_steps(args, settings, session_provider, web_factory, processor, report, errors, owned)
        return result
    except Exception as exc:  # noqa: BLE001 - qualquer erro para a POC com dados seguros
        result = _stop(report, "error", safe_error(exc) if isinstance(exc, InstagramError) else {
            "code": "internal_error", "message": f"Erro inesperado ({type(exc).__name__}); veja o log do app."}, 1)
        return result
    finally:
        ctx = owned.get("ctx")
        if ctx is not None:
            try:
                if result is None or result[0] != 0:
                    error = report.get("error") or {"code": "poc_stopped", "message": "POC interrompida."}
                    _finalize_on_error(ctx, owned["batch_id"], owned["job_ids"], error)
            finally:
                ctx.db.close_all()


def _run_steps(args, settings, session_provider, web_factory, processor, report, errors, owned):
    """Passos da POC; guarda em `owned` o contexto, o lote e os jobs criados (finalizados em erro)."""
    steps = report["steps"]
    ctx = build_context(settings, session_provider=session_provider, processor=processor)
    owned["ctx"] = ctx
    ctx.downloader_factory = lambda: _Recorder(web_factory(ctx), errors)
    configure_logging(settings.log_dir)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    ctx.storage.ensure_dirs()
    apply_migrations(ctx.db)
    ctx.worker.load_pause_state()
    status = ctx.session_provider.status()
    report["session"] = {"configured": bool(status.configured), "source": status.source}
    busy = _busy(ctx)
    if any(busy.values()):
        return _stop(report, "queue_busy", {"code": "queue_busy", "message": (
            "Há lotes ou jobs em andamento no banco. Abra o app e conclua ou cancele antes da POC."), **busy}, 2)

    # 1) Lote com limit=1 pelo serviço de importação (caminho interno; não exposto na API).
    try:
        batch_id = import_service.create_import(
            ctx, profile=args.profile, content_type=args.content, limit=1, preset_id=None, process=False
        )
    except AppError as exc:
        return _stop(report, exc.code, {"code": exc.code, "message": safe_message(exc.detail)}, 1)
    report["batch_id"] = batch_id
    owned["batch_id"] = batch_id

    # 2) Listagem (bootstrap + consulta de publicações).
    ctx.import_runner.run_next()
    batch = ctx.batches.get(batch_id)
    listing_error = next((e for e in errors if e["operation"] == "listing"), None)
    steps["bootstrap"] = "failed" if listing_error and listing_error["stage"] == "bootstrap" else (
        "unknown" if listing_error else "ok")
    steps["listing"] = "failed" if (listing_error or batch["status"] == "failed") else "ok"
    job_ids = [row["id"] for row in ctx.db.fetchall("SELECT id FROM jobs WHERE batch_id = ? ORDER BY id",
                                                      (batch_id,))]
    steps["found"] = len(job_ids)
    if listing_error or batch["status"] == "failed":
        error = listing_error or {"code": batch["error_code"], "message": safe_message(batch["error_message"])}
        return _stop(report, error["code"], error, 1)
    if not job_ids:
        return _stop(report, "no_items", {"code": "no_items", "message": "A listagem não trouxe mídias."}, 1)

    # 3) Ingestão (uma execução, sem nova tentativa).
    ctx.worker.run_pending(max_jobs=1)
    job = ctx.jobs.get(job_ids[0])
    steps["ingest"] = {"job_id": job["id"], "status": job["status"], "shortcode": job.get("source_post_id"),
                       "media_type": job.get("media_type"), "is_duplicate": bool(job.get("is_duplicate"))}
    if job["status"] != "completed" or not job.get("media_id"):
        error = next((e for e in errors if e["operation"] == "download"), None) or _job_error(job)
        return _stop(report, error["code"] or "ingest_failed", error, 1)
    media = ctx.media.get(job["media_id"])
    original = ctx.storage.resolve(media["original_path"])
    report["media"] = {"media_id": media["id"], "shortcode": media["source_post_id"],
                       "media_type": media["media_type"], "original_path": str(original),
                       "original_sha256": media["original_sha256"], "file_size": media["file_size"]}
    if not args.process:
        return _stop(report, "ok", None, 0)

    # 4) Processamento com o preset Anti-duplicidade.
    preset = next((p for p in ctx.presets.list() if p["name"] == PRESET_NAME), None)
    if preset is None:
        return _stop(report, "preset_not_found", {"code": "preset_not_found",
                                                  "message": f"Preset {PRESET_NAME!r} não encontrado."}, 2)
    process_job_id = import_service.process_media(ctx, media["id"], preset["id"])
    owned["job_ids"].append(process_job_id)
    ctx.worker.run_pending(max_jobs=1)
    pjob = ctx.jobs.get(process_job_id)
    steps["process"] = {"job_id": process_job_id, "status": pjob["status"], "preset": PRESET_NAME}
    if pjob["status"] != "completed" or not pjob.get("rendition_id"):
        error = _job_error(pjob)
        return _stop(report, error["code"] or "process_failed", error, 1)
    rendition = ctx.media.get_rendition(pjob["rendition_id"])
    processed = ctx.storage.resolve(rendition["processed_path"])
    probe = ctx.processors.get().probe(processed)
    report["processed"] = {
        "rendition_id": rendition["id"], "processed_path": str(processed),
        "processed_sha256": rendition["processed_sha256"],
        "processed_sha256_matches_file": sha256_file(processed) == rendition["processed_sha256"],
        "ffprobe": {"media_type": probe.media_type, "format": probe.format_name, "duration": probe.duration,
                    "width": probe.width, "height": probe.height, "fps": probe.fps,
                    "video_codec": probe.video_codec, "audio_codec": probe.audio_codec,
                    "has_audio": probe.has_audio},
    }
    unchanged = sha256_file(original) == media["original_sha256"]
    report["media"]["original_unchanged"] = unchanged
    if not unchanged:
        return _stop(report, "original_changed", {"code": "original_changed",
                                                  "message": "O SHA-256 do original mudou."}, 1)
    return _stop(report, "ok", None, 0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="POC do provider web do Instagram (1 mídia, app real).")
    parser.add_argument("--profile", required=True, help="perfil alvo (@usuario, usuario ou URL)")
    parser.add_argument("--content", default="all", choices=CONTENT_TYPES)
    parser.add_argument("--process", action="store_true", help="processa a mídia com o preset Anti-duplicidade")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    code, report = run(args, settings=load_settings())
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
