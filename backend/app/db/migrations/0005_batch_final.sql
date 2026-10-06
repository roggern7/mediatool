-- Rodada final de escala (docs/BATCH_ROBUSTNESS.md, seção 6).

-- GET /api/jobs?batch_id=...: ordem id DESC (keyset) direto do índice, sem TEMP B-TREE; com um status, pelo segundo.
-- O GROUP BY status das contagens do lote é coberto por (batch_id, status, id).
CREATE INDEX ix_jobs_batch_id ON jobs (batch_id, id);
CREATE INDEX ix_jobs_batch_status_id ON jobs (batch_id, status, id);

-- Contadores finais congelados quando o lote termina (a retenção apaga jobs antigos; o progresso não pode zerar).
-- NULL enquanto o lote está aberto (ou depois de reaberto).
ALTER TABLE batches ADD COLUMN final_found INTEGER;
ALTER TABLE batches ADD COLUMN final_downloaded INTEGER;
ALTER TABLE batches ADD COLUMN final_processed INTEGER;
ALTER TABLE batches ADD COLUMN final_skipped INTEGER;
ALTER TABLE batches ADD COLUMN final_failed INTEGER;

UPDATE batches SET
    final_found = MAX(found, (SELECT COUNT(*) FROM jobs j WHERE j.batch_id = batches.id)),
    final_downloaded = (SELECT COUNT(*) FROM jobs j WHERE j.batch_id = batches.id
                        AND j.kind = 'ingest' AND j.media_id IS NOT NULL AND j.is_duplicate = 0),
    final_processed = (SELECT COUNT(*) FROM jobs j WHERE j.batch_id = batches.id
                       AND j.status = 'completed' AND j.rendition_id IS NOT NULL AND COALESCE(j.stage, '') <> 'skipped'),
    final_skipped = (SELECT COUNT(*) FROM jobs j WHERE j.batch_id = batches.id
                     AND j.status = 'completed' AND j.stage = 'skipped'),
    final_failed = (SELECT COUNT(*) FROM jobs j WHERE j.batch_id = batches.id AND j.status = 'failed')
WHERE status IN ('completed', 'failed', 'cancelled');

-- Lote concluído: referências remotas de todos os jobs do lote apagadas na mesma instrução da conclusão (o
-- trigger só roda nas linhas que o UPDATE altera: refresh_status de lote ainda aberto não altera nenhuma).
CREATE TRIGGER tr_batches_completed_drop_refs AFTER UPDATE OF status ON batches
WHEN NEW.status = 'completed' AND OLD.status <> 'completed'
BEGIN
    DELETE FROM job_remote_refs WHERE job_id IN (SELECT id FROM jobs WHERE batch_id = NEW.id);
END;
