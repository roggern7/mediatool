-- Reversão da 0030 (`app.db.migrate.revert_migration`; nunca roda no startup): o design do editor visual.
ALTER TABLE capcut_styles DROP COLUMN design;
