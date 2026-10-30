-- Extensão como central de operações (docs/CONTRACTS.md, seções 8.6 e 8.7; docs/BATCH_ROBUSTNESS.md, "Tipos de mídia"
-- e "Exportação automática"). ESTRITAMENTE aditiva: só colunas anuláveis novas, índices parciais novos e a troca do
-- índice único dos jobs de ingestão por um que inclui o papel. Nenhuma linha existente é alterada ou apagada.
-- `media_set`: tipo de mídia pedido na importação unificada ('videos', 'photos', 'covers', 'videos_covers', 'all').
-- NULL nos lotes/grupos antigos = comportamento antigo (decidido por `content_type` e `media_role`).
ALTER TABLE batches ADD COLUMN media_set TEXT
    CHECK (media_set IS NULL OR media_set IN ('videos', 'photos', 'covers', 'videos_covers', 'all'));
ALTER TABLE import_groups ADD COLUMN media_set TEXT
    CHECK (media_set IS NULL OR media_set IN ('videos', 'photos', 'covers', 'videos_covers', 'all'));
-- Exportação automática ao concluir: o conteúdo pedido, o estado do disparo ('waiting' = aguardando o fim do lote/grupo;
-- 'created' = a exportação ZIP foi criada, id em `auto_export_id`; 'failed' = a criação falhou, motivo em
-- `auto_export_error`) e a exportação criada. NULL em tudo = sem exportação automática (todos os lotes antigos).
ALTER TABLE batches ADD COLUMN auto_export_content TEXT
    CHECK (auto_export_content IS NULL OR auto_export_content IN ('originals', 'edited', 'both'));
ALTER TABLE batches ADD COLUMN auto_export_state TEXT
    CHECK (auto_export_state IS NULL OR auto_export_state IN ('waiting', 'created', 'failed'));
ALTER TABLE batches ADD COLUMN auto_export_id INTEGER;
ALTER TABLE batches ADD COLUMN auto_export_error TEXT;
ALTER TABLE import_groups ADD COLUMN auto_export_content TEXT
    CHECK (auto_export_content IS NULL OR auto_export_content IN ('originals', 'edited', 'both'));
ALTER TABLE import_groups ADD COLUMN auto_export_state TEXT
    CHECK (auto_export_state IS NULL OR auto_export_state IN ('waiting', 'created', 'failed'));
ALTER TABLE import_groups ADD COLUMN auto_export_id INTEGER;
ALTER TABLE import_groups ADD COLUMN auto_export_error TEXT;
-- Disparos pendentes (só os que esperam o fim entram nos índices parciais).
CREATE INDEX ix_batches_auto_export_waiting ON batches (id) WHERE auto_export_state = 'waiting';
CREATE INDEX ix_groups_auto_export_waiting ON import_groups (id) WHERE auto_export_state = 'waiting';
-- Um item de vídeo pode gerar dois jobs no MESMO lote ("Vídeos e capas", "Todas"): o original e a capa. A chave de
-- dedup dentro do lote passa a incluir o papel (como ux_media_source na 0021); os jobs existentes têm todos chaves
-- distintas pela regra antiga, então continuam distintos pela nova.
DROP INDEX ux_jobs_batch_source;
CREATE UNIQUE INDEX ux_jobs_batch_source
    ON jobs (batch_id, source_post_id, source_media_index, media_role)
    WHERE kind = 'ingest' AND batch_id IS NOT NULL AND source_post_id IS NOT NULL;
