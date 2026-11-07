-- Reversão da 0031 (`app.db.migrate.revert_migration`; nunca roda no startup): os lotes das Legendas CapCut.
DROP INDEX IF EXISTS ix_capcut_jobs_batch;
ALTER TABLE capcut_jobs DROP COLUMN held;
ALTER TABLE capcut_jobs DROP COLUMN edit_job_id;
ALTER TABLE capcut_jobs DROP COLUMN mode;
ALTER TABLE capcut_jobs DROP COLUMN capcut_batch_id;
DROP TABLE IF EXISTS capcut_batches;
