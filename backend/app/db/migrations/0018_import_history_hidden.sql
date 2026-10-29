-- Histórico de "Importações recentes" (docs/CONTRACTS.md, seção 8.1): remover um item ou limpar o histórico só OCULTA
-- o lote avulso/grupo da lista (os lotes continuam existindo: pastas da página Editar, exportações, Fila e Biblioteca
-- seguem iguais). ESTRITAMENTE aditiva: duas colunas anuláveis; nenhuma linha existente é alterada ou apagada (NULL =
-- visível no histórico). Pula a 0017 de propósito (número usado por outro trabalho).
ALTER TABLE batches ADD COLUMN history_hidden_at TEXT;
ALTER TABLE import_groups ADD COLUMN history_hidden_at TEXT;
