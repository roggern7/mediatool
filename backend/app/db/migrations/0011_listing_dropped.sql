-- Descartes da listagem visíveis no lote (docs/BATCH_ROBUSTNESS.md, "Descartes da listagem"): contadores
-- persistentes do que o provider descartou ao listar. `listing_dropped_unexpected` é o total de mídias/posts que não
-- puderam ser adicionados (consulta barata); `listing_dropped_reasons` é um JSON compacto
-- {"unexpected": {motivo: n}, "expected": {...}, "degraded": {...}} (só nomes fixos de motivo e contagens);
-- `listing_dropped_partial` guarda {motivo: n} da página ainda SEM checkpoint até o último item gravado dela (é
-- estado, não soma: a página refeita o sobrescreve e o checkpoint dela o apaga; só entra no total se a listagem
-- terminar nessa página). Só colunas novas; lotes existentes ficam com 0/NULL: nenhum dado é reescrito.
ALTER TABLE batches ADD COLUMN listing_dropped_unexpected INTEGER NOT NULL DEFAULT 0;
ALTER TABLE batches ADD COLUMN listing_dropped_reasons TEXT;
ALTER TABLE batches ADD COLUMN listing_dropped_partial TEXT;
