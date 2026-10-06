"""Conexão SQLite: uma conexão por thread, WAL, foreign keys e transações explícitas."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

BUSY_TIMEOUT_MS = 10_000
# Limite do arquivo WAL depois de um checkpoint (docs/BATCH_ROBUSTNESS.md, seção 6).
JOURNAL_SIZE_LIMIT = 16 * 1024 * 1024


def utc_now(offset_seconds: float = 0.0) -> str:
    """Data ISO 8601 UTC com `Z` e milissegundos (formato único, comparável lexicograficamente)."""
    moment = datetime.now(UTC) + timedelta(seconds=offset_seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class Database:
    """Fábrica de conexões por thread. Autocommit por padrão; escrita agrupada usa `transaction()`."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT}")
        with self._lock:
            self._connections.append(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: tuple | dict = ()):
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Transação `BEGIN IMMEDIATE` (lock de escrita desde o início, evita deadlock de upgrade)."""
        conn = self.conn
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def wal_checkpoint_truncate(self) -> tuple[int, int, int]:
        """`wal_checkpoint(TRUNCATE)`: (busy, páginas no WAL, páginas copiadas). busy=1 se leitores impediram."""
        row = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def close_all(self) -> None:
        with self._lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()
