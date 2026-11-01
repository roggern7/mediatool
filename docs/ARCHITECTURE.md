# Media Tool: arquitetura técnica (V1)

Complementa o [README](../README.md). A fonte da verdade das interfaces é [CONTRACTS.md](CONTRACTS.md); este
documento explica como as peças se encaixam.

## 1. Visão geral

```
                         +-------------------- processo do backend (uvicorn, 127.0.0.1:8765) --------------------+
 navegador               |                                                                                        |
 (painel React) --HTTP-->|  app.api (routers /api)  --->  services / import_service  --->  app.jobs (fila, worker)|
                         |        |                               |                                 |              |
                         |        v                               v                                 v              |
                         |   app.core.security          app.db (SQLite WAL)            media_processor (FFmpeg)    |
                         |   (host, header, CORS)       migrations, repositórios       subprocess, shell=False     |
                         |                                        ^                                 |              |
                         |   LocalInstagramSessionProvider        |                                 v              |
                         |   (DPAPI, data\secrets)                |                     storage\processed, thumbs  |
                         |        | InstagramSession              |                                                |
                         |        v                               |                                                |
                         |   CompositeInstagramDownloader --------+--> cdn_downloader --> storage\temp -> originals|
                         |        | web, gallery_dl ou auto               (sem cookies, HTTPS, hosts permitidos)   |
                         +--------|-------------------------------------------------------------------------------+
                                  v
                    web (padrão): InstagramWebDownloader, no próprio processo: HTTPS só para
                    www.instagram.com (página inicial + POST /graphql/query com doc_id); cookies e tokens
                    só em memória
                    gallery_dl (ou fallback de auto): processo filho
                    python -m app.integrations.instagram.gallerydl_runner
                    (gallery-dl in-process; pedido e cookies pelo stdin; stdout = JSON lines; stderr redigido)
```

## 2. Módulos

| Módulo | Responsabilidade |
|---|---|
| `app.main` | `create_app(settings=None)`; monta middlewares, routers, arquivos estáticos de `frontend\dist` com fallback SPA, startup (migrations, recovery, worker). |
| `app.core.config` | `Settings` a partir de ambiente > `.env` > padrões; o `.env` não é copiado para `os.environ`. |
| `app.core.logging` | Logging com filtro global que redige segredos conhecidos e padrões de `sessionid`/cookies. |
| `app.core.security` | Headers de segurança e CSP, validação de Host, guarda de mutações (Origin + `X-Media-Tool-Client`), CORS (só em modo dev), `safe_join`/`ensure_within`. |
| `app.api.limits` | Limite de 1 MB no corpo das requisições (exceto o upload de vídeo local, com teto próprio). |
| `app.services.settings_service` | Settings editáveis pelo painel, incluindo a validação dos caminhos do FFmpeg/ffprobe, a estratégia `instagram_provider` (setting > env `MEDIA_TOOL_INSTAGRAM_PROVIDER` > `web`), os overrides `instagram_web_doc_ids` e o registro do último teste de sessão. |
| `app.db` | Conexão SQLite (WAL, `foreign_keys=ON`), migrations numeradas em `app\db\migrations`, repositórios. |
| `app.schemas` | Modelos Pydantic de entrada e saída da API. |
| `app.services.storage` | Caminhos de storage, promoção atômica temp -> originals, nomes internos. |
| `app.services.cdn_downloader` | Download seguro das URLs de CDN (validação de URL, DNS, redirects, tamanho, magic bytes). |
| `app.services.import_service` | Cria lotes de importação e de processamento. |
| `app.services.diagnostics` | Monta o relatório de `/api/diagnostics`. |
| `app.services.system_open` | Abre arquivo ou pasta no Explorer (só caminhos dentro das raízes permitidas; `open_folder` abre o conteúdo de uma pasta). |
| `app.services.export_service` | Exportar para pasta: escolha do destino (diálogo nativo + token), criação, cancelamento e "Abrir pasta". Nunca recebe caminho do cliente. |
| `app.services.export_destination` | Validação do destino, pasta padrão em Documentos, registro de tokens de destino (`DestinationTokens`) e o seletor nativo (`pick_folder_native` + processo filho `folder_picker_child`). |
| `app.services.export_names` | Nomes de arquivo da exportação (amigáveis, estáveis, válidos no Windows); funções puras. |
| `app.services.profile_analysis_service` | Análise de Perfis: leitura da lista colada, criação, cancelamento e apresentação. |
| `app.services.profile_report` / `xlsx_writer` | Relatório da análise (ZIP com XLSX + CSV + HTML), sob demanda a partir do banco; XLSX em stdlib. |
| `app.jobs` | `worker` (claim com lease, heartbeat, pausa de downloads, manutenção), `pipeline` (etapas de ingest e process), `import_runner` (listagem), `recovery` (startup), `errors` (classificação retryable), `export_runner` (thread própria das exportações para pasta, uma por vez), `profile_analysis_runner` (thread própria da Análise de Perfis, uma por vez, FIFO). |
| `app.integrations.instagram.session` | `InstagramSession` (opaca), `LocalInstagramSessionProvider`, DPAPI via ctypes. |
| `app.integrations.instagram.validation` | `parse_profile_input`, validação de shortcode e URL. |
| `app.integrations.instagram.downloader` | Interface `InstagramDownloader`, `RemoteMedia`, `ProfileInfo`, `InstagramError`. |
| `app.integrations.instagram.web` | Provider moderno (padrão), no próprio processo, em camadas: `http` (cliente httpx com host fixo, sem redirect automático, cookies só em memória) -> `errors` (classificação de respostas, mensagens sem jargão, `detail`) -> `bootstrap` (1 GET da página inicial; extrai `fb_dtsg`/`lsd`/viewer e calcula `jazoest`) -> `queries` (`doc_id` centralizado, `KNOWN_QUERIES`, overrides) -> `graphql` (`POST /graphql/query`, paginação com ritmo de 2 a 4 s, 1 re-bootstrap por operação) -> `normalize` (nó -> `RemoteMedia`) -> `provider` (`InstagramWebDownloader`). Contrato: [INSTAGRAM_WEB_CONTRACT.md](INSTAGRAM_WEB_CONTRACT.md). |
| `app.integrations.instagram.composite` | `CompositeInstagramDownloader`: escolhe o provider pela estratégia (`web` padrão, `gallery_dl`, `auto` com fallback ao gallery-dl só em `contract_changed`/`unsupported_media`). |
| `app.integrations.instagram.gallerydl` / `gallerydl_runner` | Implementação legada com gallery-dl em processo filho (estratégia `gallery_dl` ou fallback de `auto`). |
| `media_processor` | `MediaProcessor` (probe, build_command, process, make_thumbnail), `ProcessingOptions`, `sha256_file`. |
| `frontend` | Painel: Importar, Biblioteca, Fila, Presets, Configurações. |

## 3. Fluxo de importação

1. `POST /api/imports` valida o perfil (`parse_profile_input`) e cria um batch `listing`.
2. O runner de importação (uma listagem por vez) chama `iter_profile_media(username, content, limit)` do adapter,
   criado por operação (`downloader_factory` em `app.main`) como `CompositeInstagramDownloader`.
   - Estratégia `web` (padrão): o `InstagramWebDownloader` abre a página inicial (1 GET), consulta as publicações por
     `POST /graphql/query` com `doc_id`, página a página (2 a 4 s entre páginas),
     normaliza cada nó em `RemoteMedia` e para ao atingir o limite. Tokens e cookies vivem só nos objetos da operação.
     Paginação comprovada no real em 2026-10-03 (1 GET + 2 POST; variáveis `after`/`before`/`first`/`last`
     aceitas). A listagem só termina por quantidade atingida (do tipo pedido), cancelamento,
     erro ou fim real do perfil (`has_next_page=false`); não há teto de páginas nem parada por improdutividade, e
     fim anômalo da paginação deixa o lote em `needs_attention`. Regras, checkpoint de retomada e backpressure:
     [BATCH_ROBUSTNESS.md](BATCH_ROBUSTNESS.md). "Todos" (`limit` nulo) lista até o fim real do perfil. Vários
     perfis: grupo de importação (pai + um lote filho por perfil), CONTRACTS.md seção 8.1.
   - Estratégia `gallery_dl` (ou fallback de `auto` em `contract_changed`/`unsupported_media`): o adapter inicia o
     runner do gallery-dl como processo filho, envia o pedido JSON (com os cookies) pelo stdin e lê
     `{"type": "item" | "profile" | "error" | "done"}` em JSON lines do stdout.
3. Para cada `RemoteMedia` é criado um job `ingest` (`pending`), único por `(batch_id, source_post_id,
   source_media_index)`. Ao terminar a listagem o batch vai para `running` (ou `failed` se falhou sem itens).
   Se a mídia já existe pela origem, o job já nasce marcado como duplicado (`source_id`); se o lote não pede
   processamento, ele é concluído direto com `stage=skipped`.
4. O worker reivindica jobs `pending` com `next_attempt_at` vencido por um claim atômico com lease (`claimed_by`,
   migration `0002`), respeitando a concorrência configurada. Com o Instagram pausado (seção 7), só não são
   reivindicados ingests que ainda precisam baixar; jobs de processamento seguem.
5. Ingest (`app.jobs.pipeline`): checagem por origem de novo -> `downloading` (adapter/cdn_downloader para
   `storage\temp`; vídeo de lote por link com fluxos separados: dois downloads e `muxing`, a junção sem recodificar)
   -> `hashing` (SHA-256) -> deduplicação por hash -> `probing` -> promoção atômica para
   `storage\originals` -> `thumbnail` -> `downloaded`. No download puro (padrão, sem `params`) as etapas `probing`
   e `thumbnail` não chamam ffprobe/FFmpeg: só validação por assinatura, sem miniatura nem metadados; a análise
   fica para o processamento opcional (BATCH_ROBUSTNESS.md seção 10). Exceção: o vídeo juntado de fluxos separados
   usa FFmpeg (junção) e ffprobe (conferência) e fica com metadados; sem as ferramentas, vale o MP4 direto. Se a URL de CDN expirou (403/404/410), a URL é renovada uma
   vez e o download é tentado uma vez de novo. Web: renovação por página (`refresh_page`: bootstrap + 1 POST da
   página de origem do item, guardada no job; a página renovada atualiza todos os jobs pendentes dela, no máximo uma
   vez a cada 10 min); ver [BATCH_ROBUSTNESS.md](BATCH_ROBUSTNESS.md) seção 2. gallery-dl:
   `resolve_post(shortcode)`.
6. Se o batch pede processamento: `processing` com o preset (ver seção 6) -> `completed`. Senão, `completed`.

## 4. Estados

Job (`status`):

```
ingest:   pending -> downloading -> completed                              (download-only, o padrão)
ingest:   pending -> downloading -> downloaded -> processing -> completed  (com process: true)
process:  pending -> processing -> completed
qualquer: -> failed | cancelled
```

`stage` detalha a etapa: `queued`, `waiting_retry`, `downloading`, `muxing` (junção de vídeo e áudio do lote por
link), `hashing`, `probing`, `thumbnail`, `processing`,
`finalizing`, `done`, `skipped`. `progress` vai de 0 a 1 ou é `null` quando não há medida real (nunca é inventado).

Batch (`status`): `listing` -> `running` -> `completed` (todos os jobs terminais, mesmo com falhas parciais),
`failed` (listagem falhou sem itens) ou `cancelled`. Progresso agregado: `{found, downloaded, processed, skipped,
failed, pending, active}`.

## 4.1 Lotes grandes (resumo de docs/BATCH_ROBUSTNESS.md)

Contrato completo em `docs/BATCH_ROBUSTNESS.md`; aqui só o resumo:

- Escala (seção 0): lotes de até 10.000 mídias. Listagem incremental com checkpoint persistido em `batches`
  (`listing_cursor`, contadores, `listing_state` = `pending|running|waiting_backpressure|paused|needs_attention|done`).
  Backpressure: para com `MAX_PENDING_INGEST` (100) por lote ou `MAX_PENDING_INGEST_GLOBAL` (200), volta abaixo de 40.
  Na espera pelo teto de páginas por hora a API apresenta `waiting_rate_limit` (lote ativo, retoma sozinho).
- Paginação (seção 1): termina só na quantidade pedida ou no fim real do perfil; sem teto de páginas e sem parada por
  improdutividade (`filter_exhausted`/`low_yield` removidos); fim anômalo -> `needs_attention`.
- URL expirada (seção 2): renovação única da página de origem por (lote, página), registrada em `listing_pages`;
  falha recente faz os irmãos falharem sem nova requisição. URLs e cursor em `job_remote_refs`, nunca na API ou log.
- Autenticação (seções 3 e 7): `request_auth_failed` pausa 15 min e é recuperável; `session_invalid` só com
  evidência forte. Nada apaga a sessão DPAPI.
- Pausas (seções 4 e 8): controlador único `services/instagram_recovery.py` com backoff por motivo persistido em
  `app_state` e verificação mínima no vencimento; portão global `InstagramRequestGate` (1 requisição em voo,
  2-4 s entre páginas; 6-12 s por 1 h após 429; teto de 600 páginas por hora).
- API e banco (seção 6): `GET /api/batches/{id}` sem jobs por padrão, keyset (`before_id`/`next_before_id`) em jobs e
  mídia, `POST /api/media/process-by-filter`, índices de cobertura, retenção de 30 dias, `journal_size_limit`.
- Revisão de 10.000 (seção 9): erros de rede na listagem pausam o lote com backoff (1m, 5m, 15m, 1h);
  `access_refused`/`contract_changed` -> `needs_attention` e `POST /api/batches/{id}/resume-listing`; 409
  `profile_already_importing`; disjuntor por lote (`batches.paused_reason`/`paused_until`); dois pools
  (`download_concurrency` 1-4, `processing_concurrency` 1-2); pausa local `disk_full` (`services/disk.py`, mínimo
  5 GB + 2 x max_download_bytes x download_concurrency, verificação a cada 1 min, sem consumir tentativa); FFmpeg em
  Job Object; `SetThreadExecutionState` com lote ativo.
- Operação (M6): em segundo plano o log da aplicação vai só para `logs\media-tool.log` (`MEDIA_TOOL_LOG_CONSOLE=0`
  definido pelo `start.ps1`), uvicorn com `--no-access-log`, e `JobDetailFilter` (`app/core/logging.py`) rebaixa os
  detalhes de etapa do logger `media_tool.jobs` (`Job N: ...`, `Job N iniciado`) para DEBUG, deixando uma linha por
  job concluído, cancelado ou falho.
- Interface: faixa global de disco insuficiente (lê `disk` de `GET /api/settings`), espaço livre em Configurações,
  aviso de lote "Precisa de atenção" com **Retomar importação**, lote pausado com horário de retomada, mensagem para
  `profile_already_importing` e os dois campos de concorrência.

## 5. Deduplicação

1. **Por origem, antes de baixar:** se já existe `media` com o mesmo `(source_platform, source_post_id,
   source_media_index)`, o download não acontece; o job é marcado `is_duplicate=1`, `duplicate_reason=source_id`.
2. **Por conteúdo, depois de baixar:** se o SHA-256 já existe (`media.original_sha256` é UNIQUE), o temporário é
   descartado e o job é ligado à mídia existente com `duplicate_reason=sha256`.
3. **Processamento:** se a mídia já tem rendition do mesmo preset com os mesmos params, o job termina `completed`
   com `stage=skipped`; senão o original existente é processado.
4. **Upload de vídeo local** (`POST /api/library/uploads`, `services/upload_service.py`): mesma regra por conteúdo;
   SHA-256 já existente devolve a mídia existente (`duplicate: true`). Vídeo novo vira `media` com
   `source_platform = 'local'` (sem perfil, URL nem post) e original promovido como no download. O corpo é lido em
   streaming para `temp/upload-<uuid>/`, apagado sempre (e na partida, se sobrar de uma queda); ffprobe obrigatório e
   só vídeos MP4/MOV/WebM/MKV (extensão pelo container real; nome com extensão de outra família, ex.: MKV chamado
   `.mp4`, é recusado). A integridade é conferida decodificando do quadro-chave anterior aos últimos 3 s até o fim
   (arquivo truncado ou corrompido nesse trecho é recusado; prazo de 120 s, estourado -> `integrity_timeout`); teto
   `max_download_bytes`; no máximo 2 envios simultâneos.

## 5.1 Exportar para pasta

Camada de saída sobre a Biblioteca; não altera storage, dedupe nem a relação lote -> mídia. Contrato completo: [CONTRACTS.md](CONTRACTS.md), seção 8.2.

```
painel --POST /api/exports/pick-folder--> export_service.pick_folder
            (um diálogo por vez)             |-> processo filho (tkinter): diálogo nativo "Selecionar pasta"
                                             |-> validate_destination(pasta escolhida)
                                             '-> DestinationTokens.issue -> { token, path_display }
painel --POST /api/exports {source, destination: {token} | {default: true}}--> export_service.create_export
                                             |-> token -> pasta guardada no servidor (uso único) | pasta padrão
                                             |-> validate_destination de novo
                                             '-> exports (pending, total contado) -> acorda o ExportRunner
ExportRunner (thread própria, 1 por vez) --> página de 500 mídias por keyset --> copia cada original em blocos
   de 1 MiB para .<nome>.<id>.<aleatorio>.mtpart na pasta --> confere o tamanho --> renomeia sem sobrescrever
```

- **Fonte da verdade intacta:** o original só é aberto para leitura; nada é movido, ligado (hardlink, symlink, junção)
  nem registrado na Biblioteca. `exports`/`export_items` (migration 0013) guardam só o pedido e o progresso.
- **O que entra:** lote = mídias dos jobs de ingest concluídos (inclusive duplicatas `source_id`/`sha256`, que apontam
  para a mídia já existente); grupo = união dos filhos; Biblioteca = ids ou filtro congelado menos exclusões. Cada
  mídia uma vez (a consulta parte de `media`).
- **Escala:** nenhuma lista inteira em memória (páginas por keyset, cópia em streaming) e progresso gravado a cada 200
  arquivos ou 1 s. Seleção por filtro não materializa ids.
- **Colisão:** arquivo idêntico (tamanho + SHA-256) é pulado; arquivo diferente com o mesmo nome leva a mídia para
  ` (2)`, ` (3)`...; reexportar é idempotente.
- **Cancelamento e falhas:** cooperativo entre arquivos e entre blocos; o temporário é removido e o que já foi copiado
  fica. No startup, exportação `running` vira `failed`/`interrupted` e nada é apagado por padrão de nome (a exportação só remove temporários que a própria execução criou; um `.mtpart` pode sobrar). Destino comparado por identidade de arquivo, não só por texto.
- **Retenção x lote inteiro:** a retenção de 30 dias apaga jobs antigos (seção 4.1). Antes de apagar, na mesma transação,
  `purge_finished_before` copia os pares (lote, mídia) dos ingests e process concluídos para `batch_media` (migration
  0013, com backfill). A exportação lê `jobs concluídos ∪ batch_media`, então um lote antigo continua completo.
- **Versões editadas/processadas:** `content` no pedido (`originals` padrão, `edited`, `both`; guardado em
  `exports.selection`, sem migração). O executor percorre itens (original e/ou renditions de cada mídia da página;
  `ExportItem` com caminho e SHA-256 do próprio arquivo) em vez de mídias; lote/grupo levam só as renditions com o
  preset e os params do lote, a Biblioteca todas. Nome com marca da versão (`_editado_15s_1.5x`), pasta/ZIP planos.
- **Baixar ZIP:** mesma exportação com `mode = 'zip'`: o executor grava um `.zip` plano (STORED, ZIP64, entradas em
  blocos de 1 MiB) em `<data>\exports-tmp` (`app.services.export_zip`), depois de conferir o espaço livre; o painel
  baixa por `GET /api/exports/{id}/download?token=`. Pronto vale 24 h (1 h após o primeiro download); vencidos, parciais
  e órfãos são removidos pela varredura do executor e no startup.
- **Seletor:** dois métodos em processo filho sem shell (tkinter; se não inicializar, PowerShell com
  `FolderBrowserDialog` e script fixo); nenhum funcionou -> 501 `picker_unavailable` e o painel oferece a pasta padrão.

## 5.2 Análise de Perfis

Levantamento de uma lista de perfis, sem mídia, sobre as próprias tabelas. Contrato completo: [CONTRACTS.md](CONTRACTS.md),
seção 8.8; fonte de dados: [INSTAGRAM_WEB_CONTRACT.md](INSTAGRAM_WEB_CONTRACT.md), `web/profile_snapshot.py`.

- **Fonte:** um GET ANÔNIMO de `/<usuario>/reels/` por perfil (`ctx.profile_fetcher`, montado em `build_context`). Nenhuma
  sessão é lida. Passa pelo portão global (1 em voo, 2 a 4 s, pausa publicada), mas NÃO pelo teto de páginas por hora
  das importações: não consome a cota delas nem grava `instagram_listing_pages`, e o cancelamento não fica preso em
  `_pages_lock`. O limite vem do ritmo adaptativo do executor: 10.000 perfis, o teto de uma análise, levam cerca de 11 h
  (cerca de 28 h depois de um sinal de limite), e o ritmo não muda com o tamanho da lista. Os cookies anônimos recebidos não entram em
  `register_secret` (`register_cookies=False`): seriam novos a cada consulta e cresceriam sem limite.
- **Execução:** `ProfileAnalysisRunner`, thread própria, fila persistente, uma análise por vez. Intervalo sorteado entre
  perfis: sem espera própria e descanso de 30 a 60 s a cada 100 perfis até o primeiro sinal de limite (429, recusa ou 2
  `unverified` seguidos); depois, até o fim, 3 a 6 s com descanso a cada 30 (`cautious_pace`, gravado no banco); 10 a 20 s
  em ritmo lento global. Espera sempre por `threading.Event`.
- **Isolamento:** um 429 aqui é de requisição anônima e não diz nada sobre a conta. O backoff é LOCAL da análise (15, 30
  e 60 min, em `waiting_until`/`wait_level`): não chama `worker.pause_for`, não toca em `slow_listing` nem em nenhum estado
  global, e o fluxo de download não é afetado. A pausa global, ao contrário, é respeitada sem consumir tentativa.
- **Disjuntor:** 5 perfis `unverified` seguidos entram no mesmo backoff (`too_many_failures`), só se ainda houver perfil
  por consultar. Degraus esgotados: análise `interrupted`; o que faltava fica `cancelled` com o motivo em `error_code`, sem
  `checked_at`. Cancelamento pedido antes disso prevalece.
- **Canário:** `unavailable` não conta como sucesso, porque um IP bloqueado pode receber a mesma rota de erro. A cada 10
  em sequência, UMA consulta de controle a `instagram`, sem criar item. Ativo: a sequência vale. Qualquer outra resposta: a
  sequência volta a `pending`, sem resultado, e a análise entra no backoff local.
- **Falha de banco ou disco:** resultado que o banco recusa vira `unverified` (`internal_error`). Se nada grava, `attempts`
  limita o item (sem GET novo acima de `MAX_ATTEMPTS + 1`) e o executor espera em memória: nunca há laço de consulta.
- **Honestidade:** ausência de campo é `unverified`, nunca negativa; só evidência positiva vira `unavailable`.
- **Reinício:** item `running` volta a `pending` com a tentativa contada (`MAX_ATTEMPTS` vale entre reinícios: um ciclo de
  quedas consulta o mesmo perfil no máximo `MAX_ATTEMPTS + 1` vezes, depois ele fecha `unverified`); análise aberta é
  retomada, respeitando `waiting_until`.
- **Retomada manual:** `POST .../resume` reabre análise `cancelled` ou `interrupted`, numa transação: só itens `cancelled`
  voltam à fila. `resume_after` guarda a última posição `done`: o canário só olha adiante dela, então nenhum resultado de
  antes da retomada volta para a fila.
- **Escala:** índices para listar sem ordenar em memória (`ix_profile_items_position`) e para achar o próximo item sem ler
  a fila (`ix_profile_items_open`, parcial). Os agregados são gravados em `final_stats` ao terminar: o histórico não varre
  itens; só análise aberta faz essa varredura (um índice cobridor quase não ajudou). O passo do executor não guarda nada
  por item em memória.
- **Relatório:** gerado sob demanda a partir do banco, em fluxo: cada arquivo numa passada própria por `iter_items`
  (keyset, 500 por vez), escrito linha a linha na entrada do ZIP. O XLSX interno sai em temporário. O ZIP fica em
  `SpooledTemporaryFile` (1 MiB em memória) e `ReportResponse` o entrega em blocos e fecha em qualquer saída, inclusive na
  desconexão do cliente. Não usa `exports`: não há mídia, TTL nem fila.

## 6. Processamento

`ProcessingOptions` (`metadata_cleanup`, `trim_start`, `mirror`, `speed`, `auto_adjust`, `reencode`) vem dos params
do preset. O `MediaProcessor` faz probe com ffprobe, monta o comando FFmpeg como lista (sem shell), escreve num
temporário e renomeia; o original nunca é tocado. Vídeo: libx264 (crf ~20), yuv420p, dimensões pares, SAR 1, AAC se
houver áudio, `+faststart`, sem metadados/capítulos, `setpts`/`atempo` para velocidade, corte com A/V sincronizados.
Imagem: JPEG de alta qualidade (PNG continua PNG), hflip, sem EXIF, orientação aplicada. O resultado vai para
`storage\processed\<yyyy>\<mm>\<media_id>_<job_id>_<sha8>.<ext>` e vira uma linha em `renditions`.

Edição de vídeos (`POST /api/edits`, `services/edit_service.py`): reaproveita a mesma fila. O lote é `source =
"library"` com `preset_id = NULL` e `preset_name = "Edição"`; cada vídeo vira um job `process` com os params da edição
(`speed` 1,0 a 1,5, `max_duration` = limite + 0,03 s, `sharpen`, `audio_sync_start` (início do áudio também escalado
pela velocidade), `reencode` conforme o tipo de saída, sem limpeza de
metadados, espelho, corte inicial nem `auto_adjust`). A saída é uma rendition comum (`preset_id = NULL`), com a mesma
dedupe por mídia + preset + params (`find_rendition` usa `preset_id IS ?`). No processor, `max_duration` corta na linha
do tempo de saída (depois da velocidade) e a duração final é conferida no probe de saída com tolerância de um quadro
no vídeo e 25 ms no áudio; fora dela o job falha e o temporário é apagado. Nenhuma migração.

## 7. Retry, cancelamento e recuperação

- Erros `retryable` (classificados em `app.jobs.errors`; por exemplo `network_error`, `rate_limited`,
  `download_failed` transitório) fazem `attempts+1`; se `attempts < max_attempts` (3) o job volta a `pending` com
  `stage=waiting_retry` e `next_attempt_at` conforme `retry_delays` (30 s na 1a falha, 2 min na 2a); na 3a tentativa
  o job vai para `failed`. O terceiro valor da tupla (10 min) só seria usado com `max_attempts` maior.
- Pausa do Instagram (`Worker.pause_for` / `pause_downloads`, motivos em `worker.PAUSE_REASONS`): `rate_limited`
  pausa por `rate_limit_pause_seconds` (600 s); `action_blocked` (resultado `restricted`: `feedback_*`, `spam`,
  `restriction_enrollment_data` nesta chamada) não é retryable e aplica a pausa de proteção do Media Tool de
  `action_blocked_pause_seconds` (1 h); `checkpoint_required` pausa por 24 h (`LONG_PAUSE_SECONDS`) até o usuário
  concluir a verificação no navegador e liberar a pausa; `session_invalid` (só com evidência explícita) pausa com
  teto de 24 h e é liberada automaticamente ao salvar a sessão (`PUT /api/settings/instagram/session`) ou atualizar
  os cookies (`PATCH .../session/cookies`), via `clear_pause(only_reason="session_invalid")`, que não toca pausas de
  outro motivo. `session_expired` não pausa. Todas são liberáveis em Configurações e nenhuma cria retry novo.
  `access_refused` (302 para a home; no teste de sessão, `api_refused` + `detail=redirect_home`) não é retryable e
  não pausa: `pause_for` ignora códigos fora de `PAUSE_REASONS`.
  Origens que podem disparar a pausa: falha de job (pipeline), erro de listagem (import runner), prévia de perfil e
  teste de sessão (todas via `pause_for(code)`). O fim e o motivo ficam em `app_state`
  (`instagram_downloads_paused_until`, `instagram_pause_reason`), são relidos no startup (pausa com motivo antigo
  `access_refused` é descartada) e uma pausa nova sempre persiste o maior fim entre a vigente (memória ou banco) e a
  nova.
- Teste de sessão: uma requisição ao endpoint que o downloader usa (`feed/user`). O resultado separa
  `session_recognized` (Set-Cookie apagando o `sessionid`, redirect para login ou `require_login` = false) e
  `listing_api` (`ok`/`refused`/`rate_limited`/`unknown`); `api_refused` = sessão reconhecida e listagem recusada.
- Durante a pausa nada automático fala com o Instagram: o claim não pega ingests que precisam baixar,
  `POST /api/imports` e `POST /api/instagram/profile` respondem 409 `instagram_paused` (mensagem com horário local de
  fim e motivo), e o import runner não inicia listagens (lotes em `listing` esperam a pausa terminar; não falham).
  O teste de sessão continua permitido (ação manual, uma por vez) e devolve `paused_until`/`pause_reason`.
  Liberação manual: `DELETE /api/settings/instagram/pause` limpa a pausa e acorda worker e import runner.
- Cancelamento: `cancel_requested` é sinalizado ao job ativo (o evento de cancelamento é repassado à listagem, ao
  download e ao `MediaProcessor.process`); a manutenção do worker conclui cancelamentos de jobs ociosos.
- Heartbeat: o worker atualiza `heartbeat_at` dos jobs ativos; job com lease e sem heartbeat por mais de
  `stale_job_seconds` (600 s) é considerado travado e reenfileirado pela manutenção.
- Recovery no startup (`app.jobs.recovery`, antes do worker e do import runner): jobs `downloading`, `processing` ou
  com lease voltam para a fila (`downloading` -> `pending`, `processing` -> `downloaded`), com `attempts+1` e
  `error_code=interrupted`; esgotado -> `failed`; os que tinham cancelamento pedido viram `cancelled`. Batches
  `listing` continuam em `listing` e o import runner retoma a listagem sem duplicar jobs. Temporários órfãos em
  `storage\temp` são removidos (somente dentro de temp). Nenhum job é marcado `completed` sem arquivo verificado.

## 8. Banco

SQLite em `data\media-tool.db`, WAL, `foreign_keys=ON`, migrations em `backend\app\db\migrations\NNNN_nome.sql`
registradas em `schema_migrations`: `0001_initial.sql` (schema e seed dos presets Padrão e Anti-duplicidade) e
`0002_job_lease.sql` (coluna `jobs.claimed_by` e índice parcial para o lease). Tabelas: `presets`, `media`, `renditions`, `batches`, `jobs`, `settings`,
`app_state`, `exports`/`export_items`/`batch_media` (migration `0013`, exportar para pasta) e `profile_analyses`/`profile_analysis_items` (migration `0025`, Análise de Perfis; reversível por `revert_migration` com `migrations\down\`, nunca no startup). Nenhuma tabela guarda sessão ou cookies; `app_state.instagram_session_last_test` guarda só o resultado
do último teste (`ok`, `code`, `message`, `username`, `tested_at`). Datas em ISO 8601 UTC com `Z`.

## 9. Segurança

- **Rede:** bind só em `127.0.0.1`; Host aceito `127.0.0.1:<porta>` e `localhost:<porta>` (proteção contra DNS
  rebinding, `invalid_host`). Mutações (POST, PUT, PATCH, DELETE) exigem `X-Media-Tool-Client` não vazio (o painel
  envia `panel`; senão 403 `client_header_required`) e, se vier `Origin`, ela precisa estar na lista permitida
  (senão 403 `origin_not_allowed`).
- **Modo dev:** a lista permitida é só a do próprio backend (`http://127.0.0.1:<porta>`, `http://localhost:<porta>`).
  Com `MEDIA_TOOL_DEV=1` (`Settings.dev_mode`) entram também `http://127.0.0.1:5173` e `http://localhost:5173`, com
  CORS para essas origens; sem ele não há CORS. O `start.ps1 -Dev` define a variável só no processo do backend.
- **Limites e headers:** corpo de requisição até 1 MB (`app.api.limits`, 413 `payload_too_large`), exceto
  `/api/library/uploads` (só esse caminho exato), lido em streaming com teto `max_download_bytes`;
  Content-Security-Policy `default-src 'self'` com `script-src 'self'`, `object-src 'none'`, `base-uri 'none'`,
  `frame-ancestors 'none'`, `form-action 'self'` e imagens só locais ou da CDN do Instagram (`/docs` e
  `/openapi.json` ficam sem CSP por causa do Swagger); `nosniff`, `no-referrer`, `X-Frame-Options: DENY`.
- **FFmpeg configurável:** `app.services.settings_service` só aceita caminho absoluto, local (sem UNC nem unidade de
  rede), arquivo existente chamado `ffmpeg.exe`/`ffprobe.exe` que responde a `-version` como a ferramenta certa.
- **Sessão:** `data\secrets\instagram-session.bin` com JSON cifrado por DPAPI (`CryptProtectData`, escopo do usuário
  atual), escrita atômica, ACL restringida ao usuário atual (`icacls /inheritance:r /grant:r`, sem shell).
  Precedência: arquivo; senão `INSTAGRAM_SESSIONID` (ambiente ou `.env`). `InstagramSession` tem `repr` redigido e não é serializável; só o adapter chama
  `cookies()`. Os cookies chegam ao gallery-dl pelo stdin do processo filho, nunca por argv, variável de ambiente do
  filho ou arquivo em texto puro. `INSTAGRAM_SESSIONID` é só fallback. No provider web, cookies e tokens da página
  (`fb_dtsg`, `lsd`, `jazoest`) ficam só em memória, em objetos com `__slots__`, `repr` redigido e sem serialização,
  descartados no fim da operação; diagnósticos têm só status, path, nomes de chaves/marcadores e `detail`.
- **Logs:** filtro global redige valores de segredo conhecidos e padrões de `sessionid`/cookies; o stderr do
  gallery-dl é redigido antes de ser logado.
- **Downloads:** só `https` na porta 443, hosts `*.cdninstagram.com` / `*.fbcdn.net`, sem userinfo, DNS resolvido
  com IPs privados/loopback/link-local/reservados rejeitados, redirects revalidados (máx. 3), limite de tamanho,
  sem cookies, verificação de magic bytes.
- **Arquivos:** nomes internos derivados de hash/ids (nunca da origem), nada é sobrescrito; `system_open` só abre
  caminhos dentro das raízes permitidas (`ensure_within`) e, pelo aplicativo padrão, só extensões de mídia
  (`.mp4`, `.mov`, `.jpg`, `.png`, `.webp`...), nunca executáveis.
- **Exportar para pasta:** nenhuma rota recebe caminho do cliente. O destino só existe se um humano o escolheu no
  diálogo nativo aberto pelo backend (guardado no servidor atrás de um token aleatório de uso único, válido por
  10 min) ou se o servidor montou a pasta padrão em Documentos. O destino é validado na escolha e de novo no início da
  cópia (absoluto, existente, resolvido, fora das pastas do Media Tool e do sistema, não é raiz de unidade, gravável).
  "Abrir pasta" usa o caminho do registro no banco, revalidado. Uma página externa não consegue mandar escrever nem
  abrir um caminho: sem o header e a origem locais a mutação é recusada, e mesmo com eles não há campo de caminho.
- **Processos:** FFmpeg, ffprobe, o runner e o seletor de pasta rodam com lista de argumentos e `shell=False`.

## 10. Scripts

| Script | Função |
|---|---|
| `scripts\setup.ps1` | Pastas, `.venv` (reusa se existir), requirements, processor editable, gallery-dl editable (só se ausente), npm ci/install + build, checagem de FFmpeg. `-SkipPython`, `-SkipFrontend`. |
| `scripts\start.ps1` | Valida ambiente e porta, inicia uvicorn em segundo plano com `--no-access-log` e `MEDIA_TOOL_LOG_CONSOLE=0` (log da aplicação só em `logs\media-tool.log`; `logs\backend.*.log` só com mensagens do servidor; PID em `data\.runtime\backend.pid`), espera `/api/health`. `-Dev`, `-Foreground`, `-Open`, `-Port`, `-NoBuild`. Ambiente só dos filhos: backend recebe `MEDIA_TOOL_HOST`/`MEDIA_TOOL_PORT`, `MEDIA_TOOL_DEV=1` só com `-Dev` (removido sem `-Dev`) e nenhuma `INSTAGRAM_*` exceto `INSTAGRAM_SESSIONID`; o Vite recebe `MEDIA_TOOL_PORT` (alvo do proxy) e nenhuma `INSTAGRAM_*`. |
| `scripts\stop.ps1` | Encerra por PID file com árvore de processos (confirmando a linha de comando); fallback pela porta só para `uvicorn app.main:app`; idempotente. |
| `scripts\set-instagram-session.ps1` | Salva (sem parâmetros, entrada oculta, valor só pelo stdin), `-Status` ou `-Clear`. |
| `scripts\poc-instagram.ps1` | Prova de conceito do adapter gallery-dl (legado) com 1 mídia: `-Username <perfil>` (obrigatório), `-Content reels\|posts\|videos\|images\|all` (padrão `reels`). |
| `scripts\poc-instagram-web.ps1` | Prova de conceito do provider web sobre o app real (SQLite e storage reais): `-Username <perfil>` (obrigatório), `-Content` (padrão `all`), `-Process`. Usa 1 mídia, para no primeiro erro e recusa rodar com o app aberto ou com a fila ocupada. |
| `scripts\lib\common.ps1` | Funções comuns (processos, PID files, porta, HTTP local, logs). |

Observação: no Windows, `.venv\Scripts\python.exe` é um lançador que cria o processo do Python base; por isso o PID
gravado é o do lançador e o `stop.ps1` encerra a árvore inteira (`taskkill /T /F`).
