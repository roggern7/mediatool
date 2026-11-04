-- Análise de Perfis: Reels da aba Reels em cada perfil consultado, para contas que publicam só lá não parecerem vazias
-- (a contagem de Posts da meta tag só conta a grade). `reels_count` = Reels na 1a página lida (até 12); `reels_more` =
-- 1 se a aba tem mais (has_next_page), 0 se não; `latest_reel_at` = ISO UTC do Reel mais recente da página, ESTIMADO
-- pelo id da publicação. ESTRITAMENTE aditiva: três colunas anuláveis, itens antigos ficam NULL (não verificado).
-- Reversão: down/0028_profile_reels.down.sql (`revert_migration`; nunca roda no startup).
ALTER TABLE profile_analysis_items ADD COLUMN reels_count INTEGER;
ALTER TABLE profile_analysis_items ADD COLUMN reels_more INTEGER;
ALTER TABLE profile_analysis_items ADD COLUMN latest_reel_at TEXT;
