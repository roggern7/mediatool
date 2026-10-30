-- Pedido de importação com exportação automática (docs/CONTRACTS.md, seção 8.6; docs/BATCH_ROBUSTNESS.md,
-- "Exportação automática"). Um start da importação unificada com perfis E links cria duas partes: o lote avulso
-- (1 perfil) ou o grupo (2+ perfis) e o lote de links. Com `auto_export`, a regra é UM ZIP por pedido: a exportação
-- automática fica no pedido (não nas partes) e dispara uma vez quando TODAS as partes terminarem, com fonte
-- `library` + as mídias das partes. As partes apontam para o pedido por `request_id` (filhos de grupo não: o grupo é a
-- parte). Pedido de uma parte só continua usando as colunas da 0023 no próprio lote/grupo (sem pedido).
-- `auto_export_state`: 'waiting' (aguardando o fim das partes), 'created' (ZIP criado, id em `auto_export_id`) ou
-- 'failed' (motivo em `auto_export_error`). ESTRITAMENTE aditiva: uma tabela nova e colunas anuláveis novas; nenhuma
-- linha existente muda.
CREATE TABLE import_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    auto_export_content TEXT NOT NULL CHECK (auto_export_content IN ('originals', 'edited', 'both')),
    auto_export_state TEXT NOT NULL DEFAULT 'waiting' CHECK (auto_export_state IN ('waiting', 'created', 'failed')),
    auto_export_id INTEGER,
    auto_export_error TEXT,
    created_at TEXT NOT NULL
);
ALTER TABLE batches ADD COLUMN request_id INTEGER REFERENCES import_requests(id);
ALTER TABLE import_groups ADD COLUMN request_id INTEGER REFERENCES import_requests(id);
CREATE INDEX ix_batches_request ON batches (request_id) WHERE request_id IS NOT NULL;
CREATE INDEX ix_groups_request ON import_groups (request_id) WHERE request_id IS NOT NULL;
CREATE INDEX ix_import_requests_waiting ON import_requests (id) WHERE auto_export_state = 'waiting';
