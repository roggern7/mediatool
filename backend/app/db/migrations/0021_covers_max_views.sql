-- Importação unificada (docs/CONTRACTS.md, seção 8.6; docs/BATCH_ROBUSTNESS.md, "Capas" e "Filtro de
-- visualizações"). ADITIVA: só colunas novas com valor padrão (linhas existentes ficam com o valor de antes) e a troca
-- do índice único de origem da mídia por um que inclui o papel. Nenhuma linha existente é alterada ou apagada.
-- `media.role`: 'original' (a mídia da publicação, como sempre) ou 'cover' (a capa de um vídeo/Reel, baixada sozinha,
-- sem o vídeo). A capa aponta para o vídeo pela MESMA chave de origem (source_platform, source_post_id,
-- source_media_index): vale nos dois sentidos, qualquer que seja a ordem dos downloads, e sobrevive a apagar um deles.
ALTER TABLE media ADD COLUMN role TEXT NOT NULL DEFAULT 'original' CHECK (role IN ('original', 'cover'));
-- Dedup por origem com o papel: a capa nunca colide com o vídeo nem com a foto do feed (sha256 continua único).
DROP INDEX ux_media_source;
CREATE UNIQUE INDEX ux_media_source
    ON media (source_platform, source_post_id, source_media_index, role)
    WHERE source_post_id IS NOT NULL;
-- Filtro "Capas" da Biblioteca (só as capas entram no índice parcial).
CREATE INDEX ix_media_cover ON media (id) WHERE role = 'cover';
-- Papel do que o job baixa ('cover' = só a capa do vídeo, como imagem) e o pedido do lote/grupo (Somente capas).
ALTER TABLE jobs ADD COLUMN media_role TEXT NOT NULL DEFAULT 'original' CHECK (media_role IN ('original', 'cover'));
ALTER TABLE batches ADD COLUMN media_role TEXT NOT NULL DEFAULT 'original'
    CHECK (media_role IN ('original', 'cover'));
ALTER TABLE import_groups ADD COLUMN media_role TEXT NOT NULL DEFAULT 'original'
    CHECK (media_role IN ('original', 'cover'));
-- Máximo de visualizações (junto do mínimo da 0017); NULL = sem máximo. A contagem "acima do máximo" vai na chave
-- `above` dos JSON `listing_views`/`listing_views_partial` (só em lotes com máximo).
ALTER TABLE import_groups ADD COLUMN max_views INTEGER CHECK (max_views IS NULL OR max_views >= 1);
ALTER TABLE batches ADD COLUMN max_views INTEGER CHECK (max_views IS NULL OR max_views >= 1);
-- Contador final congelado de mídias já existentes (jobs duplicados), como os `final_*` da 0005/0006. NULL em lotes
-- encerrados antes desta versão (apresentado como 0).
ALTER TABLE batches ADD COLUMN final_duplicates INTEGER;
