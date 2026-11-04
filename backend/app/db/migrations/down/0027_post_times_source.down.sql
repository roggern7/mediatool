-- Reversão da 0027 (`app.db.migrate.revert_migration`; nunca roda no startup): a coluna de origem do horário.
ALTER TABLE post_times DROP COLUMN source;
