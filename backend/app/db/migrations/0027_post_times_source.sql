-- Origem do horário de cada publicação em `post_times` (relatório de padrões). NULL = horário exato vindo da grade do
-- perfil (PolarisProfilePostsQuery, como na 0026); 'reels_tab' = Reel que só apareceu na aba Reels, com `taken_at`
-- ESTIMADO pelo id da publicação (precisão de ~±30 s). ESTRITAMENTE aditiva: uma coluna anulável, registros antigos
-- ficam NULL (grade). Reversão: down/0027_post_times_source.down.sql (`revert_migration`; nunca roda no startup).
ALTER TABLE post_times ADD COLUMN source TEXT;
