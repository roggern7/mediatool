-- Reversão da 0029 (`app.db.migrate.revert_migration`; nunca roda no startup): só as duas tabelas das Legendas CapCut.
DROP TABLE IF EXISTS capcut_styles;
DROP TABLE IF EXISTS capcut_jobs;
