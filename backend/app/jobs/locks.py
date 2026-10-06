"""Locks em memória por chave (ex.: uma rendition por mídia+preset+params de cada vez)."""

from __future__ import annotations

import threading
from collections.abc import Hashable, Iterator
from contextlib import contextmanager

from app.jobs.errors import JobCancelled

_POLL = 0.2


class KeyedLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[Hashable, list] = {}  # chave -> [Lock, referências]

    @contextmanager
    def hold(self, key: Hashable, cancel: threading.Event | None = None) -> Iterator[None]:
        with self._guard:
            entry = self._locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            while not entry[0].acquire(timeout=_POLL):
                if cancel is not None and cancel.is_set():
                    raise JobCancelled()
            try:
                yield
            finally:
                entry[0].release()
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    self._locks.pop(key, None)
