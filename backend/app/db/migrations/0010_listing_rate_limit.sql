-- Espera pelo teto de páginas por hora do portão do Instagram (visibilidade): enquanto a listagem do lote espera a
-- janela liberar, `rate_limit_resume_at` (ISO UTC) guarda a retomada prevista e os outros dois as páginas usadas e o
-- teto da janela. `listing_state` continua `running` (a espera acontece dentro do trecho); a API apresenta
-- `waiting_rate_limit`. Só colunas novas, NULL nos lotes existentes: nenhum dado é reescrito.
ALTER TABLE batches ADD COLUMN rate_limit_resume_at TEXT;
ALTER TABLE batches ADD COLUMN rate_limit_pages_used INTEGER;
ALTER TABLE batches ADD COLUMN rate_limit_pages_max INTEGER;
