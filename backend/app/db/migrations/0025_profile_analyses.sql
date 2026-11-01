-- Análise de Perfis (docs/CONTRACTS.md, seção 8.8): lista de @/links consultada UM perfil por vez, sem sessão, a partir
-- da aba Reels pública. `profile_analyses` é a fila persistente (uma análise por vez, FIFO) e o histórico;
-- `profile_analysis_items` guarda um resultado por perfil (`checked_at` = instante em que AQUELE perfil respondeu).
-- `status`: 'pending' (na fila), 'running', 'waiting' (espera local ou pausa global; motivo em `wait_reason`, prazo em
-- `waiting_until`, degrau do backoff em `wait_level`), 'completed', 'cancelled' ou 'interrupted' (backoff esgotado).
-- `invalid_lines`: JSON com no máximo 50 linhas ignoradas (`line` truncada a 120 caracteres, `reason`).
-- `resume_after`: posição até onde o canário não olha (última `done` na retomada, ou sequência que ele confirmou): um
-- resultado anterior a ela nunca volta para a fila.
-- `cautious_pace`: 1 depois do primeiro sinal de limite; a análise segue no ritmo cauteloso até o fim.
-- `final_stats`: JSON com os agregados dos itens, gravado ao terminar (nulo enquanto aberta).
-- Ausência de dado é `unverified`, nunca negativa. ESTRITAMENTE aditiva: duas tabelas novas; nenhuma linha ou tabela
-- existente muda. Reversão: down/0025_profile_analyses.down.sql (`revert_migration`; nunca roda no startup).
CREATE TABLE profile_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'waiting', 'completed', 'cancelled', 'interrupted')),
    total INTEGER NOT NULL DEFAULT 0,
    invalid_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    invalid_lines TEXT NOT NULL DEFAULT '[]',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    waiting_until TEXT,
    wait_reason TEXT,
    wait_level INTEGER NOT NULL DEFAULT 0,
    resume_after INTEGER NOT NULL DEFAULT 0,
    cautious_pace INTEGER NOT NULL DEFAULT 0,
    final_stats TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE profile_analysis_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES profile_analyses(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    username TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'done', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    account_status TEXT CHECK (account_status IS NULL OR account_status IN ('active', 'private', 'restricted', 'unavailable',
                                                                            'unverified')),
    display_name TEXT,
    posts_state TEXT,
    posts_count INTEGER,
    posts_exact INTEGER,
    posts_text TEXT,
    views_state TEXT,
    views_total INTEGER,
    views_reels INTEGER,
    stories TEXT CHECK (stories IS NULL OR stories IN ('yes', 'no', 'unverified')),
    highlights TEXT CHECK (highlights IS NULL OR highlights IN ('yes', 'no', 'unverified')),
    error_code TEXT,
    error_message TEXT,
    checked_at TEXT,
    UNIQUE (analysis_id, username)
);
CREATE INDEX ix_profile_items_state ON profile_analysis_items (analysis_id, state, position);
-- Listagem paginada e exportação sem ordenar em memória.
CREATE INDEX ix_profile_items_position ON profile_analysis_items (analysis_id, position);
-- `next_item`: com `IN` + `ORDER BY`, o índice de estado leria todos os pendentes a cada passo.
CREATE INDEX ix_profile_items_open ON profile_analysis_items (analysis_id, position)
    WHERE state IN ('pending', 'running');
