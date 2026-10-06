"""Import runner: executa a listagem de perfis (lotes em `listing`), uma listagem por vez, em thread própria.

Listagem incremental e retomável (docs/BATCH_ROBUSTNESS.md, seção 0): cada trecho consome
`iter_profile_media(..., resume=<checkpoint salvo>, on_checkpoint=<persistir no lote>)`; os jobs de cada página e o
checkpoint são gravados numa transação. O trecho para (backpressure) quando o lote tem `MAX_PENDING_INGEST` ingests
ainda não baixados (ou o app inteiro tem `MAX_PENDING_INGEST_GLOBAL`) e retoma abaixo de `RESUME_PENDING_INGEST`;
para também em pausa do Instagram, falha de rede, cancelamento ou encerramento do app, e retoma do checkpoint (o
índice único dos jobs descarta itens repetidos). O lote só sai de `listing` quando a listagem termina
(`listing_state = done`).

Seção 9 (C1, M1, M4): erro retryable/rede pausa a listagem do lote com backoff persistido (1m, 5m, 15m, 1h, 1h...),
sem desistir; qualquer outro erro no meio do lote (há checkpoint) deixa o lote em `needs_attention` com o checkpoint
(nova tentativa automática a cada 6 h, no máximo 4) em vez de concluí-lo parcial; cursor recusado no início de um trecho retomado ganha uma nova tentativa com o
MESMO cursor depois do backoff e, recusado de novo, vai para `needs_attention`. O checkpoint nunca é apagado
automaticamente: recomeçar da página 1 só por ação do usuário (POST /api/batches/{id}/restart-listing). Disco
insuficiente ou lote pausado pelo disjuntor (`paused_until` no futuro) impedem o próximo trecho.

Espera pelo teto de páginas por hora do portão (visibilidade): a espera acontece dentro do trecho (`listing_state`
continua `running`); o portão avisa a entrada e a saída (`_on_rate_limit_wait`), o runner grava
`rate_limit_resume_at`/`rate_limit_pages_used`/`rate_limit_pages_max` no lote (a API apresenta `waiting_rate_limit`) e
registra uma linha de log na entrada e outra na saída. `restore_rate_limit_wait` reconstrói isso na inicialização.

Descartes da listagem (migration 0011; docs/BATCH_ROBUSTNESS.md, "Descartes da listagem"): as contagens da página
(`ListingCheckpoint.page_counts`) são somadas ao lote na MESMA transação do checkpoint dela (`_flush`); página sem
checkpoint que é refeita não soma duas vezes. A página em que a listagem termina sem checkpoint (limite atingido no
meio dela) entra uma vez, no encerramento, só com os descartes até o último item gravado (`listing_dropped_partial`).
Descarte nunca muda o status do lote.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from app.db.connection import utc_now
from app.db.repositories.common import canonical_json
from app.integrations.instagram.downloader import InstagramError, ListingCheckpoint, RemoteMedia
from app.jobs.errors import safe_message
from app.jobs.pipeline import instagram_paused
from app.services import disk
from app.services.instagram_gate import DEFAULT_MAX_PAGES_PER_HOUR, PAGES_STATE_KEY, InstagramPaused

if TYPE_CHECKING:
    from app.services.context import AppContext

logger = logging.getLogger("media_tool.imports")

LISTING_INTERNAL_ERROR = "Erro inesperado ao listar o perfil. Consulte o log do aplicativo."

# Backpressure: a descoberta não acumula milhares de jobs pendentes (URLs assinadas usadas minutos depois).
MAX_PENDING_INGEST = 100
RESUME_PENDING_INGEST = 40
# Estimativa de itens por página antes de conhecer a página (o provider pede 12 posts por página).
PAGE_ESTIMATE = 12
# Teto de ingests pendentes somando todos os lotes (seção 9, M4).
MAX_PENDING_INGEST_GLOBAL = 200
# Erro retryable/rede na listagem: backoff do lote por falhas seguidas (o último degrau se repete; nunca desiste).
LISTING_BACKOFF_SECONDS = (60, 300, 900, 3600)
_NETWORK_CODES = ("network_error", "timeout")
# Recusa do cursor salvo no início de um trecho retomado: 1 nova tentativa com o mesmo cursor, depois needs_attention.
_CURSOR_REFUSED_CODES = ("access_refused", "contract_changed", "content_unavailable")
# needs_attention: nova tentativa automática espaçada (persistida), no máximo ATTENTION_MAX_TRIES vezes.
ATTENTION_RETRY_SECONDS = 6 * 3600
ATTENTION_MAX_TRIES = 4

END_OF_PROFILE_MESSAGE = "O perfil não tem mais publicações compatíveis; o lote terminou com {found} itens."
UNPRODUCTIVE_MESSAGE = (
    "A listagem parou porque as últimas páginas do perfil trouxeram poucos ou nenhum item compatível com o filtro "
    "(baixo rendimento). O lote terminou com {found} itens."
)


class _Chunk:
    """Estado de um trecho de listagem (só em memória)."""

    def __init__(self) -> None:
        self.stop: str | None = None  # backpressure | paused | limit
        self.created = 0  # jobs novos criados neste trecho
        self.page_items = 0  # itens entregues desde o último checkpoint
        self.page_estimate = PAGE_ESTIMATE  # maior página vista: a próxima não deve passar de MAX_PENDING_INGEST
        self.pending = 0  # ingests pendentes do lote conhecidos (recontado no banco antes de parar no meio da página)
        self.global_pending = 0  # ingests pendentes de todos os lotes (idem)
        self.pages_ok = 0  # páginas entregues (checkpoint) neste trecho
        self.from_cursor = False  # o trecho começou de um cursor salvo
        self.retryable = False  # o erro do trecho é retryable

    @property
    def progressed(self) -> bool:
        return self.created > 0


class ImportRunner:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._current: tuple[int, threading.Event] | None = None
        self._current_thread: int | None = None  # thread que consome a listagem do lote atual
        gate = getattr(ctx, "instagram_gate", None)
        if hasattr(gate, "listing_wait_observer"):
            gate.listing_wait_observer = self._on_rate_limit_wait

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def current_batch_id(self) -> int | None:
        with self._lock:
            return None if self._current is None else self._current[0]

    def wake(self) -> None:
        self._wake.set()

    def cancel(self, batch_id: int) -> bool:
        with self._lock:
            if self._current is None or self._current[0] != batch_id:
                return False
            self._current[1].set()
            return True

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="media-import", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            if self._current is not None:
                self._current[1].set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.run_next():
                    continue
            except Exception:  # noqa: BLE001
                logger.exception("Erro no import runner")
            self._wake.wait(self.ctx.settings.poll_interval)
            self._wake.clear()

    def run_next(self) -> bool:
        """Processa um trecho de listagem do próximo lote elegível (na thread atual). False se não havia lote.

        Com o Instagram pausado nenhuma listagem começa: o lote continua em `listing` e é retomado do checkpoint
        quando a pausa termina (ou é liberada).
        """
        if instagram_paused(self.ctx.worker):
            return False
        batch = self._next_eligible()
        if batch is None:
            return False
        self._run_listing(batch)
        return True

    def _next_eligible(self) -> dict[str, Any] | None:
        candidates = self.ctx.batches.listing_candidates()
        if not candidates:
            return None
        now = utc_now()
        global_full = self.ctx.batches.pending_ingest_global() + PAGE_ESTIMATE > MAX_PENDING_INGEST_GLOBAL
        for batch in candidates:
            batch_id = batch["id"]
            if batch["listing_state"] == "needs_attention" and not batch["listing_retry_at"]:
                continue  # tentativas automáticas esgotadas: só a ação do usuário (resume-listing) retoma
            if (batch["listing_retry_at"] or "") > now or self._batch_paused(batch, now):
                continue
            waiting = batch["listing_state"] == "waiting_backpressure"
            pending = self.ctx.jobs.pending_ingest_count(batch_id)
            full = (pending >= RESUME_PENDING_INGEST) if waiting else (pending + PAGE_ESTIMATE > MAX_PENDING_INGEST)
            if full or global_full or self._process_backlog_full(batch):
                if not waiting:
                    self.ctx.batches.update_listing(batch_id, listing_state="waiting_backpressure")
                continue
            if not self._disk_ok():
                return None
            return batch
        return None

    def _process_backlog_full(self, batch: dict[str, Any]) -> bool:
        """Lote com processamento e FFmpeg atrasado (backpressure de processamento do worker, seção 9): não lista."""
        worker = self.ctx.worker
        if worker is None or not (batch["process"] and batch["params"] is not None):
            return False
        return worker.process_backlog_blocks(batch["id"])

    @staticmethod
    def _batch_paused(batch: dict[str, Any], now: str) -> bool:
        """Pausa do lote pelo disjuntor (`paused_until` no futuro) de escopo ingest: não lista enquanto vale.
        A pausa só do FFmpeg (escopo "process") não para a listagem (JobRepository.is_batch_paused)."""
        until = batch.get("paused_until")
        return bool(until) and str(until) > now and (batch.get("paused_scope") or "ingest") == "ingest"

    def _disk_ok(self) -> bool:
        """Espaço em disco suficiente (mesma verificação do worker, `disk_check`; senão services/disk.disk_ok)."""
        check = getattr(self.ctx.worker, "disk_check", None) or disk.disk_ok
        try:
            result = check()
        except Exception:  # noqa: BLE001 - a verificação de disco nunca derruba o runner
            logger.warning("Falha ao verificar o espaço em disco antes da listagem", exc_info=True)
            return True
        ok = result[0] if isinstance(result, tuple) else bool(result)
        if not ok:
            logger.warning("Espaço em disco insuficiente: listagem aguardando")
        return bool(ok)

    # ---- espera pelo teto de páginas por hora (visibilidade) ----------------------------------------------------
    def _on_rate_limit_wait(self, event: str, info: dict[str, Any]) -> None:
        """Aviso do portão: a listagem do lote atual começou ("enter") ou terminou ("exit") de esperar o teto de
        páginas por hora. Grava o estado exibido pela API e registra UMA linha de log em cada ponta."""
        with self._lock:
            if self._current is None or self._current_thread != threading.get_ident():
                return  # espera fora da thread que lista o lote atual: não é de lote nenhum
            batch_id = self._current[0]
        if event == "enter":
            seconds = max(0.0, float(info["seconds"]))
            resume_at = utc_now(seconds)
            self.ctx.batches.update_listing(
                batch_id, rate_limit_resume_at=resume_at, rate_limit_pages_used=int(info["used"]),
                rate_limit_pages_max=int(info["max"]),
            )
            logger.info(
                "Lote %s: aguardando limite de requisições (teto de %s páginas por hora atingido: %s/%s); espera "
                "prevista de %s, retomada automática às %s",
                batch_id, info["max"], info["used"], info["max"], self._minutes(seconds), resume_at,
            )
            return
        if info.get("reason") == "released":
            reason = "janela liberada"
            if instagram_paused(self.ctx.worker):
                reason += "; Instagram em pausa, a listagem aguarda a pausa"
        elif self._stop.is_set():
            reason = "encerramento do aplicativo"
        elif info.get("reason") == "cancelled":
            reason = "lote cancelado"
        else:
            reason = "espera interrompida"
        if not self._stop.is_set():
            # No encerramento o estado fica gravado: a inicialização o reconcilia com a janela reconstruída.
            self.ctx.batches.update_listing(
                batch_id, rate_limit_resume_at=None, rate_limit_pages_used=None, rate_limit_pages_max=None)
        logger.info("Lote %s: fim da espera pelo limite de requisições depois de %s (%s)",
                    batch_id, self._minutes(float(info.get("waited") or 0.0)), reason)

    @staticmethod
    def _minutes(seconds: float) -> str:
        total = int(round(max(0.0, seconds)))
        return f"{total // 60} min {total % 60:02d} s"

    def restore_rate_limit_wait(self) -> None:
        """Inicialização (antes das threads): reconstrói a janela de páginas por hora gravada em app_state e
        reconcilia a espera que uma execução anterior deixou gravada num lote: janela ainda cheia -> continua em
        espera com a retomada recalculada; janela livre (ou lote que não está mais listando) -> campos limpos."""
        gate = getattr(self.ctx, "instagram_gate", None)
        if not hasattr(gate, "restore_listing_pages"):
            return
        gate.restore_listing_pages(self.ctx.app_settings.get_state(PAGES_STATE_KEY))
        from app.integrations.instagram.web.provider import page_rate_limit   # local: evita ciclo de import

        # INSTAGRAM_PAGE_RATE_LIMIT=0 (POC de escala): sem teto horário não há espera; campos antigos são limpos.
        disabled = page_rate_limit(getattr(self.ctx.settings, "env", None)) == 0
        rows = self.ctx.db.fetchall(
            "SELECT id, status, listing_state, rate_limit_pages_max FROM batches"
            " WHERE rate_limit_resume_at IS NOT NULL")
        for row in rows:
            used, seconds = gate.listing_window(int(row["rate_limit_pages_max"] or DEFAULT_MAX_PAGES_PER_HOUR))
            if (not disabled and seconds is not None and row["status"] == "listing"
                    and row["listing_state"] == "running"):
                self.ctx.batches.update_listing(
                    row["id"], rate_limit_resume_at=utc_now(seconds), rate_limit_pages_used=used)
            else:
                self.ctx.batches.update_listing(
                    row["id"], rate_limit_resume_at=None, rate_limit_pages_used=None, rate_limit_pages_max=None)

    # ---- listagem ----------------------------------------------------------------------------------------------
    @staticmethod
    def _checkpoint_from(batch: dict[str, Any]) -> ListingCheckpoint | None:
        if batch["listing_next_page"] is None:
            return None
        return ListingCheckpoint(
            resume_cursor=batch["listing_cursor"],
            next_page_index=int(batch["listing_next_page"]),
            pages=int(batch["listing_pages"] or 0),
            posts_seen=int(batch["listing_posts_seen"] or 0),
            items_compatible=int(batch["listing_items_compatible"] or 0),
            unproductive_streak=int(batch["listing_unproductive_streak"] or 0),
            has_next=bool(batch["listing_has_next"]),
        )

    def _run_listing(self, batch: dict[str, Any]) -> None:
        batch_id = batch["id"]
        cancel = threading.Event()
        with self._lock:
            self._current = (batch_id, cancel)
            self._current_thread = threading.get_ident()
        resume = self._checkpoint_from(batch)
        limit = batch["requested_limit"]
        created_before = self.ctx.jobs.count_for_batch(batch_id)
        remaining = None if limit is None else max(0, limit - created_before)
        logger.info(
            "Lote %s: listando perfil %s (conteúdo=%s, limite=%s, %s)",
            batch_id, batch["source_profile"], batch["content_type"], limit,
            "retomada" if resume is not None else "início",
        )
        fields: dict[str, Any] = {"listing_state": "running", "listing_retry_at": None}
        if batch["listing_state"] == "paused":
            fields.update(error_code=None, error_message=None)  # motivo da pausa anterior não vale mais
        self.ctx.batches.update_listing(batch_id, **fields)

        chunk = _Chunk()
        chunk.from_cursor = resume is not None and resume.resume_cursor is not None
        error: tuple[str, str] | None = None
        failure: BaseException | None = None
        buffer: list[RemoteMedia] = []
        yielded = 0
        chunk_limit: int | None = None
        if remaining is not None:
            # Itens da página retomada que já viraram jobs voltam repetidos e contam no limite do provider.
            # Recomeço da página 1 com jobs no lote (restart-listing): todos podem voltar repetidos.
            repeats = (
                self.ctx.jobs.count_on_page(batch_id, resume.next_page_index) if resume is not None else created_before
            )
            chunk_limit = remaining + repeats

        def on_checkpoint(checkpoint: ListingCheckpoint) -> None:
            if cancel.is_set() and self._is_cancelled(batch_id):
                return
            for item in buffer:
                if item.listing_ref is not None:
                    chunk.page_estimate = max(chunk.page_estimate, item.listing_ref.page_size)
            chunk.page_estimate = max(chunk.page_estimate, chunk.page_items)
            chunk.page_items = 0
            chunk.created += self._flush(batch, buffer, checkpoint)
            chunk.pages_ok += 1
            if self.ctx.worker is not None:
                self.ctx.worker.note_auth_success()  # página entregue: resposta autenticada com sucesso (seção 7)
            chunk.pending = self.ctx.jobs.pending_ingest_count(batch_id)
            chunk.global_pending = self.ctx.batches.pending_ingest_global()
            if limit is not None and created_before + chunk.created >= limit:
                chunk.stop = "limit"
            elif (chunk.pending + chunk.page_estimate > MAX_PENDING_INGEST
                  or chunk.global_pending + chunk.page_estimate > MAX_PENDING_INGEST_GLOBAL
                  or self._process_backlog_full(batch)):
                # A próxima página passaria do teto de ingests pendentes: para na fronteira da página.
                chunk.stop = "backpressure"
            elif instagram_paused(self.ctx.worker):
                chunk.stop = "paused"
            if chunk.stop is not None:
                cancel.set()  # o provider para antes de pedir a próxima página

        try:
            if remaining == 0:
                chunk.stop = "limit"
            else:
                # Seção 8: nenhuma listagem durante pausa. O portão global é adquirido pelo cliente HTTP do
                # Instagram, por requisição (não durante o trecho inteiro).
                if instagram_paused(self.ctx.worker):
                    chunk.stop = "paused"
                else:
                    chunk.pending = self.ctx.jobs.pending_ingest_count(batch_id)
                    chunk.global_pending = self.ctx.batches.pending_ingest_global()
                    yielded = self._consume(batch, chunk, buffer, cancel, resume, chunk_limit, on_checkpoint,
                                            created_before)
        except InstagramPaused:
            # O portão recusou a próxima requisição (pausa publicada entre páginas): nada foi enviado; a listagem
            # fica `paused` e retoma do último checkpoint quando a pausa terminar.
            chunk.stop = "paused"
        except InstagramError as exc:
            failure = exc
            if not cancel.is_set():
                error = (exc.code, safe_message(exc.message))
                chunk.retryable = bool(exc.retryable)
        except Exception as exc:  # noqa: BLE001
            failure = exc
            if not cancel.is_set():
                logger.exception("Lote %s: erro inesperado na listagem", batch_id)
                error = ("internal_error", LISTING_INTERNAL_ERROR)
        finally:
            with self._lock:
                self._current = None
                self._current_thread = None
        if not self._is_cancelled(batch_id) and buffer:
            # Itens entregues depois do último checkpoint (página parcial): o índice único deduplica na retomada.
            chunk.created += self._flush(batch, buffer, None)
        # Erro numa página sem checkpoint: o provider manda junto os descartes dela até o último nó consumido
        # (inclusive depois do último item entregue, ou sem item nenhum). Ficam como estado da página aberta:
        # página refeita sobrescreve/apaga; listagem encerrada ou cancelada soma uma vez.
        pending = getattr(failure, "listing_dropped", None)
        if isinstance(pending, dict) and pending and not self._is_cancelled(batch_id):
            partial = canonical_json(pending)
            if partial != batch.get("listing_dropped_partial"):
                self.ctx.batches.update_listing(batch_id, listing_dropped_partial=partial)
                batch["listing_dropped_partial"] = partial
        self._after_chunk(batch, chunk, error, yielded, chunk_limit)

    def _consume(
        self, batch: dict[str, Any], chunk: _Chunk, buffer: list[RemoteMedia], cancel: threading.Event,
        resume: ListingCheckpoint | None, chunk_limit: int | None, on_checkpoint, created_before: int,
    ) -> int:
        """Consome o gerador da listagem; retorna quantos itens o provider entregou."""
        batch_id = batch["id"]
        limit = batch["requested_limit"]
        yielded = 0
        items = self.ctx.downloader_factory().iter_profile_media(
            batch["source_profile"], batch["content_type"], chunk_limit, cancel=cancel, resume=resume,
            on_checkpoint=on_checkpoint,
        )
        try:
            for item in items:
                if cancel.is_set():
                    break
                yielded += 1
                chunk.page_items += 1
                if (chunk.pending + len(buffer) + 1 > MAX_PENDING_INGEST
                        or chunk.global_pending + len(buffer) + 1 > MAX_PENDING_INGEST_GLOBAL):
                    # Backpressure rígido: grava o que já veio e reconta; se o próximo item passaria do teto, para
                    # no meio da página (a retomada refaz a página do último checkpoint; o índice único deduplica).
                    chunk.created += self._flush(batch, buffer, None)
                    chunk.pending = self.ctx.jobs.pending_ingest_count(batch_id)
                    chunk.global_pending = self.ctx.batches.pending_ingest_global()
                    if chunk.pending + 1 > MAX_PENDING_INGEST or chunk.global_pending + 1 > MAX_PENDING_INGEST_GLOBAL:
                        chunk.stop = "backpressure"
                        cancel.set()
                        break
                buffer.append(item)
                if limit is not None and created_before + chunk.created + len(buffer) >= limit:
                    # Pode ter atingido o limite: grava já (dedupe no índice único) e confere.
                    created = self._flush(batch, buffer, None)
                    chunk.created += created
                    chunk.pending += created
                    chunk.global_pending += created
                    if created_before + chunk.created >= limit:
                        chunk.stop = "limit"
                        break
        finally:
            close = getattr(items, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 - fechar o gerador nunca derruba o runner
                    logger.warning("Lote %s: falha ao encerrar o gerador da listagem", batch_id)
        return yielded

    def _after_chunk(
        self, batch: dict[str, Any], chunk: _Chunk, error: tuple[str, str] | None, yielded: int,
        chunk_limit: int | None,
    ) -> None:
        batch_id = batch["id"]
        current = self.ctx.batches.get(batch_id)
        if current is None:
            return
        if current["status"] == "cancelled":
            with self.ctx.db.transaction():
                running = self.ctx.jobs.cancel_batch_jobs(batch_id)
                self.ctx.batches.update_listing(batch_id, listing_state="cancelled", listing_cursor=None)
                # Página aberta gravada entre o cancelamento e o fim do trecho: soma uma vez (o parcial é apagado).
                self.ctx.batches.add_listing_dropped(batch_id, merge_partial=True)
                self.ctx.batches.refresh_status(batch_id)  # jobs criados no último trecho: recongela
            for job_id in running:
                if self.ctx.worker is not None:
                    self.ctx.worker.signal_cancel(job_id)
            logger.info("Lote %s: listagem cancelada", batch_id)
            return
        if self._stop.is_set():
            logger.info("Lote %s: listagem interrompida pelo encerramento; será retomada do checkpoint", batch_id)
            return
        self.ctx.wake_worker()
        if error is not None and error[0] == "unsupported_media" and current["listing_next_page"] is not None:
            # Páginas só com publicações de tipo não suportado no meio do lote: os itens foram pulados (contados pelo
            # provider) e os checkpoints gravados; a listagem segue pelas regras normais de fim, sem erro.
            logger.info("Lote %s: páginas sem mídia suportada puladas; a listagem continua do checkpoint", batch_id)
            error = None
            if current["listing_has_next"] and chunk.pages_ok > 0:
                self.ctx.batches.update_listing(batch_id, listing_state="pending")  # próximo trecho do checkpoint
                return
        if error is not None:
            self._handle_listing_error(current, error, chunk)
            return
        if chunk.stop == "backpressure":
            self.ctx.batches.update_listing(batch_id, listing_state="waiting_backpressure")
            logger.info("Lote %s: listagem aguardando downloads (backpressure)", batch_id)
            return
        if chunk.stop == "paused":
            self.ctx.batches.update_listing(batch_id, listing_state="paused")
            logger.info("Lote %s: listagem pausada (Instagram pausado); será retomada do checkpoint", batch_id)
            return
        limit = batch["requested_limit"]
        found = self.ctx.jobs.count_for_batch(batch_id)
        if chunk.stop == "limit" or self._limit_reached(batch_id, limit):
            self._finish_listing(batch_id, None, None)
            return
        has_next = current["listing_has_next"]
        if has_next is None or not has_next:
            message = END_OF_PROFILE_MESSAGE.format(found=found) if limit is not None and found < limit else None
            self._finish_listing(batch_id, None, message)
            return
        if chunk_limit is not None and yielded >= chunk_limit and chunk.progressed:
            # O provider parou pelo limite contando itens repetidos da página retomada: novo trecho do checkpoint.
            self.ctx.batches.update_listing(batch_id, listing_state="pending")
            return
        self._finish_listing(batch_id, None, UNPRODUCTIVE_MESSAGE.format(found=found))

    def _handle_listing_error(self, batch: dict[str, Any], error: tuple[str, str], chunk: _Chunk) -> None:
        batch_id = batch["id"]
        code, message = error
        if self.ctx.worker is not None and self.ctx.worker.pause_for(code):
            # Pausa do Instagram: a listagem fica pausada e retoma do checkpoint quando a pausa termina.
            self.ctx.batches.update_listing(batch_id, listing_state="paused", error_code=code, error_message=message)
            logger.warning("Lote %s: listagem pausada por %s; será retomada do checkpoint", batch_id, code)
            return
        # O checkpoint nunca é apagado aqui: recomeçar da página 1 só por ação do usuário (restart-listing).
        cursor_refused = chunk.from_cursor and chunk.pages_ok == 0 and code in _CURSOR_REFUSED_CODES
        if cursor_refused and not batch["listing_cursor_refusals"]:
            # 1a recusa do cursor salvo no início do trecho: nova tentativa com o MESMO cursor depois do backoff.
            self._pause_with_backoff(batch, chunk, code, message, listing_cursor_refusals=1)
            return
        if chunk.retryable or code in _NETWORK_CODES:
            self._pause_with_backoff(batch, chunk, code, message)
            return
        # No meio do lote (checkpoint com próxima página) nenhum erro conclui o lote nem apaga o cursor: mantém o que
        # foi listado e espera ação (internal_error, session_*, profile_not_found, access_refused, contract_changed,
        # content_unavailable...). Antes da primeira página (ou depois da última) não há o que retomar e o lote
        # termina como antes.
        mid_batch = batch["listing_next_page"] is not None and bool(batch["listing_has_next"])
        if cursor_refused or mid_batch:
            tries = int(batch["listing_attention_tries"] or 0)
            retry_at = utc_now(ATTENTION_RETRY_SECONDS) if tries < ATTENTION_MAX_TRIES else None
            self.ctx.batches.update_listing(
                batch_id, listing_state="needs_attention", error_code=code, error_message=message,
                listing_retry_at=retry_at, listing_attention_tries=min(tries + 1, ATTENTION_MAX_TRIES),
                listing_cursor_refusals=1,  # a nova tentativa automática recusada volta direto para needs_attention
            )
            logger.warning("Lote %s: listagem aguardando ação (%s); checkpoint mantido%s", batch_id, code,
                           "; nova tentativa automática em 6 h" if retry_at else "")
            return
        self._finish_listing(batch_id, error, None)

    def _pause_with_backoff(self, batch: dict[str, Any], chunk: _Chunk, code: str, message: str, **fields: Any) -> None:
        """Listagem `paused` com o backoff do lote (1m, 5m, 15m, 1h, 1h...); retoma do checkpoint no prazo."""
        level = 0 if chunk.progressed else int(batch["listing_retry_level"] or 0)
        delay = LISTING_BACKOFF_SECONDS[min(level, len(LISTING_BACKOFF_SECONDS) - 1)]
        self.ctx.batches.update_listing(
            batch["id"], listing_state="paused", listing_retry_level=level + 1, listing_retry_at=utc_now(delay),
            error_code=code, error_message=message, **fields,
        )
        logger.warning("Lote %s: falha na listagem (%s); retomada do checkpoint em %ss", batch["id"], code, delay)

    def _finish_listing(self, batch_id: int, error: tuple[str, str] | None, message: str | None) -> None:
        """Listagem encerrada (`done`): o lote sai de `listing` e termina quando todos os jobs forem terminais."""
        batches = self.ctx.batches
        with self.ctx.db.transaction():
            # Página final sem checkpoint (limite no meio dela): os descartes até o último item gravado entram agora,
            # uma única vez (a coluna parcial é apagada na mesma instrução).
            batches.add_listing_dropped(batch_id, merge_partial=True)
            batches.update_listing(batch_id, listing_state="done", listing_cursor=None)
        batches.sync_found(batch_id)
        found = batches.get(batch_id)["found"]
        if error is not None and found == 0:
            batches.update(batch_id, status="failed", error_code=error[0], error_message=error[1], finished_at=utc_now())
            logger.warning("Lote %s falhou na listagem: %s", batch_id, error[0])
            return
        fields: dict[str, Any] = {"status": "running", "error_code": None, "error_message": message}
        if error is not None:
            fields.update(error_code=error[0], error_message=error[1])
            logger.warning("Lote %s: listagem parcial (%s itens) com erro %s", batch_id, found, error[0])
        batches.update(batch_id, **fields)
        batches.refresh_status(batch_id)
        logger.info("Lote %s: listagem concluída com %s itens", batch_id, found)
        self.ctx.wake_worker()

    def _is_cancelled(self, batch_id: int) -> bool:
        current = self.ctx.batches.get(batch_id)
        return current is None or current["status"] == "cancelled"

    def _limit_reached(self, batch_id: int, limit: int | None) -> bool:
        return limit is not None and self.ctx.jobs.count_for_batch(batch_id) >= limit

    def _flush(self, batch: dict[str, Any], buffer: list[RemoteMedia], checkpoint: ListingCheckpoint | None) -> int:
        """Grava os jobs dos itens acumulados (e o checkpoint) numa transação. Retorna quantos jobs criou."""
        batch_id = batch["id"]
        limit = batch["requested_limit"]
        created = 0
        last: RemoteMedia | None = None
        with self.ctx.db.transaction():
            count = self.ctx.jobs.count_for_batch(batch_id)
            for item in buffer:
                if limit is not None and count >= limit:
                    break
                last = item
                if self._add_item(batch, item):
                    created += 1
                    count += 1
            if checkpoint is not None:
                self.ctx.batches.update_listing(
                    batch_id,
                    listing_cursor=checkpoint.resume_cursor,
                    listing_next_page=checkpoint.next_page_index,
                    listing_pages=checkpoint.pages,
                    listing_posts_seen=checkpoint.posts_seen,
                    listing_items_compatible=checkpoint.items_compatible,
                    listing_unproductive_streak=checkpoint.unproductive_streak,
                    listing_has_next=int(bool(checkpoint.has_next)),
                    # Página entregue: backoff, recusas do cursor e tentativas de needs_attention recomeçam.
                    listing_retry_level=0, listing_cursor_refusals=0, listing_attention_tries=0,
                    listing_dropped_partial=None,  # a página fechou: vale a contagem inteira dela, abaixo
                )
                # Descartes da página, na mesma transação do checkpoint dela: cada página soma uma única vez.
                page_counts = getattr(checkpoint, "page_counts", None)
                if page_counts:
                    self.ctx.batches.add_listing_dropped(batch_id, page_counts)
                batch["listing_dropped_partial"] = None
            elif last is not None:
                # Página ainda sem checkpoint: guarda (como estado, sem somar) os descartes dela até o último item
                # gravado. Página refeita sobrescreve; só entra no total se a listagem terminar aqui.
                dropped = getattr(last, "listing_dropped", None)
                partial = canonical_json(dropped) if dropped else None
                if partial != batch.get("listing_dropped_partial"):
                    self.ctx.batches.update_listing(batch_id, listing_dropped_partial=partial)
                    batch["listing_dropped_partial"] = partial
            self.ctx.batches.sync_found(batch_id)
        buffer.clear()
        if created:
            self.ctx.wake_worker()
        return created

    def _add_item(self, batch: dict[str, Any], item: RemoteMedia) -> bool:
        """Cria o job do item (sem duplicar dentro do lote). Retorna True se o item é novo no lote."""
        data = asdict(item)
        ref = item.listing_ref
        source = {
            "source_platform": data["source_platform"] or "instagram",
            "source_profile": data["source_profile"],
            "source_url": data["source_url"],
            "source_post_id": data["source_post_id"],
            "source_media_index": int(data["source_media_index"] or 0),
            "post_id": data["post_id"],
            "media_id_remote": data["media_id"],
            "media_type": data["media_type"],
            "media_url": data["media_url"],
            "thumbnail_url": data["thumbnail_url"],
            "taken_at": data["taken_at"],
            "listing_page_index": None if ref is None else ref.page_index,
            "listing_cursor": None if ref is None else ref.cursor,
            "listing_page_size": None if ref is None else ref.page_size,
        }
        process = batch["process"] and batch["params"] is not None
        existing = self.ctx.media.find_by_source(
            source["source_platform"], source["source_post_id"], source["source_media_index"]
        )
        kwargs: dict[str, Any] = {}
        primary = None
        if existing is not None:
            needs_processing = process and self.ctx.media.find_rendition(
                existing["id"], batch["preset_id"], batch["params"]
            ) is None
            kwargs = {
                "media_id": existing["id"],
                "is_duplicate": True,
                "duplicate_reason": "source_id",
                "completed_skipped": not needs_processing,
            }
        elif source["source_post_id"] and (primary := self.ctx.media.ingest_in_flight(
            source["source_platform"], source["source_post_id"], source["source_media_index"],
            exclude_batch_id=batch["id"],
        )) is not None:
            # Outro lote aberto ainda vai baixar o mesmo item: este job fica `pending` aguardando o primário (não é
            # reivindicado nem conta no backpressure); concluído o primário, liga à mídia (e processa se o lote
            # pede); primário falho/cancelado, baixa sozinho (seção 9). Sem segundo download no caso feliz.
            kwargs = {"is_duplicate": True, "duplicate_reason": "duplicate_in_flight", "primary_job_id": primary}
        job_id = self.ctx.jobs.create_ingest(
            batch_id=batch["id"],
            source=source,
            preset_id=batch["preset_id"],
            preset_name=batch["preset_name"],
            params=batch["params"] if process else None,
            **kwargs,
        )
        return job_id is not None
