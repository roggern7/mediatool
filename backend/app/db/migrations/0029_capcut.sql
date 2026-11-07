-- Legendas CapCut (docs/CONTRACTS.md, seção 8.9): fila persistente de vídeos legendados no CapCut, UM job por vez (o
-- CapCut é exclusivo). `capcut_jobs.status`: 'pending' (na fila; `next_attempt_at` = prazo da próxima tentativa),
-- 'running', 'waiting' (CapCut ocupado; `wait_reason`, sem gastar tentativa), 'completed', 'failed' ou 'cancelled'.
-- `source_path`: cópia do `original_path` da mídia (relativo ao storage) na criação; `output_path`: arquivo final
-- (absoluto). `options`: instantâneo JSON das configurações usadas + `style_id` + estilo serializado (mudanças
-- posteriores nas configurações não alteram jobs já criados). `transcript`: JSON das palavras, reaproveitado nas
-- retentativas. `origin`: 'manual' (painel), 'auto' (após o download) ou 'api'.
-- `capcut_styles`: modelos de legenda copiados de um rascunho do CapCut; o embutido "Padrão (capcut-agent-skill)" não
-- fica aqui (id 0/NULL na API). ESTRITAMENTE aditiva: duas tabelas novas; nenhuma linha ou tabela existente muda.
-- Reversão: down/0029_capcut.down.sql (`revert_migration`; nunca roda no startup).
CREATE TABLE capcut_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
    source_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'waiting', 'completed', 'failed', 'cancelled')),
    stage TEXT,
    stage_progress REAL,
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    detail TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_attempt_at TEXT,
    wait_reason TEXT,
    error_code TEXT,
    error_message TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    options TEXT NOT NULL DEFAULT '{}',
    transcript TEXT,
    chunks INTEGER,
    draft_name TEXT,
    output_path TEXT,
    output_probe TEXT,
    warnings TEXT,
    origin TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'auto', 'api')),
    batch_id INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);
-- Fila: o executor pega o mais antigo com prazo vencido.
CREATE INDEX ix_capcut_jobs_queue ON capcut_jobs (status, next_attempt_at, id);
CREATE INDEX ix_capcut_jobs_media ON capcut_jobs (media_id);
CREATE TABLE capcut_styles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    source_draft TEXT,
    style TEXT NOT NULL,
    summary TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
