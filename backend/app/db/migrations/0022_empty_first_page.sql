-- Primeira página vazia na importação por perfil (docs/BATCH_ROBUSTNESS.md, "Primeira página vazia"). ESTRITAMENTE
-- aditiva: só uma coluna anulável; nenhuma linha existente é alterada ou apagada.
-- `listing_empty_first_page`: 1 quando a listagem terminou porque a 1a página do perfil veio com 0 publicações e sem
-- próxima página (perfil sem posts, privado ou limite temporário do Instagram). O `listing_end` continua
-- 'profile_end' (a CHECK da 0012 não muda); a API apresenta `listing_end = "empty_first_page"`. NULL nos demais casos.
ALTER TABLE batches ADD COLUMN listing_empty_first_page INTEGER
    CHECK (listing_empty_first_page IS NULL OR listing_empty_first_page IN (0, 1));
