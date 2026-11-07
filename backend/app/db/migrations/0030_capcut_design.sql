-- Editor visual das Legendas CapCut (docs/CONTRACTS.md, seção 8.9): `capcut_styles.design` = JSON versionado do
-- editor (fonte, tamanho, cores, contorno, sombra, fundo, posição, destaque, animações) de um modelo criado na tela;
-- NULL nos modelos copiados de um rascunho do CapCut (como todos os existentes). O `style` continua sendo o estilo
-- serializado que os jobs usam. ESTRITAMENTE aditiva: uma coluna anulável; nenhuma linha existente muda.
-- Reversão: down/0030_capcut_design.down.sql (`revert_migration`; nunca roda no startup).
ALTER TABLE capcut_styles ADD COLUMN design TEXT;
