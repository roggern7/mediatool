"""Portão global de requisições ao Instagram (docs/BATCH_ROBUSTNESS.md, seção 8).

No máximo 1 requisição em voo no app inteiro e intervalo mínimo (sorteado em 2-4 s, injetável) entre requisições de
listagem/renovação, contado do fim de uma ao início da próxima. Downloads de CDN e FFmpeg NÃO passam por aqui.
Relógio e espera injetáveis (testes com relógio simulado).

Seção 9 (A2): depois de um 429 a listagem anda em ritmo lento (6-12 s por página) por 1 h (``slow_listing``; quem
persiste o prazo é o controlador de recuperação) e há um teto global de páginas de listagem por hora
(``wait_listing_page``, padrão 300, configurável por ``MEDIA_TOOL_IG_MAX_PAGES_PER_HOUR``).

Visibilidade da espera pelo teto por hora: o portão avisa o início e o fim da espera a ``listing_wait_observer`` (o
import runner grava o estado no lote e registra uma linha de log na entrada e outra na saída) e persiste as reservas
de página em relógio de parede (``save_pages``; ``restore_listing_pages`` reconstrói a janela na inicialização), para
que o mesmo teto valha através de um reinício do app. A matemática da janela não muda.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("media_tool.instagram_gate")

DEFAULT_INTERVAL = (2.0, 4.0)
SLOW_PAGE_DELAY = (6.0, 12.0)        # ritmo da listagem depois de um 429
SLOW_PACE_SECONDS = 3600.0           # duração do ritmo lento
WAIT_NOTICE_MIN_SECONDS = 30.0       # espera pelo teto por hora menor que isto não é avisada (estado/log)
DEFAULT_MAX_PAGES_PER_HOUR = 300     # teto global de páginas de listagem por hora
PAGE_WINDOW_SECONDS = 3600.0
PAGES_STATE_KEY = "instagram_listing_pages"   # app_state: reservas de página da última hora (epoch, JSON)


class GateCancelled(Exception):
    """O cancelamento foi pedido enquanto a requisição esperava a vez no portão (``is_cancellation`` reconhece)."""

    code = "cancelled"


class InstagramPaused(Exception):
    """O portão recusou a admissão: há pausa publicada (``scope`` ``"instagram"`` ou ``"session"``). Nenhum byte foi
    enviado; quem recebe trata como pausa (listagem ``paused``, renovação volta a ``pending``)."""

    code = "instagram_paused"

    def __init__(self, scope: str) -> None:
        super().__init__(f"Instagram em pausa (escopo {scope})")
        self.scope = scope


class InstagramRequestGate:
    """Admissão (seção 8, revisão de concorrência): a pausa é publicada aqui (``publish_pause``/``clear_pause``) sob
    ``_pause_lock``, a mesma trava em que cada requisição é admitida, imediatamente antes do envio (depois da vaga e
    de qualquer espera). Publicada a pausa, nenhuma requisição nova começa; a que já foi admitida termina normalmente.
    Durante a pausa só passam as chamadas com passagem explícita (``verification_pass``): a verificação do
    controlador e o "Testar sessão" manual, cada uma presa à própria thread e à época da pausa vigente."""

    def __init__(self, *, interval: tuple[float, float] = DEFAULT_INTERVAL,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] | None = None,
                 rng: Callable[[float, float], float] = random.uniform,
                 wall_clock: Callable[[], float] = time.time,
                 save_pages: Callable[[str], None] | None = None) -> None:
        low, high = sorted(max(0.0, float(v)) for v in interval)
        self._interval = (low, high)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._lock = threading.Lock()          # 1 em voo
        self._last_end: float | None = None
        self._slow_until: float | None = None  # relógio do portão (monotonic)
        self._pages_lock = threading.Lock()
        self._pages: deque[float] = deque()    # início das páginas de listagem da última hora
        self._wall = wall_clock                # relógio de parede (epoch), só para persistir/reconstruir a janela
        self._save_pages = save_pages          # grava a janela (JSON) a cada página reservada; None: só memória
        # Avisado no início ("enter": seconds, used, max) e no fim ("exit": waited, reason) da espera pelo teto.
        self.listing_wait_observer: Callable[[str, dict[str, Any]], None] | None = None
        # Pausa publicada (folha: nada é chamado para fora segurando esta trava).
        self._pause_lock = threading.RLock()
        self._pause_scope: str | None = None
        self._pause_epoch = 0                  # sobe a cada publicação/liberação
        self._verify_threads: dict[int, int] = {}   # thread da verificação -> época concedida

    # ---- pausa (admissão) --------------------------------------------------------------------------------------
    def publish_pause(self, scope: str | None) -> int:
        """Publica a pausa (síncrono): a partir do retorno nenhuma requisição nova é admitida. Retorna a época."""
        with self._pause_lock:
            self._pause_scope = scope or "instagram"
            self._pause_epoch += 1
            return self._pause_epoch

    def clear_pause(self) -> None:
        with self._pause_lock:
            if self._pause_scope is not None:
                self._pause_scope = None
                self._pause_epoch += 1

    def pause_epoch(self) -> int:
        with self._pause_lock:
            return self._pause_epoch

    def paused_scope(self) -> str | None:
        with self._pause_lock:
            return self._pause_scope

    @contextmanager
    def verification_pass(self, epoch: int) -> Iterator[None]:
        """Passagem explícita (verificação do controlador ou "Testar sessão" manual): requisições desta thread são
        admitidas durante a pausa cuja época é ``epoch``. Uma publicação nova (ou liberação seguida de nova pausa)
        invalida a passagem."""
        ident = threading.get_ident()
        with self._pause_lock:
            previous = self._verify_threads.get(ident)
            self._verify_threads[ident] = epoch
        try:
            yield
        finally:
            with self._pause_lock:
                if previous is None:
                    self._verify_threads.pop(ident, None)
                else:
                    self._verify_threads[ident] = previous

    def _admit(self) -> None:
        """Admissão atômica com a publicação: recusa com ``InstagramPaused`` se houver pausa vigente."""
        with self._pause_lock:
            scope = self._pause_scope
            if scope is None:
                return
            if self._verify_threads.get(threading.get_ident()) == self._pause_epoch:
                return
            raise InstagramPaused(scope)

    # ---- ritmo da listagem (seção 9, A2) -----------------------------------------------------------------------
    def slow_listing(self, seconds: float = SLOW_PACE_SECONDS) -> None:
        """Ritmo lento da listagem pelos próximos ``seconds`` (nunca encurta um prazo maior já vigente)."""
        until = self._clock() + max(0.0, float(seconds))
        if self._slow_until is None or until > self._slow_until:
            self._slow_until = until

    def listing_slowed(self) -> bool:
        return self._slow_until is not None and self._clock() < self._slow_until

    def listing_page_delay(self, default: tuple[float, float] | None) -> tuple[float, float] | None:
        """Intervalo entre páginas de listagem: ``SLOW_PAGE_DELAY`` durante o ritmo lento, senão ``default``."""
        return SLOW_PAGE_DELAY if self.listing_slowed() else default

    def wait_listing_page(self, cancel: threading.Event | None = None, *,
                          max_per_hour: int = DEFAULT_MAX_PAGES_PER_HOUR) -> None:
        """Reserva uma página de listagem no teto global por hora; espera (respeitando ``cancel``) quando o teto da
        última hora já foi atingido. Levanta ``GateCancelled`` se cancelarem durante a espera."""
        limit = max(1, int(max_per_hour))
        waited_from: float | None = None   # só quando a espera foi avisada (>= WAIT_NOTICE_MIN_SECONDS)
        checked = False
        reason = "interrupted"
        try:
            with self._pages_lock:
                while True:
                    now = self._clock()
                    while self._pages and now - self._pages[0] >= PAGE_WINDOW_SECONDS:
                        self._pages.popleft()
                    if len(self._pages) < limit:
                        self._pages.append(now)
                        self._persist_pages(now)
                        reason = "released"
                        return
                    if not checked:
                        checked = True
                        # Só para o aviso: instante em que a janela volta a ter vaga (a espera abaixo não muda).
                        # Esperas curtas (o ritmo normal de quem lista encostado no teto) não viram estado nem log.
                        free_at = self._pages[len(self._pages) - limit] + PAGE_WINDOW_SECONDS
                        if free_at - now >= WAIT_NOTICE_MIN_SECONDS:
                            waited_from = now
                            self._notify_wait("enter", {"seconds": free_at - now, "used": len(self._pages),
                                                        "max": limit})
                    self._wait(self._pages[0] + PAGE_WINDOW_SECONDS - now, cancel)
        except GateCancelled:
            reason = "cancelled"
            raise
        finally:
            if waited_from is not None:
                self._notify_wait("exit", {"waited": self._clock() - waited_from, "reason": reason})

    def _notify_wait(self, event: str, info: dict[str, Any]) -> None:
        observer = self.listing_wait_observer
        if observer is None:
            return
        try:
            observer(event, info)
        except Exception:  # noqa: BLE001 - o aviso da espera nunca interrompe a listagem
            logger.warning("Falha ao registrar a espera pelo limite de páginas por hora", exc_info=True)

    def _persist_pages(self, now: float) -> None:
        """Grava a janela em relógio de parede (chamado com ``_pages_lock``, uma vez por página reservada)."""
        if self._save_pages is None:
            return
        wall = self._wall()
        try:
            self._save_pages(json.dumps([round(wall - (now - page), 3) for page in self._pages]))
        except Exception:  # noqa: BLE001 - falha ao persistir não interrompe a listagem (a janela segue em memória)
            logger.warning("Falha ao persistir a janela de páginas por hora", exc_info=True)

    def restore_listing_pages(self, raw: str | None) -> int:
        """Reconstrói a janela gravada por ``save_pages`` (inicialização). Entradas com mais de 1 h são descartadas;
        entrada no futuro ou ilegível conta como página usada agora (nunca espera mais que 1 h); valor inteiro
        ilegível conta como janela cheia agora. Retorna quantas páginas ficaram na janela."""
        if not raw:
            return 0
        try:
            values = json.loads(raw)
            if not isinstance(values, list):
                raise ValueError("lista esperada")
        except (ValueError, RecursionError):
            logger.warning("Janela de páginas por hora ilegível: considerada cheia por até 1 h")
            values = [None] * DEFAULT_MAX_PAGES_PER_HOUR
        now, wall = self._clock(), self._wall()
        ages = []
        for value in values:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            age = max(0.0, wall - value) if valid else 0.0
            if age < PAGE_WINDOW_SECONDS:
                ages.append(age)
        with self._pages_lock:
            self._pages = deque(sorted(now - age for age in ages))
        return len(ages)

    def listing_window(self, max_per_hour: int = DEFAULT_MAX_PAGES_PER_HOUR) -> tuple[int, float | None]:
        """(páginas na janela, segundos até haver vaga ou None se já há). Só para a reconciliação da inicialização:
        usa ``_pages_lock``, que fica ocupada durante uma espera."""
        limit = max(1, int(max_per_hour))
        with self._pages_lock:
            now = self._clock()
            pages = [page for page in self._pages if now - page < PAGE_WINDOW_SECONDS]
            if len(pages) < limit:
                return len(pages), None
            return len(pages), pages[len(pages) - limit] + PAGE_WINDOW_SECONDS - now

    @contextmanager
    def slot(self, cancel: threading.Event | None = None, *, paced: bool = True) -> Iterator[None]:
        """Ocupa o portão durante UMA requisição (ou uma verificação curta). ``paced=False`` não espera o intervalo
        (ainda respeita 1 em voo). Levanta ``GateCancelled`` se ``cancel`` for acionado durante a espera e
        ``InstagramPaused`` se houver pausa publicada no momento da admissão (depois da vaga e da espera)."""
        while not self._lock.acquire(timeout=0.05):
            if cancel is not None and cancel.is_set():
                raise GateCancelled()
        started = False
        try:
            if paced and self._last_end is not None:
                delay = self._rng(*self._interval)
                remaining = self._last_end + delay - self._clock()
                if remaining > 0:
                    self._wait(remaining, cancel)
            self._admit()
            started = True
            yield
        finally:
            if started:
                self._last_end = self._clock()
            self._lock.release()

    def _wait(self, seconds: float, cancel: threading.Event | None) -> None:
        if self._sleep is not None:
            self._sleep(seconds)
        elif cancel is not None:
            if cancel.wait(seconds):
                raise GateCancelled()
        else:
            time.sleep(seconds)
        if cancel is not None and cancel.is_set():
            raise GateCancelled()
