"""Executor das Legendas CapCut: thread própria, fila persistente, UM job por vez (o CapCut é exclusivo), FIFO.

Para cada job monta a especificação do núcleo (`app.integrations.capcut.CapCutJobSpec`) a partir do instantâneo de
opções gravado na criação e chama `ctx.capcut_processor` (testes) ou `process_video` do núcleo. O andamento de cada
etapa vira `stage`/`stage_progress`/`progress` (agregado 0..100) no banco; a transcrição é gravada assim que sai, e
uma nova tentativa a reaproveita.

Erros do núcleo são reconhecidos pelo `code`/`transient` (`CapCutError`), sem importar as classes: CapCut ocupado =>
`waiting` por 30 s sem gastar tentativa; transitório => nova tentativa com espera (30 s, 2 min, 5 min) até
`max_attempts`; o resto => `failed`. Cancelamento e encerramento chegam ao núcleo por um `threading.Event`; um job
interrompido pelo encerramento volta para a fila (e `recover_capcut_jobs` cobre a queda do processo).

Modo de exportação (`export_mode` das configurações, lido quando o job/prévia começa): `isolated` (padrão) ou
`interactive`; o modo usado fica em `options.export_mode` do job (e em `export_mode` da prévia). Sem fallback: se o
isolado não puder ser usado, o núcleo levanta `isolated_unavailable` (não transitório) e o job falha.

Lotes (docs/CAPCUT.md, "Lotes"): job com `edit_job_id` só é pego quando a edição (job `process` do worker) terminou; a
fonte passa a ser a rendition editada (edição com erro/cancelada => `failed` `edit_failed`, sem nova tentativa). No
modo `edit` o CapCut não é aberto: a rendition é copiada para a pasta do lote como `<nome>_editado.mp4`. A pausa da
fila também segura esses jobs.

Prévias do editor visual: ficam só em memória (fora do banco e da lista de jobs), passam na frente dos jobs comuns
(mesmo com a fila pausada), rodam no mesmo executor (um CapCut por vez) e não mexem em tentativas nem em nada dos jobs.
Uma prévia nova cancela as prévias ainda na fila (pendentes/em espera). CapCut ocupado/PC em uso => `waiting` com nova
checagem; qualquer outro erro => `failed` (sem nova tentativa). Saída em `<data_dir>/capcut/previews/<id>.mp4`; ficam
as 10 mais recentes.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import uuid
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
MSG_EDIT_MISSING = "A versão editada deste vídeo não está mais na Biblioteca."
MSG_EDIT_CANCELLED = "a edição foi cancelada."
MSG_COPY_FAILED = "Não foi possível copiar o vídeo editado para a pasta do lote."
EDIT_MODES = ("edit", "edit_caption")
PREVIEW_SAMPLE_TEXT = "Exemplo de legenda no seu estilo"
PREVIEW_KEEP_FILES = 10
PREVIEW_KEEP_ENTRIES = 30
PREVIEW_OPEN = ("pending", "waiting")


def stage_percent(stage: str | None, fraction: float | None) -> int:
    """Progresso agregado de uma etapa com `fraction` (0..1) concluída; etapa desconhecida não move o progresso."""
    if stage not in STAGE_RANGES:
        return 0
    start, end = STAGE_RANGES[stage]
    value = 0.0 if fraction is None else min(1.0, max(0.0, float(fraction)))
    return int(round(start + (end - start) * value))


def recover_capcut_jobs(ctx: AppContext) -> int:
    """Startup: `running` volta para `pending` (detalhe "retomado após interrupção") devolvendo a tentativa
    interrompida; job aberto com cancelamento pedido (dele ou do lote) termina cancelado; temporários de ZIP de lote
    que sobraram são apagados. Retorna quantos voltaram para a fila."""
    cancelled, resumed = CapCutRepository(ctx.db).recover()
    from app.services.capcut_batch_service import sweep_zip_temps

    if sweep_zip_temps(ctx):
        logger.info("Recovery: temporários de ZIP de lote das Legendas CapCut apagados")
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
        self._previews: dict[str, dict[str, Any]] = {}
        self._current_preview: str | None = None

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
        """Executa a próxima prévia vencida ou, sem prévia, o próximo job vencido até o fim. False = nada a fazer
        (fila vazia, pausada ou nada vencido)."""
        if self.run_next_preview():
            return True
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
            if job.get("capcut_batch_id") is not None:
                try:
                    self.repo.refresh_batch_finished(job["capcut_batch_id"])
                except Exception:  # noqa: BLE001 - só o `finished_at` do lote; a leitura seguinte corrige
                    logger.warning("Legendas CapCut: lote %s sem atualizar o fim", job["capcut_batch_id"],
                                   exc_info=True)
        return True

    def _now(self, offset: float = 0.0) -> str:
        return stamp(self._clock() + timedelta(seconds=offset))

    def _run(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        if self.repo.cancel_requested(job_id):
            self._finish_cancelled(job_id)
            return
        mode = job.get("mode") or "caption"
        if job.get("edit_job_id") is not None or mode in EDIT_MODES:
            edited = self._edited_source(job)
            if edited is None:
                return
            video, rendition = edited
            if mode == "edit":
                self._deliver_edit(job, video, rendition)
                return
        else:
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
        mode = capcut_service.export_mode(self.ctx)
        try:
            self.repo.update_job(job_id, options={**(job["options"] or {}), "export_mode": mode})
            spec = spec_cls(**self._spec_fields(job, video), export_mode=mode)
            result = processor(spec, cancel=self._cancel, on_stage=self._stage_callback(job_id),
                               on_transcript=self._transcript_callback(job_id))
        except Exception as exc:  # noqa: BLE001 - classificado abaixo; a fila segue
            self._handle_failure(job, exc)
            self._pause_if_user_wants_capcut(None, exc)
            return
        self._complete(job_id, result)
        self._pause_if_user_wants_capcut(result, None)

    def _edited_source(self, job: dict[str, Any]) -> tuple[Path, dict[str, Any]] | None:
        """(arquivo da rendition editada, rendition) do job de lote com edição. None = o job já foi encerrado aqui
        (edição com erro/cancelada, rendition ausente) ou voltou para a fila (edição aberta de novo)."""
        job_id = job["id"]
        edit = self.ctx.jobs.get(job["edit_job_id"]) if job.get("edit_job_id") else None
        rendition = None
        if edit is None:
            # Job de edição apagado (limpeza do histórico): a rendition com os mesmos params da edição.
            params = (job["options"] or {}).get("edit_params")
            if params is not None and job["media_id"]:
                rendition = self.ctx.media.find_rendition(job["media_id"], None, params)
        elif edit["status"] in ("failed", "cancelled"):
            reason = edit.get("error_message") or (MSG_EDIT_CANCELLED if edit["status"] == "cancelled"
                                                   else "erro desconhecido.")
            self._fail(job_id, "edit_failed", f"A edição deste vídeo falhou: {reason}")
            return None
        elif edit["status"] != "completed":
            # A edição voltou a ficar aberta depois da escolha do job: espera de novo, sem gastar a tentativa.
            self._requeue(job_id, status="pending", attempts=max(job["attempts"] - 1, 0), stage=None,
                          stage_progress=None, progress=0, detail=None)
            return None
        elif edit.get("rendition_id"):
            rendition = self.ctx.media.get_rendition(edit["rendition_id"])
        path = None
        if rendition is not None:
            try:
                path = self.ctx.storage.resolve(rendition["processed_path"])
            except Exception:  # noqa: BLE001 - caminho inválido no banco: mesmo efeito de arquivo ausente
                path = None
        if path is None or not path.is_file():
            self._fail(job_id, "source_missing", MSG_EDIT_MISSING)
            return None
        return path, rendition

    def _output_stem(self, job: dict[str, Any]) -> str:
        media = self.ctx.media.get(job["media_id"]) if job["media_id"] else None
        return Path((media or {}).get("original_filename") or job["source_path"]).stem

    def _deliver_edit(self, job: dict[str, Any], video: Path, rendition: dict[str, Any]) -> None:
        """Modo `edit`: copia a rendition para a pasta do lote (`<nome>_editado.mp4`, sem sobrescrever) e conclui."""
        job_id = job["id"]
        folder = Path((job["options"] or {})["output_dir"])
        suffix = video.suffix.lower() or ".mp4"
        stem = f"{self._output_stem(job)}_editado"
        part: Path | None = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{stem}{suffix}"
            counter = 2
            while target.exists():
                target = folder / f"{stem} ({counter}){suffix}"
                counter += 1
            part = folder / f".{target.name}.{job_id}.part"
            self.repo.update_job(job_id, stage="finalizing", progress=50, detail="Copiando o vídeo editado")
            shutil.copyfile(video, part)
            os.replace(part, target)
        except OSError:
            logger.warning("Legendas CapCut: job %s sem copiar a edição para a pasta do lote", job_id, exc_info=True)
            if part is not None:
                part.unlink(missing_ok=True)
            self._fail(job_id, "copy_failed", MSG_COPY_FAILED)
            return
        probe = {key: rendition.get(key) for key in ("width", "height", "duration", "fps")}
        probe["has_audio"] = bool(rendition.get("has_audio"))
        self._complete(job_id, SimpleNamespace(output_path=target, probe=probe, warnings=[], chunks=None,
                                               draft_name=None, words=None))

    def _spec_fields(self, job: dict[str, Any], video: Path) -> dict[str, Any]:
        options = job["options"] or {}
        stem = self._output_stem(job)
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

    def _pause_if_user_wants_capcut(self, result: Any, exc: BaseException | None) -> None:
        """Alguém tentou abrir o CapCut durante a exportação isolada (aviso marcado do núcleo): pausa a fila com o
        motivo, para o próximo job não reabrir o CapCut oculto e o dono conseguir usar o dele."""
        from app.integrations.capcut.jobspec import USER_CAPCUT_NOTICE

        hit = bool(exc is not None and getattr(exc, "user_capcut_attempt", False) is True)
        hit = hit or (result is not None and USER_CAPCUT_NOTICE in [str(w) for w in (getattr(result, "warnings", None) or [])])
        if hit and not capcut_service.queue_paused(self.ctx):
            capcut_service.set_queue_paused(self.ctx, True, USER_CAPCUT_NOTICE)
            logger.info("Legendas CapCut: fila pausada porque o dono tentou abrir o CapCut durante a exportação")

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

    # ---- prévias ----------------------------------------------------------------------------------------------
    def submit_preview(self, request: dict[str, Any]) -> dict[str, Any]:
        """Põe uma prévia na frente da fila; as prévias ainda não iniciadas são canceladas (só a última importa)."""
        preview_id = uuid.uuid4().hex[:12]
        entry = {"id": preview_id, "status": "pending", "stage": None, "progress": 0, "detail": None,
                 "error_code": None, "error_message": None, "output_path": None, "warnings": [],
                 "media_id": request.get("media_id"), "start_s": float(request.get("start_s") or 0.0),
                 "seconds": float(request.get("seconds") or 6.0), "not_before": None, "cancel_requested": False,
                 "export_mode": None, "request": dict(request)}
        entry["warnings"] = [str(w) for w in (request.get("warnings") or [])]  # avisos do design
        with self._lock:
            for other in self._previews.values():
                if other["status"] in PREVIEW_OPEN:
                    other.update(status="cancelled", detail="Substituída por uma prévia mais nova.", not_before=None)
            self._previews[preview_id] = entry
            for old in [k for k, v in self._previews.items() if v["status"] not in ("pending", "waiting", "running")]:
                if len(self._previews) <= PREVIEW_KEEP_ENTRIES:
                    break
                del self._previews[old]
            out = self._preview_copy(entry)
        self.wake()
        return out

    @staticmethod
    def _preview_copy(entry: dict[str, Any]) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in entry.items() if k != "request"}

    def get_preview(self, preview_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._previews.get(preview_id)
            return self._preview_copy(entry) if entry is not None else None

    def cancel_preview(self, preview_id: str) -> dict[str, Any] | None:
        """Na fila: cancelada na hora; em execução: sinaliza o núcleo. None = prévia desconhecida."""
        with self._lock:
            entry = self._previews.get(preview_id)
            if entry is None:
                return None
            if entry["status"] in PREVIEW_OPEN:
                entry.update(status="cancelled", not_before=None, detail=None)
            elif entry["status"] == "running":
                entry["cancel_requested"] = True
                if self._current_preview == preview_id:
                    self._cancel.set()
            return self._preview_copy(entry)

    def run_next_preview(self) -> bool:
        """Executa a prévia vencida mais antiga. False = nenhuma."""
        now = self._clock()
        with self._lock:
            entry = next((e for e in self._previews.values() if e["status"] in PREVIEW_OPEN
                          and (e["not_before"] is None or e["not_before"] <= now)), None)
            if entry is None:
                return False
            entry.update(status="running", stage=None, progress=0, detail=None, not_before=None,
                         export_mode=capcut_service.export_mode(self.ctx))
            self._current_preview = entry["id"]
            self._cancel = threading.Event()
            if self._stop.is_set():
                self._cancel.set()
        try:
            self._run_preview(entry)
        finally:
            with self._lock:
                self._current_preview = None
        return True

    def _set_preview(self, entry: dict[str, Any], **fields: Any) -> None:
        with self._lock:
            entry.update(fields)

    def _preview_spec(self, entry: dict[str, Any], video: Path) -> dict[str, Any]:
        req = entry["request"]
        data_dir = self.ctx.settings.data_dir
        return {
            "job_id": 0,
            "video_path": video,
            "draft_name": f"MT_preview_{entry['id']}",
            "output_dir": capcut_service.previews_dir(self.ctx),
            "output_name": entry["id"],
            "work_dir": data_dir / "capcut" / "work" / f"preview_{entry['id']}",
            "models_dir": data_dir / "models" / "whisper",
            "style": req.get("style"),
            "highlight": bool(req.get("highlight", True)),
            "highlight_color": req.get("highlight_color"),
            "max_words": int(req.get("max_words", 3)),
            "max_chars": int(req.get("max_chars", 24)),
            "whisper_model": req.get("whisper_model", "large-v3-turbo"),
            "language": "pt",
            "resolution": "720P",
            "bitrate": "recommended",
            # Mesmo fps das configurações (padrão 60, pedido do dono), não um valor fixo.
            "fps": str(req.get("fps") or capcut_service.get_settings(self.ctx).get("fps") or "60"),
            "keep_original_audio": False,   # sem remux do áudio original na prévia
            "keep_draft": False,
            "transcript": None,
            "clip_start": entry["start_s"],
            "clip_duration": entry["seconds"],
            "sample_text": PREVIEW_SAMPLE_TEXT,
        }

    def _run_preview(self, entry: dict[str, Any]) -> None:
        video = Path(entry["request"]["video_path"])
        if not video.is_file():
            self._set_preview(entry, status="failed", error_code="source_missing", error_message=MSG_SOURCE_MISSING)
            return
        try:
            core = capcut_service.load_core()
        except Exception:  # noqa: BLE001 - sem núcleo só um processador injetado (testes) consegue rodar
            core = None
        processor = self.ctx.capcut_processor or (core.process_video if core is not None else None)
        if processor is None:
            self._set_preview(entry, status="failed", error_code="deps_missing",
                              error_message=capcut_service.MSG_UNAVAILABLE)
            return
        spec_cls = getattr(core, "CapCutJobSpec", None) or SimpleNamespace

        def on_stage(stage: str, fraction: float | None, detail: str | None) -> None:
            with self._lock:
                entry.update(stage=stage, detail=detail,
                             progress=max(int(entry["progress"] or 0), stage_percent(stage, fraction)))

        try:
            spec = spec_cls(**self._preview_spec(entry, video), export_mode=entry["export_mode"])
            result = processor(spec, cancel=self._cancel, on_stage=on_stage, on_transcript=None)
        except Exception as exc:  # noqa: BLE001 - classificado abaixo; a fila segue
            self._preview_failure(entry, exc)
            self._pause_if_user_wants_capcut(None, exc)
            return
        self._set_preview(entry, status="completed", stage=None, progress=100, detail=None, error_code=None,
                          error_message=None, output_path=str(result.output_path),
                          warnings=[str(w) for w in (entry["request"].get("warnings") or [])]
                          + [str(w) for w in (result.warnings or [])])
        self._prune_previews()
        self._pause_if_user_wants_capcut(result, None)
        logger.info("Legendas CapCut: prévia %s concluída", entry["id"])

    def _preview_failure(self, entry: dict[str, Any], exc: Exception) -> None:
        code = getattr(exc, "code", None)
        if self._stop.is_set() or entry.get("cancel_requested") or code == CANCELLED_CODE:
            self._set_preview(entry, status="cancelled", stage=None, detail=None)
            return
        if code in WAIT_CODES:
            delay, text = WAIT_CODES[code]
            self._set_preview(entry, status="waiting", stage=None, progress=0, detail=text, error_code=None,
                              not_before=self._clock() + timedelta(seconds=delay or BUSY_RETRY_SECONDS))
            return
        if not isinstance(code, str) or not code:
            logger.error("Legendas CapCut: falha inesperada na prévia %s", entry["id"], exc_info=exc)
            self._set_preview(entry, status="failed", stage=None, error_code="unexpected_error",
                              error_message=MSG_UNEXPECTED)
            return
        self._set_preview(entry, status="failed", stage=None, error_code=code,
                          error_message=str(getattr(exc, "message", None) or exc or "Erro no CapCut."))
        logger.warning("Legendas CapCut: prévia %s falhou (%s)", entry["id"], code)

    def _prune_previews(self) -> None:
        """Ficam só os 10 vídeos de prévia mais recentes."""
        folder = capcut_service.previews_dir(self.ctx)
        try:
            files = sorted((f for f in folder.glob("*.mp4") if f.is_file()), key=lambda f: f.stat().st_mtime,
                           reverse=True)
        except OSError:
            return
        for old in files[PREVIEW_KEEP_FILES:]:
            try:
                old.unlink()
            except OSError:
                logger.warning("Legendas CapCut: prévia antiga não pôde ser apagada: %s", old.name)

    def _finish_cancelled(self, job_id: int) -> None:
        self.repo.update_job(job_id, status="cancelled", stage=None, stage_progress=None, detail=None,
                             next_attempt_at=None, wait_reason=None, finished_at=self._now())
        logger.info("Legendas CapCut: job %s cancelado", job_id)
