from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from app.db.connection import Database
from app.db.migrate import apply_migrations, discover_migrations, schema_version, split_statements
from app.db.repositories.common import canonical_json
from app.schemas.presets import PresetParams

EXPECTED_TABLES = {"presets", "media", "renditions", "batches", "jobs", "settings", "app_state", "schema_migrations"}
DEFAULT_PARAMS = {"metadata_cleanup": True, "trim_start": 0.5, "mirror": True, "speed": 1.05,
                  "auto_adjust": True, "reencode": True}
# Anti-duplicidade depois da migration 0015: os 6 originais + ajustes opcionais (tom e granulação desligados).
ANTI_DUPLICATE_PARAMS = {**DEFAULT_PARAMS, "zoom": 3.0, "brightness": 0.01, "contrast": 1.02, "saturation": 1.03,
                         "trim_end": 0.3}
SEED_TEXT = ('{"metadata_cleanup": true, "trim_start": 0.5, "mirror": true, "speed": 1.05, "auto_adjust": true,'
             ' "reencode": true}')


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
    assert json.loads(presets[0]["params"]) == DEFAULT_PARAMS and presets[0]["params"] == SEED_TEXT
    assert json.loads(presets[1]["params"]) == ANTI_DUPLICATE_PARAMS

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


def test_import_links_schema_and_constraints(db):
    """Migration 0014: `batches.import_mode` (NULL ou 'links') e `batch_links` (um shortcode por lote)."""
    apply_migrations(db)
    assert schema_version(db) >= 14
    columns = {row["name"] for row in db.fetchall("PRAGMA table_info(batches)")}
    assert "import_mode" in columns
    assert {row["name"] for row in db.fetchall("PRAGMA table_info(batch_links)")} == {
        "batch_id", "position", "shortcode", "url", "status", "error_code", "error_message", "items_found", "profile",
        "resolved_at", "attention_count"}
    assert "link_fail_streak" in columns
    now = "2026-01-01T00:00:00.000Z"
    db.execute("INSERT INTO batches (id, source, process, status, found, created_at, updated_at, import_mode)"
               " VALUES (1, 'instagram_import', 0, 'listing', 0, ?, ?, 'links')", (now, now))
    insert = "INSERT INTO batch_links (batch_id, position, shortcode, url) VALUES (1, ?, ?, 'u')"
    db.execute(insert, (1, "ABCDE1"))
    assert db.scalar("SELECT status FROM batch_links WHERE batch_id = 1 AND position = 1") == "pending"
    for position, shortcode in ((1, "OUTRO1"), (2, "ABCDE1")):   # posição e shortcode únicos no lote
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, (position, shortcode))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE batch_links SET status = 'cancelled' WHERE batch_id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE batches SET import_mode = 'outro' WHERE id = 1")
    db.execute("DELETE FROM batches WHERE id = 1")
    assert db.scalar("SELECT COUNT(*) FROM batch_links") == 0, "ON DELETE CASCADE"


def _migrations_until(tmp_path, version):
    """Cópia das migrations até `version` (banco "existente" de antes das seguintes)."""
    directory = tmp_path / f"migs_until_{version}"
    directory.mkdir()
    for migration in discover_migrations():
        if migration.version <= version:
            shutil.copy(migration.path, directory / migration.path.name)
    return directory


def _preset_rows(db):
    return {row["name"]: dict(row) for row in db.fetchall("SELECT * FROM presets")}


@pytest.mark.parametrize("stored", [SEED_TEXT, canonical_json(DEFAULT_PARAMS),
                                    '{"speed":1.050,"reencode":true,"mirror":true,"trim_start":5e-1,'
                                    '"auto_adjust":true,"metadata_cleanup":true}'])
def test_anti_duplicate_migration_updates_untouched_builtin(tmp_path, db, stored):
    """Migration 0015: banco existente com o Anti-duplicidade original (texto do seed, canonical_json ou outro)."""
    apply_migrations(db, _migrations_until(tmp_path, 14))
    now = "2026-01-01T00:00:00.000Z"
    db.execute("UPDATE presets SET params = ?, updated_at = ?", (stored, now))
    db.execute("INSERT INTO presets (name, is_builtin, params, created_at, updated_at) VALUES ('Meu', 0, ?, ?, ?)",
               (stored, now, now))
    db.execute("INSERT INTO media (id, source_platform, media_type, original_path, original_sha256, file_size,"
               " created_at, updated_at) VALUES (1, 'instagram', 'video', 'x', ?, 1, ?, ?)", ("a" * 64, now, now))
    anti_id = db.scalar("SELECT id FROM presets WHERE name = 'Anti-duplicidade'")
    db.execute("INSERT INTO renditions (media_id, preset_id, preset_name, params, processed_path, processed_sha256,"
               " file_size, created_at) VALUES (1, ?, 'Anti-duplicidade', ?, 'p', ?, 1, ?)",
               (anti_id, canonical_json(DEFAULT_PARAMS), "b" * 64, now))
    before = _preset_rows(db)

    assert apply_migrations(db) == [m.version for m in discover_migrations() if m.version > 14]
    after = _preset_rows(db)
    anti = after["Anti-duplicidade"]
    assert json.loads(anti["params"]) == ANTI_DUPLICATE_PARAMS and anti["is_builtin"] == 1
    # formato de canonical_json, igual ao que a API grava ao salvar o mesmo preset pela interface
    assert anti["params"] == canonical_json(ANTI_DUPLICATE_PARAMS)
    assert anti["params"] == canonical_json(PresetParams(**ANTI_DUPLICATE_PARAMS, pitch_shift=0.0, grain=0.0).model_dump())
    assert anti["updated_at"] > now and anti["updated_at"].endswith("Z")
    assert after["Padrão"] == before["Padrão"] and after["Meu"] == before["Meu"]
    # arquivos já processados ficam como estão
    rendition = db.fetchone("SELECT preset_id, params FROM renditions")
    assert rendition["preset_id"] == anti_id and rendition["params"] == canonical_json(DEFAULT_PARAMS)
    # idempotência: a migration já registrada em schema_migrations não roda de novo
    db.execute("UPDATE presets SET params = ? WHERE name = 'Anti-duplicidade'", (stored,))
    assert apply_migrations(db) == []
    assert _preset_rows(db)["Anti-duplicidade"]["params"] == stored


@pytest.mark.parametrize("edited", [
    {**DEFAULT_PARAMS, "speed": 1.1},
    {**DEFAULT_PARAMS, "mirror": False},
    {**DEFAULT_PARAMS, "metadata_cleanup": False},
    {**DEFAULT_PARAMS, "auto_adjust": False},
    {**DEFAULT_PARAMS, "reencode": False},
    {**DEFAULT_PARAMS, "trim_start": 0},
    {**DEFAULT_PARAMS, "zoom": 1.0},                       # já tem ajuste opcional (7 chaves)
    {k: v for k, v in DEFAULT_PARAMS.items() if k != "reencode"},
])
def test_anti_duplicate_migration_keeps_user_edited_preset(tmp_path, db, edited):
    apply_migrations(db, _migrations_until(tmp_path, 14))
    db.execute("UPDATE presets SET params = ? WHERE name = 'Anti-duplicidade'", (canonical_json(edited),))
    before = _preset_rows(db)
    apply_migrations(db)
    assert schema_version(db) == discover_migrations()[-1].version
    assert _preset_rows(db) == before


def test_anti_duplicate_migration_only_touches_the_builtin_row(tmp_path, db):
    """Um preset do usuário (is_builtin = 0) com esse nome e os 6 params originais não muda."""
    apply_migrations(db, _migrations_until(tmp_path, 14))
    db.execute("UPDATE presets SET is_builtin = 0 WHERE name = 'Anti-duplicidade'")
    before = _preset_rows(db)
    apply_migrations(db)
    assert _preset_rows(db) == before


def test_split_statements_handles_strings_with_semicolons():
    sql = "-- comentário\nCREATE TABLE t (v TEXT);\nINSERT INTO t VALUES ('a;b');\n"
    assert split_statements(sql) == ["CREATE TABLE t (v TEXT);", "INSERT INTO t VALUES ('a;b');"]
