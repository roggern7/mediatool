-- Filtro opcional de visualizações mínimas na importação por perfil (docs/BATCH_ROBUSTNESS.md, "Filtro de
-- visualizações mínimas"). ESTRITAMENTE aditiva: só colunas anuláveis; nenhuma linha existente é alterada ou apagada
-- (grupos e lotes antigos, e os novos criados sem o filtro, ficam com NULL em todas).
-- `min_views`: mínimo pedido (>= 1); NULL = filtro desligado.
-- `listing_views`: JSON {"approved": n, "below": n, "no_count": n} somado por página na transação do checkpoint dela
-- (só totais; "analisados" é a soma dos três e não é guardado).
-- `listing_views_partial`: o mesmo formato para a página ainda SEM checkpoint até o último item gravado dela (estado,
-- não soma: espelho de `listing_dropped_partial`; só entra no total se a listagem terminar nessa página).
ALTER TABLE import_groups ADD COLUMN min_views INTEGER CHECK (min_views IS NULL OR min_views >= 1);
ALTER TABLE batches ADD COLUMN min_views INTEGER CHECK (min_views IS NULL OR min_views >= 1);
ALTER TABLE batches ADD COLUMN listing_views TEXT CHECK (listing_views IS NULL OR json_valid(listing_views));
ALTER TABLE batches ADD COLUMN listing_views_partial TEXT
    CHECK (listing_views_partial IS NULL OR json_valid(listing_views_partial));
