-- Reversão da 0025 (`app.db.migrate.revert_migration`; nunca roda no startup): só as duas tabelas da Análise de Perfis.
DROP TABLE IF EXISTS profile_analysis_items;
DROP TABLE IF EXISTS profile_analyses;
