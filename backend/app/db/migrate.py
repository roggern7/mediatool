"""Runner de migrations SQL versionadas (`app/db/migrations/NNNN_nome.sql`), cada uma em transação."""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.db.connection import Database, utc_now

logger = logging.getLogger("media_tool.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if not match:
            raise RuntimeError(f"Nome de migration inválido: {path.name}")
        migrations.append(Migration(int(match.group(1)), match.group(2), path))
    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Versões de migration duplicadas")
    return migrations


def split_statements(sql: str) -> list[str]:
    """Divide um script em instruções completas (respeita strings e corpos de trigger)."""
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        if not buffer and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise RuntimeError("Migration com instrução SQL incompleta")
    return statements


def _ensure_table(db: Database) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )


def schema_version(db: Database) -> int:
    _ensure_table(db)
    return int(db.scalar("SELECT COALESCE(MAX(version), 0) FROM schema_migrations"))


def apply_migrations(db: Database, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Aplica migrations pendentes em ordem. Retorna as versões aplicadas nesta chamada."""
    _ensure_table(db)
    applied: list[int] = []
    for migration in discover_migrations(directory):
        with db.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (migration.version,)
            ).fetchone()
            if exists:
                continue
            for statement in split_statements(migration.path.read_text(encoding="utf-8")):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, utc_now()),
            )
        logger.info("Migration %04d_%s aplicada", migration.version, migration.name)
        applied.append(migration.version)
    return applied
