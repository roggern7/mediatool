-- Importação por link (docs/CONTRACTS.md, seção 8.3): o usuário cola links de publicações e o lote baixa todas as
-- mídias de cada uma. O lote é um lote comum de importação (`source = 'instagram_import'`, mesma fila de listagem,
-- mesmos jobs) marcado com `batches.import_mode = 'links'` (NULL = importação por perfil, inclusive lotes antigos).
-- `batch_links` guarda os links do lote na ordem colada (`position`, 1-based) e o resultado de cada um: `pending`
-- (ainda não pedido ao Instagram), `resolved` (jobs criados; `items_found` mídias, `profile` = dono) ou `failed`
-- (erro só deste link, em `error_code`/`error_message`). O mesmo shortcode nunca se repete no lote.
-- `attention_count`: quantas vezes ESTE link deixou o lote em `needs_attention` (na 2a o link vira `failed` e o lote
-- segue). `batches.link_fail_streak`: links seguidos falhos sem nenhum resolvido entre eles (disjuntor de 3; zera
-- quando um link resolve, quando o disjuntor dispara e no "Retomar").
-- A renovação de link de download expirado de uma publicação é registrada em `listing_pages` com
-- `page_index = position` do link (lote por link não tem páginas de listagem).
-- Só colunas e tabela novas: lotes existentes ficam com import_mode NULL e nenhum dado é reescrito.
ALTER TABLE batches ADD COLUMN import_mode TEXT CHECK (import_mode IS NULL OR import_mode = 'links');
ALTER TABLE batches ADD COLUMN link_fail_streak INTEGER NOT NULL DEFAULT 0;
CREATE TABLE batch_links (
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    shortcode TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'resolved', 'failed')),
    error_code TEXT,
    error_message TEXT,
    items_found INTEGER,
    profile TEXT,
    resolved_at TEXT,
    attention_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (batch_id, position),
    UNIQUE (batch_id, shortcode)
);
