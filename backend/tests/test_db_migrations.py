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


# Colunas da 0021 (importação unificada), da 0022 (1a página vazia) e da 0023 (tipo de mídia e exportação automática)
# e o valor que as linhas antigas recebem.
_COLUMNS_0023 = {"media_set": None, "auto_export_content": None, "auto_export_state": None, "auto_export_id": None,
                 "auto_export_error": None}
_COLUMNS_0024 = {"request_id": None}   # pedido com várias partes (import_requests)
_COLUMNS_0021 = {"media": {"role": "original"}, "jobs": {"media_role": "original"},
                 "batches": {"media_role": "original", "max_views": None, "final_duplicates": None,
                             "listing_empty_first_page": None, **_COLUMNS_0023, **_COLUMNS_0024},
                 "import_groups": {"media_role": "original", "max_views": None, **_COLUMNS_0023, **_COLUMNS_0024}}


def _pop_0021(table, rows):
    """Tira das linhas as colunas da 0021, conferindo o valor padrão (linhas antigas não mudam)."""
    for row in rows:
        for name, value in _COLUMNS_0021.get(table, {}).items():
            if name in row:
                assert row.pop(name) == value, (table, name)


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


def test_dash_streams_migration_is_additive_over_existing_rows(tmp_path, db):
    """Migration 0016: `job_remote_refs.dash_streams` e `media.video_codec` (anuláveis) num banco COM dados; nenhuma
    linha existente muda ou some."""
    apply_migrations(db, _migrations_until(tmp_path, 15))
    assert schema_version(db) == 15
    now = "2026-01-01T00:00:00.000Z"
    db.execute("INSERT INTO batches (id, source, process, status, found, created_at, updated_at)"
               " VALUES (1, 'instagram_import', 0, 'running', 2, ?, ?)", (now, now))
    for media_id, kind, sha in ((1, "video", "a" * 64), (2, "image", "b" * 64)):
        db.execute("INSERT INTO media (id, source_platform, source_post_id, source_media_index, media_type,"
                   " original_path, original_sha256, file_size, width, height, fps, has_audio, mime_type, created_at,"
                   " updated_at) VALUES (?, 'instagram', ?, 0, ?, ?, ?, 17978873, 720, 1280, 30.0, 1, 'video/mp4',"
                   " ?, ?)", (media_id, f"POST{media_id}", kind, f"originals/{sha[:2]}/{sha}.mp4", sha, now, now))
    db.execute("INSERT INTO jobs (id, batch_id, media_id, kind, status, source_post_id, source_media_index,"
               " created_at, updated_at) VALUES (1, 1, 1, 'ingest', 'completed', 'POST1', 0, ?, ?)", (now, now))
    db.execute("INSERT INTO jobs (id, batch_id, kind, status, source_post_id, source_media_index, created_at,"
               " updated_at) VALUES (2, 1, 'ingest', 'pending', 'POST3', 0, ?, ?)", (now, now))
    db.execute("INSERT INTO job_remote_refs (job_id, media_url, thumbnail_url, listing_cursor, listing_page_index,"
               " listing_page_size) VALUES (2, 'https://video-fake.fbcdn.net/v.mp4?oh=FAKE', NULL, 'FAKE_CURSOR', 1,"
               " 12)")
    tables = ("media", "jobs", "job_remote_refs", "batches", "presets")
    before = {table: [dict(row) for row in db.fetchall(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}

    # Só até a 0016 (as seguintes acrescentam colunas e têm teste próprio).
    until_16 = _migrations_until(tmp_path, 16)
    assert apply_migrations(db, until_16) == [16]
    assert schema_version(db) == 16
    new_columns = {"media": "video_codec", "job_remote_refs": "dash_streams"}
    for table in tables:
        rows = [dict(row) for row in db.fetchall(f"SELECT * FROM {table} ORDER BY 1")]
        column = new_columns.get(table)
        if column is not None:
            info = {row["name"]: row for row in db.fetchall(f"PRAGMA table_info({table})")}
            assert info[column]["type"] == "TEXT" and not info[column]["notnull"] and info[column]["dflt_value"] is None
            assert rows and all(row.pop(column) is None for row in rows), "linhas antigas ficam com NULL"
        _pop_0021(table, rows)
        assert rows == before[table], table
    assert db.fetchall("PRAGMA foreign_key_check") == [] and db.scalar("PRAGMA integrity_check") == "ok"

    # A coluna nova só aceita NULL ou JSON válido, e não impede apagar/limpar a linha.
    db.execute("UPDATE job_remote_refs SET dash_streams = ? WHERE job_id = 2", ('{"dash_width":1080}',))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE job_remote_refs SET dash_streams = 'nao e json' WHERE job_id = 2")
    db.execute("UPDATE media SET video_codec = 'vp9' WHERE id = 1")
    db.execute("UPDATE batches SET status = 'completed' WHERE id = 1")   # trigger da 0005: apaga as referências
    assert db.scalar("SELECT COUNT(*) FROM job_remote_refs") == 0
    assert apply_migrations(db, until_16) == []


def test_history_hidden_migration_is_additive_over_existing_rows(tmp_path, db):
    """Migration 0018: `batches.history_hidden_at` e `import_groups.history_hidden_at` (anuláveis) num banco COM
    dados; nenhuma linha existente muda ou some (NULL = visível no histórico)."""
    apply_migrations(db, _migrations_until(tmp_path, 16))
    now = "2026-01-01T00:00:00.000Z"
    db.execute("INSERT INTO import_groups (id, content_type, requested_limit, process, created_at)"
               " VALUES (1, 'all', 10, 0, ?)", (now,))
    db.execute("INSERT INTO batches (id, source, process, status, found, created_at, updated_at)"
               " VALUES (1, 'instagram_import', 0, 'completed', 0, ?, ?)", (now, now))
    db.execute("INSERT INTO batches (id, source, process, status, found, created_at, updated_at, group_id,"
               " group_position) VALUES (2, 'instagram_import', 0, 'completed', 0, ?, ?, 1, 1)", (now, now))
    tables = ("batches", "import_groups")
    before = {table: [dict(row) for row in db.fetchall(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}
    assert apply_migrations(db) == [m.version for m in discover_migrations() if m.version > 16]
    # Colunas das 0017/0019 (filtros) também chegam NULL; aqui a da 0018 é conferida em detalhe.
    filters = ("min_views", "listing_views", "listing_views_partial", "posted_within_days", "posted_after",
               "listing_period_end")
    for table in tables:
        info = {row["name"]: row for row in db.fetchall(f"PRAGMA table_info({table})")}
        column = info["history_hidden_at"]
        assert column["type"] == "TEXT" and not column["notnull"] and column["dflt_value"] is None
        rows = [dict(row) for row in db.fetchall(f"SELECT * FROM {table} ORDER BY 1")]
        assert rows and all(row.pop("history_hidden_at") is None for row in rows)
        for row in rows:
            for name in filters:
                assert row.pop(name, None) is None
        _pop_0021(table, rows)
        assert rows == before[table], table
    assert db.fetchall("PRAGMA foreign_key_check") == [] and db.scalar("PRAGMA integrity_check") == "ok"


def test_min_views_migration_is_additive_over_existing_rows(tmp_path, db):
    """Migration 0017: `import_groups.min_views`, `batches.min_views`, `batches.listing_views` e
    `batches.listing_views_partial` (anuláveis) num banco COM dados; nenhuma linha existente muda ou some."""
    apply_migrations(db, _migrations_until(tmp_path, 16))
    assert schema_version(db) == 16
    now = "2026-01-01T00:00:00.000Z"
    db.execute("INSERT INTO import_groups (id, content_type, requested_limit, process, created_at)"
               " VALUES (1, 'reels', 10, 0, ?)", (now,))
    db.execute("INSERT INTO batches (id, source, source_profile, content_type, requested_limit, process, status, found,"
               " created_at, updated_at, group_id, group_position, listing_dropped_partial)"
               " VALUES (1, 'instagram_import', 'perfil', 'reels', 10, 0, 'listing', 1, ?, ?, 1, 1, '{\"x\":1}')",
               (now, now))
    db.execute("INSERT INTO jobs (id, batch_id, kind, status, source_post_id, source_media_index, created_at,"
               " updated_at) VALUES (1, 1, 'ingest', 'pending', 'POST1', 0, ?, ?)", (now, now))
    tables = ("import_groups", "batches", "jobs", "media")
    before = {table: [dict(row) for row in db.fetchall(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}

    assert apply_migrations(db) == [m.version for m in discover_migrations() if m.version > 16]
    assert schema_version(db) >= 17
    new_columns = {"import_groups": ("min_views", "history_hidden_at", "posted_within_days", "posted_after"),
                   "batches": ("min_views", "listing_views", "listing_views_partial", "history_hidden_at",
                               "posted_within_days", "posted_after", "listing_period_end")}
    for table in tables:
        rows = [dict(row) for row in db.fetchall(f"SELECT * FROM {table} ORDER BY 1")]
        info = {row["name"]: row for row in db.fetchall(f"PRAGMA table_info({table})")}
        for column in new_columns.get(table, ()):
            assert not info[column]["notnull"] and info[column]["dflt_value"] is None
            assert rows and all(row.pop(column) is None for row in rows), "linhas antigas ficam com NULL"
        _pop_0021(table, rows)
        assert rows == before[table], table
    assert db.fetchall("PRAGMA foreign_key_check") == [] and db.scalar("PRAGMA integrity_check") == "ok"

    # Só NULL, inteiro >= 1 (mínimo) e JSON válido (contadores).
    db.execute("UPDATE batches SET min_views = 500000, listing_views = '{\"approved\":1}' WHERE id = 1")
    db.execute("UPDATE import_groups SET min_views = 1 WHERE id = 1")
    for sql in ("UPDATE batches SET min_views = 0 WHERE id = 1", "UPDATE import_groups SET min_views = -1 WHERE id = 1",
                "UPDATE batches SET listing_views = 'nao e json' WHERE id = 1",
                "UPDATE batches SET listing_views_partial = 'nao e json' WHERE id = 1"):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
    assert apply_migrations(db) == []


# sha256 (fins de linha normalizados para LF) da 0017 de bf9b84b, já aplicada como versão 17 no banco real do dono.
MIGRATION_0017_SHA256 = "b1ca6927b01f15c4388f9912deef8d0fa4e8614e7368ff58fdac9e9b21be2e00"


def test_min_views_migration_keeps_the_name_and_content_already_applied_by_the_owner():
    import hashlib

    [migration] = [m for m in discover_migrations() if m.version == 17]
    assert migration.path.name == "0017_min_views.sql"
    text = migration.path.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(text).hexdigest() == MIGRATION_0017_SHA256


def _bank_with_rows(db):
    now = "2026-01-01T00:00:00.000Z"
    db.execute("INSERT INTO import_groups (id, content_type, requested_limit, process, created_at)"
               " VALUES (1, 'reels', 10, 0, ?)", (now,))
    db.execute("INSERT INTO batches (id, source, source_profile, content_type, requested_limit, process, status, found,"
               " created_at, updated_at, group_id, group_position, listing_end)"
               " VALUES (1, 'instagram_import', 'perfil', 'reels', 10, 0, 'listing', 1, ?, ?, 1, 1, 'profile_end')",
               (now, now))
    db.execute("INSERT INTO jobs (id, batch_id, kind, status, source_post_id, source_media_index, created_at,"
               " updated_at) VALUES (1, 1, 'ingest', 'pending', 'POST1', 0, ?, ?)", (now, now))


@pytest.mark.parametrize("applied_until", [16, 17])
def test_posted_period_migration_is_additive_over_existing_rows(tmp_path, db, applied_until):
    """Migration 0019 (período de postagem) sobre banco com dados: até a 0016 (aplica 0017+0018+0019) e com a 0017
    já aplicada SEM a 0018 (o banco real do dono: aplica 0018+0019). Nenhuma linha existente muda ou some."""
    apply_migrations(db, _migrations_until(tmp_path, applied_until))
    assert schema_version(db) == applied_until
    _bank_with_rows(db)
    tables = ("import_groups", "batches", "jobs", "media")
    before = {table: [dict(row) for row in db.fetchall(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}

    assert apply_migrations(db) == [v for v in (17, 18, 19, 20, 21, 22, 23, 24) if v > applied_until]
    assert schema_version(db) == 24
    added = {"import_groups": ("posted_within_days", "posted_after", "history_hidden_at"),
             "batches": ("posted_within_days", "posted_after", "listing_period_end", "history_hidden_at")}
    if applied_until < 17:
        added["import_groups"] += ("min_views",)
        added["batches"] += ("min_views", "listing_views", "listing_views_partial")
    for table in tables:
        rows = [dict(row) for row in db.fetchall(f"SELECT * FROM {table} ORDER BY 1")]
        info = {row["name"]: row for row in db.fetchall(f"PRAGMA table_info({table})")}
        for column in added.get(table, ()):
            assert not info[column]["notnull"] and info[column]["dflt_value"] is None
            assert rows and all(row.pop(column) is None for row in rows), "linhas antigas ficam com NULL"
        _pop_0021(table, rows)
        assert rows == before[table], table
    assert db.fetchall("PRAGMA foreign_key_check") == [] and db.scalar("PRAGMA integrity_check") == "ok"

    db.execute("UPDATE batches SET posted_within_days = 30, posted_after = '2026-09-29T00:00:00Z',"
               " listing_period_end = 1 WHERE id = 1")
    db.execute("UPDATE import_groups SET posted_within_days = 3650 WHERE id = 1")
    for sql in ("UPDATE batches SET posted_within_days = 0 WHERE id = 1",
                "UPDATE import_groups SET posted_within_days = 3651 WHERE id = 1",
                "UPDATE batches SET listing_period_end = 2 WHERE id = 1"):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
    assert apply_migrations(db) == []


def test_new_database_gets_17_18_and_19(db):
    assert apply_migrations(db)[-8:] == [17, 18, 19, 20, 21, 22, 23, 24]
    assert schema_version(db) == 24
    columns = {row["name"] for row in db.fetchall("PRAGMA table_info(batches)")}
    assert {"min_views", "history_hidden_at", "posted_after", "posted_within_days", "listing_period_end"} <= columns


def test_batch_media_params_migration_is_additive(tmp_path, db):
    """Migration 0020 (params do job no batch_media, recorte manual da Edição): pares antigos ficam com NULL."""
    apply_migrations(db, _migrations_until(tmp_path, 19))
    _bank_with_rows(db)
    now = "2026-01-01T00:00:00.000Z"
    db.execute("INSERT INTO media (id, source_platform, media_type, original_path, original_sha256, file_size,"
               " created_at, updated_at) VALUES (7, 'local', 'video', 'x', ?, 1, ?, ?)", ("a" * 64, now, now))
    db.execute("INSERT INTO batch_media (batch_id, media_id) VALUES (1, 7)")
    before = [dict(row) for row in db.fetchall("SELECT * FROM batch_media ORDER BY 1, 2")]
    assert before
    assert apply_migrations(db) == [20, 21, 22, 23, 24]
    rows = [dict(row) for row in db.fetchall("SELECT * FROM batch_media ORDER BY 1, 2")]
    assert all(row.pop("params") is None for row in rows) and rows == before
    db.execute("UPDATE batch_media SET params = '{\"speed\": 1.0}'")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE batch_media SET params = 'x'")
    assert db.fetchall("PRAGMA foreign_key_check") == [] and db.scalar("PRAGMA integrity_check") == "ok"


def test_media_set_auto_export_migration_is_additive_over_a_bank_at_0022(tmp_path, db):
    """Migration 0023 sobre banco com 0001–0022 (o banco real do dono) e dados: só colunas NULL novas, nenhuma linha
    muda; o índice único dos jobs de ingestão passa a incluir o papel (vídeo e capa do mesmo item no mesmo lote)."""
    apply_migrations(db, _migrations_until(tmp_path, 22))
    assert schema_version(db) == 22
    _bank_with_rows(db)
    tables = ("import_groups", "batches", "jobs", "media")
    before = {table: [dict(row) for row in db.fetchall(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}
    assert apply_migrations(db) == [23, 24]
    assert schema_version(db) == 24
    for table in tables:
        rows = [dict(row) for row in db.fetchall(f"SELECT * FROM {table} ORDER BY 1")]
        info = {row["name"]: row for row in db.fetchall(f"PRAGMA table_info({table})")}
        if table in ("batches", "import_groups"):
            for column in {**_COLUMNS_0023, **_COLUMNS_0024}:
                assert not info[column]["notnull"] and info[column]["dflt_value"] is None
                assert all(row.pop(column) is None for row in rows), "linhas antigas ficam com NULL"
        assert rows == before[table], table
    assert db.fetchall("PRAGMA foreign_key_check") == [] and db.scalar("PRAGMA integrity_check") == "ok"
    now = "2026-01-01T00:00:00.000Z"
    insert = ("INSERT INTO jobs (batch_id, kind, status, source_post_id, source_media_index, media_role, created_at,"
              " updated_at) VALUES (1, 'ingest', 'pending', 'POST1', 0, ?, ?, ?)")
    db.execute(insert, ("cover", now, now))   # mesmo item, outro papel: aceito
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("cover", now, now))   # mesmo item e mesmo papel: recusado
    db.execute("UPDATE batches SET media_set = 'videos_covers', auto_export_content = 'both',"
               " auto_export_state = 'waiting' WHERE id = 1")
    for sql in ("UPDATE batches SET media_set = 'outro' WHERE id = 1",
                "UPDATE import_groups SET auto_export_content = 'tudo' WHERE id = 1",
                "UPDATE batches SET auto_export_state = 'pronto' WHERE id = 1"):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
    assert apply_migrations(db) == []


def test_import_requests_migration_is_additive_over_a_bank_at_0023(tmp_path, db):
    """Migration 0024 (pedido com várias partes e UM ZIP): só uma tabela nova; nenhuma linha existente muda."""
    apply_migrations(db, _migrations_until(tmp_path, 23))
    _bank_with_rows(db)
    tables = ("import_groups", "batches", "jobs", "media")
    before = {table: [dict(row) for row in db.fetchall(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}
    assert apply_migrations(db) == [24]
    assert schema_version(db) == 24
    for table in tables:
        rows = [dict(row) for row in db.fetchall(f"SELECT * FROM {table} ORDER BY 1")]
        if table in ("batches", "import_groups"):
            info = {row["name"]: row for row in db.fetchall(f"PRAGMA table_info({table})")}
            assert not info["request_id"]["notnull"] and info["request_id"]["dflt_value"] is None
            assert all(row.pop("request_id") is None for row in rows), "linhas antigas ficam com NULL"
        assert rows == before[table], table
    assert db.scalar("SELECT COUNT(*) FROM import_requests") == 0
    now = "2026-01-01T00:00:00.000Z"
    db.execute("INSERT INTO import_requests (auto_export_content, created_at) VALUES ('originals', ?)", (now,))
    assert db.scalar("SELECT auto_export_state FROM import_requests") == "waiting"
    db.execute("UPDATE batches SET request_id = 1 WHERE id = 1")
    for sql in ("INSERT INTO import_requests (auto_export_content, created_at) VALUES ('tudo', '" + now + "')",
                "UPDATE import_requests SET auto_export_state = 'pronto'"):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
    assert db.fetchall("PRAGMA foreign_key_check") == [] and db.scalar("PRAGMA integrity_check") == "ok"
    assert apply_migrations(db) == []
