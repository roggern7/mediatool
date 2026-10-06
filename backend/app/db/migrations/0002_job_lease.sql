-- Lease do job: identifica o processo (worker) que está executando o job.
-- Job com claimed_by preenchido nunca é reivindicado de novo, mesmo em estado intermediário ('downloaded').

ALTER TABLE jobs ADD COLUMN claimed_by TEXT;

CREATE INDEX ix_jobs_claimed ON jobs (claimed_by) WHERE claimed_by IS NOT NULL;
