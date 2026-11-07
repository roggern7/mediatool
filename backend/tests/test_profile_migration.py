from __future__ import annotations

import shutil
import sqlite3

import pytest

from app.db.connection import Database
from app.db.migrate import MIGRATIONS_DIR, apply_migrations, discover_migrations, revert_migration, schema_version

NOW = "2026-01-01T00:00:00.000Z"
OLD_TABLES = ("presets", "media", "batches", "jobs", "exports", "settings", "app_state", "import_groups")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "data" / "media-tool.db")
    yield database
    database.close_all()


def old_migrations(tmp_path, before: str = "0025"):
    folder = tmp_path / f"migrations_before_{before}"
    folder.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < before:
            shutil.copy(path, folder)
    return folder


def seed_old_data(db) -> None:
    db.execute(
        "INSERT INTO media (id, source_platform, source_post_id, source_media_index, media_type, original_path,"
        " original_sha256, file_size, created_at, updated_at)"
        " VALUES (1, 'instagram', 'P1', 0, 'image', 'o/x.jpg', ?, 1, ?, ?)", ("1" * 64, NOW, NOW))
    db.execute("INSERT INTO batches (id, source, status, found, process, created_at, updated_at)"
               " VALUES (1, 'instagram_import', 'completed', 0, 0, ?, ?)", (NOW, NOW))
    db.execute("INSERT INTO app_state (key, value, updated_at) VALUES ('chave', 'valor', ?)", (NOW,))


def counts(db) -> dict[str, int]:
    return {table: db.scalar(f"SELECT COUNT(*) FROM {table}") for table in OLD_TABLES}


def table_names(db) -> set[str]:
    return {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_down_folder_is_not_discovered_as_migration():
    names = [migration.path.name for migration in discover_migrations()]
    assert "0025_profile_analyses.sql" in names and not any("down" in name for name in names)
    assert (MIGRATIONS_DIR / "down" / "0025_profile_analyses.down.sql").is_file()


def test_database_at_version_24_with_data_migrates_without_changing_counts(tmp_path, db):
    apply_migrations(db, old_migrations(tmp_path))
    assert schema_version(db) == 24
    seed_old_data(db)
    before = counts(db)
    schema_before = {row["sql"] for row in db.fetchall("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")}

    # Só até a 0025 (a 0026 acrescenta colunas a batches/import_groups e muda o SQL delas).
    assert apply_migrations(db, old_migrations(tmp_path, "0026")) == [25]
    assert counts(db) == before
    assert schema_before <= {row["sql"] for row in db.fetchall("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")}
    assert {"profile_analyses", "profile_analysis_items"} <= table_names(db)


def test_indexes_come_and_go_with_the_tables(db):
    apply_migrations(db)
    names = {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"ix_profile_items_state", "ix_profile_items_position", "ix_profile_items_open"} <= names
    partial = db.scalar("SELECT sql FROM sqlite_master WHERE name = 'ix_profile_items_open'")
    assert "WHERE state IN ('pending', 'running')" in partial
    revert_migration(db, 30)   # só a última aplicada pode ser revertida (0030: editor de legendas)
    revert_migration(db, 29)   # 0029: Legendas CapCut
    revert_migration(db, 28)   # 0028: Reels da análise
    revert_migration(db, 27)   # 0027: origem do horário
    revert_migration(db, 26)   # 0026: horário dos posts
    revert_migration(db, 25)
    assert db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'ix_profile_items_%'") == 0


def test_up_down_up_keeps_old_data(db):
    apply_migrations(db)
    seed_old_data(db)
    db.execute("INSERT INTO profile_analyses (total, created_at) VALUES (1, ?)", (NOW,))
    db.execute("INSERT INTO profile_analysis_items (analysis_id, position, username) VALUES (1, 1, 'a1')")
    before = counts(db)

    revert_migration(db, 30)   # só a última aplicada pode ser revertida (0030: editor de legendas)
    revert_migration(db, 29)   # 0029: Legendas CapCut
    revert_migration(db, 28)   # 0028: Reels da análise
    revert_migration(db, 27)   # 0027: origem do horário
    revert_migration(db, 26)   # 0026: horário dos posts
    revert_migration(db, 25)
    assert schema_version(db) == 24 and counts(db) == before
    assert not {"profile_analyses", "profile_analysis_items"} & table_names(db)
    assert db.scalar("SELECT COUNT(*) FROM schema_migrations WHERE version = 25") == 0

    assert apply_migrations(db) == [25, 26, 27, 28, 29, 30]
    assert schema_version(db) == 30 and counts(db) == before
    assert db.scalar("SELECT COUNT(*) FROM profile_analyses") == 0


def test_only_latest_applied_migration_can_be_reverted(tmp_path, db):
    apply_migrations(db)
    # A 0024 não tem arquivo de reversão: aqui ela ganha um, para provar que a regra da última vale por si.
    folder = tmp_path / "migs"
    (folder / "down").mkdir(parents=True)
    for path in MIGRATIONS_DIR.glob("*.sql"):
        shutil.copy(path, folder)
    (folder / "down" / "0024_import_requests.down.sql").write_text("DROP TABLE settings;", encoding="utf-8")
    with pytest.raises(RuntimeError, match="última"):
        revert_migration(db, 24, folder)
    assert schema_version(db) == 30 and "settings" in table_names(db)


def test_revert_requires_down_file(tmp_path, db):
    folder = tmp_path / "migs"
    folder.mkdir()
    (folder / "0001_um.sql").write_text("CREATE TABLE um (id INTEGER PRIMARY KEY);", encoding="utf-8")
    apply_migrations(db, folder)
    with pytest.raises(RuntimeError, match="reversão"):
        revert_migration(db, 1, folder)
    assert schema_version(db) == 1 and "um" in table_names(db)


def test_revert_unknown_version(db):
    apply_migrations(db)
    with pytest.raises(RuntimeError, match="não encontrada"):
        revert_migration(db, 999)


def test_failed_revert_rolls_back(tmp_path, db):
    folder = tmp_path / "migs"
    (folder / "down").mkdir(parents=True)
    (folder / "0001_um.sql").write_text("CREATE TABLE um (id INTEGER PRIMARY KEY);", encoding="utf-8")
    (folder / "down" / "0001_um.down.sql").write_text(
        "DROP TABLE um;\nDROP TABLE nao_existe;", encoding="utf-8")
    apply_migrations(db, folder)
    with pytest.raises(sqlite3.OperationalError):
        revert_migration(db, 1, folder)
    assert schema_version(db) == 1 and "um" in table_names(db)


def test_constraints(db):
    apply_migrations(db)
    db.execute("INSERT INTO profile_analyses (total, created_at) VALUES (1, ?)", (NOW,))
    item = "INSERT INTO profile_analysis_items (analysis_id, position, username, state, account_status, stories)" \
           " VALUES (1, ?, ?, ?, ?, ?)"
    db.execute(item, (1, "a1", "pending", None, None))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(item, (2, "a1", "pending", None, None))     # UNIQUE (analysis_id, username)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(item, (3, "b", "outro", None, None))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(item, (3, "b", "done", "talvez", None))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(item, (3, "b", "done", "active", "talvez"))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE profile_analyses SET status = 'outro'")
    db.execute("DELETE FROM profile_analyses")
    assert db.scalar("SELECT COUNT(*) FROM profile_analysis_items") == 0     # ON DELETE CASCADE


def test_0027_adds_and_removes_post_times_source(db):
    def columns() -> set[str]:
        return {row["name"] for row in db.fetchall("PRAGMA table_info(post_times)")}

    apply_migrations(db)
    revert_migration(db, 30)
    revert_migration(db, 29)
    revert_migration(db, 28)
    assert "source" in columns() and schema_version(db) == 27
    assert (MIGRATIONS_DIR / "down" / "0027_post_times_source.down.sql").is_file()
    revert_migration(db, 27)
    assert "source" not in columns() and {"code", "taken_at", "pinned"} <= columns() and schema_version(db) == 26
    assert apply_migrations(db) == [27, 28, 29, 30] and "source" in columns()


def test_0028_adds_and_removes_profile_reels(db):
    def columns() -> set[str]:
        return {row["name"] for row in db.fetchall("PRAGMA table_info(profile_analysis_items)")}

    reels = {"reels_count", "reels_more", "latest_reel_at"}
    apply_migrations(db)
    db.execute("INSERT INTO profile_analyses (total, created_at) VALUES (1, ?)", (NOW,))
    db.execute("INSERT INTO profile_analysis_items (analysis_id, position, username) VALUES (1, 1, 'a1')")
    assert reels <= columns() and schema_version(db) == 30
    assert db.fetchone("SELECT reels_count, reels_more, latest_reel_at FROM profile_analysis_items")[0] is None
    assert (MIGRATIONS_DIR / "down" / "0028_profile_reels.down.sql").is_file()
    revert_migration(db, 30)
    revert_migration(db, 29)
    revert_migration(db, 28)
    assert not reels & columns() and "views_reels" in columns() and schema_version(db) == 27
    assert db.scalar("SELECT COUNT(*) FROM profile_analysis_items") == 1
    assert apply_migrations(db) == [28, 29, 30] and reels <= columns()
