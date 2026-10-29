-- Recorte manual da Edição (docs/CONTRACTS.md, seção 8.4): o job `process` de um vídeo recortado tem params próprios
-- (diferentes dos params do lote), e a exportação de editados do lote acha a versão pelos params do job. Para isso
-- continuar valendo depois de a retenção apagar os jobs antigos, o purge também guarda aqui os params do job
-- `process` concluído. ESTRITAMENTE aditiva: só uma coluna anulável; linhas existentes ficam com NULL (= params do
-- lote, como antes).
ALTER TABLE batch_media ADD COLUMN params TEXT CHECK (params IS NULL OR json_valid(params));
