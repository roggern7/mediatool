"""Worker da fila persistente: thread despachante + pool de threads de execução.

- Claim atômico no SQLite com lease (`claimed_by`): um job em execução nunca é reivindicado de novo.
- Dois pools (docs/BATCH_ROBUSTNESS.md, seção 9, A3): download (I/O, `download_concurrency` 1..4) e processamento
  FFmpeg (CPU, `concurrency` 1..2), ajustáveis em runtime; claim por rodízio entre lotes; lote pausado pelo
  disjuntor não é reivindicado.
- Disco (C2): antes de cada claim de download verifica o espaço livre; insuficiente -> pausa local `disk_full`
  (nada é reivindicado nem consome tentativa), nova verificação a cada 1 min.
- Verificação automática do controlador de recuperação em thread própria (B6); enquanto houver lote ativo, impede a
  suspensão do Windows (B7).
- Heartbeat periódico dos jobs ativos.
- Cancelamento por `threading.Event` por job (o processor/downloader encerram FFmpeg/download).
- Pausa global do tráfego automático ao Instagram após `rate_limited` (10 min) ou `action_blocked` (1 h, proteção
  do Media Tool diante de campos explícitos de restrição); `request_auth_failed` pausa `auth_check` (15 min) e só
  escala para `session_invalid` na 2a verificação seguida sem sucesso autenticado entre elas (`pause_scope` define
  o que cada pausa bloqueia). `access_refused` (redirect para a página inicial) não pausa. Persistida em app_state (fim e motivo), relida no startup (pausa antiga com motivo `access_refused` é
  descartada); uma pausa nova nunca encurta a vigente. Pode ser liberada manualmente.
- Manutenção periódica: cancelamentos pendentes de jobs ociosos e jobs travados (lease sem heartbeat); ociosa
  (sem jobs ativos nem listagem): wal_checkpoint(TRUNCATE), retenção de jobs antigos e VACUUM ocasional.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.core.config import MAX_CONCURRENCY, MAX_DOWNLOAD_CONCURRENCY, MIN_CONCURRENCY
from app.db.connection import parse_utc, utc_now
from app.jobs.pipeline import JobExecutor
from app.services import disk
from app.services.instagram_gate import InstagramRequestGate
from app.services.instagram_recovery import AUTH_FAIL_STREAK_KEY, InstagramRecoveryController, pause_scope

# Manutenção ociosa (docs/BATCH_ROBUSTNESS.md, seção 6).
JOB_RETENTION_DAYS = 30
RETENTION_INTERVAL_SECONDS = 3600.0
VACUUM_INTERVAL_SECONDS = 24 * 3600.0
VACUUM_MIN_FREE_PAGES = 2048  # ~8 MB com páginas de 4 KB
VACUUM_MIN_FREE_RATIO = 0.25

if TYPE_CHECKING:
    from app.services.context import AppContext

logger = logging.getLogger("media_tool.worker")

PAUSE_STATE_KEY = "instagram_downloads_paused_until"
PAUSE_REASON_KEY = "instagram_pause_reason"
PAUSE_REASONS = ("rate_limited", "action_blocked", "checkpoint_required", "session_invalid", "auth_check")
# checkpoint_required: até o usuário concluir a verificação no navegador e liberar a pausa.
# session_invalid: até salvar/atualizar a sessão ou os cookies, ou um teste de sessão válido (limpa sozinha), com
# este mesmo teto. Nunca apaga a sessão salva.
LONG_PAUSE_SECONDS = 24 * 3600.0
# auth_check (seção 7 de docs/BATCH_ROBUSTNESS.md): 1o degrau de request_auth_failed (pause_seconds_for).
AUTH_CHECK_PAUSE_SECONDS = 15 * 60.0
# Motivos de versões anteriores que não pausam mais; uma pausa persistida com eles é descartada no startup.
OBSOLETE_PAUSE_REASONS = ("access_refused",)
# Pausa vencida aguardando a verificação do controlador: continua vigente, informada como agora + isto.
VERIFY_HOLD_SECONDS = 60.0
# Intervalo com que o laço do worker pergunta ao controlador se a verificação venceu.
RECOVERY_CHECK_SECONDS = 30.0
# Disco insuficiente: nova verificação a cada minuto (seção 9, C2).
DISK_RECHECK_SECONDS = 60.0
# Lote ativo -> SetThreadExecutionState (seção 9, B7), reavaliado a cada:
KEEP_AWAKE_CHECK_SECONDS = 30.0
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
POOLS = ("download", "process")


def set_thread_execution_state(keep_awake: bool) -> bool:
    """Impede (ou volta a permitir) a suspensão do Windows enquanto a thread chamadora viver. No-op fora do
    Windows. Não impede desligar a tela."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if keep_awake else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags)))
    except (AttributeError, OSError):
        return False


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Worker:
    def __init__(self, ctx: AppContext, concurrency: int = 1, download_concurrency: int | None = None):
        self.ctx = ctx
        self._concurrency = self._clamp(concurrency)
        self._download_concurrency = self._clamp_download(
            ctx.settings.download_concurrency if download_concurrency is None else download_concurrency)
        self._lock = threading.Lock()
        self._active: dict[int, threading.Event] = {}
        self._pool_of: dict[int, str] = {}
        self._last_batch: dict[str | None, int | None] = {}  # rodízio entre lotes, por pool
        # Backpressure de processamento (seção 9): lotes com downloads parados e teto global (com histerese).
        self._backlog_lock = threading.Lock()
        self._backlog_blocked: set[int] = set()
        self._backlog_global = False
        # Disco (C2): verificação injetável nos testes; estado da pausa local disk_full.
        self.disk_check: Callable[[], tuple[bool, int, int]] = self._disk_check
        self._disk_full = False
        self._disk_next_check = 0.0
        self._disk_free: int | None = None
        self._disk_required: int | None = None
        self._configure_disk()
        self._recovery_thread: threading.Thread | None = None
        self._keep_awake = False
        self._last_keep_awake_check = float("-inf")
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._pause_lock = threading.Lock()
        self._pause_until: datetime | None = None  # UTC
        self._pause_reason: str | None = None
        self._last_heartbeat = 0.0
        self._last_maintenance = 0.0
        self._last_retention = float("-inf")
        self._last_vacuum = float("-inf")
        self._last_recovery_check = 0.0
        # Relógio das pausas (UTC aware); injetável nos testes com relógio simulado.
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        # Portão global (seção 8): 1 requisição ao Instagram em voo e intervalo mínimo entre listagem/renovação.
        if getattr(ctx, "instagram_gate", None) is None:
            ctx.instagram_gate = InstagramRequestGate(
                interval=ctx.settings.instagram_request_interval)
        self.recovery = InstagramRecoveryController(ctx, self)
        # Identifica este processo/worker no lease dos jobs.
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _clamp(value: int) -> int:
        return max(MIN_CONCURRENCY, min(MAX_CONCURRENCY, int(value)))

    @staticmethod
    def _clamp_download(value: int) -> int:
        return max(MIN_CONCURRENCY, min(MAX_DOWNLOAD_CONCURRENCY, int(value)))

    def _configure_disk(self) -> None:
        settings = self.ctx.settings
        disk.configure(self.ctx.storage.root, disk.required_free_bytes(
            settings.max_download_bytes, self._download_concurrency, settings.disk_reserve_bytes,
            processing_concurrency=self._concurrency), disk.required_free_bytes(
            settings.max_download_bytes, self._download_concurrency, settings.disk_reserve_bytes))

    def _disk_check(self) -> tuple[bool, int, int]:
        """Verificação padrão do disco: sem processamento em aberto (só lotes download-only) o mínimo não inclui a
        parcela das saídas do FFmpeg."""
        return disk.disk_ok(download_only=not self.ctx.jobs.has_processing_work())

    # ---- estado ------------------------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def concurrency(self) -> int:
        return self._concurrency

    def set_concurrency(self, value: int) -> None:
        """Concorrência do pool de processamento FFmpeg."""
        self._concurrency = self._clamp(value)
        self._configure_disk()
        logger.info("Concorrência de processamento ajustada para %s", self._concurrency)
        self.wake()

    @property
    def download_concurrency(self) -> int:
        return self._download_concurrency

    def set_download_concurrency(self, value: int) -> None:
        self._download_concurrency = self._clamp_download(value)
        self._configure_disk()
        logger.info("Concorrência de downloads ajustada para %s", self._download_concurrency)
        self.wake()

    def active_count_for(self, pool: str) -> int:
        with self._lock:
            return sum(1 for value in self._pool_of.values() if value == pool)

    def active_job_ids(self) -> list[int]:
        with self._lock:
            return list(self._active)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def wake(self) -> None:
        self._wake.set()

    # ---- pausa do Instagram ------------------------------------------------------------------------------------
    def pause_seconds_for(self, reason: str) -> float:
        if reason in ("checkpoint_required", "session_invalid"):
            return LONG_PAUSE_SECONDS
        if reason == "action_blocked":
            return self.ctx.settings.action_blocked_pause_seconds
        if reason == "auth_check":
            return AUTH_CHECK_PAUSE_SECONDS
        return self.ctx.settings.rate_limit_pause_seconds

    def pause_for(self, code: str | None) -> bool:
        """Aplica a pausa correspondente ao código de erro do Instagram (degraus do controlador de recuperação,
        seção 8). Retorna True se o código pausa. Nada aqui apaga a sessão salva."""
        return self.recovery.on_failure(code)

    def note_auth_success(self) -> None:
        """Resposta autenticada com sucesso (página da listagem, refresh_page, teste de sessão válido): zera o
        contador de verificações com ``request_auth_failed`` (seção 7)."""
        with self._pause_lock:
            if self.ctx.app_settings.get_state(AUTH_FAIL_STREAK_KEY):
                self.ctx.app_settings.set_state(AUTH_FAIL_STREAK_KEY, None)

    def _publish_gate(self, reason: str | None) -> None:
        """Publica (``reason``) ou retira (None) a pausa no portão, de forma síncrona; chamado sob ``_pause_lock``.
        Depois do retorno nenhuma requisição nova ao Instagram é admitida (só a verificação do controlador)."""
        gate = getattr(self.ctx, "instagram_gate", None)
        if reason is None:
            clear = getattr(gate, "clear_pause", None)
            if clear is not None:
                clear()
            return
        publish = getattr(gate, "publish_pause", None)
        if publish is not None:
            publish(pause_scope(reason) or "instagram")

    def set_pause(self, until: datetime, reason: str) -> None:
        """Grava a pausa decidida pelo controlador de recuperação (substitui a vigente) e a publica no portão antes de
        retornar."""
        reason = reason if reason in PAUSE_REASONS else "rate_limited"
        with self._pause_lock:
            self._publish_gate(reason)
            self._pause_until, self._pause_reason = until, reason
            self.ctx.app_settings.set_state(PAUSE_STATE_KEY, _iso(until))
            self.ctx.app_settings.set_state(PAUSE_REASON_KEY, reason)

    def pause_downloads(self, seconds: float | None = None, *, reason: str = "rate_limited") -> None:
        """Pausa o tráfego automático ao Instagram. Nunca encurta uma pausa vigente (memória ou persistida)."""
        reason = reason if reason in PAUSE_REASONS else "rate_limited"
        duration = self.pause_seconds_for(reason) if seconds is None else seconds
        new_until = self.clock() + timedelta(seconds=duration)
        with self._pause_lock:
            current, current_reason = self._current_pause_locked()
            if current is not None and current >= new_until:
                logger.info("Pausa do Instagram mantida até %s (%s); novo evento: %s",
                            _iso(current), current_reason, reason)
                return
            self._publish_gate(reason)
            self._pause_until, self._pause_reason = new_until, reason
            self.ctx.app_settings.set_state(PAUSE_STATE_KEY, _iso(new_until))
            self.ctx.app_settings.set_state(PAUSE_REASON_KEY, reason)
        logger.warning("Tráfego automático ao Instagram pausado por %ss (motivo: %s)", int(duration), reason)

    def _current_pause_locked(self) -> tuple[datetime | None, str | None]:
        """Maior entre a pausa em memória e a persistida (outra instância pode ter gravado). Vencida, continua
        vigente enquanto o controlador não fizer a verificação (``awaiting_verification``)."""
        persisted = parse_utc(self.ctx.app_settings.get_state(PAUSE_STATE_KEY))
        until, reason = self._pause_until, self._pause_reason
        if persisted is not None and (until is None or persisted > until):
            until, reason = persisted, self.ctx.app_settings.get_state(PAUSE_REASON_KEY)
        if until is None:
            return None, None
        now = self.clock()
        if until <= now:
            if not self.recovery.awaiting_verification():
                return None, None
            until = now + timedelta(seconds=VERIFY_HOLD_SECONDS)
        return until, reason if reason in PAUSE_REASONS else "rate_limited"

    def pause_state(self) -> tuple[str | None, str | None]:
        """(paused_until ISO UTC, motivo) da pausa vigente, ou (None, None). Pausa vencida aguardando a verificação
        automática continua vigente (fim informado como agora + VERIFY_HOLD_SECONDS)."""
        with self._pause_lock:
            until = self._pause_until
            if until is None:
                return None, None
            now = self.clock()
            if until <= now:
                if not self.recovery.awaiting_verification():
                    # Venceu sem verificação pendente do controlador: a pausa acabou; o portão volta a admitir.
                    self._publish_gate(None)
                    return None, None
                until = now + timedelta(seconds=VERIFY_HOLD_SECONDS)
            return _iso(until), self._pause_reason

    def current_pause_scope(self) -> str | None:
        """Escopo da pausa vigente (``pause_scope``), ou None sem pausa."""
        return pause_scope(self.pause_state()[1])

    def downloads_paused(self) -> bool:
        """Há pausa vigente de qualquer escopo: bloqueia listagem, prévia, renovação de URL e teste automático."""
        return self.pause_state()[0] is not None

    def ingest_paused(self) -> bool:
        """Pausa de escopo ``instagram`` (rate_limited/action_blocked): bloqueia também novos downloads de ingest."""
        return self.current_pause_scope() == "instagram"

    def clear_pause(self, only_reason: str | None = None) -> bool:
        """Libera a pausa (e o backoff do controlador); com `only_reason`, só se a pausa vigente tiver esse motivo.
        True se liberou. Não mexe na sessão salva."""
        with self._pause_lock:
            if only_reason is not None and self._current_pause_locked()[1] != only_reason:
                return False
            self._pause_until, self._pause_reason = None, None
            self.ctx.app_settings.set_state(PAUSE_STATE_KEY, None)
            self.ctx.app_settings.set_state(PAUSE_REASON_KEY, None)
            self._publish_gate(None)
        self.recovery.reset()
        if only_reason is None:
            logger.warning("Pausa do Instagram liberada manualmente")
        else:
            logger.warning("Pausa do Instagram (motivo: %s) liberada automaticamente", only_reason)
        self.wake()
        return True

    def load_pause_state(self) -> None:
        """Relê a pausa e o backoff persistidos (sobrevivem a reinícios do app). Descarta pausa de motivo obsoleto."""
        self.recovery.reload()
        with self._pause_lock:
            if self.ctx.app_settings.get_state(PAUSE_REASON_KEY) in OBSOLETE_PAUSE_REASONS:
                self.ctx.app_settings.set_state(PAUSE_STATE_KEY, None)
                self.ctx.app_settings.set_state(PAUSE_REASON_KEY, None)
                logger.warning("Pausa antiga do Instagram (motivo access_refused) descartada: não pausa mais")
            persisted = parse_utc(self.ctx.app_settings.get_state(PAUSE_STATE_KEY))
            until, reason = self._current_pause_locked()
            if until is not None and persisted is not None:
                until = persisted   # o fim real (a verificação no vencimento é do controlador)
            self._pause_until, self._pause_reason = until, reason
            self._publish_gate(reason if until is not None else None)
        if until is not None:
            logger.warning("Tráfego ao Instagram continua pausado até %s (motivo: %s)", _iso(until), reason)

    def _recovery_if_due(self) -> None:
        """B6: a verificação (até o prazo do teste de sessão) roda numa thread curta, sem segurar os claims."""
        now = time.monotonic()
        if now - self._last_recovery_check < RECOVERY_CHECK_SECONDS:
            return
        thread = self._recovery_thread
        if thread is not None and thread.is_alive():
            return
        self._last_recovery_check = now
        self._recovery_thread = threading.Thread(target=self._recovery_tick, name="instagram-recovery-tick",
                                                 daemon=True)
        self._recovery_thread.start()

    def _recovery_tick(self) -> None:
        try:
            self.recovery.tick()
        except Exception:  # noqa: BLE001 - a verificação nunca derruba o worker
            logger.exception("Erro na verificação automática do Instagram")

    # ---- disco (seção 9, C2) -----------------------------------------------------------------------------------
    def _disk_allows_ingest(self) -> bool:
        now = time.monotonic()
        if self._disk_full and now < self._disk_next_check:
            return False
        ok, free, required = self.disk_check()
        self._disk_free, self._disk_required = free, required
        if not ok:
            if not self._disk_full:
                logger.warning("Disco quase cheio (%s MB livres, mínimo %s MB): downloads pausados (disk_full)",
                               free // 2**20, required // 2**20)
            self._disk_full = True
            self._disk_next_check = now + DISK_RECHECK_SECONDS
            return False
        if self._disk_full:
            logger.warning("Espaço em disco liberado; downloads retomados")
            self._disk_full = False
        return True

    def note_disk_full(self) -> None:
        """Um job encontrou o disco cheio: pausa local disk_full até a próxima verificação."""
        self._disk_full = True
        self._disk_next_check = time.monotonic() + DISK_RECHECK_SECONDS

    def disk_status(self) -> dict[str, object]:
        """Pausa local disk_full e o espaço livre/mínimo medidos agora (diagnóstico)."""
        ok, free, required = self.disk_check()
        return {"disk_full": self._disk_full, "ok": ok, "free_bytes": free, "required_bytes": required}

    # ---- suspensão do Windows (seção 9, B7) --------------------------------------------------------------------
    def _keep_awake_if_due(self) -> None:
        """Chamado só pela thread despachante (o estado vale por thread)."""
        now = time.monotonic()
        if now - self._last_keep_awake_check < KEEP_AWAKE_CHECK_SECONDS:
            return
        self._last_keep_awake_check = now
        active = bool(self.active_count) or self._has_active_work()
        if active != self._keep_awake:
            set_thread_execution_state(active)
            self._keep_awake = active
            logger.info("Suspensão do Windows %s", "bloqueada (lote ativo)" if active else "liberada (ocioso)")

    def _has_active_work(self) -> bool:
        """Helper do import runner (needs_attention e pausas longas não contam); sem ele, lote listing/running."""
        helper = getattr(self.ctx.import_runner, "has_active_work", None)
        if not callable(helper):
            helper = getattr(getattr(self.ctx, "batches", None), "has_active_work", None)
        if callable(helper):
            return bool(helper())
        return bool(self.ctx.db.scalar(
            "SELECT EXISTS (SELECT 1 FROM batches WHERE status IN ('listing', 'running'))"))

    def _release_keep_awake(self) -> None:
        if self._keep_awake:
            set_thread_execution_state(False)
            self._keep_awake = False

    # ---- cancelamento ------------------------------------------------------------------------------------------
    def signal_cancel(self, job_id: int) -> bool:
        """Sinaliza um job em execução. Retorna False se o job não está ativo neste worker."""
        with self._lock:
            event = self._active.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    # ---- ciclo -------------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENCY + MAX_DOWNLOAD_CONCURRENCY,
                                            thread_name_prefix="media-job")
        self._thread = threading.Thread(target=self._loop, name="media-worker", daemon=True)
        self._thread.start()
        logger.info("Worker iniciado (downloads %s, processamento %s)", self._download_concurrency,
                    self._concurrency)

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            for event in self._active.values():
                event.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        logger.info("Worker parado")

    def _loop(self) -> None:
        interval = self.ctx.settings.poll_interval
        while not self._stop.is_set():
            try:
                self._heartbeat_if_due()
                self._maintenance_if_due()
                self._recovery_if_due()
                self._keep_awake_if_due()
                self._dispatch()
            except Exception:  # noqa: BLE001 - o despachante nunca morre por erro de banco transitório
                logger.exception("Erro no despachante do worker")
            self._wake.wait(interval)
            self._wake.clear()
        self._release_keep_awake()

    def _heartbeat_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat >= self.ctx.settings.heartbeat_interval:
            self._last_heartbeat = now
            self.ctx.jobs.heartbeat(self.active_job_ids())

    def _maintenance_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_maintenance >= self.ctx.settings.maintenance_interval:
            self._last_maintenance = now
            self.maintenance()

    def maintenance(self) -> None:
        """Conclui cancelamentos pendentes de jobs ociosos e reenfileira jobs travados (sem heartbeat)."""
        for batch_id in self.ctx.jobs.finalize_cancel_requests():
            self.ctx.batches.refresh_status(batch_id)
        cutoff = utc_now(-self.ctx.settings.stale_job_seconds)
        result = self.ctx.jobs.recover_stale(cutoff=cutoff, exclude_ids=self.active_job_ids())
        if result["failed"] or result["requeued"] or result["cancelled"]:
            logger.warning(
                "Jobs travados tratados como interrompidos: %s reenfileirados, %s falhos, %s cancelados",
                result["requeued"], result["failed"], result["cancelled"],
            )
        for batch_id in result["batch_ids"]:
            self.ctx.batches.refresh_status(batch_id)
        # Exportações automáticas devidas (migration 0023): lotes/grupos encerrados por qualquer caminho (fim da
        # listagem, cancelamento, recuperação) e disparos adiados por erro passageiro. Nunca levanta.
        from app.services import auto_export

        auto_export.run_due(self.ctx)
        try:
            self._idle_maintenance()
        except sqlite3.Error:
            logger.warning("Manutenção ociosa do banco adiada (banco ocupado)", exc_info=True)

    def _idle_maintenance(self) -> None:
        """Só sem jobs ativos nem listagem: retenção (de hora em hora), VACUUM ocasional e checkpoint do WAL."""
        runner = self.ctx.import_runner
        if self.active_count or (runner is not None and runner.current_batch_id is not None):
            return
        now = time.monotonic()
        if now - self._last_retention >= RETENTION_INTERVAL_SECONDS:
            self._last_retention = now
            removed = self.ctx.jobs.purge_finished_before(utc_now(-JOB_RETENTION_DAYS * 86400.0))
            if removed:
                logger.info("Retenção: %s jobs terminados há mais de %s dias apagados", removed, JOB_RETENTION_DAYS)
            if now - self._last_vacuum >= VACUUM_INTERVAL_SECONDS:
                self._last_vacuum = now
                self._vacuum_if_fragmented()
        self.ctx.db.wal_checkpoint_truncate()

    def _vacuum_if_fragmented(self) -> bool:
        free = int(self.ctx.db.scalar("PRAGMA freelist_count") or 0)
        pages = int(self.ctx.db.scalar("PRAGMA page_count") or 0)
        if free < VACUUM_MIN_FREE_PAGES or free < pages * VACUUM_MIN_FREE_RATIO:
            return False
        self.ctx.db.execute("VACUUM")
        logger.info("VACUUM concluído (%s de %s páginas livres)", free, pages)
        return True

    def _dispatch(self) -> None:
        while not self._stop.is_set():
            claimed = None
            if self.active_count_for("download") < self._download_concurrency:
                claimed = self._claim("download")
            if claimed is None and self.active_count_for("process") < self._concurrency:
                claimed = self._claim("process")
            if claimed is None:
                return
            job, event = claimed
            assert self._executor is not None
            self._executor.submit(self._execute, job, event)

    def process_backlog_state(self) -> tuple[set[int], bool]:
        """(lotes com novos downloads parados, teto global atingido) pelo backlog de processamento (seção 9).

        Histerese: o lote para com `max_process_backlog` ingests baixando/baixados/processando e só volta abaixo de
        `resume_process_backlog`; o global, com `max_process_backlog_global` e abaixo de
        `resume_process_backlog_global`. Lotes sem processamento nunca entram. Usado pelo claim de download e pela
        listagem."""
        settings = self.ctx.settings
        counts = self.ctx.jobs.process_backlog()
        total = sum(counts.values())
        with self._backlog_lock:
            blocked = {b for b in self._backlog_blocked if counts.get(b, 0) >= settings.resume_process_backlog}
            blocked |= {b for b, n in counts.items() if n >= settings.max_process_backlog}
            limit = (settings.resume_process_backlog_global if self._backlog_global
                     else settings.max_process_backlog_global)
            self._backlog_global = total >= limit
            self._backlog_blocked = blocked
            return set(blocked), self._backlog_global

    def process_backlog_blocks(self, batch_id: int) -> bool:
        """O lote (com processamento) está parado pelo backlog de processamento, do lote ou global."""
        blocked, global_full = self.process_backlog_state()
        return batch_id in blocked or global_full

    def _claim(self, pool: str | None = None) -> tuple[dict, threading.Event] | None:
        """Reivindica um job do pool (None: qualquer um), por rodízio entre os lotes abertos e não pausados; depois,
        jobs sem lote (ou de lotes fora do rodízio), em ordem de id."""
        # Pausa de escopo "session": ingest com URL de mídia continua (CDN não usa a sessão); sem URL, espera.
        scope = getattr(self, "current_pause_scope", lambda: None)()
        # Disco (C2) antes de cada claim, dos dois pools: downloads e saídas do FFmpeg precisam de espaço.
        if not self._disk_allows_ingest():
            return None
        allow = not self.ingest_paused()
        if pool == "download" and not allow:
            return None
        kwargs = {"owner": self.owner, "allow_downloads": allow, "url_only": scope == "session", "pool": pool}
        batches = self.ctx.jobs.claimable_batch_ids(pool)
        if pool == "download":
            # Backpressure de processamento: lotes com FFmpeg atrasado não baixam mais nada até o backlog cair.
            blocked, global_full = self.process_backlog_state()
            kwargs.update(exclude_batch_ids=blocked, block_process_ingest=global_full)
            batches = [b for b in batches if b not in blocked]
        last = self._last_batch.get(pool)
        if last is not None:
            batches = [b for b in batches if b > last] + [b for b in batches if b <= last]
        job = None
        for batch_id in batches:
            job = self.ctx.jobs.claim(batch_id=batch_id, **kwargs)
            if job is not None:
                break
        if job is None:
            job = self.ctx.jobs.claim(**kwargs)
        if job is None:
            return None
        if job.get("batch_id") is not None:
            self._last_batch[pool] = job["batch_id"]
        event = threading.Event()
        with self._lock:
            self._active[job["id"]] = event
            self._pool_of[job["id"]] = "download" if job["status"] == "downloading" else "process"
        return job, event

    def _execute(self, job: dict, event: threading.Event) -> None:
        try:
            JobExecutor(self.ctx, self, job, event).run()
        except Exception:  # noqa: BLE001 - última barreira: o worker continua
            logger.exception("Falha inesperada fora do pipeline no job %s", job["id"])
            try:
                # Marca falho e limpa as URLs (numa transação) e só depois reavalia o lote: o refresh_status do
                # pipeline rodou antes desta marcação, e o último job do lote o deixaria aberto para sempre.
                self.ctx.jobs.finish(
                    job["id"], status="failed", stage=None, error_code="internal_error",
                    error_message="Erro interno inesperado. Consulte o log do aplicativo.", finished_at=utc_now(),
                )
                if job.get("batch_id") is not None:
                    self.ctx.batches.refresh_status(job["batch_id"])
            except Exception:  # noqa: BLE001
                logger.exception("Não foi possível marcar o job %s como falho", job["id"])
        finally:
            # Sai dos ativos ANTES de liberar o lease: liberado, o mesmo job (`downloaded`) pode ser reivindicado na
            # hora pelo pool de FFmpeg, e o pop depois apagaria o registro do novo claim.
            with self._lock:
                self._active.pop(job["id"], None)
                self._pool_of.pop(job["id"], None)
            try:
                self.ctx.jobs.release(job["id"], self.owner)
            except Exception:  # noqa: BLE001 - a manutenção recupera o lease depois
                logger.exception("Não foi possível liberar o lease do job %s", job["id"])
            self._wake.set()

    # ---- execução síncrona (testes e ferramentas) --------------------------------------------------------------
    def run_pending(self, max_jobs: int = 1000) -> int:
        """Executa na thread atual os jobs elegíveis agora, um por vez. Retorna quantos rodou."""
        count = 0
        self.maintenance()
        while count < max_jobs:
            claimed = self._claim("download") or self._claim("process")
            if claimed is None:
                break
            self._execute(*claimed)
            count += 1
        return count
