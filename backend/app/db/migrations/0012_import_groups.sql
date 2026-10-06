-- Importação de vários perfis de uma vez (docs/CONTRACTS.md, "Grupos de importação"): UMA importação pai
-- (`import_groups`) e um lote filho por perfil (`batches.group_id`, na ordem `group_position`, 1-based). O grupo só
-- guarda o que foi pedido; o estado dele é DERIVADO dos filhos (nenhum status duplicado para manter consistente).
-- `batches.listing_end` diz por que a listagem do lote terminou: 'profile_end' (fim real do perfil,
-- has_next_page=false) ou 'limit' (quantidade pedida atingida); NULL enquanto lista, em erro e em lotes antigos.
-- Só tabela, colunas e índice novos: lotes existentes ficam com group_id/group_position/listing_end NULL e nenhum
-- dado é reescrito.
CREATE TABLE import_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_type TEXT NOT NULL,
    requested_limit INTEGER,
    preset_id INTEGER,
    preset_name TEXT,
    params TEXT,
    process INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
ALTER TABLE batches ADD COLUMN group_id INTEGER REFERENCES import_groups(id);
ALTER TABLE batches ADD COLUMN group_position INTEGER;
ALTER TABLE batches ADD COLUMN listing_end TEXT CHECK (listing_end IS NULL OR listing_end IN ('profile_end', 'limit'));
CREATE INDEX ix_batches_group ON batches(group_id, group_position) WHERE group_id IS NOT NULL;
