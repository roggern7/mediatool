-- Listagem robusta (docs/BATCH_ROBUSTNESS.md, seção 9: C1, M1, M4, M3).

-- listing_state ganha 'needs_attention' (erro que exige ação; checkpoint mantido) e 'cancelled' (lote cancelado).
-- O CHECK de uma coluna não muda com ALTER: coluna nova com o CHECK novo, cópia, troca de nome.
ALTER TABLE batches ADD COLUMN listing_state_v2 TEXT
    CHECK (listing_state_v2 IS NULL OR listing_state_v2 IN
           ('pending', 'running', 'waiting_backpressure', 'paused', 'needs_attention', 'done', 'cancelled'));
UPDATE batches SET listing_state_v2 = listing_state;
ALTER TABLE batches DROP COLUMN listing_state;
ALTER TABLE batches RENAME COLUMN listing_state_v2 TO listing_state;
UPDATE batches SET listing_state = 'cancelled'
WHERE status = 'cancelled' AND listing_state IS NOT NULL AND listing_state <> 'done';

-- Backoff próprio da listagem do lote em erro retryable/rede (1m, 5m, 15m, 1h, 1h...) e prazo da próxima tentativa
-- (também a nova tentativa automática de `needs_attention`, a cada 6 h).
ALTER TABLE batches ADD COLUMN listing_retry_level INTEGER NOT NULL DEFAULT 0;
ALTER TABLE batches ADD COLUMN listing_retry_at TEXT;
-- Recusas seguidas do cursor salvo no início de um trecho retomado (1a: nova tentativa com o mesmo cursor depois do
-- backoff; 2a: needs_attention). Zera a cada página entregue.
ALTER TABLE batches ADD COLUMN listing_cursor_refusals INTEGER NOT NULL DEFAULT 0;
-- Tentativas automáticas já agendadas a partir de needs_attention (máximo 4). Zera a cada página entregue.
ALTER TABLE batches ADD COLUMN listing_attention_tries INTEGER NOT NULL DEFAULT 0;

-- jobs.duplicate_reason ganha 'duplicate_in_flight': o mesmo item já está sendo baixado por outro lote aberto.
ALTER TABLE jobs ADD COLUMN duplicate_reason_v2 TEXT
    CHECK (duplicate_reason_v2 IS NULL OR duplicate_reason_v2 IN ('source_id', 'sha256', 'duplicate_in_flight'));
UPDATE jobs SET duplicate_reason_v2 = duplicate_reason WHERE duplicate_reason IS NOT NULL;
ALTER TABLE jobs DROP COLUMN duplicate_reason;
ALTER TABLE jobs RENAME COLUMN duplicate_reason_v2 TO duplicate_reason;
-- Busca do item em download por outro lote (criação de jobs da listagem), sem varrer jobs.
CREATE INDEX ix_jobs_ingest_in_flight ON jobs (source_post_id, source_media_index)
    WHERE kind = 'ingest' AND status IN ('pending', 'downloading');

-- Renditions únicas por (mídia, preset, params). `params` é JSON canônico (canonical_json): a igualdade do texto
-- equivale à de um hash dos params. Duplicatas existentes: fica a mais antiga; jobs apontam para ela antes da remoção.
UPDATE jobs SET rendition_id = (
    SELECT MIN(k.id) FROM renditions k JOIN renditions r ON r.id = jobs.rendition_id
    WHERE k.media_id = r.media_id AND k.preset_id = r.preset_id AND k.params = r.params
)
WHERE rendition_id IN (
    SELECT r.id FROM renditions r
    WHERE r.preset_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM renditions k
        WHERE k.media_id = r.media_id AND k.preset_id = r.preset_id AND k.params = r.params AND k.id < r.id)
);
DELETE FROM renditions
WHERE preset_id IS NOT NULL AND EXISTS (
    SELECT 1 FROM renditions k
    WHERE k.media_id = renditions.media_id AND k.preset_id = renditions.preset_id AND k.params = renditions.params
      AND k.id < renditions.id
);
CREATE UNIQUE INDEX ux_renditions_media_preset_params ON renditions (media_id, preset_id, params);
