-- Reversão da 0028 (`app.db.migrate.revert_migration`; nunca roda no startup): os Reels da Análise de Perfis.
ALTER TABLE profile_analysis_items DROP COLUMN latest_reel_at;
ALTER TABLE profile_analysis_items DROP COLUMN reels_more;
ALTER TABLE profile_analysis_items DROP COLUMN reels_count;
