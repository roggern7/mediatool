-- Importação por link na maior qualidade da página pública (docs/CONTRACTS.md, seção 8): fluxos separados de vídeo
-- e áudio (DASH) juntados sem recodificar. ESTRITAMENTE aditiva: duas colunas anuláveis; nenhuma linha existente é
-- alterada ou apagada (mídias e jobs antigos ficam com NULL nas duas).
-- `job_remote_refs.dash_streams`: JSON com as URLs assinadas dos dois fluxos e os tamanhos/dimensões esperados. Tão
-- sensível quanto `media_url`: nunca sai pela API nem por log e é apagado nos mesmos pontos (job que falha ou é
-- cancelado: NULL; job concluído e lote concluído/cancelado: a linha inteira some).
-- `media.video_codec`: codec de vídeo do original (ex.: "vp9", "h264") quando conhecido.
ALTER TABLE job_remote_refs ADD COLUMN dash_streams TEXT CHECK (dash_streams IS NULL OR json_valid(dash_streams));
ALTER TABLE media ADD COLUMN video_codec TEXT;
