-- Item já em download por outro lote aberto (seção 9): o job do 2o lote fica `pending` aguardando o job primário
-- (não é reivindicado nem conta no backpressure enquanto o primário está pending/downloading). Concluído o primário,
-- liga à mídia pela origem; primário falho/cancelado: baixa sozinho. Sem FK: a retenção pode apagar o primário.
ALTER TABLE jobs ADD COLUMN primary_job_id INTEGER;
