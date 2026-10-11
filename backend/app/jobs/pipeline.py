"""Execução de um job (ingest/process) dentro de uma thread do worker.

Ingest: download (temp) -> sha256 -> dedup -> probe -> promoção atômica do original -> thumbnail. Download-only (sem
`params`, o padrão) NÃO chama ffprobe/FFmpeg (valida por assinatura, sem probe nem thumbnail) e termina em `completed`; com processamento pedido o job fica `downloaded` e o processamento é
etapa separada, reivindicada pelo pool de FFmpeg (docs/BATCH_ROBUSTNESS.md, seção 9, A3).
Process: processamento a partir do original existente. O original nunca é aberto para escrita.
"""

from __future__ import annotations

import errno
import logging
import os
import threading
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from media_processor import FFmpegNotFoundError, InvalidMediaError, ProcessingOptions, ProcessorError, sha256_file

from app.db.connection import parse_utc, utc_now
from app.db.repositories.common import canonical_json
from app.integrations.instagram.downloader import InstagramError, ListingRef, RemoteMedia
from app.integrations.instagram.validation import post_path_kind
from app.jobs.errors import Failure, JobCancelled, JobFailure, classify, is_cancellation
from app.services.instagram_gate import InstagramPaused
from app.services.storage import has_media_signature, mime_for, normalize_extension

if TYPE_CHECKING:
    from app.jobs.worker import Worker
    from app.services.context import AppContext

logger = logging.getLogger("media_tool.jobs")

# Progresso: no máximo 1 gravação a cada 2 s por job (mudança de etapa sempre grava; seção 6).
PROGRESS_MIN_INTERVAL = 2.0
PROGRESS_MIN_DELTA = 0.01

# Renovação de URL expirada (docs/BATCH_ROBUSTNESS.md, seção 2): no máximo uma renovação por página a cada 10 min.
REFRESH_MIN_INTERVAL = 600.0
NO_LISTING_REF_MESSAGE = (
    "O link de download desta mídia expirou e o item não tem referência de listagem para renová-lo. "
    "Importe o perfil de novo para baixar este item."
)
STILL_REFUSED_MESSAGE = "O link de download desta mídia continua recusado mesmo após renovar a listagem."
NOT_IN_PAGE_MESSAGE = "A mídia não apareceu mais na listagem do perfil ao renovar o link de download."
RECENT_REFRESH_FAILED_MESSAGE = (
    "A renovação do link desta página da listagem falhou há pouco; o item não foi tentado de novo para poupar o "
    "Instagram."
)
# Lote por link: a renovação pede a publicação de novo (resolve_post), sem página de listagem.
POST_STILL_REFUSED_MESSAGE = (
    "O link de download desta mídia continua recusado mesmo após consultar a publicação de novo."
)
RECENT_POST_REFRESH_FAILED_MESSAGE = (
    "A renovação do link desta publicação falhou há pouco; o item não foi tentado de novo para poupar o Instagram."
)
NOT_IN_POST_MESSAGE ="A mídia não apareceu mais na publicação ao renovar o link de download."
# Lote por link: a consulta de renovação devolveu OUTRA publicação (shortcode diferente do pedido).
POST_MISMATCH_MESSAGE = (
    "O Instagram respondeu com outra publicação ao renovar o link de download. Nenhum link foi atualizado."
)
# Lote por link, recusa em espera (`BatchRepository.links_hold`): depois de um `access_refused` de `resolve_post`
# (resolução de um link OU renovação de URL) nenhuma consulta do lote é feita até a ação do dono. Os rótulos entre
# aspas são os botões reais da interface.
LINKS_REFRESH_REFUSED_CODE = "access_refused"
# Mensagem do LOTE quando quem recebeu a recusa foi uma renovação: com o lote ainda resolvendo links / já fora disso.
LINKS_HOLD_LISTING_MESSAGE = (
    "O Instagram recusou a renovação de um link de download deste lote. Isso não indica sessão inválida. Não haverá "
    "nova tentativa automática: use \"Retomar importação\" para tentar de novo (uma consulta por vez) ou cancele o "
    "lote."
)
LINKS_HOLD_DONE_MESSAGE = (
    "O Instagram recusou uma consulta deste lote. Isso não indica sessão inválida. Não haverá nova tentativa "
    "automática. Se algum item do lote falhar, use \"Tentar falhas novamente\" para tentar de novo (uma consulta "
    "por vez)."
)
# Mensagem do JOB que precisava renovar e não consultou (falha com `access_refused` sem requisição).
LINKS_REFRESH_BLOCKED_LISTING_MESSAGE = (
    "O Instagram recusou uma consulta deste lote e o link de download desta mídia precisa ser renovado. Não haverá "
    "nova tentativa automática: use \"Retomar importação\" no lote; quando a importação dos links terminar, "
    "\"Tentar falhas novamente\" repete este item."
)
LINKS_REFRESH_BLOCKED_MESSAGE = (
    "O Instagram recusou uma consulta deste lote e o link de download desta mídia precisa ser renovado. Não haverá "
    "nova tentativa automática: use \"Tentar falhas novamente\" no lote para tentar de novo (uma consulta por vez)."
)
# Irmãos de uma renovação que falhou com estes códigos voltam pelo retry controlado (os demais falham na hora).
RETRYABLE_REFRESH_CODES = frozenset({"network_error", "timeout", "rate_limited", "request_auth_failed"})

# Disjuntor por lote (seção 9, A4): N falhas seguidas do mesmo código pausam o lote, com retomada automática.
BREAKER_REFRESH_FAILURES = 3
BREAKER_JOB_FAILURES = 20
BATCH_PAUSE_STEPS = (600.0, 1800.0, 3600.0, 7200.0)
BATCH_PAUSE_REASON = "repeated_failures"
_DISK_FULL_ERRNOS = {errno.ENOSPC}
_DISK_FULL_WINERRORS = {39, 112}  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
# Saída do FFmpeg quando o disco enche (strerror do ENOSPC no Windows e no POSIX).
_DISK_FULL_MARKERS = ("no space left on device", "not enough space on the disk", "there is not enough space")



class _AwaitPause(Exception):
    """A renovação de URL precisa do Instagram, mas há pausa vigente: o job volta à fila sem consumir tentativa."""


class _ReplayedFailure(JobFailure):
    """Irmão de uma renovação recente que falhou: repete o código registrado sem requisição nova. Não aplica pausa
    (só a requisição real que falhou aplica); respeita a vigente."""


class _RefreshBlocked(_ReplayedFailure):
    """Lote por link aguardando a ação do dono depois de um `access_refused`: o job falha sem requisição nenhuma e
    sem contar no disjuntor do lote (que pausaria e retomaria sozinho)."""


def _is_disk_full(exc: BaseException) -> bool:
    if isinstance(exc, OSError):
        return exc.errno in _DISK_FULL_ERRNOS or getattr(exc, "winerror", None) in _DISK_FULL_WINERRORS
    if isinstance(exc, ProcessorError):
        text = f"{exc} {getattr(exc, 'stderr', '') or ''}".lower()
        return any(marker in text for marker in _DISK_FULL_MARKERS)
    return False


def instagram_paused(worker: Any) -> bool:
    """Há pausa vigente de qualquer escopo (nenhuma requisição ao Instagram: listagem, renovação de URL)."""
    return worker is not None and bool(worker.downloads_paused())


_refresh_guard = threading.Lock()
_refresh_locks: dict[tuple, list] = {}  # chave -> [Lock, usuários]


@contextmanager
def _page_refresh_lock(key: tuple, cancel: threading.Event) -> Iterator[None]:
    """Trava em memória por (app, lote, página): uma renovação por vez; a entrada some quando ninguém a usa."""
    with _refresh_guard:
        entry = _refresh_locks.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        while not entry[0].acquire(timeout=0.1):
            if cancel.is_set():
                raise JobCancelled()
        try:
            yield
        finally:
            entry[0].release()
    finally:
        with _refresh_guard:
            entry[1] -= 1
            if entry[1] == 0:
                _refresh_locks.pop(key, None)


def links_query_lock(ctx: Any, batch_id: int, cancel: threading.Event):
    """Trava ÚNICA das consultas de publicação (`resolve_post`) de um lote por link: o runner (próximo link) e a
    renovação de URL a adquirem antes de consultar, conferem a recusa em espera sob ela e, se a consulta for
    recusada, armam a espera antes de soltá-la. Duas consultas do mesmo lote nunca ficam em voo ao mesmo tempo e a
    segunda sempre vê a recusa da primeira. Ordem das travas: esta primeiro, depois (dentro de `resolve_post`) o
    portão do Instagram e a espera do teto de páginas; nada adquire esta trava segurando aquelas. A espera é
    cancelável (`JobCancelled` quando `cancel` é acionado)."""
    return _page_refresh_lock((id(ctx), batch_id, "post"), cancel)


class JobExecutor:
    def __init__(self, ctx: AppContext, worker: Worker, job: dict[str, Any], cancel: threading.Event):
        self.ctx = ctx
        self.worker = worker
        self.job = job
        self.job_id: int = job["id"]
        self.cancel = cancel
        self._last_progress_time = 0.0
        self._last_progress: float | None = None
        # cancel_requested visto no banco pela última gravação de etapa/progresso (UPDATE ... RETURNING).
        self._cancel_in_db = False
        self.ref: dict[str, Any] | None = None  # job_remote_refs (URLs/cursor), carregado só no download
        # Pool que reivindicou o job (seção 9, A3): ingest reivindicado como download para em `downloaded`.
        self.phase = "download" if job["status"] == "downloading" else "process"
        # Lote por link: renovação recusada (`access_refused`) ou bloqueada à espera do dono não conta no disjuntor.
        self._skip_breaker = False

    # ---- ciclo de vida -----------------------------------------------------------------------------------------
    def run(self) -> None:
        job = self.job
        logger.info(
            "Job %s iniciado (kind=%s, status=%s, tentativa %s/%s)",
            self.job_id, job["kind"], job["status"], job["attempts"] + 1, job["max_attempts"],
        )
        try:
            self._check_cancel()
            if job["kind"] == "ingest":
                self._run_ingest()
            else:
                self._run_process_job()
        except BaseException as exc:  # noqa: BLE001 - nenhum erro de job derruba o worker
            if is_cancellation(exc) or self.cancel.is_set():
                self._handle_interruption()
            elif isinstance(exc, (_AwaitPause, InstagramPaused)):
                # InstagramPaused: o portão recusou a requisição (pausa publicada depois da conferência acima).
                self._wait_for_pause()
            elif _is_disk_full(exc):
                self._wait_for_disk()
            else:
                self._handle_failure(exc)
        finally:
            self.ctx.storage.remove_job_temp(self.job_id)
            if job["batch_id"] is not None:
                self.ctx.batches.refresh_status(job["batch_id"])
                if job["kind"] == "ingest":
                    # Backpressure: um ingest a menos pendente pode liberar a retomada da listagem do lote.
                    self.ctx.wake_import_runner()

    @property
    def _download_only(self) -> bool:
        """Ingest sem processamento (download puro): nunca espera o pool de FFmpeg nem chama ffprobe/FFmpeg
        (validação por assinatura; metadados técnicos e miniatura ficam para a análise/processamento opcional)."""
        return self.job["kind"] == "ingest" and self.job["params"] is None

    def _requeue_status(self) -> str:
        """Status de volta à fila: ingest com original registrado e processamento pedido retoma no pool de FFmpeg
        (`downloaded`); download-only e o resto voltam a `pending` (o pool de download conclui sem baixar de novo)."""
        job = self.job
        resumes_processing = job["kind"] == "ingest" and job.get("media_id") and job["params"] is not None
        return "downloaded" if resumes_processing else "pending"

    def _check_cancel(self) -> None:
        """Sem SELECT: o worker sinaliza o evento do job em execução, e cada gravação de etapa/progresso devolve o
        `cancel_requested` do banco (cobre o pedido que chega entre o claim e o registro do evento)."""
        if self.cancel.is_set() or self._cancel_in_db:
            self.cancel.set()
            raise JobCancelled()

    def _set(self, **fields: Any) -> None:
        if self.ctx.jobs.update(self.job_id, **fields):
            self._cancel_in_db = True
        self.job.update(fields)

    def _stage(self, stage: str, *, status: str | None = None, progress: float | None = None) -> None:
        fields: dict[str, Any] = {"stage": stage, "progress": progress, "heartbeat_at": utc_now()}
        if status:
            fields["status"] = status
        self._set(**fields)
        self._last_progress = progress
        logger.info("Job %s: etapa %s", self.job_id, stage)

    def _report_progress(self, value: float | None) -> None:
        if value is None:
            return
        value = min(1.0, max(0.0, float(value)))
        now = time.monotonic()
        last = self._last_progress
        if last is not None and abs(value - last) < PROGRESS_MIN_DELTA:
            return
        if now - self._last_progress_time < PROGRESS_MIN_INTERVAL:
            return
        self._last_progress_time = now
        self._last_progress = value
        if self.ctx.jobs.update(self.job_id, progress=value, heartbeat_at=utc_now()):
            self._cancel_in_db = True

    def _download_progress(self, done: int, total: int | None) -> None:
        if total:
            self._report_progress(done / total)

    def _complete(self, *, stage: str, rendition_id: int | None = None) -> None:
        media_id = self.job.get("media_id")
        media = self.ctx.media.get(media_id) if media_id else None
        if media is None or not self._original_path(media).is_file():
            raise JobFailure("original_missing", "Arquivo original não encontrado no storage.")
        fields: dict[str, Any] = {
            "status": "completed", "stage": stage, "progress": 1.0, "finished_at": utc_now(),
            "error_code": None, "error_message": None, "next_attempt_at": None, "temp_path": None,
        }
        if rendition_id is not None:
            fields["rendition_id"] = rendition_id
        self._finish(**fields)
        self._breaker_success()
        logger.info("Job %s concluído (etapa final %s)", self.job_id, stage)

    def _handle_interruption(self) -> None:
        if self.worker.stopping and not self.ctx.jobs.is_cancel_requested(self.job_id):
            # Encerramento do app: volta para a fila sem consumir tentativa.
            status = "downloaded" if self.job.get("media_id") and not self._download_only else "pending"
            self._set(status=status, stage="queued", progress=None, temp_path=None)
            logger.info("Job %s interrompido pelo encerramento; volta para %s", self.job_id, status)
            return
        self._finish(status="cancelled", stage=None, progress=None, finished_at=utc_now(), temp_path=None)
        logger.info("Job %s cancelado", self.job_id)

    def _finish(self, **fields: Any) -> None:
        """Estado terminal: concluído apaga a referência remota; falha/cancelamento apaga só as URLs."""
        self.ctx.jobs.finish(self.job_id, **fields)
        self.job.update(fields)

    def _wait_for_pause(self) -> None:
        """Renovação de URL bloqueada por pausa: volta a pending sem consumir tentativa nem falhar.

        A URL recusada é apagada: o claim (pausa de escopo session) só pega ingest com URL, e o de escopo
        instagram não pega ingest; quando a pausa termina o job é reivindicado e renova pela listagem (portão)."""
        self.ctx.jobs.clear_remote_urls(self.job_id)
        self._set(status="pending", stage="waiting_retry", progress=None, next_attempt_at=None, temp_path=None)
        logger.info("Job %s: renovação do link aguarda o fim da pausa do Instagram", self.job_id)

    def _wait_for_disk(self) -> None:
        """Disco cheio durante o job (seção 9, C2): pausa local disk_full; o job volta à fila sem consumir
        tentativa."""
        status = self._requeue_status()
        self._set(status=status, stage="queued", progress=None, next_attempt_at=None, temp_path=None)
        note =getattr(self.worker, "note_disk_full", None)
        if note is not None:
            note()
        logger.warning("Job %s: disco cheio; volta para %s sem consumir tentativa", self.job_id, status)

    def _handle_failure(self, exc: BaseException) -> None:
        failure: Failure = classify(exc)
        if failure.code == "internal_error":
            logger.exception("Job %s: erro inesperado", self.job_id)
        else:
            logger.warning("Job %s falhou: %s (%s)", self.job_id, failure.code, failure.message)
        paused = False if isinstance(exc, _ReplayedFailure) else self.worker.pause_for(failure.code)
        if self.ctx.jobs.is_cancel_requested(self.job_id):
            self._finish(status="cancelled", stage=None, progress=None, finished_at=utc_now(),
                         temp_path=None, error_code=failure.code, error_message=failure.message)
            logger.info("Job %s cancelado (cancelamento pedido durante a falha)", self.job_id)
            return
        if paused and failure.retryable:
            # Seção 9, M7: a pausa não consome tentativa; o claim só volta a pegar o job depois dela.
            status = self._requeue_status()
            self._set(status=status, stage="waiting_retry", progress=None, next_attempt_at=None,
                      error_code=failure.code, error_message=failure.message, temp_path=None)
            logger.info("Job %s aguarda o fim da pausa do Instagram (%s) sem consumir tentativa", self.job_id,
                        failure.code)
            return
        if not self._skip_breaker:
            self._breaker_failure(failure.code)
        attempts = int(self.job["attempts"]) + 1
        max_attempts = int(self.job["max_attempts"])
        if failure.retryable and attempts < max_attempts:
            delays = self.ctx.settings.retry_delays
            delay = delays[min(attempts - 1, len(delays) - 1)]
            # Ingest com original já registrado retoma do processamento; o resto volta para pending.
            status = self._requeue_status()
            self._set(
                status=status, stage="waiting_retry", progress=None, attempts=attempts,
                next_attempt_at=utc_now(delay), error_code=failure.code, error_message=failure.message,
                temp_path=None,
            )
            logger.info("Job %s agendado para nova tentativa em %ss", self.job_id, int(delay))
            return
        self._finish(
            status="failed", stage=None, progress=None, attempts=attempts, finished_at=utc_now(),
            error_code=failure.code, error_message=failure.message, next_attempt_at=None, temp_path=None,
        )

    # ---- disjuntor do lote (seção 9, A4) -----------------------------------------------------------------------
    def _breaker_failure(self, code: str) -> None:
        """Escopo pela etapa: falhas de download/renovação pausam o ingest do lote; de processamento, o FFmpeg."""
        batch_id = self.job.get("batch_id")
        scope = "process" if self.phase == "process" else "ingest"
        if batch_id is not None and self.ctx.jobs.record_batch_failure(
                batch_id, f"{scope}:{code}") >= BREAKER_JOB_FAILURES:
            self._pause_batch(batch_id, code, f"{BREAKER_JOB_FAILURES} jobs seguidos", scope=scope)

    def _breaker_success(self) -> None:
        if self.job.get("batch_id") is not None:
            self.ctx.jobs.record_batch_success(self.job["batch_id"])

    def _pause_batch(self, batch_id: int, code: str, why: str, *, scope: str = "ingest") -> None:
        until = self.ctx.jobs.pause_batch(batch_id, reason=BATCH_PAUSE_REASON, code=code, steps=BATCH_PAUSE_STEPS,
                                          scope=scope)
        logger.warning("Lote %s pausado (%s) até %s: %s falharam com %s (retomada automática)", batch_id, scope,
                       until, why, code)

    # ---- ingest ------------------------------------------------------------------------------------------------
    def _run_ingest(self) -> None:
        media = self._existing_media()
        if media is None:
            media = self._ingest_new_or_duplicate()
        self._check_cancel()
        if not self._download_only and not media.get("thumbnail_path"):
            self._ensure_thumbnail(media["id"], self._original_path(media))
        if self.job["params"] is None:
            # Download-only: conclui assim que o original está salvo, validado e registrado, sem passar por
            # `downloaded` (estado de espera do pool de FFmpeg).
            self._stage("finalizing")
            self._complete(stage="skipped" if self.job["is_duplicate"] else "done")
            return
        if self.phase == "download":
            # Seção 9, A3: o processamento é reivindicado depois pelo pool de FFmpeg. (Na etapa do pool de FFmpeg o
            # job já está `processing` pelo claim; não volta a `downloaded` no meio.)
            self._set(status="downloaded", stage="queued", progress=None)
            self._breaker_success()
            logger.info("Job %s: download concluído; aguarda o processamento", self.job_id)
            return
        self._process(media)

    def _existing_media(self) -> dict[str, Any] | None:
        media_id = self.job.get("media_id")
        if media_id:
            media = self.ctx.media.get(media_id)
            if media is not None:
                return media
            self._set(media_id=None, is_duplicate=0, duplicate_reason=None)
        if self.job.get("source_post_id"):
            media = self.ctx.media.find_by_source(
                self.job["source_platform"] or "instagram", self.job["source_post_id"],
                int(self.job["source_media_index"] or 0),
            )
            if media is not None:
                # Dependente de um job de outro lote (seção 9) mantém o motivo `duplicate_in_flight`.
                in_flight = self.job.get("duplicate_reason") == "duplicate_in_flight"
                reason = "duplicate_in_flight" if in_flight else "source_id"
                self._set(media_id=media["id"], is_duplicate=1, duplicate_reason=reason)
                logger.info("Job %s: mídia já existente pela origem (media %s), sem download", self.job_id, media["id"])
                return media
        if self.job.get("duplicate_reason") == "duplicate_in_flight":
            # O primário falhou ou foi cancelado sem gerar a mídia: este job baixa sozinho, como um job normal.
            self._set(is_duplicate=0, duplicate_reason=None)
        return None

    def _remote_media(self) -> RemoteMedia:
        job = self.job
        source_url = job["source_url"] or ""
        return RemoteMedia(
            source_platform=job["source_platform"] or "instagram",
            source_profile=job["source_profile"] or "",
            source_post_id=job["source_post_id"] or "",
            post_id=job["post_id"],
            media_id=job["media_id_remote"],
            source_media_index=int(job["source_media_index"] or 0),
            media_type=job["media_type"] or "image",
            is_reel="/reel/" in source_url,
            source_url=source_url,
            media_url=self._ref().get("media_url") or "",
            thumbnail_url=self._ref().get("thumbnail_url"),
            width=None,
            height=None,
            duration=None,
            taken_at=job["taken_at"],
            extension=None,
        )

    def _ref(self) -> dict[str, Any]:
        if self.ref is None:
            self.ref = self.ctx.jobs.get_remote_ref(self.job_id)
        return self.ref

    def _ingest_new_or_duplicate(self) -> dict[str, Any]:
        storage = self.ctx.storage
        temp_dir = storage.job_temp_dir(self.job_id, fresh=True)
        self._set(temp_path=storage.relative(temp_dir))
        self._stage("downloading", status="downloading")
        self._check_cancel()  # pedido visto pela gravação da etapa: não começa o download
        downloader = self.ctx.downloader_factory()
        item = self._remote_media()
        if not item.media_url:
            # URL já limpa (ex.: job terminado e repetido): só a renovação pela listagem pode obter outra.
            downloaded = self._download_after_expired(downloader, item.media_url, temp_dir)
        else:
            try:
                downloaded = self._download(downloader, item, temp_dir)
            except InstagramError as exc:
                if exc.code != "media_url_expired":
                    raise
                self._check_cancel()
                downloaded = self._download_after_expired(downloader, item.media_url, temp_dir)
        self._check_cancel()
        if not downloaded.is_file() or not downloaded.resolve().is_relative_to(temp_dir.resolve()):
            raise JobFailure("download_failed", "O download não produziu um arquivo válido.")

        self._stage("hashing")
        sha256 = sha256_file(downloaded)
        size = downloaded.stat().st_size
        self._check_cancel()

        existing = self.ctx.media.find_by_sha(sha256)
        if existing is not None:
            return self._link_sha_duplicate(existing, downloaded)

        self._stage("probing")
        probe = self._validated_probe(downloaded)
        media_type = probe.media_type if probe is not None else (self.job["media_type"] or "image")
        ext = normalize_extension(downloaded.suffix, media_type)
        self._check_cancel()

        with self.ctx.ingest_lock:
            existing = self.ctx.media.find_by_sha(sha256)
            if existing is not None:
                return self._link_sha_duplicate(existing, downloaded)
            original = storage.promote_original(downloaded, sha256, ext)
            try:
                media_id = self.ctx.media.insert(
                    {
                        "source_platform": self.job["source_platform"] or "instagram",
                        "source_profile": self.job["source_profile"],
                        "source_url": self.job["source_url"],
                        "source_post_id": self.job["source_post_id"],
                        "source_media_index": int(self.job["source_media_index"] or 0),
                        "post_id": self.job["post_id"],
                        "media_id_remote": self.job["media_id_remote"],
                        "media_type": media_type,
                        "original_filename": downloaded.name,
                        "original_path": storage.relative(original),
                        "original_sha256": sha256,
                        "file_size": size,
                        "duration": probe.duration if probe else None,
                        "width": probe.width if probe else None,
                        "height": probe.height if probe else None,
                        "fps": probe.fps if probe else None,
                        "has_audio": int(probe.has_audio) if probe else None,
                        "mime_type": mime_for(original),
                        "taken_at": self.job["taken_at"],
                    }
                )
            except BaseException:
                original.unlink(missing_ok=True)
                raise
            self._set(media_id=media_id, temp_path=None)
        logger.info("Job %s: original registrado (media %s, sha %s)", self.job_id, media_id, sha256[:12])

        if not self._download_only:
            self._stage("thumbnail")
            self._ensure_thumbnail(media_id, original)
        return self.ctx.media.get(media_id)

    def _download(self, downloader, item: RemoteMedia, temp_dir: Path) -> Path:
        return Path(downloader.download(item, temp_dir, cancel=self.cancel, progress=self._download_progress))

    # ---- renovação de URL expirada (seção 2) -------------------------------------------------------------------
    def _download_after_expired(self, downloader, used_url: str, temp_dir: Path) -> Path:
        """URL recusada: renova a página da listagem (uma vez por página) e tenta este download UMA vez de novo."""
        job = self.job
        batch_id = job.get("batch_id")
        ref = self._ref()
        page_index = ref.get("listing_page_index") or job.get("listing_page_index")
        page_size = ref.get("listing_page_size") or job.get("listing_page_size")
        cursor = ref.get("listing_cursor")
        if batch_id is not None and page_index is None and self._links_batch(batch_id):
            # Lote por link: não há página de listagem; a publicação é pedida de novo (resolve_post).
            return self._download_after_expired_post(downloader, used_url, temp_dir)
        if batch_id is None or page_index is None or page_size is None or (int(page_index) > 1 and not cursor):
            raise JobFailure("content_unavailable", NO_LISTING_REF_MESSAGE)
        page_index = int(page_index)
        logger.info("Job %s: link de download expirado; renovação da página %s do lote %s", self.job_id,
                    page_index, batch_id)
        with _page_refresh_lock((id(self.ctx), batch_id, page_index), self.cancel):
            self._check_cancel()
            current = self.ctx.jobs.get_remote_ref(self.job_id)
            if current.get("media_url") and current["media_url"] != used_url:
                logger.info("Job %s: link já renovado por outro job da mesma página", self.job_id)
            else:
                now = parse_utc(utc_now())
                page = self.ctx.jobs.listing_page(batch_id, page_index) or {}
                page_at = parse_utc(page.get("last_refresh_at"))
                page_recent = page_at is not None and (now - page_at).total_seconds() < REFRESH_MIN_INTERVAL
                if page_recent and not page.get("last_refresh_ok") and not instagram_paused(self.worker):
                    # Seção 9, A1: renovação recente desta página falhou; mesmo código, sem nova requisição.
                    code = page.get("last_refresh_code") or "content_unavailable"
                    raise _ReplayedFailure(code, RECENT_REFRESH_FAILED_MESSAGE,
                                           retryable=code in RETRYABLE_REFRESH_CODES)
                last = parse_utc(self.ctx.jobs.page_refreshed_at(batch_id, page_index))
                if page_recent and page.get("last_refresh_ok") and (last is None or page_at > last):
                    last = page_at
                if last is not None and (now - last).total_seconds() < REFRESH_MIN_INTERVAL:
                    raise JobFailure("content_unavailable", STILL_REFUSED_MESSAGE)
                # Seção 8: nenhuma requisição ao Instagram durante pausa (o job aguarda sem consumir tentativa).
                # O portão global é adquirido pelo cliente HTTP do Instagram, por requisição.
                if instagram_paused(self.worker):
                    raise _AwaitPause()
                self._check_cancel()
                self._refresh_page(downloader, ListingRef(page_index=page_index, cursor=cursor,
                                                          page_size=int(page_size)))
                current = self.ctx.jobs.get_remote_ref(self.job_id)
        self.ref = current
        self._check_cancel()
        try:
            return self._download(downloader, self._remote_media(), temp_dir)
        except InstagramError as exc:
            if exc.code != "media_url_expired":
                raise
            raise JobFailure("content_unavailable", STILL_REFUSED_MESSAGE) from None

    def _refresh_page(self, downloader, ref: ListingRef) -> None:
        """UMA chamada de renovação; grava as URLs novas de todos os jobs não terminais do lote presentes na página.

        Erros da renovação sobem com a classificação normal (pausa etc.), sem nova tentativa aqui.
        """
        job = self.job
        try:
            items = downloader.refresh_page(job["source_profile"] or "", ref, cancel=self.cancel)
        except BaseException as exc:
            if not (is_cancellation(exc) or self.cancel.is_set() or isinstance(exc, InstagramPaused)):
                # Seção 9, A1/A4: toda tentativa fica registrada; 3 falhas seguidas do mesmo código pausam o lote.
                code = classify(exc).code
                streak = self.ctx.jobs.record_refresh(job["batch_id"], ref.page_index, ok=False, code=code,
                                                      at=utc_now())
                if streak >= BREAKER_REFRESH_FAILURES:
                    self._pause_batch(job["batch_id"], code, f"{BREAKER_REFRESH_FAILURES} renovações seguidas")
            raise
        self.ctx.jobs.record_refresh(job["batch_id"], ref.page_index, ok=True, code=None, at=utc_now())
        self.worker.note_auth_success()  # renovação ok: resposta autenticada com sucesso (seção 7)
        self._check_cancel()
        now = utc_now()
        urls = [
            (item.source_post_id, int(item.source_media_index or 0), item.media_url, item.thumbnail_url)
            for item in items
            if item.media_url
        ]
        updated = self.ctx.jobs.apply_refreshed_urls(job["batch_id"], urls, now)
        logger.info("Lote %s: página %s renovada (%s jobs com link novo)", job["batch_id"], ref.page_index, updated)
        key = (job["source_post_id"], int(job["source_media_index"] or 0))
        if key not in {(post_id, index) for post_id, index, _, _ in urls}:
            # A página conta como renovada agora (evita nova chamada para os irmãos dentro do intervalo mínimo).
            self._set(media_url_refreshed_at=now)
            raise JobFailure("content_unavailable", NOT_IN_PAGE_MESSAGE)

    # ---- renovação de URL expirada em lote por link (docs/CONTRACTS.md, seção 8.3) -----------------------------
    def _links_batch(self, batch_id: int) -> bool:
        batch = self.ctx.batches.get(batch_id)
        return batch is not None and batch.get("import_mode") == "links"

    def _batch_listing(self, batch_id: int) -> bool:
        batch = self.ctx.batches.get(batch_id)
        return batch is not None and batch["status"] == "listing"

    def _download_after_expired_post(self, downloader, used_url: str, temp_dir: Path) -> Path:
        """URL recusada (ou já apagada) num lote por link: pede a publicação de novo (uma vez por publicação) e tenta
        este download UMA vez de novo.

        `access_refused` (docs/CONTRACTS.md, seção 8.3): com a recusa em espera do lote armada
        (`BatchRepository.links_hold`; armada por qualquer `access_refused` de `resolve_post`, na resolução de um
        link ou numa renovação), nenhuma renovação consulta o Instagram até a ação do dono. A trava é a do LOTE
        (`links_query_lock`), a mesma do runner: a recusa de uma consulta é vista pela seguinte antes de ela sair."""
        job = self.job
        batch_id = job["batch_id"]
        shortcode = job["source_post_id"] or ""
        logger.info("Job %s: link de download expirado; renovação da publicação no lote %s", self.job_id, batch_id)
        with links_query_lock(self.ctx, batch_id, self.cancel):
            self._check_cancel()
            current = self.ctx.jobs.get_remote_ref(self.job_id)
            if current.get("media_url") and current["media_url"] != used_url:
                logger.info("Job %s: link já renovado por outro job da mesma publicação", self.job_id)
            else:
                if self.ctx.batches.links_hold(batch_id):
                    self._skip_breaker = True
                    logger.warning("Job %s: renovação do link não consultada (lote %s aguarda ação manual depois de "
                                   "%s)", self.job_id, batch_id, LINKS_REFRESH_REFUSED_CODE)
                    raise _RefreshBlocked(
                        LINKS_REFRESH_REFUSED_CODE,
                        LINKS_REFRESH_BLOCKED_LISTING_MESSAGE if self._batch_listing(batch_id)
                        else LINKS_REFRESH_BLOCKED_MESSAGE)
                # Toda tentativa de renovação da publicação fica registrada em `listing_pages`, com a posição do link
                # no lugar do índice de página (lote por link não tem páginas): mesmas proteções de `_refresh_page`.
                position = self.ctx.batches.link_position(batch_id, shortcode)
                if position is None:
                    # Publicação sem link correspondente no lote: chave própria (negativa), nunca dividida com outra.
                    position = -(zlib.crc32(shortcode.encode("utf-8")) + 1)
                now = parse_utc(utc_now())
                record = self.ctx.jobs.listing_page(batch_id, position) or {}
                record_at = parse_utc(record.get("last_refresh_at"))
                recent = record_at is not None and (now - record_at).total_seconds() < REFRESH_MIN_INTERVAL
                if (recent and not record.get("last_refresh_ok") and not instagram_paused(self.worker)
                        and record.get("last_refresh_code") != LINKS_REFRESH_REFUSED_CODE):
                    # Renovação recente desta publicação falhou: mesmo código, sem nova requisição (os itens irmãos
                    # do carrossel não repetem a consulta). `access_refused` não entra aqui: quem segura é a recusa
                    # em espera do lote (acima); desarmada pelo dono, este job faz a única consulta nova.
                    code = record.get("last_refresh_code") or "content_unavailable"
                    raise _ReplayedFailure(code, RECENT_POST_REFRESH_FAILED_MESSAGE,
                                           retryable=code in RETRYABLE_REFRESH_CODES)
                last = parse_utc(self.ctx.jobs.post_refreshed_at(batch_id, shortcode))
                if recent and record.get("last_refresh_ok") and (last is None or record_at > last):
                    last = record_at
                if last is not None and (now - last).total_seconds() < REFRESH_MIN_INTERVAL:
                    raise JobFailure("content_unavailable", POST_STILL_REFUSED_MESSAGE)
                # Seção 8: nenhuma requisição ao Instagram durante pausa (o job aguarda sem consumir tentativa).
                if instagram_paused(self.worker):
                    raise _AwaitPause()
                self._check_cancel()
                self._refresh_post(downloader, shortcode, position)
                current = self.ctx.jobs.get_remote_ref(self.job_id)
        self.ref = current
        self._check_cancel()
        try:
            return self._download(downloader, self._remote_media(), temp_dir)
        except InstagramError as exc:
            if exc.code != "media_url_expired":
                raise
            raise JobFailure("content_unavailable", POST_STILL_REFUSED_MESSAGE) from None

    def _refresh_post(self, downloader, shortcode: str, position: int) -> None:
        """UMA chamada de `resolve_post`; grava as URLs novas de todos os jobs não terminais da publicação no lote.
        Erros sobem com a classificação normal (pausa etc.), sem nova tentativa aqui."""
        job = self.job
        try:
            # O tipo do caminho (/p/ ou /reel/) vem da URL de origem gravada no job.
            items = downloader.resolve_post(shortcode, cancel=self.cancel,
                                            path_kind=post_path_kind(job.get("source_url") or ""))
            if any(item.source_post_id != shortcode for item in items):
                # Resposta de OUTRA publicação (comparação exata): nenhum job recebe URL dela. O provider web já
                # recusa antes (`post_mismatch`); isto cobre os demais. Conta como renovação falha (abaixo).
                raise JobFailure("contract_changed", POST_MISMATCH_MESSAGE)
        except BaseException as exc:
            refused = isinstance(exc, InstagramError) and exc.code == LINKS_REFRESH_REFUSED_CODE
            if refused:
                # Recusa: arma a espera do lote AGORA, ainda sob a trava (o runner e as outras renovações a veem
                # antes de consultar). Vale mesmo com o job cancelado durante a consulta: a recusa aconteceu.
                self._skip_breaker = True
                self.ctx.batches.arm_links_hold(
                    job["batch_id"],
                    LINKS_HOLD_LISTING_MESSAGE if self._batch_listing(job["batch_id"]) else LINKS_HOLD_DONE_MESSAGE)
                self.ctx.wake_import_runner()
                logger.warning("Lote %s: renovação de link recusada (%s); aguardando ação manual, sem nova "
                               "consulta automática", job["batch_id"], LINKS_REFRESH_REFUSED_CODE)
            if not (is_cancellation(exc) or self.cancel.is_set() or isinstance(exc, InstagramPaused)):
                # Como em `_refresh_page`: a tentativa fica registrada; 3 falhas seguidas do mesmo código pausam o
                # lote (menos a recusa: a pausa do lote retomaria sozinha).
                code = classify(exc).code
                streak = self.ctx.jobs.record_refresh(job["batch_id"], position, ok=False, code=code, at=utc_now())
                if refused:
                    pass
                elif streak >= BREAKER_REFRESH_FAILURES:
                    self._pause_batch(job["batch_id"], code, f"{BREAKER_REFRESH_FAILURES} renovações seguidas")
            raise
        self.ctx.jobs.record_refresh(job["batch_id"], position, ok=True, code=None, at=utc_now())
        self._check_cancel()
        if getattr(downloader, "resolve_post_authenticated", True):
            # Publicação entregue por consulta autenticada (seção 7). O documento público (provider web) é lido
            # SEM a sessão do dono e não prova nada sobre ela.
            self.worker.note_auth_success()
        now = utc_now()
        urls = [
            (item.source_post_id, int(item.source_media_index or 0), item.media_url, item.thumbnail_url)
            for item in items
            if item.media_url
        ]
        updated = self.ctx.jobs.apply_refreshed_urls(job["batch_id"], urls, now)
        logger.info("Lote %s: publicação renovada (%s jobs com link novo)", job["batch_id"], updated)
        key = (job["source_post_id"], int(job["source_media_index"] or 0))
        if key not in {(post_id, index) for post_id, index, _, _ in urls}:
            # A publicação conta como renovada agora (evita nova chamada para os irmãos no intervalo mínimo).
            self._set(media_url_refreshed_at=now)
            raise JobFailure("content_unavailable", NOT_IN_POST_MESSAGE)

    def _link_sha_duplicate(self, existing: dict[str, Any], downloaded: Path) -> dict[str, Any]:
        downloaded.unlink(missing_ok=True)
        self._set(media_id=existing["id"], is_duplicate=1, duplicate_reason="sha256", temp_path=None)
        logger.info("Job %s: conteúdo idêntico à media %s (sha256); temporário descartado", self.job_id, existing["id"])
        return existing

    def _validated_probe(self, path: Path):
        """Rejeita lixo (ex.: página HTML de erro salva como .jpg) antes de virar original.

        Com ffprobe: exige stream de vídeo/imagem com dimensões. Sem ffprobe: exige assinatura (magic bytes)
        de mídia conhecida e registra sem metadados técnicos.
        """
        invalid = JobFailure("invalid_media", "O arquivo baixado não é uma mídia válida.")
        if self._download_only:
            # Download puro: zero ffprobe. A ausência de metadados técnicos é esperada, sem aviso por job.
            if not has_media_signature(path):
                raise invalid
            return None
        try:
            probe = self.ctx.processors.get().probe(path)
        except FFmpegNotFoundError:
            if not has_media_signature(path):
                raise invalid from None
            logger.warning("Job %s: ffprobe ausente; mídia registrada sem metadados técnicos", self.job_id)
            return None
        except InvalidMediaError as exc:
            if isinstance(exc.__cause__, OSError) and has_media_signature(path):
                # O ffprobe existe mas não pôde ser executado (indisponível): não é defeito da mídia baixada.
                logger.warning("Job %s: ffprobe indisponível; mídia registrada sem metadados técnicos", self.job_id)
                return None
            raise invalid from exc
        if not probe.has_video or not probe.width or not probe.height:
            raise invalid
        return probe

    def _ensure_metadata(self, media: dict[str, Any]) -> None:
        """Análise opcional: preenche os metadados técnicos de uma mídia baixada em download puro (sem ffprobe).
        Nunca falha o job; sem ffprobe a mídia continua sem metadados."""
        if media.get("width") is not None:
            return
        try:
            probe = self.ctx.processors.get().probe(self._original_path(media))
        except Exception as exc:  # noqa: BLE001 - metadados são opcionais
            logger.info("Job %s: metadados técnicos não obtidos para media %s: %s", self.job_id, media["id"],
                        getattr(exc, "code", exc.__class__.__name__))
            return
        if not probe.width or not probe.height:
            return
        self.ctx.media.update(media["id"], duration=probe.duration, width=probe.width, height=probe.height,
                              fps=probe.fps, has_audio=int(probe.has_audio))

    def _ensure_thumbnail(self, media_id: int, original: Path) -> None:
        """Reaproveita `thumbs/<media_id>.jpg` se já existe; senão gera em temp e promove sem sobrescrever."""
        storage = self.ctx.storage
        target = storage.thumb_path(media_id)
        if not target.is_file():
            processor = self.ctx.processors.get()
            if not self.ctx.processors.ffmpeg_info().found or not original.is_file():
                return
            temp = storage.job_temp_dir(self.job_id) / f"thumb-{media_id}.jpg"
            temp.unlink(missing_ok=True)
            try:
                processor.make_thumbnail(original, temp)
                storage.promote_thumbnail(temp, media_id)
            except Exception as exc:  # noqa: BLE001 - falha de thumbnail não falha o job
                logger.warning("Job %s: thumbnail não gerada para media %s: %s", self.job_id, media_id,
                               getattr(exc, "code", exc.__class__.__name__))
                return
        if target.is_file():
            self.ctx.media.update(media_id, thumbnail_path=storage.relative(target))

    # ---- processamento -----------------------------------------------------------------------------------------
    def _run_process_job(self) -> None:
        media_id = self.job.get("media_id")
        media = self.ctx.media.get(media_id) if media_id else None
        if media is None:
            raise JobFailure("media_not_found", "A mídia deste job não existe mais na biblioteca.")
        if not media.get("thumbnail_path"):
            self._ensure_thumbnail(media["id"], self._original_path(media))
        self._process(media)

    def _original_path(self, media: dict[str, Any]) -> Path:
        return self.ctx.storage.resolve(media["original_path"])

    def _process(self, media: dict[str, Any]) -> None:
        self._ensure_metadata(media)
        params = self.job["params"] or {}
        key = (media["id"], self.job["preset_id"], canonical_json(params))
        with self.ctx.rendition_locks.hold(key, self.cancel):
            self._process_locked(media, params)

    def _process_locked(self, media: dict[str, Any], params: dict[str, Any]) -> None:
        existing = self.ctx.media.find_rendition(media["id"], self.job["preset_id"], params)
        if existing is not None:
            if self.ctx.storage.resolve(existing["processed_path"]).is_file():
                logger.info("Job %s: rendition %s já existe com o mesmo preset/params", self.job_id, existing["id"])
                self._complete(stage="skipped", rendition_id=existing["id"])
                return
            # Arquivo sumiu do disco: a linha obsoleta é substituída pela nova rendition.
            logger.warning("Job %s: rendition %s sem arquivo; será substituída", self.job_id, existing["id"])
            self.ctx.media.delete_rendition(existing["id"])

        original = self._original_path(media)
        if not original.is_file():
            raise JobFailure("original_missing", "Arquivo original não encontrado no storage.")
        self._stage("processing", status="processing", progress=0.0)
        self._check_cancel()  # pedido visto pela gravação da etapa: não inicia o FFmpeg
        processor = self.ctx.processors.get()
        source_probe = processor.probe(original)
        options = ProcessingOptions.from_params(params)
        suffix = processor.output_suffix(source_probe, options)
        temp_dir = self.ctx.storage.job_temp_dir(self.job_id)
        self._set(temp_path=self.ctx.storage.relative(temp_dir))
        output_tmp = temp_dir / f"processed{suffix}"
        if os.path.lexists(output_tmp):  # sobra de tentativa anterior no temp do próprio job
            output_tmp.unlink()
        result = processor.process(
            original, output_tmp, options, progress=self._report_progress, cancel=self.cancel
        )
        self._check_cancel()

        self._stage("finalizing", progress=1.0)
        output = Path(result.output_path)
        if not output.is_file():
            raise JobFailure("processing_failed", "O FFmpeg não gerou o arquivo de saída.")
        final = self.ctx.storage.promote_processed(output, media["id"], self.job_id, result.sha256, output.suffix.lower())
        out_probe = result.probe
        try:
            rendition_id = self.ctx.media.insert_rendition(
                {
                    "media_id": media["id"],
                    "preset_id": self.job["preset_id"],
                    "preset_name": self.job["preset_name"],
                    "params": params,
                    "job_id": self.job_id,
                    "processed_path": self.ctx.storage.relative(final),
                    "processed_sha256": result.sha256,
                    "file_size": final.stat().st_size,
                    "duration": out_probe.duration,
                    "width": out_probe.width,
                    "height": out_probe.height,
                    "fps": out_probe.fps,
                    "has_audio": int(bool(out_probe.has_audio)),
                }
            )
        except BaseException:
            final.unlink(missing_ok=True)
            raise
        logger.info("Job %s: rendition %s criada (sha %s)", self.job_id, rendition_id, result.sha256[:12])
        self._complete(stage="done", rendition_id=rendition_id)
