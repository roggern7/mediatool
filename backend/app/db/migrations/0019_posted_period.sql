-- Filtro de período de postagem na importação por perfil (docs/BATCH_ROBUSTNESS.md, "Filtros da importação por
-- perfil"). ESTRITAMENTE aditiva: só colunas anuláveis; nenhuma linha existente é alterada ou apagada (grupos e lotes
-- antigos, e os novos criados sem o filtro, ficam com NULL em todas).
-- `posted_within_days`: "últimos N dias" pedido (1 a 3650); NULL = qualquer data.
-- `posted_after`: corte calculado UMA vez na criação (agora - N dias, UTC, "AAAA-MM-DDTHH:MM:SSZ"); retomada e
-- reinício usam este valor, nunca recalculam. Publicação com `taken_at` anterior ao corte não é baixada.
-- `listing_period_end`: 1 quando a listagem terminou por ter chegado às publicações mais antigas que o corte (o
-- `listing_end` continua 'profile_end', cuja CHECK da 0012 não muda); NULL nos demais casos.
-- A contagem "fora do período" vai na chave `out_of_period` dos JSON `listing_views`/`listing_views_partial` (0017).
ALTER TABLE import_groups ADD COLUMN posted_within_days INTEGER
    CHECK (posted_within_days IS NULL OR (posted_within_days >= 1 AND posted_within_days <= 3650));
ALTER TABLE import_groups ADD COLUMN posted_after TEXT;
ALTER TABLE batches ADD COLUMN posted_within_days INTEGER
    CHECK (posted_within_days IS NULL OR (posted_within_days >= 1 AND posted_within_days <= 3650));
ALTER TABLE batches ADD COLUMN posted_after TEXT;
ALTER TABLE batches ADD COLUMN listing_period_end INTEGER
    CHECK (listing_period_end IS NULL OR listing_period_end IN (0, 1));
