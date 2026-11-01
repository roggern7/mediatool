"""Executor da Análise de Perfis: thread própria, fila persistente, UMA análise por vez, FIFO (padrão do ExportRunner).

Consulta os perfis em ordem, um por vez, com intervalo sorteado. Sem sessão e sem efeito em estado global: um 429 aqui
é um limite de requisição ANÔNIMA, não diz nada sobre a conta e não pode afetar os downloads. Por isso o backoff é
LOCAL da análise (`waiting`, degraus de 15/30/60 min, persistidos): nada de `worker.pause_for`, `slow_listing` nem
`app_state`.
A pausa global, ao contrário, é respeitada (a análise espera, sem consumir tentativa). O limite de consultas vem do
ritmo daqui (3 a 6 s + descansos: menos de 360 perfis por hora) e do portão: o teto por hora das importações não é
usado.

Um IP bloqueado pode receber a mesma rota de erro de um perfil inexistente, o que faria todos saírem Indisponível. Por
isso `unavailable` não conta como sucesso e cada `CANARY_EVERY` em sequência passa por uma consulta de controle.

Falha de banco ou disco nunca vira laço de consulta: o item conta `attempts`, passa do teto sem GET novo e, enquanto
nada puder ser gravado, o executor espera em memória. Depois de `STORAGE_MAX_FAILURES` falhas seguidas a análise é
interrompida, para não prender a fila.

Toda espera é por `threading.Event` (cancelável; encerramento rápido) e o banco é a fonte da verdade do cancelamento.
Relógio, espera e sorteio são injetáveis: os testes de lógica chamam `run_next()` sem thread e sem esperar de verdade.
"""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.db.connection import parse_utc
from app.integrations.instagram.downloader import InstagramError
from app.services.instagram_gate import GateCancelled, InstagramPaused

if TYPE_CHECKING:
    from app.services.context import AppContext

logger = logging.getLogger("media_tool.profile_analysis")

# Ritmo adaptativo: a análise começa sem espera própria (só o intervalo do portão, 2 a 4 s entre consultas) e, no
# primeiro sinal de limite, passa para o ritmo cauteloso até o fim. Nunca há duas consultas ao mesmo tempo.
FAST_REST_EVERY = 100
FAST_REST_DELAY = (30.0, 60.0)
PROFILE_DELAY = (3.0, 6.0)
SLOW_PROFILE_DELAY = (10.0, 20.0)     # enquanto o portão estiver em ritmo lento (`gate.listing_slowed()`)
REST_EVERY = 30
REST_DELAY = (45.0, 90.0)
MAX_ATTEMPTS = 2
BREAKER_LIMIT = 5                     # perfis `unverified` seguidos: bloqueio provável
CANARY_EVERY = 10                     # perfis `unavailable` seguidos: consulta de controle
CANARY_PROFILE = "instagram"          # perfil que certamente existe
# Só estes provam que o Instagram está respondendo de verdade.
CONFIRMED_STATUSES = ("active", "private", "restricted")
LIMIT_SIGN_CODES = frozenset({"access_refused"})     # recusa de um perfil: sinal de limite, além do 429
LIMIT_SIGN_STREAK = 2                                # `unverified` seguidos: sinal de limite; um isolado não é
# Resposta que já foi passageira no real (@cbf_futebol, 2026-11-01): uma releitura antes de aceitar.
REREAD_CODES = frozenset({"profile_restricted", "profile_unreadable"})
STORAGE_RETRY_SECONDS = (30.0, 60.0, 120.0, 300.0)   # espera em memória com banco/disco falhando
STORAGE_MAX_FAILURES = 6              # falhas seguidas fora da consulta: a análise desiste, sem repetir para sempre
BACKOFF_SECONDS = (900.0, 1800.0, 3600.0)
RATE_LIMITED, TOO_MANY_FAILURES, INSTAGRAM_PAUSE = "rate_limited", "too_many_failures", "instagram_pause"
INTERNAL_ERROR = "internal_error"
RETRY_CODES = frozenset({"timeout", "network_error"})
PAUSE_RECHECK_SECONDS = 2.0
WAIT_SLICE_SECONDS = 2.0              # uma espera longa é fatiada para enxergar cancelamento e encerramento
IDLE_WAIT_SECONDS = 2.0

MSG_UNEXPECTED = "Erro inesperado ao consultar este perfil. O resultado não pôde ser confirmado."
MSG_NO_ANSWER = "A consulta deste perfil não terminou depois de várias tentativas. O resultado não pôde ser confirmado."
_MSG_NOT_CONSULTED = {
    RATE_LIMITED: "Não consultado: o Instagram seguiu limitando as consultas e a análise foi interrompida.",
    TOO_MANY_FAILURES: "Não consultado: muitos perfis seguidos sem resposta confirmada; a análise foi interrompida.",
    INTERNAL_ERROR: "Não consultado: falha interna ao registrar o andamento; a análise foi interrompida.",
}
_MSG_INTERRUPTED = {
    RATE_LIMITED: "O Instagram seguiu limitando as consultas depois de todas as esperas. Os perfis que faltavam não"
                  " foram consultados.",
    TOO_MANY_FAILURES: "Muitos perfis seguidos sem resposta confirmada depois de todas as esperas. Os perfis que"
                       " faltavam não foram consultados.",
    INTERNAL_ERROR: "Falha interna repetida ao registrar o andamento. Os perfis que faltavam não foram consultados.",
}


def stamp(moment: datetime) -> str:
    """Instante UTC no formato único do banco (ISO 8601 com `Z` e milissegundos)."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def recover_profile_analyses(ctx: AppContext) -> int:
    """Startup: item `running` volta para `pending`; análise `running`/`waiting` segue aberta e é retomada (o
    `waiting_until` gravado é respeitado); com cancelamento pedido, termina cancelada. Retorna quantas seguem
    abertas."""
    repo = ctx.profile_analyses
    resumed = 0
    for analysis in repo.open_analyses():
        if analysis["cancel_requested"]:
            repo.finish_cancelled(analysis["id"], stamp(datetime.now(UTC)))
            continue
        repo.requeue_running(analysis["id"])
        resumed += 1
    if resumed:
        logger.info("Recovery: %s análise(s) de perfis aberta(s) continuam na fila", resumed)
    return resumed


class ProfileAnalysisRunner:
    def __init__(self, ctx: AppContext, *, clock: Callable[[], datetime] | None = None,
                 wait: Callable[[float], None] | None = None,
                 rng: Callable[[float, float], float] = random.uniform, start_fast: bool = True) -> None:
        self.ctx = ctx
        self._start_fast = start_fast
        self._cautious = False     # ritmo cauteloso (`cautious_pace`, gravado no banco)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._wait = wait
        self._rng = rng
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Protege (análise atual, evento de interrupção) contra a corrida com `cancel()`/`stop()`.
        self._lock = threading.Lock()
        self._current_id: int | None = None
        self._interrupt = threading.Event()
        self._level = 0            # degrau do backoff (espelha `wait_level`)
        self._fetched = False      # já houve consulta neste processo (a 1a não tem intervalo antes)
        self._since_rest = 0       # perfis concluídos desde o último descanso
        self._validated = 0        # posição até onde o canário não olha (`resume_after`, gravada no banco)
        self._storage_failures = 0

    # ---- ciclo de vida -----------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="profile-analysis-runner", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._interrupt.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def cancel(self, analysis_id: int) -> None:
        """Interrompe a espera da análise em execução (o pedido já está gravado no banco por quem chama)."""
        with self._lock:
            if self._current_id == analysis_id:
                self._interrupt.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_next()
            except Exception:  # noqa: BLE001 - a thread nunca morre por causa de uma análise
                logger.exception("Falha inesperada no executor da análise de perfis")
                worked = False
            if not worked:
                self._wake.wait(IDLE_WAIT_SECONDS)
                self._wake.clear()

    # ---- execução ----------------------------------------------------------------------------------------------
    def run_next(self) -> bool:
        """Executa a próxima análise aberta até o fim (ou até o encerramento). Retorna False se a fila está vazia."""
        with self._lock:
            analysis = self.ctx.profile_analyses.claim_next(stamp(self._clock()))
            if analysis is None:
                return False
            self._current_id = analysis["id"]
            self._interrupt = threading.Event()
        try:
            self._run(analysis)
        finally:
            with self._lock:
                self._current_id = None
        return True

    def _sleep(self, seconds: float) -> None:
        if self._wait is not None:
            self._wait(seconds)
        else:
            self._interrupt.wait(seconds)

    def _halted(self, analysis_id: int) -> bool:
        return self._stop.is_set() or self.ctx.profile_analyses.cancel_requested(analysis_id)

    def _global_pause(self) -> bool:
        gate = getattr(self.ctx, "instagram_gate", None)
        paused_scope = getattr(gate, "paused_scope", None)
        return self.ctx.worker.downloads_paused() or (paused_scope is not None and paused_scope() is not None)

    def _run(self, analysis: dict[str, Any]) -> None:
        repo = self.ctx.profile_analyses
        analysis_id = analysis["id"]
        self._level = analysis["wait_level"]
        self._cautious = bool(analysis["cautious_pace"]) or not self._start_fast
        self._validated = analysis["resume_after"]
        if analysis["status"] == "waiting":
            until = parse_utc(analysis["waiting_until"]) if analysis["wait_reason"] != INSTAGRAM_PAUSE else None
            if until is not None:
                self._wait_until(analysis_id, until)
            elif not self._halted(analysis_id):
                repo.resume(analysis_id)
        while not self._stop.is_set():
            try:
                if not self._step(analysis_id):
                    return
                self._storage_failures = 0
            except Exception:  # noqa: BLE001 - banco ou disco com problema: espera aqui, sem repetir consulta em laço
                logger.warning("Análise de perfis %s: falha ao gravar ou ler o andamento", analysis_id, exc_info=True)
                self._storage_failures += 1
                if self._storage_failures >= STORAGE_MAX_FAILURES:
                    self._give_up(analysis_id)
                    return
                self._sleep(STORAGE_RETRY_SECONDS[min(self._storage_failures, len(STORAGE_RETRY_SECONDS)) - 1])

    def _give_up(self, analysis_id: int) -> None:
        """Falha persistente fora da consulta: encerra a análise para não prender a fila nem repetir GET. Se nem isso
        gravar, espera o maior degrau antes de a fila tentar de novo, e o contador segue valendo."""
        try:
            self.ctx.profile_analyses.finish_interrupted(
                analysis_id, code=INTERNAL_ERROR, message=_MSG_NOT_CONSULTED[INTERNAL_ERROR],
                error=_MSG_INTERRUPTED[INTERNAL_ERROR], now=stamp(self._clock()))
            self._storage_failures = 0
        except Exception:  # noqa: BLE001 - banco ainda fora: a thread não morre
            logger.warning("Análise de perfis %s: não foi possível encerrar", analysis_id, exc_info=True)
            self._sleep(STORAGE_RETRY_SECONDS[-1])

    def _step(self, analysis_id: int) -> bool:
        """Um passo do laço. False = a análise terminou."""
        repo = self.ctx.profile_analyses
        if repo.cancel_requested(analysis_id):
            repo.finish_cancelled(analysis_id, stamp(self._clock()))
            logger.info("Análise de perfis %s cancelada", analysis_id)
            return False
        suspects = repo.trailing_unavailable(analysis_id, self._validated, CANARY_EVERY)
        canary_due = len(suspects) >= CANARY_EVERY
        item = repo.next_item(analysis_id)
        if item is None and not canary_due:
            repo.finish(analysis_id, "completed", stamp(self._clock()))
            logger.info("Análise de perfis %s concluída", analysis_id)
            return False
        if not canary_due and item["attempts"] > MAX_ATTEMPTS:
            # Só chega aqui quando a gravação falhou de novo e de novo: o item fecha sem nenhum GET novo.
            # Sem resposta não há instante de consulta: `checked_at` fica nulo.
            repo.save_result(item["id"], self._failure(INTERNAL_ERROR, MSG_NO_ANSWER), None)
            return self._settle(analysis_id, "unverified", self._clock())
        if self._global_pause():
            self._hold_for_pause(analysis_id)
            return True
        delay = self._next_delay()
        if delay > 0:
            self._sleep(delay)
            if self._halted(analysis_id):
                return True
        return self._canary(analysis_id, suspects) if canary_due else self._consult(analysis_id, item)

    def _next_delay(self) -> float:
        """Intervalo antes da próxima consulta; o descanso periódico substitui o intervalo normal."""
        if not self._fetched:
            return 0.0
        rest_every, rest_delay = (REST_EVERY, REST_DELAY) if self._cautious else (FAST_REST_EVERY, FAST_REST_DELAY)
        if self._since_rest >= rest_every:
            self._since_rest = 0
            return self._rng(*rest_delay)
        gate = getattr(self.ctx, "instagram_gate", None)
        slowed = getattr(gate, "listing_slowed", None)
        if slowed is not None and slowed():
            return self._rng(*SLOW_PROFILE_DELAY)
        return self._rng(*PROFILE_DELAY) if self._cautious else 0.0

    def _slow_down(self, analysis_id: int) -> None:
        """Primeiro sinal de limite: ritmo cauteloso até o fim desta análise, inclusive depois de reinício e retomada."""
        if self._cautious:
            return
        self._cautious = True
        logger.warning("Análise de perfis %s: sinal de limite do Instagram; ritmo cauteloso até o fim", analysis_id)
        self.ctx.profile_analyses.set_cautious(analysis_id)

    def _consult(self, analysis_id: int, item: dict[str, Any]) -> bool:
        """Uma tentativa do item. False = a análise terminou aqui (interrompida)."""
        repo = self.ctx.profile_analyses
        item_id = item["id"]
        repo.start_item(item_id)
        self._fetched = True
        try:
            fields = asdict(self.ctx.profile_fetcher(item["username"], self._interrupt))
            moment = self._clock()
            if fields["error_code"] in REREAD_CODES and item["attempts"] + 1 < MAX_ATTEMPTS:
                repo.release_item(item_id, consume_attempt=True)
                return True
        except InstagramPaused:
            repo.release_item(item_id)
            self._hold_for_pause(analysis_id)
            return True
        except GateCancelled:
            repo.release_item(item_id)   # cancelado ou encerrando enquanto esperava a vez: o laço decide
            return True
        except InstagramError as exc:
            moment = self._clock()
            if exc.code == RATE_LIMITED:
                repo.release_item(item_id)
                self._slow_down(analysis_id)
                return self._back_off(analysis_id, RATE_LIMITED, moment)
            if exc.code in RETRY_CODES and item["attempts"] + 1 < MAX_ATTEMPTS:
                repo.release_item(item_id, consume_attempt=True)
                return True
            if exc.code in LIMIT_SIGN_CODES:
                self._slow_down(analysis_id)
            fields = self._failure(exc.code, exc.message)
        except Exception:  # noqa: BLE001 - erro de um perfil não para os demais
            moment = self._clock()
            logger.warning("Análise de perfis %s: falha inesperada no item %s", analysis_id, item["position"],
                           exc_info=True)
            fields = self._failure(INTERNAL_ERROR, MSG_UNEXPECTED)
        return self._settle(analysis_id, self._store(analysis_id, item, fields, moment), moment)

    def _store(self, analysis_id: int, item: dict[str, Any], fields: dict[str, Any], moment: datetime) -> str:
        """Grava o resultado e devolve o status final. Um resultado que o banco recusa vira `unverified`; se nem isso
        gravar, a exceção sobe para o laço, que espera (`attempts` já conta esta consulta)."""
        repo = self.ctx.profile_analyses
        try:
            repo.save_result(item["id"], fields, stamp(moment))
            return fields["account_status"]
        except Exception:  # noqa: BLE001 - valor fora do que o banco aceita não para os demais perfis
            logger.warning("Análise de perfis %s: resultado do item %s não gravado", analysis_id, item["position"],
                           exc_info=True)
        repo.save_result(item["id"], self._failure(INTERNAL_ERROR, MSG_UNEXPECTED), stamp(moment))
        return "unverified"

    def _settle(self, analysis_id: int, status: str, moment: datetime) -> bool:
        """Depois do resultado gravado. `unavailable` é neutro: pode ser a rota de erro servida a um IP bloqueado, então
        não zera degrau nem disjuntor (quem confere é o canário). Sem perfil restante não há o que proteger: o disjuntor
        não abre e a análise termina normalmente."""
        repo = self.ctx.profile_analyses
        self._since_rest += 1
        if status == "unverified" and repo.trailing_unverified(analysis_id, LIMIT_SIGN_STREAK) >= LIMIT_SIGN_STREAK:
            self._slow_down(analysis_id)
        if status in CONFIRMED_STATUSES:
            self._reset_level(analysis_id)
        elif (status == "unverified" and repo.trailing_unverified(analysis_id, BREAKER_LIMIT) >= BREAKER_LIMIT
              and repo.next_item(analysis_id) is not None):
            return self._back_off(analysis_id, TOO_MANY_FAILURES, moment)
        return True

    def _reset_level(self, analysis_id: int) -> None:
        if self._level:
            self._level = 0
            self.ctx.profile_analyses.reset_level(analysis_id)

    def _canary(self, analysis_id: int, suspects: list[dict[str, Any]]) -> bool:
        """`CANARY_EVERY` perfis `unavailable` seguidos: uma consulta de controle a um perfil que certamente existe, sem
        criar item. Ativo: a sequência é legítima. Qualquer outra resposta: a rota de erro pode ser um bloqueio, então
        a sequência volta para a fila e a análise entra no backoff local."""
        repo = self.ctx.profile_analyses
        self._fetched = True
        status, reason = None, TOO_MANY_FAILURES
        try:
            status = self.ctx.profile_fetcher(CANARY_PROFILE, self._interrupt).account_status
        except InstagramPaused:
            self._hold_for_pause(analysis_id)
            return True
        except GateCancelled:
            return True
        except InstagramError as exc:
            reason = RATE_LIMITED if exc.code == RATE_LIMITED else TOO_MANY_FAILURES
        except Exception:  # noqa: BLE001 - canário sem resposta confirmada conta como não confirmado
            logger.warning("Análise de perfis %s: falha inesperada no canário", analysis_id, exc_info=True)
        moment = self._clock()
        if status == "active":
            self._validated = max(suspect["position"] for suspect in suspects)
            repo.set_resume_after(analysis_id, self._validated)   # no banco: um reinício não repete a conferência
            self._reset_level(analysis_id)
            return True
        logger.warning("Análise de perfis %s: canário não confirmou a sequência de indisponíveis", analysis_id)
        self._slow_down(analysis_id)
        repo.reopen_items([suspect["id"] for suspect in suspects])
        return self._back_off(analysis_id, reason, moment)

    @staticmethod
    def _failure(code: str, message: str) -> dict[str, Any]:
        return {"account_status": "unverified", "posts_state": "unverified", "views_state": "unverified",
                "stories": "unverified", "highlights": "unverified", "error_code": code, "error_message": message}

    def _back_off(self, analysis_id: int, reason: str, moment: datetime) -> bool:
        """Próximo degrau da espera local; degraus esgotados interrompem a análise (retorna False), salvo se o
        cancelamento já foi pedido: aí o laço a encerra como cancelada."""
        repo = self.ctx.profile_analyses
        if self._level >= len(BACKOFF_SECONDS):
            finished = repo.finish_interrupted(analysis_id, code=reason, message=_MSG_NOT_CONSULTED[reason],
                                               error=_MSG_INTERRUPTED[reason], now=stamp(self._clock()))
            if finished:
                logger.warning("Análise de perfis %s interrompida (%s)", analysis_id, reason)
            return not finished
        until = moment + timedelta(seconds=BACKOFF_SECONDS[self._level])
        repo.set_waiting(analysis_id, reason, stamp(until), self._level + 1)
        self._level += 1
        logger.info("Análise de perfis %s aguardando (%s, degrau %s)", analysis_id, reason, self._level)
        self._wait_until(analysis_id, until)
        return True

    def _wait_until(self, analysis_id: int, until: datetime) -> None:
        while not self._halted(analysis_id):
            remaining = (until - self._clock()).total_seconds()
            if remaining <= 0:
                self.ctx.profile_analyses.resume(analysis_id)
                return
            self._sleep(min(remaining, WAIT_SLICE_SECONDS))

    def _hold_for_pause(self, analysis_id: int) -> None:
        """Pausa global vigente: `waiting`, sem prazo próprio, rechecando a cada 2 s."""
        repo = self.ctx.profile_analyses
        repo.set_waiting(analysis_id, INSTAGRAM_PAUSE, None, self._level)
        while not self._halted(analysis_id):
            self._sleep(PAUSE_RECHECK_SECONDS)
            if not self._global_pause():
                repo.resume(analysis_id)
                return
