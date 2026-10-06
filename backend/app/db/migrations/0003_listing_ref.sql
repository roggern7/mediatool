-- Listagem incremental/retomável e renovação de URL assinada expirada (docs/BATCH_ROBUSTNESS.md, seções 0 e 2).
-- Todas as colunas abaixo são internas: nunca saem pela API nem por log (cursor e URL são sensíveis).

-- Referência de listagem do item (página/cursor/tamanho) para renovar a URL expirada da mídia.
-- jobs.listing_cursor é limpo quando o job termina.
ALTER TABLE jobs ADD COLUMN listing_page_index INTEGER;
ALTER TABLE jobs ADD COLUMN listing_cursor TEXT;
ALTER TABLE jobs ADD COLUMN listing_page_size INTEGER;
ALTER TABLE jobs ADD COLUMN media_url_refreshed_at TEXT;

-- Checkpoint da listagem do lote (cursor de retomada e contadores), persistido após cada página.
-- listing_state NULL em lotes antigos equivale a 'pending'.
ALTER TABLE batches ADD COLUMN listing_cursor TEXT;
ALTER TABLE batches ADD COLUMN listing_next_page INTEGER;
ALTER TABLE batches ADD COLUMN listing_pages INTEGER NOT NULL DEFAULT 0;
ALTER TABLE batches ADD COLUMN listing_posts_seen INTEGER NOT NULL DEFAULT 0;
ALTER TABLE batches ADD COLUMN listing_items_compatible INTEGER NOT NULL DEFAULT 0;
ALTER TABLE batches ADD COLUMN listing_unproductive_streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE batches ADD COLUMN listing_has_next INTEGER CHECK (listing_has_next IS NULL OR listing_has_next IN (0, 1));
ALTER TABLE batches ADD COLUMN listing_state TEXT
    CHECK (listing_state IS NULL OR listing_state IN ('pending', 'running', 'waiting_backpressure', 'paused', 'done'));

-- Contagem de ingest pendentes por lote (backpressure) e jobs da mesma página (renovação) sem varrer o lote.
CREATE INDEX ix_jobs_batch_status ON jobs (batch_id, status);
CREATE INDEX ix_jobs_batch_page ON jobs (batch_id, listing_page_index) WHERE listing_page_index IS NOT NULL;
