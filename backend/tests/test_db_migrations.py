from __future__ import annotations

import json
import sqlite3

import pytest

from app.db.connection import Database
from app.db.migrate import apply_migrations, discover_migrations, schema_version, split_statements

EXPECTED_TABLES = {"presets", "media", "renditions", "batches", "jobs", "settings", "app_state", "schema_migrations"}
DEFAULT_PARAMS = {"metadata_cleanup": True, "trim_start": 0.5, "mirror": True, "speed": 1.05,
                  "auto_adjust": True, "reencode": True}


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "data" / "media-tool.db")
    yield database
    database.close_all()


def test_apply_from_scratch_creates_schema_and_seeds(db):
    applied = apply_migrations(db)
    latest = discover_migrations()[-1].version
    assert applied == [m.version for m in discover_migrations()]
    assert schema_version(db) == latest

    tables = {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert EXPECTED_TABLES <= tables
    assert not any("session" in name or "cookie" in name for name in tables)

    presets = db.fetchall("SELECT name, is_builtin, params FROM presets ORDER BY id")
    assert [(p["name"], p["is_builtin"]) for p in presets] == [("Padrão", 1), ("Anti-duplicidade", 1)]
    assert all(json.loads(p["params"]) == DEFAULT_PARAMS for p in presets)

    row = db.fetchone("SELECT version, name, applied_at FROM schema_migrations WHERE version = 1")
    assert row["name"] == "initial" and row["applied_at"].endswith("Z")


def test_migrations_are_idempotent(db):
    apply_migrations(db)
    assert apply_migrations(db) == []
    assert db.scalar("SELECT COUNT(*) FROM presets") == 2
    assert db.scalar("SELECT COUNT(*) FROM schema_migrations") == len(discover_migrations())


def test_pragmas_wal_and_foreign_keys(db):
    apply_migrations(db)
    assert db.scalar("PRAGMA journal_mode").lower() == "wal"
    assert db.scalar("PRAGMA foreign_keys") == 1
    assert db.scalar("PRAGMA busy_timeout") >= 5000


def test_failed_migration_rolls_back(tmp_path, db):
    migrations = tmp_path / "migs"
    migrations.mkdir()
    (migrations / "0001_ok.sql").write_text("CREATE TABLE a (id INTEGER PRIMARY KEY);", encoding="utf-8")
    (migrations / "0002_broken.sql").write_text(
        "CREATE TABLE b (id INTEGER PRIMARY KEY);\nINSERT INTO nao_existe VALUES (1);", encoding="utf-8"
    )
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(db, migrations)
    tables = {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "a" in tables and "b" not in tables
    assert schema_version(db) == 1


def test_unique_indexes_enforced(db):
    apply_migrations(db)
    now = "2026-01-01T00:00:00.000Z"
    insert = (
        "INSERT INTO media (source_platform, source_post_id, source_media_index, media_type, original_path,"
        " original_sha256, file_size, created_at, updated_at) VALUES ('instagram', ?, 0, 'image', 'x', ?, 1, ?, ?)"
    )
    db.execute(insert, ("ABC", "a" * 64, now, now))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("ABC", "b" * 64, now, now))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("DEF", "a" * 64, now, now))


def test_split_statements_handles_strings_with_semicolons():
    sql = "-- comentário\nCREATE TABLE t (v TEXT);\nINSERT INTO t VALUES ('a;b');\n"
    assert split_statements(sql) == ["CREATE TABLE t (v TEXT);", "INSERT INTO t VALUES ('a;b');"]
