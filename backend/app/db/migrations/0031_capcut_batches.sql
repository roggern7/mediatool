-- Lotes das Legendas CapCut (docs/CONTRACTS.md, seção 8.9; docs/CAPCUT.md, "Lotes"): `capcut_batches` agrupa os jobs
-- de um pedido da página (um `capcut_jobs` por vídeo). `mode`: 'caption' (só legendas, fonte = original), 'edit' (só
-- edição: a rendition editada é copiada para a pasta do lote, sem abrir o CapCut) ou 'edit_caption' (edição e depois
-- legendas sobre a rendition). `options`: instantâneo JSON do pedido (legenda + edição + exportação + resumo legível).
-- `output_dir`: pasta absoluta do lote (`<pasta de saída>/Lote <id>`, criada no primeiro uso). `edit_batch_id`: lote
-- `batches` dos jobs `process` da edição (NULL em 'caption'). `paused`: o executor não pega jobs do lote. `hidden`:
-- removido do histórico (os arquivos ficam). Em `capcut_jobs`: `capcut_batch_id` (o lote; `batch_id` continua sendo o
-- lote de download do modo automático), `mode`, `edit_job_id` (jobs.id da edição; o executor espera ele terminar) e
-- `held` (pausa individual de job na fila). ESTRITAMENTE aditiva: uma tabela nova e colunas com padrão; os jobs
-- existentes ficam 'caption', sem lote e sem pausa (comportamento idêntico ao de antes).
-- Reversão: down/0031_capcut_batches.down.sql (`revert_migration`; nunca roda no startup).
CREATE TABLE capcut_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('caption', 'edit', 'edit_caption')),
    options TEXT NOT NULL DEFAULT '{}',
    output_dir TEXT NOT NULL,
    edit_batch_id INTEGER,
    paused INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);
ALTER TABLE capcut_jobs ADD COLUMN capcut_batch_id INTEGER REFERENCES capcut_batches(id) ON DELETE SET NULL;
ALTER TABLE capcut_jobs ADD COLUMN mode TEXT NOT NULL DEFAULT 'caption';
ALTER TABLE capcut_jobs ADD COLUMN edit_job_id INTEGER;
ALTER TABLE capcut_jobs ADD COLUMN held INTEGER NOT NULL DEFAULT 0;
CREATE INDEX ix_capcut_jobs_batch ON capcut_jobs (capcut_batch_id, status);
