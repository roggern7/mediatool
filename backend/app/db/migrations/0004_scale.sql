-- Escala da API e do banco (docs/BATCH_ROBUSTNESS.md, seção 6).

-- URLs assinadas e cursor de listagem saem de `jobs` para uma tabela à parte (nunca saem pela API nem por log).
-- Linha apagada quando o job conclui; URLs apagadas quando falha/cancela (cursor/página ficam para o retry);
-- tudo apagado quando o lote conclui ou é cancelado. jobs.listing_page_index/listing_page_size (não sensíveis)
-- continuam em `jobs` para o índice de página e a renovação da página 1 de jobs repetidos.
CREATE TABLE job_remote_refs (
    job_id              INTEGER PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    media_url           TEXT,
    thumbnail_url       TEXT,
    listing_cursor      TEXT,
    listing_page_index  INTEGER,
    listing_page_size   INTEGER
);

INSERT INTO job_remote_refs (job_id, media_url, thumbnail_url, listing_cursor, listing_page_index, listing_page_size)
SELECT id,
       CASE WHEN status IN ('failed', 'cancelled') THEN NULL ELSE media_url END,
       CASE WHEN status IN ('failed', 'cancelled') THEN NULL ELSE thumbnail_url END,
       listing_cursor, listing_page_index, listing_page_size
FROM jobs
WHERE kind = 'ingest' AND status <> 'completed'
  AND (media_url IS NOT NULL OR thumbnail_url IS NOT NULL OR listing_cursor IS NOT NULL
       OR listing_page_index IS NOT NULL)
  AND (batch_id IS NULL OR batch_id NOT IN (SELECT id FROM batches WHERE status IN ('completed', 'cancelled')));

ALTER TABLE jobs DROP COLUMN media_url;
ALTER TABLE jobs DROP COLUMN thumbnail_url;
ALTER TABLE jobs DROP COLUMN listing_cursor;

UPDATE batches SET listing_cursor = NULL WHERE status IN ('completed', 'failed', 'cancelled');

-- Índices das consultas quentes (sem SCAN de jobs).
DROP INDEX IF EXISTS ix_jobs_batch;
-- (batch_id, status) é prefixo do índice de cobertura abaixo.
DROP INDEX IF EXISTS ix_jobs_batch_status;
CREATE INDEX ix_jobs_batch_cover
    ON jobs (batch_id, status, kind, is_duplicate, stage, media_id, rendition_id);
CREATE INDEX ix_jobs_status_id ON jobs (status, id);
CREATE INDEX ix_jobs_rendition ON jobs (rendition_id) WHERE rendition_id IS NOT NULL;
CREATE INDEX ix_jobs_cancel ON jobs (id) WHERE cancel_requested = 1;
CREATE INDEX ix_media_profile_nocase ON media (source_profile COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_jobs_batch_page
    ON jobs (batch_id, listing_page_index) WHERE listing_page_index IS NOT NULL;
