-- Horário dos posts e relatório de padrões (importação unificada, `post_times`). Por lote/grupo: `post_times` = 1
-- quando o pedido marcou "Puxar horário dos posts"; `patterns_only` = 1 no modo "só padrões" (`media: "none"`): a
-- listagem roda e grava os horários, mas nenhum job é criado e nada é baixado. `post_times` guarda UM registro por
-- publicação vista na listagem do perfil (nível de post, antes do filtro de tipo de mídia; carrossel conta uma vez):
-- `taken_at` em UTC (ISO 8601 com `Z`; NULL se o Instagram não trouxe a data), `kind` ('photo', 'video', 'reel',
-- 'carousel' ou NULL) e `pinned` (1/0; NULL = não se sabe). UNIQUE (batch_id, code): página refeita na retomada não
-- duplica. ESTRITAMENTE aditiva: colunas com padrão 0 (lotes/grupos antigos = sem horários) e uma tabela nova.
-- Reversão: down/0026_post_times.down.sql (`revert_migration`; nunca roda no startup).
ALTER TABLE batches ADD COLUMN post_times INTEGER NOT NULL DEFAULT 0 CHECK (post_times IN (0, 1));
ALTER TABLE batches ADD COLUMN patterns_only INTEGER NOT NULL DEFAULT 0 CHECK (patterns_only IN (0, 1));
ALTER TABLE import_groups ADD COLUMN post_times INTEGER NOT NULL DEFAULT 0 CHECK (post_times IN (0, 1));
ALTER TABLE import_groups ADD COLUMN patterns_only INTEGER NOT NULL DEFAULT 0 CHECK (patterns_only IN (0, 1));
CREATE TABLE post_times (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    username TEXT NOT NULL,
    code TEXT NOT NULL,
    taken_at TEXT,
    kind TEXT CHECK (kind IS NULL OR kind IN ('photo', 'video', 'reel', 'carousel')),
    pinned INTEGER CHECK (pinned IS NULL OR pinned IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, code)
);
