-- Execução robusta (docs/BATCH_ROBUSTNESS.md, seção 9: A1, A4).

-- A1: toda tentativa de renovação de URL por página do lote fica registrada; falha recente faz os irmãos falharem
-- na hora com o mesmo código, sem nova requisição ao Instagram.
CREATE TABLE listing_pages (
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    page_index INTEGER NOT NULL,
    last_refresh_at TEXT,
    last_refresh_ok INTEGER,
    last_refresh_code TEXT,
    refresh_attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (batch_id, page_index)
) WITHOUT ROWID;

-- A4: disjuntor por lote. Pausa com retomada automática (paused_until) e backoff (pause_level); contadores de
-- falhas seguidas do mesmo código (jobs e renovações).
ALTER TABLE batches ADD COLUMN paused_reason TEXT;
ALTER TABLE batches ADD COLUMN paused_until TEXT;
ALTER TABLE batches ADD COLUMN paused_code TEXT;
ALTER TABLE batches ADD COLUMN pause_level INTEGER NOT NULL DEFAULT 0;
ALTER TABLE batches ADD COLUMN fail_streak_code TEXT;
ALTER TABLE batches ADD COLUMN fail_streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE batches ADD COLUMN refresh_fail_code TEXT;
ALTER TABLE batches ADD COLUMN refresh_fail_streak INTEGER NOT NULL DEFAULT 0;
-- Escopo da pausa do disjuntor: 'ingest' (códigos de download/renovação: claims de ingest e listagem) ou 'process'
-- (códigos de processamento: só o FFmpeg do lote).
ALTER TABLE batches ADD COLUMN paused_scope TEXT CHECK (paused_scope IS NULL OR paused_scope IN ('ingest', 'process'));

-- M7: reinício abrupto não consome tentativa, mas um job interrompido 3 vezes seguidas (derruba o processo) falha
-- com interrupted_repeatedly. Zerado sempre que uma execução do job termina normalmente.
ALTER TABLE jobs ADD COLUMN crash_count INTEGER NOT NULL DEFAULT 0;
