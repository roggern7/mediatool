"""Execução com prazo duro: a chamada roda em thread própria e quem espera desiste no deadline.

A thread continua em segundo plano até a chamada terminar (o adapter mata o processo filho no seu próprio teto);
nada fica bloqueado além do prazo. Com `lock`, só uma chamada por vez: a trava é liberada quando a chamada termina
de fato (mesmo depois do prazo) ou se a thread não puder ser iniciada.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class DeadlineExceeded(Exception):
    pass


class CallInProgress(Exception):
    """Já existe uma chamada em andamento para esta trava."""


class _Outcome(Generic[T]):
    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: T | None = None
        self.error: BaseException | None = None


def run_with_deadline(
    fn: Callable[[], T], timeout: float, *, name: str, lock: threading.Lock | None = None
) -> T:
    """Executa `fn` em thread daemon e espera até `timeout` segundos.

    Levanta `CallInProgress` se `lock` já estiver ocupada, `DeadlineExceeded` se o prazo estourar; exceções de
    `fn` são relançadas.
    """
    if lock is not None and not lock.acquire(blocking=False):
        raise CallInProgress()
    outcome: _Outcome[T] = _Outcome()

    def target() -> None:
        try:
            outcome.value = fn()
        except BaseException as exc:  # noqa: BLE001 - repassado para quem espera
            outcome.error = exc
        finally:
            # Libera a trava antes de sinalizar o fim: quem acorda já pode iniciar outra chamada.
            try:
                if lock is not None:
                    lock.release()
            finally:
                outcome.done.set()

    try:
        threading.Thread(target=target, name=name, daemon=True).start()
    except BaseException:
        if lock is not None:
            lock.release()
        raise
    if not outcome.done.wait(timeout):
        raise DeadlineExceeded()
    if outcome.error is not None:
        raise outcome.error
    return outcome.value  # type: ignore[return-value]
