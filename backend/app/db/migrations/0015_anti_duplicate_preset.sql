-- Preset embutido "Anti-duplicidade" passa a usar os ajustes opcionais (docs/CONTRACTS.md, seção 9): zoom central de
-- 3%, cor (brilho 0.01, contraste 1.02, saturação 1.03) e corte final de 0,3 s. Até aqui ele era idêntico ao "Padrão".
-- Só a linha embutida e só se os params ainda forem os 6 originais do seed (comparados por valor, porque o texto pode
-- estar no formato do seed ou no de `canonical_json`): preset editado pelo usuário, presets personalizados e o
-- "Padrão" não são tocados. O novo valor é gravado no formato de `canonical_json` (chaves ordenadas, sem espaços;
-- ajustes neutros, como tom e granulação, ficam ausentes).
-- Renditions, jobs e lotes não mudam: arquivos já processados ficam como estão; novos processamentos com o
-- Anti-duplicidade têm params diferentes e geram outra rendition.
UPDATE presets
SET params = '{"auto_adjust":true,"brightness":0.01,"contrast":1.02,"metadata_cleanup":true,"mirror":true,"reencode":true,"saturation":1.03,"speed":1.05,"trim_end":0.3,"trim_start":0.5,"zoom":3.0}',
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE name = 'Anti-duplicidade' AND is_builtin = 1
  AND (SELECT COUNT(*) FROM json_each(presets.params)) = 6
  AND json_type(params, '$.metadata_cleanup') = 'true'
  AND json_type(params, '$.mirror') = 'true'
  AND json_type(params, '$.auto_adjust') = 'true'
  AND json_type(params, '$.reencode') = 'true'
  AND json_extract(params, '$.trim_start') = 0.5
  AND json_extract(params, '$.speed') = 1.05;
