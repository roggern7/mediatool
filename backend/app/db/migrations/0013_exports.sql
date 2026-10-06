-- Exportação para pasta (docs/CONTRACTS.md, seção 8.2): cópia plana das mídias de uma importação (lote avulso ou
-- grupo) ou de uma seleção da Biblioteca para uma pasta escolhida pelo usuário. A Biblioteca continua sendo a fonte
-- da verdade: estas tabelas só registram o pedido e o progresso da cópia.
-- `selection` (só em kind = 'library'): JSON com o filtro congelado ou a marcação de ids. `export_items` guarda, de
-- forma compacta, os ids escolhidos (seleção por ids) ou os ids EXCLUÍDOS (seleção por filtro). Para lote e grupo a
-- lista de mídias é derivada por consulta (nada é materializado).
-- `batch_media`: relação DURÁVEL lote -> mídia. A retenção apaga jobs encerrados antigos; antes de apagar, o purge
-- copia para cá o par (lote, mídia) dos ingests concluídos, e a exportação de lote/grupo lê a UNIÃO (jobs concluídos
-- do lote + batch_media). Mídia ou lote apagados somem da relação (ON DELETE CASCADE). O backfill abaixo cobre os
-- jobs que existem hoje.
-- Só tabelas e índices novos (mais o backfill de batch_media): nenhum dado existente é reescrito.
CREATE TABLE exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('batch', 'group', 'library')),
    batch_id INTEGER REFERENCES batches(id) ON DELETE SET NULL,
    group_id INTEGER REFERENCES import_groups(id) ON DELETE SET NULL,
    selection TEXT,
    dest_path TEXT NOT NULL,
    -- 1 = pasta padrão criada pelo servidor para ESTA exportação (removida, se vazia, em cancelamento/falha).
    dest_created INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'cancelled', 'failed', 'expired')),
    -- 'folder' = cópia para uma pasta do usuário; 'zip' = um arquivo .zip plano preparado pelo servidor em
    -- <data>\exports-tmp (zip_path = nome interno aleatório; zip_name = nome do download; zip_token = token do
    -- download; zip_expires_at = até quando o ZIP pronto fica disponível; depois o registro vira 'expired').
    mode TEXT NOT NULL DEFAULT 'folder' CHECK (mode IN ('folder', 'zip')),
    zip_path TEXT,
    zip_name TEXT,
    zip_token TEXT,
    zip_bytes INTEGER,
    zip_expires_at TEXT,
    zip_downloaded_at TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    copied INTEGER NOT NULL DEFAULT 0,
    skipped_existing INTEGER NOT NULL DEFAULT 0,
    renamed INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX ix_exports_status ON exports(status, id);
CREATE INDEX ix_exports_batch ON exports(batch_id, id) WHERE batch_id IS NOT NULL;
CREATE INDEX ix_exports_group ON exports(group_id, id) WHERE group_id IS NOT NULL;
CREATE TABLE export_items (
    export_id INTEGER NOT NULL REFERENCES exports(id) ON DELETE CASCADE,
    media_id INTEGER NOT NULL,
    PRIMARY KEY (export_id, media_id)
) WITHOUT ROWID;
CREATE TABLE batch_media (
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    PRIMARY KEY (batch_id, media_id)
) WITHOUT ROWID;
INSERT OR IGNORE INTO batch_media (batch_id, media_id)
    SELECT batch_id, media_id FROM jobs
    WHERE kind = 'ingest' AND status = 'completed' AND media_id IS NOT NULL AND batch_id IS NOT NULL;
