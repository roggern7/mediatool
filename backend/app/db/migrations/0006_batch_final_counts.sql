-- Contadores por status congelados (GET /api/batches/{id} de lote encerrado não agrega mais os jobs do lote).
-- Invariante: final_* preenchidos => lote encerrado e sem job aberto.
ALTER TABLE batches ADD COLUMN final_completed INTEGER;
ALTER TABLE batches ADD COLUMN final_cancelled INTEGER;

UPDATE batches SET
    final_completed = (SELECT COUNT(*) FROM jobs j WHERE j.batch_id = batches.id AND j.status = 'completed'),
    final_cancelled = (SELECT COUNT(*) FROM jobs j WHERE j.batch_id = batches.id AND j.status = 'cancelled')
WHERE final_found IS NOT NULL;

UPDATE batches SET
    final_found = NULL, final_downloaded = NULL, final_processed = NULL, final_skipped = NULL, final_failed = NULL,
    final_completed = NULL, final_cancelled = NULL
WHERE final_found IS NOT NULL
  AND EXISTS (SELECT 1 FROM jobs j WHERE j.batch_id = batches.id
              AND j.status IN ('pending', 'downloading', 'downloaded', 'processing'));
