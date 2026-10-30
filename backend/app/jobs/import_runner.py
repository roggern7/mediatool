"""Import runner: executa a listagem de perfis (lotes em `listing`), uma listagem por vez, em thread própria.

Listagem incremental e retomável (docs/BATCH_ROBUSTNESS.md, seção 0): cada trecho consome
`iter_profile_media(..., resume=<checkpoint salvo>, on_checkpoint=<persistir no lote>)`; os jobs de cada página e o
checkpoint são gravados numa transação. O trecho para (backpressure) quando o lote tem `MAX_PENDING_INGEST` ingests
ainda não baixados (ou o app inteiro tem `MAX_PENDING_INGEST_GLOBAL`) e retoma abaixo de `RESUME_PENDING_INGEST`;
para também em pausa do Instagram, falha de rede, cancelamento ou encerramento do app, e retoma do checkpoint (o
índice único dos jobs descarta itens repetidos). O lote só sai de `listing` quando a listagem termina
(`listing_state = done`), e ela só termina por quantidade atingida (`listing_end = limit`) ou pelo fim REAL do perfil
(`listing_end = profile_end`: checkpoint com `has_next = False`, isto é, `has_next_page=false`). Fim anômalo da
paginação (`LISTING_ANOMALY_DETAILS`) nunca conclui: `needs_attention` com o checkpoint, ou falha visível quando
acontece na 1a página sem nenhum item.

Vários perfis (grupo, migration 0012): os filhos são lotes comuns e entram na fila pela ordem de criação
(`group_position`). Um trecho de listagem por vez no app inteiro (esta thread). Dentro de um grupo o próximo filho só
começa quando o anterior terminou de listar, ou ficou parado por conta própria (`needs_attention`, backoff do lote,
pausa do disjuntor); enquanto o filho da vez espera backpressure ou o teto de páginas, os seguintes continuam na fila.

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
from app.db.repositories.batches import (
    LINKS_PARTIAL_MESSAGE,
    TYPE_FILTER_REASON,
    VIEWS_KEYS_MAX,
    load_listing_counts,
)
from app.db.repositories.common import canonical_json
from app.integrations.instagram.downloader import (
    LISTING_ANOMALY_DETAILS,
    InstagramError,
    ListingCheckpoint,
    RemoteMedia,
)
from app.integrations.instagram.validation import post_path_kind
from app.jobs.dash_ref import dash_json
from app.jobs.errors import JobCancelled, message_with_diagnostic, safe_diagnostic, safe_message
from app.jobs.pipeline import (
    LINKS_HOLD_DONE_MESSAGE,
    instagram_paused,
    links_query_lock,
    links_resolve_options,
    note_links_session_refusal,
)
from app.services import disk, edit_service
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
# 1a página da listagem com 0 publicações e sem próxima página (visto no real com limite temporário do Instagram, com o
# perfil tendo posts): o lote termina, mas com `listing_empty_first_page` e esta mensagem, não como fim do perfil.
EMPTY_FIRST_PAGE_MESSAGE = (
    "O Instagram não devolveu nenhuma publicação deste perfil agora (perfil sem posts, privado ou limite temporário). "
    "Tente de novo mais tarde."
)
# O provider encerrou o trecho sem erro, sem fim real do perfil e sem avançar nenhuma página: não é fim do perfil.
LISTING_STALLED_MESSAGE = (
    "A listagem parou antes do fim do perfil sem avançar. A importação não foi dada como concluída e o ponto de "
    "retomada foi mantido."
)


# Importação por link: erros que são DO LINK (o link fica `failed` e o lote segue para o próximo). Qualquer outro
# erro é do lote/ambiente: o link continua pendente e nenhum outro é pedido no trecho.
LINK_ERROR_CODES = frozenset({
    "content_unavailable", "profile_private", "profile_not_found", "unsupported_media", "invalid_input",
})
# Detail de `contract_changed` que é erro de UMA publicação: formato desconhecido e resposta de OUTRA publicação.
# `post_envelope_missing`: o documento público da publicação veio sem o envelope conhecido.
LINK_CONTRACT_DETAILS = frozenset({"post_shape_unknown", "post_mismatch", "post_envelope_missing"})
# Defesa do runner (vale para qualquer provider): a resposta trouxe mídia de uma publicação que não é a pedida.
LINK_MISMATCH_MESSAGE = (
    "O Instagram respondeu com outra publicação, diferente da pedida. Nada foi importado deste link."
)
LINKS_INTERNAL_ERROR = "Erro inesperado ao consultar um link. Consulte o log do aplicativo."
LINK_EMPTY_MESSAGE = "A publicação não trouxe nenhuma mídia que possa ser baixada."
LINKS_ALL_FAILED = "Nenhum dos {total} links pôde ser importado."
LINKS_ALL_FAILED_ONE = "O link não pôde ser importado."
LINKS_PARTIAL = LINKS_PARTIAL_MESSAGE
# Erro do lote/ambiente que NUNCA marca o link como falho (sessão, pausa do Instagram, rede): o link segue pendente.
_LINK_KEEP_PENDING_CODES = frozenset({
    "session_missing", "session_invalid", "session_expired", "checkpoint_required", "rate_limited", "action_blocked",
    "request_auth_failed", "network_error", "timeout",
})
# O MESMO link levou o lote a `needs_attention` este número de vezes: o link vira `failed` e o lote segue.
LINK_ATTENTION_LIMIT = 2
# Disjuntor: links SEGUIDOS falhos (nenhum resolvido entre eles) com links ainda pendentes -> `needs_attention`.
LINK_FAIL_BREAKER = 3
LINKS_FAILING_MESSAGE = (
    "3 links seguidos não puderam ser importados. Confira os links; se eles abrem no navegador, o Media Tool pode "
    "precisar de atualização. \"Retomar importação\" continua pelos próximos links."
)
# Lote por link: a consulta de uma publicação devolveu `access_refused`. O lote inteiro para e espera a ação do dono
# (`needs_attention` SEM `listing_retry_at`): nenhuma nova tentativa automática e nenhum outro link é pedido.
# O estado é a recusa em espera do lote (`BatchRepository.links_hold`), a MESMA que a renovação de URL consulta e
# arma (pipeline). "Retomar importação" é o rótulo real do botão.
LINKS_REFUSED_CODE = "access_refused"
LINKS_REFUSED_MESSAGE = (
    "O Instagram recusou a consulta desta publicação. Isso não indica sessão inválida. Não haverá nova tentativa "
    "automática: use \"Retomar importação\" para tentar de novo (uma consulta por vez) ou cancele o lote."
)


def _diagnostic_suffix(diagnostic: str) -> str:
    """Sufixo de log com o diagnóstico seguro do erro (`safe_diagnostic`); vazio se o erro não trouxe nenhum."""
    return f" [{diagnostic}]" if diagnostic else ""


def _filters_on(batch: dict[str, Any]) -> bool:
    """Lote com filtro de visualizações (mínimo 0017, máximo 0021) e/ou período de postagem (0019)."""
    return (batch.get("min_views") is not None or batch.get("max_views") is not None
            or batch.get("posted_after") is not None)


def _count_of(counts: dict[str, Any], key: str) -> int:
    """Contador válido (inteiro positivo) ou 0, como em presenters._views_filter."""
    value = counts.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _covers(batch: dict[str, Any]) -> bool:
    """Lote "Somente capas" (migration 0021): cada vídeo vira um job da CAPA dele (imagem), nunca do vídeo."""
    return batch.get("media_role") == "cover"


def _wanted_by_type(batch: dict[str, Any], item: RemoteMedia) -> bool:
    """Lote por link da importação unificada com "Somente vídeos"/"Somente capas" (`content_type` "videos"): só os
    vídeos da publicação viram job. Lotes por link antigos ("all") recebem todas as mídias, como antes."""
    return batch.get("content_type") != "videos" or item.media_type == "video"


def _is_link_error(exc: InstagramError) -> bool:
    """Erro DO LINK: os códigos de `LINK_ERROR_CODES` e o `contract_changed` de UMA publicação específica (detail
    `post_shape_unknown`: formato desconhecido; `post_mismatch`: a resposta é de outra publicação)."""
    if exc.code in LINK_ERROR_CODES:
        return True
    diagnostic = getattr(exc, "diagnostic", None)
    return (exc.code == "contract_changed" and isinstance(diagnostic, dict)
            and diagnostic.get("detail") in LINK_CONTRACT_DETAILS)


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
        self.anomaly = False  # o erro do trecho é um fim anômalo da paginação (não é fim do perfil)
        self.link_position: int | None = None  # lote por link: link que estava sendo pedido quando o trecho parou
        self.diagnostic = ""  # diagnóstico seguro do erro do trecho (safe_diagnostic), só para o log/mensagem

    @property
    def progressed(self) -> bool:
        return self.created > 0


class ImportRunner:
    def __init__(self, ctx: AppContext):
        # Lotes que já registraram "filtro de visualizações desativado" (uma linha por lote e execução).
        self._views_off_logged: set[int] = set()
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
        # Grupos cujo filho da vez espera backpressure: os filhos seguintes continuam na fila (sem mudar de estado).
        waiting_groups: set[int] = set()
        for batch in candidates:
            batch_id = batch["id"]
            group_id = batch.get("group_id")
            if group_id is not None and group_id in waiting_groups:
                continue
            if batch.get("import_mode") == "links" and batch["error_code"] == LINKS_REFUSED_CODE:
                # Lote por link com a recusa em espera (armada pelo runner ou por uma renovação de URL; inclusive
                # um lote agendado por uma versão anterior, ou interrompido com o trecho em andamento): nunca é
                # escolhido. Fica em `needs_attention` sem nova tentativa, o estado que "Retomar importação" aceita.
                if batch["listing_state"] != "needs_attention" or batch["listing_retry_at"]:
                    self._set_listing(batch_id, listing_state="needs_attention", listing_retry_at=None)
                continue
            if batch["listing_state"] == "needs_attention" and not batch["listing_retry_at"]:
                # Tentativas automáticas esgotadas: só a ação do usuário (resume-listing) retoma.
                continue
            if (batch["listing_retry_at"] or "") > now or self._batch_paused(batch, now):
                continue
            waiting = batch["listing_state"] == "waiting_backpressure"
            pending = self.ctx.jobs.pending_ingest_count(batch_id)
            full = (pending >= RESUME_PENDING_INGEST) if waiting else (pending + PAGE_ESTIMATE > MAX_PENDING_INGEST)
            if full or global_full or self._process_backlog_full(batch):
                if not waiting:
                    self._set_listing(batch_id, listing_state="waiting_backpressure")
                if group_id is not None:
                    waiting_groups.add(group_id)
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
        from app.integrations.instagram.web.provider import page_rate_limit   # local: evita ciclo de import

        # INSTAGRAM_PAGE_RATE_LIMIT=0 (POC de escala): sem teto horário não há espera; campos antigos são limpos.
        per_hour = page_rate_limit(getattr(self.ctx.settings, "env", None))
        disabled = per_hour == 0
        # Valor persistido ilegível = janela cheia pelo teto VIGENTE.
        gate.restore_listing_pages(self.ctx.app_settings.get_state(PAGES_STATE_KEY),
                                   max_per_hour=per_hour or DEFAULT_MAX_PAGES_PER_HOUR)
        rows = self.ctx.db.fetchall(
            "SELECT id, status, listing_state FROM batches WHERE rate_limit_resume_at IS NOT NULL")
        for row in rows:
            # Teto VIGENTE, não o gravado no lote: depois de o teto subir (300 -> 600) a janela pode já ter vaga.
            used, seconds = gate.listing_window(per_hour or DEFAULT_MAX_PAGES_PER_HOUR)
            if (not disabled and seconds is not None and row["status"] == "listing"
                    and row["listing_state"] == "running"):
                self.ctx.batches.update_listing(
                    row["id"], rate_limit_resume_at=utc_now(seconds), rate_limit_pages_used=used,
                    rate_limit_pages_max=per_hour)
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
        if batch.get("import_mode") == "links":
            self._run_links(batch)
            return
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
            "Lote %s: listando perfil %s (conteúdo=%s, limite=%s, %s%s)",
            batch_id, batch["source_profile"], batch["content_type"], limit,
            "retomada" if resume is not None else "início",
            ("" if batch.get("min_views") is None else f", min_views={batch['min_views']}")
            + ("" if batch.get("max_views") is None else f", max_views={batch['max_views']}")
            + ("" if batch.get("posted_after") is None else f", publicadas desde {batch['posted_after']}")
            + (", só capas" if _covers(batch) else ""),
        )
        # Transição condicional e atômica: se o lote foi cancelado depois de escolhido (`_next_eligible`) e antes
        # daqui, nada é listado. `_current` já está registrado: um cancelamento posterior aciona `cancel`.
        # (Lote `paused`: o motivo da pausa anterior não vale mais.)
        if not self.ctx.batches.start_listing(batch_id, clear_error=batch["listing_state"] == "paused"):
            with self._lock:
                self._current = None
                self._current_thread = None
            logger.info("Lote %s: cancelado ou encerrado antes do início do trecho; nada foi listado", batch_id)
            return

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
            # A cada página confere o banco (não só o aviso em memória): lote cancelado para na fronteira da página,
            # sem pedir a próxima. O `_flush` repete a conferência dentro da transação dele.
            if self._is_cancelled(batch_id):
                cancel.set()
                return
            for item in buffer:
                if item.listing_ref is not None:
                    chunk.page_estimate = max(chunk.page_estimate, item.listing_ref.page_size)
            chunk.page_estimate = max(chunk.page_estimate, chunk.page_items)
            chunk.page_items = 0
            chunk.created += self._flush(batch, buffer, checkpoint)
            if self._is_cancelled(batch_id):
                cancel.set()
                return
            chunk.pages_ok += 1
            if chunk.created > 0 and batch.get("listing_attention_tries"):
                # Progresso REAL (job novo criado): as tentativas automáticas de needs_attention recomeçam. Página
                # com checkpoint mas sem nenhum job novo (ex.: o servidor repetindo os mesmos posts) não zera.
                self.ctx.batches.update_listing(batch_id, listing_attention_tries=0)
                batch["listing_attention_tries"] = 0
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
                chunk.diagnostic = safe_diagnostic(exc)
                diagnostic = getattr(exc, "diagnostic", None)
                chunk.anomaly = isinstance(diagnostic, dict) and diagnostic.get("detail") in LISTING_ANOMALY_DETAILS
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
        # Idem para os vereditos do filtro de visualizações da página aberta (até o último nó consumido).
        pending_views = getattr(failure, "listing_views", None)
        if (_filters_on(batch) and isinstance(pending_views, dict) and pending_views
                and not self._is_cancelled(batch_id)):
            partial = canonical_json(pending_views)
            if partial != batch.get("listing_views_partial"):
                self.ctx.batches.update_listing(batch_id, listing_views_partial=partial)
                batch["listing_views_partial"] = partial
        self._after_chunk(batch, chunk, error, yielded, chunk_limit)

    def _consume(
        self, batch: dict[str, Any], chunk: _Chunk, buffer: list[RemoteMedia], cancel: threading.Event,
        resume: ListingCheckpoint | None, chunk_limit: int | None, on_checkpoint, created_before: int,
    ) -> int:
        """Consome o gerador da listagem; retorna quantos itens o provider entregou."""
        batch_id = batch["id"]
        limit = batch["requested_limit"]
        yielded = 0
        # Filtros (migrations 0017 e 0019): o kwarg só é repassado quando o lote o tem (providers e fakes antigos não o
        # conhecem; sem filtro a chamada é idêntica à de antes). O corte do período é o GRAVADO na criação.
        extra: dict[str, Any] = {}
        if batch.get("min_views") is not None or batch.get("max_views") is not None:
            from app.services.import_service import MIN_VIEWS_ENABLED

            if MIN_VIEWS_ENABLED:
                # Retomada e reinício usam os valores GRAVADOS no lote (mínimo e/ou máximo).
                if batch.get("min_views") is not None:
                    extra["min_views"] = batch["min_views"]
                if batch.get("max_views") is not None:
                    extra["max_views"] = batch["max_views"]
            elif batch_id not in self._views_off_logged:
                # Lote criado com o filtro antes de ele ser desligado: lista SEM ele (nenhuma requisição à aba Reels).
                self._views_off_logged.add(batch_id)
                logger.warning("Lote %s: filtro de visualizações desativado nesta versão; a listagem segue sem ele",
                               batch_id)
        if batch.get("posted_after") is not None:
            extra["posted_after"] = batch["posted_after"]
        if _covers(batch):
            extra["covers"] = True   # só decide o provider (capas só pelo método web)
        items = self.ctx.downloader_factory().iter_profile_media(
            batch["source_profile"], batch["content_type"], chunk_limit, cancel=cancel, resume=resume,
            on_checkpoint=on_checkpoint, **extra,
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
                self.ctx.batches.add_listing_views(batch_id, merge_partial=True)
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
        if current.get("import_mode") == "links":
            self._after_links_chunk(current, chunk, error)
            return
        if error is not None and error[0] == "unsupported_media" and current["listing_next_page"] is not None:
            # Páginas só com publicações de tipo não suportado no meio do lote: os itens foram pulados (contados pelo
            # provider) e os checkpoints gravados; a listagem segue pelas regras normais de fim, sem erro.
            logger.info("Lote %s: páginas sem mídia suportada puladas; a listagem continua do checkpoint", batch_id)
            error = None
            if current["listing_has_next"] and chunk.pages_ok > 0:
                self._set_listing(batch_id, listing_state="pending")  # próximo trecho do checkpoint
                return
        if error is not None:
            self._handle_listing_error(current, error, chunk)
            return
        if chunk.stop == "backpressure":
            self._set_listing(batch_id, listing_state="waiting_backpressure")
            logger.info("Lote %s: listagem aguardando downloads (backpressure)", batch_id)
            return
        if chunk.stop == "paused":
            self._set_listing(batch_id, listing_state="paused")
            logger.info("Lote %s: listagem pausada (Instagram pausado); será retomada do checkpoint", batch_id)
            return
        limit = batch["requested_limit"]
        found = self.ctx.jobs.count_for_batch(batch_id)
        if chunk.stop == "limit" or self._limit_reached(batch_id, limit):
            self._finish_listing(batch_id, None, None, listing_end="limit")
            return
        has_next = current["listing_has_next"]
        if has_next is None or not has_next:
            # ÚNICO fim de perfil: o provider encerrou sem erro e o último checkpoint diz que não há próxima página
            # (has_next_page=false). Sem checkpoint nenhum = provider que não pagina por checkpoint (gallery-dl).
            message = END_OF_PROFILE_MESSAGE.format(found=found) if limit is not None and found < limit else None
            if current.get("listing_period_end"):
                # Fim pelo corte do período de postagem (checkpoint `period_end`): não é o fim do perfil.
                message = None
                logger.info("Lote %s: listagem chegou às publicações anteriores ao período pedido", batch_id)
            elif found == 0 and current.get("listing_pages") == 1 and not current.get("listing_posts_seen"):
                # 1a página com 0 publicações e sem próxima: termina, mas com motivo próprio (não "fim do perfil").
                message = EMPTY_FIRST_PAGE_MESSAGE
                self.ctx.batches.update_listing(batch_id, listing_empty_first_page=1)
                logger.warning("Lote %s: a 1a página do perfil veio vazia e sem próxima página", batch_id)
            self._finish_listing(batch_id, None, message, listing_end="profile_end")
            return
        if chunk.pages_ok > 0 or (chunk_limit is not None and yielded >= chunk_limit and chunk.progressed):
            # Ainda há páginas e o trecho avançou (o provider parou pelo teto de páginas do trecho, ou pelo limite
            # contando itens repetidos da página retomada): novo trecho do checkpoint.
            self._set_listing(batch_id, listing_state="pending")
            return
        # Trecho encerrado sem erro, sem avançar e com próxima página pendente: não é fim do perfil.
        chunk.anomaly = True
        self._handle_listing_error(current, ("contract_changed", LISTING_STALLED_MESSAGE), chunk)

    def _handle_listing_error(self, batch: dict[str, Any], error: tuple[str, str], chunk: _Chunk) -> None:
        batch_id = batch["id"]
        code, message = error
        if self.ctx.worker is not None and self.ctx.worker.pause_for(code):
            # Pausa do Instagram: a listagem fica pausada e retoma do checkpoint quando a pausa termina.
            self._set_listing(batch_id, listing_state="paused", error_code=code, error_message=message)
            logger.warning("Lote %s: listagem pausada por %s; será retomada do checkpoint%s", batch_id, code,
                           _diagnostic_suffix(chunk.diagnostic))
            return
        # O checkpoint nunca é apagado aqui: recomeçar da página 1 só por ação do usuário (restart-listing).
        # Fim anômalo da paginação não é recusa do cursor: sem nova tentativa em 1 min (nenhuma requisição extra).
        cursor_refused = (chunk.from_cursor and chunk.pages_ok == 0 and code in _CURSOR_REFUSED_CODES
                          and not chunk.anomaly)
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
        # Lote por link: erro do lote/ambiente nunca o conclui (os links pendentes são o ponto de retomada).
        mid_batch = (batch["listing_next_page"] is not None and bool(batch["listing_has_next"])
                     or batch.get("import_mode") == "links")
        # Fim anômalo ainda na 1a página (sem checkpoint) mas com itens já listados: também não conclui; a nova
        # tentativa pede a página 1 de novo e o índice único descarta os repetidos. Sem item nenhum, o lote falha
        # com o erro visível (abaixo), nunca como "perfil vazio concluído".
        first_page_partial = chunk.anomaly and not mid_batch and self.ctx.jobs.count_for_batch(batch_id) > 0
        if cursor_refused or mid_batch or first_page_partial:
            tries = int(batch["listing_attention_tries"] or 0)
            retry_at = utc_now(ATTENTION_RETRY_SECONDS) if tries < ATTENTION_MAX_TRIES else None
            self._set_listing(
                batch_id, listing_state="needs_attention", error_code=code, error_message=message,
                listing_retry_at=retry_at, listing_attention_tries=min(tries + 1, ATTENTION_MAX_TRIES),
                listing_cursor_refusals=1,  # a nova tentativa automática recusada volta direto para needs_attention
            )
            logger.warning("Lote %s: listagem aguardando ação (%s); checkpoint mantido%s%s", batch_id, code,
                           "; nova tentativa automática em 6 h" if retry_at else "",
                           _diagnostic_suffix(chunk.diagnostic))
            return
        self._finish_listing(batch_id, error, None, diagnostic=chunk.diagnostic)

    def _pause_with_backoff(self, batch: dict[str, Any], chunk: _Chunk, code: str, message: str, **fields: Any) -> None:
        """Listagem `paused` com o backoff do lote (1m, 5m, 15m, 1h, 1h...); retoma do checkpoint no prazo."""
        level = 0 if chunk.progressed else int(batch["listing_retry_level"] or 0)
        delay = LISTING_BACKOFF_SECONDS[min(level, len(LISTING_BACKOFF_SECONDS) - 1)]
        self._set_listing(
            batch["id"], listing_state="paused", listing_retry_level=level + 1, listing_retry_at=utc_now(delay),
            error_code=code, error_message=message, **fields,
        )
        logger.warning("Lote %s: falha na listagem (%s); retomada do checkpoint em %ss%s", batch["id"], code, delay,
                       _diagnostic_suffix(chunk.diagnostic))

    def _set_listing(self, batch_id: int, **fields: Any) -> bool:
        """Grava o estado de fim de trecho SÓ se o lote ainda está em `listing` (conferência e gravação na mesma
        transação, como em `_flush`): um lote cancelado, ou já fora de `listing`, nunca é tirado do estado terminal por
        uma gravação atrasada do runner. False se nada foi gravado."""
        with self.ctx.db.transaction():
            current = self.ctx.batches.get(batch_id)
            if current is None or current["status"] != "listing":
                return False
            self.ctx.batches.update_listing(batch_id, **fields)
            return True

    def _finish_listing(self, batch_id: int, error: tuple[str, str] | None, message: str | None, *,
                        listing_end: str | None = None, diagnostic: str = "") -> None:
        """Listagem encerrada (`done`): o lote sai de `listing` e termina quando todos os jobs forem terminais.
        `listing_end`: "limit" (quantidade atingida) ou "profile_end" (fim real do perfil); None em erro.
        Lote cancelado (ou já fora de `listing`) não é tocado: a conferência do status e todas as gravações ficam na
        mesma transação, então o cancelamento que chega no meio nunca é desfeito (`running`/`completed`)."""
        batches = self.ctx.batches
        failed = False
        with self.ctx.db.transaction():
            current = batches.get(batch_id)
            if current is None or current["status"] != "listing":
                logger.info("Lote %s: fim da listagem ignorado (lote cancelado ou já encerrado)", batch_id)
                return
            # Página final sem checkpoint (limite no meio dela): os descartes até o último item gravado entram agora,
            # uma única vez (a coluna parcial é apagada na mesma instrução).
            batches.add_listing_dropped(batch_id, merge_partial=True)
            batches.add_listing_views(batch_id, merge_partial=True)
            batches.update_listing(batch_id, listing_state="done", listing_cursor=None, listing_end=listing_end)
            batches.sync_found(batch_id)
            found = batches.get(batch_id)["found"]
            if error is not None and found == 0:
                batches.update(batch_id, status="failed", error_code=error[0], error_message=error[1],
                               finished_at=utc_now())
                failed = True
            else:
                fields: dict[str, Any] = {"status": "running", "error_code": None, "error_message": message}
                if error is not None:
                    fields.update(error_code=error[0], error_message=error[1])
                batches.update(batch_id, **fields)
                batches.refresh_status(batch_id)
        if failed:
            logger.warning("Lote %s falhou na listagem: %s%s", batch_id, error[0], _diagnostic_suffix(diagnostic))
            return
        if error is not None:
            logger.warning("Lote %s: listagem parcial (%s itens) com erro %s%s", batch_id, found, error[0],
                           _diagnostic_suffix(diagnostic))
        logger.info("Lote %s: listagem concluída com %s itens", batch_id, found)
        self._log_views(batch_id)
        self.ctx.wake_worker()

    def _log_views(self, batch_id: int) -> None:
        """Totais dos filtros no fim da listagem (só totais, nunca contagem individual). Lote com filtro de
        visualizações que fecha sem nenhum aprovado e com vídeos sem contagem: UM aviso."""
        current = self.ctx.batches.get(batch_id)
        if current is None or not _filters_on(current):
            return
        # Mesmos totais do `views_filter` da API (presenters._views_filter): todas as chaves, inclusive "acima do
        # máximo", somando a página aberta (`listing_views_partial`, se houver).
        stored = load_listing_counts(current.get("listing_views"))
        partial = load_listing_counts(current.get("listing_views_partial"))
        merged = {key: _count_of(stored, key) + _count_of(partial, key) for key in VIEWS_KEYS_MAX}
        approved, below, no_count, above = merged["approved"], merged["below"], merged["no_count"], merged["above"]
        logger.info("Lote %s: filtros: %s analisados, %s aprovados, %s abaixo do mínimo, %s acima do máximo, %s sem "
                    "contagem, %s fora do período", batch_id, sum(merged.values()), approved, below, above, no_count,
                    merged["out_of_period"])
        if current.get("min_views") is not None and approved == 0 and no_count > 0:
            logger.warning("Lote %s: nenhum vídeo com contagem de visualizações aprovado (%s sem contagem); verifique "
                           "se a consulta do Instagram traz esse campo", batch_id, no_count)

    # ---- importação por link (migration 0014) ------------------------------------------------------------------
    def _run_links(self, batch: dict[str, Any]) -> None:
        """Um trecho do lote por link: resolve os links pendentes em ordem, um por vez, até acabar ou até um motivo
        de parada (pausa, backpressure, erro do lote/ambiente, cancelamento, encerramento)."""
        batch_id = batch["id"]
        cancel = threading.Event()
        with self._lock:
            self._current = (batch_id, cancel)
            self._current_thread = threading.get_ident()
        logger.info("Lote %s: resolvendo links (%s)", batch_id,
                    "início" if batch["listing_state"] in (None, "pending") else "retomada")
        try:
            with links_query_lock(self.ctx, batch_id, cancel):
                # Sob a trava do lote: a recusa em espera armada por uma renovação depois da escolha do lote não é
                # apagada pelo `clear_error` (o próximo `_next_eligible` deixa o lote em `needs_attention`).
                started = (not self.ctx.batches.links_hold(batch_id)
                           and self.ctx.batches.start_listing(batch_id, clear_error=batch["listing_state"] == "paused"))
        except JobCancelled:
            started = False
        if not started:
            with self._lock:
                self._current = None
                self._current_thread = None
            logger.info("Lote %s: cancelado, encerrado ou aguardando ação manual antes do início do trecho; nenhum "
                        "link foi pedido", batch_id)
            return
        chunk = _Chunk()
        error: tuple[str, str] | None = None
        try:
            self._consume_links(batch, chunk, cancel)
        except InstagramPaused:
            # O portão recusou a requisição (pausa publicada entre links): nada foi enviado; o link segue pendente.
            chunk.stop = "paused"
        except InstagramError as exc:
            if not cancel.is_set():
                error = (exc.code, safe_message(exc.message))
                chunk.retryable = bool(exc.retryable)
                chunk.diagnostic = safe_diagnostic(exc)
        except Exception:  # noqa: BLE001
            if not cancel.is_set():
                logger.exception("Lote %s: erro inesperado ao resolver um link", batch_id)
                error = ("internal_error", LINKS_INTERNAL_ERROR)
        finally:
            with self._lock:
                self._current = None
                self._current_thread = None
        self._after_chunk(batch, chunk, error, 0, None)

    def _consume_links(self, batch: dict[str, Any], chunk: _Chunk, cancel: threading.Event) -> None:
        """Resolve os links pendentes do lote. Erro DO LINK marca só aquele link como falho e segue; qualquer outro
        erro sobe (o link continua pendente e nenhum outro é pedido neste trecho)."""
        batch_id = batch["id"]
        batches = self.ctx.batches
        downloader = self.ctx.downloader_factory()
        while True:
            if cancel.is_set() or self._is_cancelled(batch_id):
                return
            link = batches.next_pending_link(batch_id)
            if link is None:
                return
            # Seção 8: nenhuma requisição durante pausa. Backpressure: mesmos tetos da listagem de perfil.
            if instagram_paused(self.ctx.worker):
                chunk.stop = "paused"
                return
            if (self.ctx.jobs.pending_ingest_count(batch_id) + PAGE_ESTIMATE > MAX_PENDING_INGEST
                    or batches.pending_ingest_global() + PAGE_ESTIMATE > MAX_PENDING_INGEST_GLOBAL
                    or self._process_backlog_full(batch)):
                chunk.stop = "backpressure"
                return
            chunk.link_position = link["position"]  # se o trecho parar por erro, foi neste link
            failure: tuple[str, str] | None = None
            # Falha de formato do documento aberto com a sessão (`session_off`): o link falha, mas não conta no
            # disjuntor `links_failing` (a sessão já foi desligada no lote; os links públicos seguem).
            breaker = True
            items: list[RemoteMedia] = []
            try:
                # Trava ÚNICA das consultas do lote (a mesma da renovação de URL): confere a recusa em espera sob
                # ela, consulta, e arma a espera antes de soltá-la se a consulta for recusada.
                with links_query_lock(self.ctx, batch_id, cancel):
                    if batches.links_hold(batch_id):
                        # Uma renovação de URL foi recusada: o link continua pendente e nada é pedido.
                        chunk.link_position = None
                        chunk.stop = "refused_hold"
                        return
                    try:
                        # O tipo do caminho (/p/ ou /reel/) vem do link colado; o provider nunca escolhe sozinho.
                        # Publicação restrita: o provider web tenta UMA vez com a sessão, menos depois de uma recusa
                        # dessa tentativa neste lote (os links seguintes ficam só no documento público).
                        items = downloader.resolve_post(link["shortcode"], cancel=cancel,
                                                        path_kind=post_path_kind(link["url"]),
                                                        **links_resolve_options(self.ctx, batch_id))
                    except InstagramError as exc:
                        note_links_session_refusal(self.ctx, batch_id, exc)
                        if exc.code == LINKS_REFUSED_CODE:
                            # Vale mesmo com o trecho cancelado/encerrando durante a consulta: a recusa aconteceu.
                            batches.arm_links_hold(batch_id, LINKS_REFUSED_MESSAGE)
                        if cancel.is_set() or not _is_link_error(exc):
                            raise
                        failure = (exc.code, safe_message(exc.message))
                        breaker = getattr(exc, "session_off", False) is not True
                        # Só a posição do link no lote: o shortcode nunca vai para o log.
                        logger.warning("Lote %s: link %s não pôde ser importado (%s)%s", batch_id, link["position"],
                                       exc.code, _diagnostic_suffix(safe_diagnostic(exc)))
            except JobCancelled:
                return  # cancelado ou app encerrando enquanto esperava a trava do lote: o link continua pendente
            if cancel.is_set():
                return  # cancelado ou app encerrando durante a requisição: o link continua pendente
            # A requisição do link terminou: erro local depois daqui (ex.: ao gravar os jobs) não é erro do link.
            chunk.link_position = None
            if failure is None and any(item.source_post_id != link["shortcode"] for item in items):
                # Resposta de OUTRA publicação (comparação exata, diferenciando maiúsculas): nada é gravado e o link
                # não fica resolvido. O provider web já recusa antes (`post_mismatch`); isto cobre os demais.
                failure = ("contract_changed", LINK_MISMATCH_MESSAGE)
                items = []
                logger.warning("Lote %s: link %s não pôde ser importado (contract_changed) [detail=post_mismatch]",
                               batch_id, link["position"])
            if failure is None and not items:
                failure = ("content_unavailable", LINK_EMPTY_MESSAGE)
            if failure is not None:
                if self._fail_link(batch, link["position"], *failure, breaker=breaker):
                    chunk.stop = "links_failing"
                    return
                continue
            created = 0
            wanted = [item for item in items if _wanted_by_type(batch, item)]
            with self.ctx.db.transaction():
                if self._is_cancelled(batch_id):
                    return
                # Jobs de todas as mídias (ordem do carrossel) e o link resolvido na MESMA transação: se o app cair
                # antes, o link é pedido de novo e o índice único dos jobs descarta os repetidos.
                for item in wanted:
                    if self._add_item(batch, item):
                        created += 1
                if len(wanted) < len(items):
                    # Itens de outro tipo (ex.: fotos com "Somente vídeos"): filtrados de propósito, contados uma vez
                    # (mesma transação que resolve o link).
                    batches.add_listing_dropped(batch_id, {"expected": {TYPE_FILTER_REASON: len(items) - len(wanted)}})
                batches.resolve_link(batch_id, link["position"], items_found=len(items),
                                     profile=items[0].source_profile or None)
                if batch["listing_retry_level"] or batch["listing_attention_tries"] or batch["link_fail_streak"]:
                    # Link resolvido: o backoff, as tentativas automáticas de needs_attention e a sequência de
                    # links falhos recomeçam.
                    batches.update_listing(batch_id, listing_retry_level=0, listing_attention_tries=0,
                                           link_fail_streak=0)
                    batch["listing_retry_level"] = batch["listing_attention_tries"] = batch["link_fail_streak"] = 0
                batches.sync_found(batch_id)
            chunk.created += created
            chunk.pages_ok += 1
            if self.ctx.worker is not None and getattr(downloader, "resolve_post_authenticated", True):
                # Publicação entregue por consulta autenticada. O documento público (provider web) é lido SEM a
                # sessão do dono e não prova nada sobre ela.
                self.ctx.worker.note_auth_success()
            if created:
                self.ctx.wake_worker()

    def _fail_link(self, batch: dict[str, Any], position: int, code: str, message: str, *,
                   breaker: bool = True) -> bool:
        """Marca o link como falho e conta a sequência de links falhos. True se o disjuntor disparou: é a
        `LINK_FAIL_BREAKER`-ésima falha seguida e ainda há link pendente (sem pendentes o lote conclui normal).
        `breaker=False`: o link falha sem mexer na sequência (nunca dispara)."""
        batches = self.ctx.batches
        batches.fail_link(batch["id"], position, code=code, message=message)
        if not breaker:
            return False
        streak = int(batch.get("link_fail_streak") or 0) + 1
        tripped = streak >= LINK_FAIL_BREAKER and batches.next_pending_link(batch["id"]) is not None
        batch["link_fail_streak"] = 0 if tripped else streak   # disparou: a retomada começa do zero
        batches.update_listing(batch["id"], link_fail_streak=batch["link_fail_streak"])
        return tripped

    def _links_refused(self, batch: dict[str, Any], chunk: _Chunk, message: str) -> bool:
        """Lote por link com a consulta de uma publicação recusada (`access_refused`): o lote para em
        `needs_attention` SEM `listing_retry_at` (nenhuma nova tentativa automática; `_next_eligible` não o escolhe)
        e nenhum outro link é pedido. Só "Retomar" (resume-listing) faz uma nova consulta; cancelar encerra.

        `LINK_ATTENTION_LIMIT` continua valendo: recusado pela 2a vez, o MESMO link fica `failed`, mas o lote
        continua parado (o próximo link só é pedido por outra ação do dono). O disjuntor `links_failing` não é
        acionado aqui: ele agendaria a nova tentativa de 6 h. Retorna False só quando esse link era o último
        pendente (não há mais o que consultar: o lote encerra normalmente)."""
        batch_id = batch["id"]
        batches = self.ctx.batches
        position = chunk.link_position
        if position is not None and batches.bump_link_attention(batch_id, position) >= LINK_ATTENTION_LIMIT:
            logger.warning("Lote %s: link %s marcado como falho depois de repetir o erro %s", batch_id, position,
                           LINKS_REFUSED_CODE)
            self._fail_link(batch, position, LINKS_REFUSED_CODE, message_with_diagnostic(message, chunk.diagnostic))
            if batches.next_pending_link(batch_id) is None:
                # A recusa em espera continua armada (`_consume_links`): quem encerra decide se ela ainda vale.
                return False
        self._set_listing(
            batch_id, listing_state="needs_attention", error_code=LINKS_REFUSED_CODE,
            error_message=message_with_diagnostic(LINKS_REFUSED_MESSAGE, chunk.diagnostic), listing_retry_at=None,
        )
        logger.warning("Lote %s: consulta por link recusada (%s); aguardando ação manual, sem nova tentativa "
                       "automática%s", batch_id, LINKS_REFUSED_CODE, _diagnostic_suffix(chunk.diagnostic))
        return True

    def _after_links_chunk(self, batch: dict[str, Any], chunk: _Chunk, error: tuple[str, str] | None) -> None:
        """Fim de um trecho do lote por link, sob a trava de consultas do lote: a conferência da recusa em espera
        e a gravação do estado/erro do fim do trecho acontecem juntas, então uma renovação recusada não arma a
        espera no meio (ela armaria e a gravação a apagaria). Nada aqui consulta o Instagram nem adquire o portão
        (sem inversão de travas). A espera pela trava é interrompida pelo encerramento do app."""
        batch_id = batch["id"]
        current: dict[str, Any] | None = None
        try:
            with links_query_lock(self.ctx, batch_id, self._stop):
                # A espera pela trava pode durar uma renovação em voo: o lote é RELIDO aqui (status, estado da
                # listagem, espera armada). Cancelado ou já fora de `listing` enquanto isso: nada é gravado.
                current = self.ctx.batches.get(batch_id)
                if current is not None and current["status"] == "listing":
                    self._after_links_chunk_locked(current, chunk, error)
                    return
        except JobCancelled:
            logger.info("Lote %s: fim do trecho interrompido pelo encerramento; os links pendentes serão retomados",
                        batch_id)
            return
        if current is not None and current["status"] == "cancelled":
            # Cancelado durante a espera: vale o encerramento de lote cancelado de `_after_chunk` (fora da trava).
            self._after_chunk(batch, chunk, error, 0, None)

    def _after_links_chunk_locked(self, batch: dict[str, Any], chunk: _Chunk,
                                  error: tuple[str, str] | None) -> None:
        """Fim de um trecho do lote por link (já conferidos cancelamento e encerramento do app)."""
        batch_id = batch["id"]
        if error is not None and error[0] == LINKS_REFUSED_CODE:
            if self._links_refused(batch, chunk, error[1]):
                return
            error = None  # o link recusado pela 2a vez era o último pendente: o lote encerra pelas regras abaixo
        elif self.ctx.batches.links_hold(batch_id) and self.ctx.batches.next_pending_link(batch_id) is not None:
            # Recusa em espera armada por uma renovação de URL durante o trecho (qualquer que tenha sido o motivo
            # da parada dele): o lote espera a ação do dono, sem contar tentativa de link e sem agendar nada.
            self._set_listing(batch_id, listing_state="needs_attention", listing_retry_at=None)
            logger.warning("Lote %s: renovação de link recusada (%s); links aguardando ação manual, sem nova "
                           "tentativa automática", batch_id, LINKS_REFUSED_CODE)
            return
        if error is not None:
            code, message = error
            position = chunk.link_position
            stuck = (position is not None and not chunk.retryable and code not in _LINK_KEEP_PENDING_CODES
                     and self.ctx.batches.bump_link_attention(batch_id, position) >= LINK_ATTENTION_LIMIT)
            if not stuck:
                # Pausa do Instagram -> `paused`; rede/retryable -> backoff; o resto -> `needs_attention`.
                self._handle_listing_error(batch, error, chunk)
                return
            # O MESMO link levou o lote a `needs_attention` de novo: ele fica `failed` com esse erro e o lote segue
            # (um link que sempre falha não trava os seguintes).
            logger.warning("Lote %s: link %s marcado como falho depois de repetir o erro %s", batch_id, position, code)
            if self._fail_link(batch, position, code, message):
                chunk.stop = "links_failing"
            else:
                self.ctx.batches.update_listing(batch_id, error_code=None, error_message=None)
        if chunk.stop == "links_failing":
            # Disjuntor: needs_attention (nova tentativa automática de 6 h, máx. 4; "Retomar" segue pelos pendentes).
            self._handle_listing_error(batch, ("links_failing", LINKS_FAILING_MESSAGE), chunk)
            return
        if chunk.stop == "backpressure":
            self._set_listing(batch_id, listing_state="waiting_backpressure")
            logger.info("Lote %s: links aguardando downloads (backpressure)", batch_id)
            return
        if chunk.stop == "paused":
            self._set_listing(batch_id, listing_state="paused")
            logger.info("Lote %s: links pausados (Instagram pausado); os pendentes serão retomados", batch_id)
            return
        counts = self.ctx.batches.link_counts([batch_id]).get(batch_id, {})
        if counts.get("pending"):
            self._set_listing(batch_id, listing_state="pending")  # novo trecho dos links pendentes
            return
        total, failed = counts.get("total", 0), counts.get("failed", 0)
        if self.ctx.jobs.count_for_batch(batch_id) == 0:
            # Nenhum job criado: todos os links falharam; o lote falha com erro visível.
            message = LINKS_ALL_FAILED_ONE if total == 1 else LINKS_ALL_FAILED.format(total=total)
            self._finish_listing(batch_id, ("links_failed", message), None)
            return
        if self.ctx.batches.links_hold(batch_id):
            # Recusa em espera ainda armada (ex.: o último link pendente foi recusado pela 2a vez): o lote sai de
            # `listing` SEM desarmar. Quando o lote conclui sem job falho que o dono possa repetir, a espera é
            # desarmada e o aviso normal volta (`BatchRepository.refresh_status`, chamado já em `_finish_listing`).
            self._finish_listing(batch_id, (LINKS_REFUSED_CODE, LINKS_HOLD_DONE_MESSAGE), None)
            return
        message = LINKS_PARTIAL.format(failed=failed, total=total) if failed else None
        self._finish_listing(batch_id, None, message)

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
            if self._is_cancelled(batch_id):
                # Cancelado (mesma transação que gravaria os jobs e o checkpoint): nada é criado nem avançado.
                buffer.clear()
                return 0
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
                    # Página entregue: backoff e recusas do cursor recomeçam (as tentativas de needs_attention só
                    # com job novo criado: ver on_checkpoint).
                    listing_retry_level=0, listing_cursor_refusals=0,
                    listing_dropped_partial=None,  # a página fechou: vale a contagem inteira dela, abaixo
                )
                # Descartes da página, na mesma transação do checkpoint dela: cada página soma uma única vez.
                page_counts = getattr(checkpoint, "page_counts", None)
                if page_counts:
                    self.ctx.batches.add_listing_dropped(batch_id, page_counts)
                batch["listing_dropped_partial"] = None
                if _filters_on(batch):
                    # Vereditos dos filtros da página, idem (o parcial dela é apagado junto).
                    self.ctx.batches.update_listing(batch_id, listing_views_partial=None)
                    self.ctx.batches.add_listing_views(batch_id, getattr(checkpoint, "page_views", None))
                    batch["listing_views_partial"] = None
                if getattr(checkpoint, "period_end", False):
                    # Fim pelo corte do período (migration 0019), na mesma transação do checkpoint `has_next = False`.
                    self.ctx.batches.update_listing(batch_id, listing_period_end=1)
                    batch["listing_period_end"] = 1
            elif last is not None:
                # Página ainda sem checkpoint: guarda (como estado, sem somar) os descartes dela até o último item
                # gravado. Página refeita sobrescreve; só entra no total se a listagem terminar aqui.
                dropped = getattr(last, "listing_dropped", None)
                partial = canonical_json(dropped) if dropped else None
                if partial != batch.get("listing_dropped_partial"):
                    self.ctx.batches.update_listing(batch_id, listing_dropped_partial=partial)
                    batch["listing_dropped_partial"] = partial
                if _filters_on(batch):
                    views = getattr(last, "listing_views", None)
                    partial = canonical_json(views) if views else None
                    if partial != batch.get("listing_views_partial"):
                        self.ctx.batches.update_listing(batch_id, listing_views_partial=partial)
                        batch["listing_views_partial"] = partial
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
            "dash_streams": dash_json(item),
        }
        role = "original"
        if _covers(batch) and (item.media_type != "video" or not data["thumbnail_url"]):
            # Defesa: o provider web já descarta fotos em "Somente capas"; um item sem capa de vídeo nunca vira job.
            logger.warning("Lote %s: item sem capa de vídeo ignorado em \"Somente capas\"", batch["id"])
            return False
        if _covers(batch):
            # "Somente capas": o job baixa só a capa do vídeo (a maior de `image_versions2`, já em `thumbnail_url`)
            # como imagem; o vídeo nunca é baixado. Renovação de URL expirada: a pipeline usa a capa renovada.
            role = "cover"
            source.update(media_type="image", media_url=data["thumbnail_url"], dash_streams=None, media_role=role)
        process = batch["process"] and batch["params"] is not None and role == "original"
        # "Editar após baixar": só vídeo é editado; foto do lote de Edição é só baixada (download-only, sem preset).
        image_in_edit = process and source["media_type"] != "video" and edit_service.is_edit(batch)
        if image_in_edit:
            process = False
        existing = self.ctx.media.find_by_source(
            source["source_platform"], source["source_post_id"], source["source_media_index"], role
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
            exclude_batch_id=batch["id"], role=role,
        )) is not None:
            # Outro lote aberto ainda vai baixar o mesmo item: este job fica `pending` aguardando o primário (não é
            # reivindicado nem conta no backpressure); concluído o primário, liga à mídia (e processa se o lote
            # pede); primário falho/cancelado, baixa sozinho (seção 9). Sem segundo download no caso feliz.
            kwargs = {"is_duplicate": True, "duplicate_reason": "duplicate_in_flight", "primary_job_id": primary}
        job_id = self.ctx.jobs.create_ingest(
            batch_id=batch["id"],
            source=source,
            preset_id=batch["preset_id"],
            preset_name=None if image_in_edit or role == "cover" else batch["preset_name"],
            params=batch["params"] if process else None,
            **kwargs,
        )
        return job_id is not None
