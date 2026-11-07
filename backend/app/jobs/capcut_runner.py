"""Executor das Legendas CapCut: thread própria, fila persistente, UM job por vez (o CapCut é exclusivo), FIFO.

Para cada job monta a especificação do núcleo (`app.integrations.capcut.CapCutJobSpec`) a partir do instantâneo de
opções gravado na criação e chama `ctx.capcut_processor` (testes) ou `process_video` do núcleo. O andamento de cada
etapa vira `stage`/`stage_progress`/`progress` (agregado 0..100) no banco; a transcrição é gravada assim que sai, e
uma nova tentativa a reaproveita.

Erros do núcleo são reconhecidos pelo `code`/`transient` (`CapCutError`), sem importar as classes: CapCut ocupado =>
`waiting` por 30 s sem gastar tentativa; transitório => nova tentativa com espera (30 s, 2 min, 5 min) até
`max_attempts`; o resto => `failed`. Cancelamento e encerramento chegam ao núcleo por um `threading.Event`; um job
interrompido pelo encerramento volta para a fila (e `recover_capcut_jobs` cobre a queda do processo).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from app.db.repositories.capcut import CapCutRepository
from app.jobs.profile_analysis_runner import stamp
from app.services import capcut_service

if TYPE_CHECKING:
    from app.services.context import AppContext

logger = logging.getLogger("media_tool.capcut")

# Faixa de cada etapa no progresso agregado (0..100). As etapas do CapCut (esperar, abrir, carregar) dividem 40-50.
STAGE_RANGES: dict[str, tuple[int, int]] = {
    "transcribing": (0, 35),
    "drafting": (35, 40),
    "waiting_capcut": (40, 42),
    "launching": (42, 46),
    "opening": (46, 50),
    "exporting": (50, 90),
    "finalizing": (90, 98),
    "cleaning": (98, 100),
}
BUSY_RETRY_SECONDS = 30.0
BACKOFF_SECONDS = (30.0, 120.0, 300.0)
IDLE_WAIT_SECONDS = 2.0
# Encerramento do app: tempo para o núcleo fechar o CapCut que abriu (WM_CLOSE + espera de até 40 s).
STOP_TIMEOUT_SECONDS = 45.0
CANCELLED_CODE = "cancelled"
BUSY_CODE = "capcut_busy"
DESKTOP_BUSY_CODE = "desktop_busy"
# Espera sem gastar tentativa: (segundos até a próxima checagem, texto da tela).
WAIT_CODES = {
    BUSY_CODE: (None, "O CapCut está em uso. Nova tentativa em 30 s."),
    DESKTOP_BUSY_CODE: (60, "O PC está em uso. A exportação no CapCut espera ele ficar livre (nova checagem em 1 min)."),
}
MSG_UNEXPECTED = "Erro inesperado ao legendar este vídeo. Os detalhes ficaram no log do Media Tool."
MSG_SOURCE_MISSING = "O arquivo original deste vídeo não está mais na Biblioteca."
MSG_RESUMED = "Retomado após interrupção"


def stage_percent(stage: str | None, fraction: float | None) -> int:
    """Progresso agregado de uma etapa com `fraction` (0..1) concluída; etapa desconhecida não move o progresso."""
    if stage not in STAGE_RANGES:
        return 0
    start, end = STAGE_RANGES[stage]
    value = 0.0 if fraction is None else min(1.0, max(0.0, float(fraction)))
    return int(round(start + (end - start) * value))


def recover_capcut_jobs(ctx: AppContext) -> int:
    """Startup: `running` volta para `pending` (detalhe "retomado após interrupção") devolvendo a tentativa
    interrompida; job aberto com cancelamento pedido termina cancelado. Retorna quantos voltaram para a fila."""
    cancelled, resumed = CapCutRepository(ctx.db).recover()
    if cancelled or resumed:
        logger.info("Recovery: %s job(s) de legenda de volta à fila, %s cancelado(s)", resumed, cancelled)
    return resumed


class CapCutRunner:
    def __init__(self, ctx: AppContext, *, clock: Callable[[], datetime] | None = None) -> None:
        self.ctx = ctx
        self.repo = CapCutRepository(ctx.db)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Protege (job atual, evento de cancelamento) contra a corrida com `cancel()`/`stop()`.
        self._lock = threading.Lock()
        self._current_id: int | None = None
        self._cancel = threading.Event()

    # ---- ciclo de vida -----------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="capcut-runner", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Sinaliza o cancelamento cooperativo e espera até `timeout` o job em andamento voltar para a fila (o núcleo
        fecha o CapCut que abriu); não bloqueia além disso."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._cancel.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def cancel(self, job_id: int) -> bool:
        """Interrompe o job em execução (o pedido já está gravado no banco por quem chama). False = este executor não
        está rodando esse job."""
        with self._lock:
            if self._current_id == job_id:
                self._cancel.set()
                return True
            return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_next()
            except Exception:  # noqa: BLE001 - a thread nunca morre por causa de um job
                logger.exception("Falha inesperada no executor das Legendas CapCut")
                worked = False
            if not worked:
                self._wake.wait(IDLE_WAIT_SECONDS)
                self._wake.clear()

    # ---- execução ----------------------------------------------------------------------------------------------
    def run_next(self) -> bool:
        """Executa o próximo job vencido até o fim. False = fila vazia, pausada ou nada vencido."""
        if capcut_service.queue_paused(self.ctx):
            return False
        with self._lock:
            job = self.repo.claim_next(stamp(self._clock()))
            if job is None:
                return False
            self._current_id = job["id"]
            self._cancel = threading.Event()
            if self._stop.is_set():
                self._cancel.set()
        try:
            self._run(job)
        finally:
            with self._lock:
                self._current_id = None
        return True

    def _now(self, offset: float = 0.0) -> str:
        return stamp(self._clock() + timedelta(seconds=offset))

    def _run(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        if self.repo.cancel_requested(job_id):
            self._finish_cancelled(job_id)
            return
        try:
            video = self.ctx.storage.resolve(job["source_path"])
        except Exception:  # noqa: BLE001 - caminho inválido no banco: mesmo efeito de arquivo ausente
            video = None
        if video is None or not video.is_file():
            self._fail(job_id, "source_missing", MSG_SOURCE_MISSING)
            return
        try:
            core = capcut_service.load_core()
        except Exception:  # noqa: BLE001 - sem núcleo só um processador injetado (testes) consegue rodar
            core = None
        processor = self.ctx.capcut_processor or (core.process_video if core is not None else None)
        if processor is None:
            self._fail(job_id, "deps_missing", capcut_service.MSG_UNAVAILABLE)
            return
        spec_cls = getattr(core, "CapCutJobSpec", None) or SimpleNamespace
        try:
            spec = spec_cls(**self._spec_fields(job, video))
            result = processor(spec, cancel=self._cancel, on_stage=self._stage_callback(job_id),
                               on_transcript=self._transcript_callback(job_id))
        except Exception as exc:  # noqa: BLE001 - classificado abaixo; a fila segue
            self._handle_failure(job, exc)
            return
        self._complete(job_id, result)

    def _spec_fields(self, job: dict[str, Any], video: Path) -> dict[str, Any]:
        options = job["options"] or {}
        media = self.ctx.media.get(job["media_id"]) if job["media_id"] else None
        stem = Path((media or {}).get("original_filename") or job["source_path"]).stem
        data_dir = self.ctx.settings.data_dir
        return {
            "job_id": job["id"],
            "video_path": video,
            "draft_name": f"MT_{job['id']}_m{job['media_id'] or 0}",
            "output_dir": Path(options["output_dir"]),
            "output_name": f"{stem}_legendado",
            "work_dir": capcut_service.work_dir(self.ctx, job["id"]),
            "models_dir": data_dir / "models" / "whisper",
            "style": options.get("style"),
            "highlight": bool(options.get("highlight", True)),
            "highlight_color": options.get("highlight_color"),
            "max_words": int(options.get("max_words", 3)),
            "max_chars": int(options.get("max_chars", 24)),
            "whisper_model": options.get("whisper_model", "large-v3-turbo"),
            "language": "pt",
            "resolution": options.get("resolution", "original"),
            "bitrate": options.get("bitrate", "recommended"),
            "fps": options.get("fps", "original"),
            "keep_original_audio": bool(options.get("keep_original_audio", True)),
            "keep_draft": bool(options.get("keep_draft", False)),
            "transcript": job["transcript"],
        }

    def _stage_callback(self, job_id: int) -> Callable[[str, float | None, str | None], None]:
        last: dict[str, Any] = {"key": None, "progress": 0}

        def on_stage(stage: str, fraction: float | None, detail: str | None) -> None:
            progress = max(last["progress"], stage_percent(stage, fraction))
            key = (stage, progress, detail)
            if key == last["key"]:
                return
            last.update(key=key, progress=progress)
            try:
                self.repo.update_job(job_id, stage=stage, stage_progress=fraction, progress=progress, detail=detail)
            except Exception:  # noqa: BLE001 - andamento é informativo; não interrompe a exportação
                logger.warning("Legendas CapCut: job %s sem gravar o andamento", job_id, exc_info=True)

        return on_stage

    def _transcript_callback(self, job_id: int) -> Callable[[list[dict[str, Any]]], None]:
        def on_transcript(words: list[dict[str, Any]]) -> None:
            self.repo.update_job(job_id, transcript=list(words))

        return on_transcript

    def _complete(self, job_id: int, result: Any) -> None:
        fields: dict[str, Any] = {
            "status": "completed", "stage": None, "stage_progress": None, "progress": 100, "detail": None,
            "error_code": None, "error_message": None, "next_attempt_at": None, "wait_reason": None,
            "output_path": str(result.output_path), "output_probe": dict(result.probe or {}),
            "warnings": [str(warning) for warning in (result.warnings or [])], "chunks": result.chunks,
            "draft_name": result.draft_name, "finished_at": self._now(),
        }
        if result.words:
            fields["transcript"] = list(result.words)
        self.repo.update_job(job_id, **fields)
        logger.info("Legendas CapCut: job %s concluído", job_id)

    def _requeue(self, job_id: int, **fields: Any) -> bool:
        """Volta para `pending`/`waiting` só se o cancelamento não foi pedido (condição na própria gravação); senão
        termina cancelado. True = voltou para a fila."""
        if self.repo.requeue_job(job_id, **fields):
            return True
        self._finish_cancelled(job_id)
        return False

    def _handle_failure(self, job: dict[str, Any], exc: Exception) -> None:
        job_id = job["id"]
        cancel_requested = self.repo.cancel_requested(job_id)
        if self._stop.is_set() and not cancel_requested:
            # Encerramento do app: o job volta para a fila sem gastar a tentativa.
            self._requeue(job_id, status="pending", attempts=max(job["attempts"] - 1, 0), stage=None,
                          stage_progress=None, progress=0, detail=MSG_RESUMED)
            return
        code = getattr(exc, "code", None)
        if cancel_requested or code == CANCELLED_CODE:
            self._finish_cancelled(job_id)
            return
        if not isinstance(code, str) or not code:
            logger.error("Legendas CapCut: falha inesperada no job %s", job_id, exc_info=exc)
            self._fail(job_id, "unexpected_error", MSG_UNEXPECTED)
            return
        message = str(getattr(exc, "message", None) or exc or "Erro no CapCut.")
        if code in WAIT_CODES:
            delay, text = WAIT_CODES[code]
            if not self._requeue(
                job_id, status="waiting", wait_reason=code, next_attempt_at=self._now(delay or BUSY_RETRY_SECONDS),
                attempts=max(job["attempts"] - 1, 0), stage=None, stage_progress=None, progress=0,
                detail=text):
                return
            logger.info("Legendas CapCut: job %s aguardando (%s)", job_id, code)
            return
        if getattr(exc, "transient", False) and job["attempts"] < job["max_attempts"]:
            delay = BACKOFF_SECONDS[min(job["attempts"], len(BACKOFF_SECONDS)) - 1]
            if not self._requeue(
                job_id, status="pending", next_attempt_at=self._now(delay), error_code=code, error_message=message,
                stage=None, stage_progress=None, progress=0,
                detail=f"Tentativa {job['attempts']} de {job['max_attempts']} falhou. Nova tentativa em breve."):
                return
            logger.warning("Legendas CapCut: job %s falhou (%s: %s); nova tentativa em %.0f s", job_id, code, message, delay)
            return
        self._fail(job_id, code, message)

    def _fail(self, job_id: int, code: str, message: str) -> None:
        self.repo.update_job(job_id, status="failed", error_code=code, error_message=message, stage=None,
                             stage_progress=None, detail=None, next_attempt_at=None, wait_reason=None,
                             finished_at=self._now())
        logger.warning("Legendas CapCut: job %s falhou (%s: %s)", job_id, code, message)

    def _finish_cancelled(self, job_id: int) -> None:
        self.repo.update_job(job_id, status="cancelled", stage=None, stage_progress=None, detail=None,
                             next_attempt_at=None, wait_reason=None, finished_at=self._now())
        logger.info("Legendas CapCut: job %s cancelado", job_id)
