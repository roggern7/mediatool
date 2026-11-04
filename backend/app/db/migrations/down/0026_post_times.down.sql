-- Reversão da 0026 (`app.db.migrate.revert_migration`; nunca roda no startup): a tabela dos horários e as colunas.
DROP TABLE IF EXISTS post_times;
ALTER TABLE import_groups DROP COLUMN patterns_only;
ALTER TABLE import_groups DROP COLUMN post_times;
ALTER TABLE batches DROP COLUMN patterns_only;
ALTER TABLE batches DROP COLUMN post_times;
