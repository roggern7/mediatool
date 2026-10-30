# Robustez de lotes (contrato desta fase)

Complementa `docs/INSTAGRAM_WEB_CONTRACT.md`. Autenticação, bootstrap, formulário GraphQL e variáveis de paginação
já foram validados no real e NÃO mudam, exceto alterações localizadas citadas aqui.

## 0.1 "Todos", quantidade do tipo pedido, fim real x fim anômalo e vários perfis (REVISÃO MAIS RECENTE; prevalece)

Decisões do dono que substituem o que conflitar nas seções 0, 1, 6 e 9:

- **"Todos" (`limit: null`)** é aceito e percorre o perfil até o FIM REAL da paginação (`has_next_page=false`). Não
  existe teto de quantidade (`MAX_ITEMS` saiu) nem de páginas (`max_pages_for` saiu); NÃO é "um número grande".
  "Últimos N": N inteiro de 1 a 1.000.000 (`downloader.MAX_IMPORT_LIMIT`, só validação para rejeitar lixo).
- **Quantidade é do TIPO pedido**: "100 + Reels" = 100 Reels. A paginação continua até achar a quantidade ou chegar
  ao fim real. As heurísticas `filter_exhausted` (páginas seguidas sem item compatível) e `low_yield` (rendimento
  baixo) foram REMOVIDAS, com e sem filtro; `ListingBudget` e as variáveis `MEDIA_TOOL_IG_UNPRODUCTIVE_PAGES*`,
  `MEDIA_TOOL_IG_MIN_PAGES_FOR_YIELD` e `MEDIA_TOOL_IG_MIN_YIELD` não existem mais (ignoradas se definidas).
- **Tipos**: `all`, `reels`, `photos_carousels` (interface: "Publicações do feed" = tudo o que não é Reel: fotos
  simples, vídeo avulso antigo que não é Reel e TODOS os itens de carrosséis, inclusive vídeo de carrossel) e os
  antigos `posts` (= `all` no provider web),
  `videos`, `images`. Em `reels` item de carrossel nunca entra. O filtro acontece na normalização, antes de criar
  jobs; o que ele ignora conta em `expected.content_filter`, fora de `listing_dropped.unexpected_total`.
  Carrossel conta por ITEM; se o limite cai no meio de um carrossel, o corte é por mídia (os primeiros itens entram).
  **Vídeo avulso que não é Reel** (legado: sem `product_type` `clips`, fora de carrossel) entra em `all`, em
  `photos_carousels` ("Publicações do feed") e no antigo `videos`; em `reels` é ignorado e contado como
  `content_filter` (esperado), nunca como descarte. `reels` e `photos_carousels` são complementares: toda mídia
  suportada cai em exatamente um dos dois.
- **Regras de parada da listagem** (únicas):

  | Situação | Resultado |
  |---|---|
  | Quantidade pedida do tipo atingida | listagem `done`, `listing_end = "limit"`, lote conclui |
  | `has_next_page=false` (resposta válida) | listagem `done`, `listing_end = "profile_end"`, lote conclui com o que achou (mensagem informativa em `error_message` se achou menos que N) |
  | Páginas sem item compatível / rendimento baixo | NADA: continua paginando |
  | `has_next_page=true` com página vazia, sem cursor (com ou sem `errors`), cursor repetido, 2 páginas sem post novo; `has_next_page` não booleano | fim ANÔMALO: `needs_attention` com o checkpoint anterior, `error_code = contract_changed`, lote NÃO concluído, `listing_end = null`; sem requisição extra; nova tentativa automática a cada 6 h (máx. 4) ou `resume-listing` |
  | Fim anômalo na 1a página, sem nenhum item | lote `failed` com o erro visível (nunca "perfil vazio concluído") |
  | Fim anômalo na 1a página, com itens | itens viram jobs; `needs_attention` (a nova tentativa refaz a página 1; o índice único deduplica) |
  | Erro retryable/rede | `paused` com backoff do lote (como antes) |
  | Outro erro no meio do lote | `needs_attention` (como antes) |
  | Pausa do Instagram, backpressure, teto de páginas por hora, disco | espera e retoma do checkpoint (como antes) |
  | Provider encerra sem erro, sem fim real e sem avançar página | `needs_attention` (não é fim do perfil) |

  `ListingCheckpoint.has_next` só fica falso no fim real. O teto de páginas por trecho do construtor do provider
  (`max_pages`, só testes/POC) encerra o trecho com `has_next` verdadeiro e o runner abre outro trecho.
- **`batches.listing_end`** (migration 0012): `"profile_end"` | `"limit"` | NULL (listando, erro, cancelado, lote
  antigo). A interface usa para dizer "perfil chegou ao fim: 37 de 100".
- **Vários perfis (grupo)**: migration 0012 cria `import_groups` (só o que foi pedido: `content_type`,
  `requested_limit`, preset, `process`, `created_at`) e `batches.group_id`/`group_position`. O estado do grupo é
  DERIVADO dos filhos (nenhum status gravado no grupo). Cada filho é um lote comum (cancelar/retomar/recomeçar pelas
  rotas de lote). A quantidade é POR PERFIL. API em docs/CONTRACTS.md, seção 8.1.
- **Fila e justiça entre perfis** (`ImportRunner._next_eligible`): a listagem continua globalmente serial (uma thread,
  um trecho por vez; o portão garante 1 requisição em voo). Os candidatos são os lotes em `listing` por ordem de
  criação (= `group_position` dentro do grupo). Dentro de um grupo, o filho seguinte só começa quando o da vez
  terminou de listar OU ficou parado por conta própria (`needs_attention`, backoff do lote `listing_retry_at`,
  pausa do disjuntor). Enquanto o filho da vez espera backpressure (por lote, global ou de processamento) ou o teto
  de páginas por hora, os filhos seguintes do MESMO grupo continuam na fila sem mudar de estado (`group_state =
  "waiting"`). Lotes avulsos e grupos diferentes mantêm o comportamento anterior (um lote em backpressure deixa o
  seguinte listar, limitado pelo teto global de 200 pendentes). Pausa global do Instagram para todos.
  Consequências a saber: (a) quando o perfil da vez é "Todos" de um perfil grande, os perfis seguintes do grupo podem
  esperar HORAS (a listagem dele encosta no teto de páginas por hora e no backpressure dos downloads); (b) um lote
  avulso criado depois entra na mesma fila por ordem de criação: ele só lista quando os lotes anteriores terminaram
  de listar ou estão parados/esperando (lote avulso não fica preso atrás de um grupo em backpressure, mas todos
  dividem a mesma janela de páginas por hora e o teto global de 200 pendentes).
- **Cancelamento durante a escolha/listagem**: a entrada em `running` é um UPDATE condicional (`start_listing`: só se
  o lote ainda está em `listing`); lote cancelado depois de escolhido não é listado. A cada página o runner confere
  o status no banco (e de novo dentro da transação do `_flush`): cancelado, para na fronteira da página sem pedir a
  próxima, sem criar jobs nem avançar o checkpoint.
- **Tentativas automáticas de `needs_attention`** (6 h, máx. 4): o contador só zera com progresso REAL (pelo menos um
  job novo criado no trecho) ou por ação do usuário. Página com checkpoint mas sem job novo não zera; assim um
  servidor que repete os mesmos posts com cursores novos para em 4 tentativas. Efeito colateral aceito: retomadas
  que só atravessam páginas sem item do tipo pedido também não zeram.
- **Nó sem chave estável** (sem `pk`, `id` nem `code`) não conta como post novo: duas páginas seguidas só com nós
  assim (ou já vistos) são fim anômalo `no_new_items`.
- **Estratégia `auto`**: nenhum fim anômalo (`LISTING_ANOMALY_DETAILS`) aciona o fallback para o gallery-dl.
- **Risco conhecido**: `has_next_page` ausente ou nulo é tratado como anomalia (`has_next_invalid`). Se o Instagram
  passar a omitir o campo na última página, os lotes ficarão em `needs_attention` em vez de concluir, até o código
  ser ajustado.
- **Erro em um perfil** (inexistente, privado, `needs_attention`, falha) fica no filho; os demais seguem.
- **Reinício**: filhos concluídos ficam, o filho em listagem retoma do checkpoint, a fila segue na mesma ordem.
- **Teto de páginas por hora: padrão 600** (antes 300). `INSTAGRAM_PAGE_RATE_LIMIT` continua valendo (`0` desativa;
  inválido -> 600). A janela é uma só para todos os perfis. Valor persistido ilegível = janela cheia pelo teto
  VIGENTE (`restore_listing_pages(raw, max_per_hour=...)`). A reconciliação da inicialização
  (`restore_rate_limit_wait`) também decide pelo teto vigente, não pelo gravado no lote: um lote que esperava sob o
  teto antigo não aparece `waiting_rate_limit` se a janela já tem vaga sob o novo. Um "Todos" grande encosta no teto e fica em
  `waiting_rate_limit` até a janela liberar (esperado).
- Provider `gallery_dl` (legado, não é o padrão): tem teto próprio de 5.000 itens e não conhece `photos_carousels`.
  Por isso "Todos" (`limit` nulo), N acima de 5.000 e `photos_carousels` só existem no provider web: com o método
  `gallery_dl` configurado a API recusa a criação (422 `not_supported_by_provider`); no composite, a estratégia `gallery_dl`
  recusa a listagem com erro claro (lote criado antes da troca de método falha com a mensagem, sem truncar) e a
  estratégia `auto` não faz fallback para o gallery-dl nesses dois casos (o erro do web fica no lote).
- Lista de perfis: um perfil por linha (vírgula e ponto e vírgula também separam; espaço não separa; entrada com
  espaço interno é inválida). URL só de perfil: `/usuario`, `/usuario/reels/`, `/usuario/tagged/`.
- Cancelamento do grupo: a parte de banco é uma transação só para todos os filhos abertos; o filho em
  listagem/download é avisado depois do commit (cooperativo).
- Testes: `backend/tests/test_import_all_filters.py`, `test_import_groups.py`, `test_import_groups_scale.py` (`slow`).

## 0.2 Importação por link (execução do lote)

Contrato da API: `docs/CONTRACTS.md`, seção 8.3. O lote por link (`batches.import_mode = 'links'`, migration 0014) é
um lote comum de importação e reaproveita tudo o que existe: a MESMA fila e a mesma thread de listagem (uma listagem
por vez no app inteiro, perfis e links juntos, em ordem de criação), os jobs de ingest, a deduplicação, o portão
global, o teto de páginas por hora, as pausas e a exportação. Não há downloader nem thread a mais.

- **Um link por vez** (`ImportRunner._run_links`): para cada link `pending` de `batch_links`, em ordem de `position`,
  o runner confere cancelamento, pausa do Instagram e backpressure (mesmos tetos da listagem de perfil:
  `MAX_PENDING_INGEST`, `MAX_PENDING_INGEST_GLOBAL`, backlog de processamento) e chama
  `downloader.resolve_post(shortcode, cancel=..., path_kind=...)`: UMA requisição ao Instagram pelo portão. No
  provider web ela é um **GET anônimo do documento público da publicação** (sem nenhum cookie da sessão do dono; ver
  `docs/INSTAGRAM_WEB_CONTRACT.md`, "Documento público da publicação"); `path_kind` (`p` ou `reel`) vem da URL
  normalizada do link (`batch_links.url`, via `validation.post_path_kind`), redirect nunca é seguido e a publicação
  entregue **não** chama `worker.note_auth_success()` (`resolve_post_authenticated` é False no provider web: a
  resposta anônima não prova a sessão; no gallery-dl segue True). Verificado no Instagram real só para um Reel
  público sem login (2026-10-10); foto, carrossel, caminho `/p/` e publicação restrita NÃO foram verificados. A
  criação do lote continua exigindo sessão configurada (`POST /api/import-links` -> 409 `session_missing`), como
  antes; só a consulta da publicação deixou de usá-la. Numa transação só, cria
  os jobs de todas as mídias da publicação (ordem do carrossel; `listing_page_index`/cursor/`listing_page_size`
  NULL) e marca o link `resolved` (`items_found`, `profile`, `resolved_at`). Link `resolved` ou `failed` nunca é
  pedido de novo; o mesmo shortcode nunca aparece duas vezes no lote (UNIQUE).
- **Resposta de outra publicação** (`post_mismatch`): o shortcode devolvido tem de ser EXATAMENTE o pedido
  (comparação que diferencia maiúsculas). Fonte única no provider web (`InstagramWebDownloader.resolve_post`: o
  `code` do envelope do documento e o do nó da mídia, conferidos em `post_document.select_media_node` e de novo com
  `normalize.post_code`; os filhos de carrossel não entram na comparação): resposta de outra publicação levanta
  `contract_changed` com `detail = post_mismatch` antes de normalizar, sem devolver mídia; vale para a resolução e
  para a renovação de URL. Resposta SEM shortcode não é `post_mismatch`: continua como antes (`normalize_node` não
  devolve mídia e o provider levanta `contract_changed`/`post_shape_unknown`), então nenhum formato que hoje
  funciona passa a falhar. Defesa para qualquer provider (fakes, gallery-dl): o runner (`_consume_links`) recusa a
  resposta inteira se algum item tem `source_post_id` diferente do shortcode do link (link `failed` com
  `contract_changed`, nenhum job criado, link nunca `resolved`), e a renovação (`pipeline._refresh_post`) falha o
  job com `contract_changed` sem gravar URL em job nenhum; a renovação interna do provider `gallery-dl`
  (`GalleryDlInstagramDownloader.download`) faz a mesma conferência antes de escolher a URL nova. `post_mismatch` é erro DO LINK (como
  `post_shape_unknown`): o lote segue para o próximo. Nenhum dos dois shortcodes vai para o log.
- **Documento sem o envelope** (`contract_changed` com `detail = post_envelope_missing`: o HTML veio, mas sem os
  dados da publicação no formato conhecido; inclui uma eventual tela de login respondida com 200, caso NÃO
  observado): também é erro DO LINK (`LINK_CONTRACT_DETAILS`), sem fallback para o gallery-dl em `auto`. Se o
  formato mudou para todos, o disjuntor de 3 links falhos seguidos para o lote. **Publicação restrita** no
  documento público: UMA 2a tentativa pelo mesmo documento aberto com a sessão salva (só com sessão salva e sem
  pausa; `docs/INSTAGRAM_WEB_CONTRACT.md`, "2a tentativa de uma publicação restrita"). Sem sessão salva
  (`post_gated_logged_out`), ainda restrita com a sessão (`post_gated_with_session`) ou com a sessão já desligada no
  lote (`post_gated_session_off`): o link falha com `content_unavailable` e NÃO arma a recusa em espera (não é recusa
  da requisição). Recusa da 2a tentativa (429, checkpoint, login, 401/403, rede...): a política de sempre para o
  código (pausa, recusa em espera, backoff; link `pending`), sem nova tentativa, e a sessão deixa de ser usada no
  resto do lote. Publicação inexistente (404): `content_unavailable` (`post_http_404`).
- **Erro do link** (`content_unavailable`, `profile_private`, `profile_not_found`, `unsupported_media`,
  `invalid_input`, ou resposta sem nenhuma mídia): o link fica `failed` com o código e a mensagem, e o runner segue
  para o próximo. Não muda o estado dos outros links nem pausa nada.
- **Erro do lote/ambiente**: o link continua `pending` e NENHUM outro link é pedido no trecho (um problema sistêmico
  nunca vira N requisições falhas seguidas). Pausa do Instagram (`InstagramPaused` do portão, ou código que pausa:
  `rate_limited`, `action_blocked`, `request_auth_failed`, `checkpoint_required`, `session_invalid`/`expired`) ->
  listagem `paused`, retomada sozinha no fim da pausa. Erro retryable ou de rede (`network_error`, `timeout`) ->
  `paused` com o backoff do lote (1 min, 5 min, 15 min, 1 h...). Qualquer outro (`session_missing`,
  `contract_changed`, `internal_error`, exceção inesperada) -> `needs_attention` com o erro
  visível, nova tentativa automática a cada 6 h (máx. 4) e "Retomar" (`resume-listing`). Link resolvido zera o
  backoff e as tentativas.
- **Recusa em espera** (`access_refused` em lote por link; UM bloqueio por lote, compartilhado pelo runner e pela
  renovação de URL). A recusa é da consulta, não do link; repetir sozinho só mandaria mais requisições recusadas.
  No documento público, `access_refused` é qualquer redirect (login, challenge, checkpoint, consent, home ou outro,
  inclusive `/p/<código>/` -> `/reel/<código>/`: redirect nunca é seguido) e 401/403. Como a requisição é anônima,
  nenhum desses casos vira `session_expired`, `request_auth_failed` ou `checkpoint_required`: a sessão do dono não
  é tocada.
  - **Estado** (persistente, sem coluna nova): `batches.error_code = 'access_refused'` num lote `import_mode =
    'links'` (`BatchRepository.links_hold` / `arm_links_hold` / `clear_links_hold`; a MESMA função é consultada
    pelos dois caminhos). Por que este campo: já é o que o lote parado por recusa gravava e a API/presenters
    mostram (`error_code`/`error_message`), vale em qualquer `status` (lote que já saiu de `listing` com erro
    visível é um estado que já existia, o da listagem parcial), sobrevive ao reinício e não mexe na máquina de
    estados. Os campos de pausa do disjuntor (`paused_*`) foram descartados: param também os downloads com URL
    válida, têm prazo e retomada automática e são zerados por `pause_batch`; os contadores `refresh_fail_*` também
    são zerados por `pause_batch`. Nenhum lote por link grava `access_refused` em `error_code` por outro motivo.
  - **Trava** (`pipeline.links_query_lock`, em memória, por app e lote): toda consulta `resolve_post` do lote, do
    runner (`_consume_links`) ou da renovação (`_download_after_expired_post`), adquire a trava, RE-CONFERE a
    espera sob ela, só então consulta e, recusada, ARMA a espera antes de soltar a trava. Duas consultas do mesmo
    lote nunca ficam em voo ao mesmo tempo e a segunda sempre vê a recusa da primeira. O início do trecho do
    runner (`start_listing`, que apaga o erro de um lote `paused`) também acontece sob a trava e não começa com a
    espera armada. Ordem das travas: trava do lote primeiro; dentro dela `resolve_post` usa o portão do Instagram e
    a espera do teto de páginas; nada adquire a trava do lote segurando aquelas (sem inversão). A espera pela
    trava é cancelável (confere o `cancel` a cada 0,1 s: job cancelado sai com `JobCancelled`; o runner abandona o
    trecho com o link ainda pendente).
  - **Arma**: qualquer `access_refused` de `resolve_post` em lote por link. Runner: em `_consume_links`, mesmo com
    o trecho cancelado/encerrando durante a consulta. Renovação: em `_refresh_post`, mesmo com o job cancelado
    durante a consulta (o job fica `cancelled`; a espera fica armada).
  - **Com a espera armada**: `_next_eligible` nunca escolhe o lote e o deixa em `needs_attention` com
    `listing_retry_at = NULL` (o estado que "Retomar importação" aceita), venha ele de `running` interrompido,
    `paused` por backoff, `pending`, backpressure ou de um agendamento de versão anterior; o trecho em andamento
    para antes do próximo link (`_after_links_chunk`, sem contar tentativa do link e sem agendar nada); a
    renovação não consulta: o job falha com `access_refused` e `LINKS_REFRESH_BLOCKED_MESSAGE` (ou
    `LINKS_REFRESH_BLOCKED_LISTING_MESSAGE` com o lote ainda em `listing`, onde `retry-failed` responde 409), sem
    requisição, sem pausa do Instagram e sem contar no disjuntor de jobs do lote. Job com URL ainda válida, ou já
    renovada por um irmão, baixa normalmente. Nada é pausado: a renovação recusada não conta no disjuntor de jobs
    e 3 renovações recusadas seguidas não acionam `BREAKER_REFRESH_FAILURES` (a pausa do lote retomaria sozinha).
  - **Desarma** (só ação explícita do dono): "Retomar importação" (`resume_listing`, que já apagava
    `error_code`; lote em `listing`) e "Tentar falhas novamente" (`retry_failed`) ou repetir um job falho
    (`retry_job`), estes com o lote fora de `listing` (`clear_links_hold`). Depois de desarmada, no máximo UMA
    consulta recusada acontece antes de ela se armar de novo, no lote inteiro e em qualquer dos dois caminhos:
    12 publicações com URL vencida = 1 consulta, não 12; worker antes do runner, ou runner antes do worker = 1.
    A primeira renovação depois da ação consulta mesmo dentro de `REFRESH_MIN_INTERVAL` (o registro recente com
    `access_refused` não é repetido sem requisição, ao contrário dos outros códigos).
  - **2a recusa do mesmo link** (`LINK_ATTENTION_LIMIT`, `_links_refused`): o link fica `failed` e o lote continua
    parado (o próximo link só sai por outra ação). O disjuntor `links_failing` não é acionado por essa recusa (ele
    agendaria a tentativa de 6 h). Se era o ÚLTIMO link pendente, o lote sai de `listing` SEM desarmar
    (`_finish_listing` com `error_code = access_refused` e `LINKS_HOLD_DONE_MESSAGE`; `links_failed` se nenhum
    job foi criado).
  - **Fim do lote** (`BatchRepository.refresh_status`, o ponto único em que `running` vira `completed`: worker
    ao terminar o último job, recovery, fim da listagem): a espera só faz sentido enquanto ainda pode haver
    consulta. Lote concluído SEM job `failed` (nada que o dono possa repetir) -> a espera é desarmada ali
    mesmo (`_release_finished_links_hold`) e o lote fica com o aviso normal ("X de N links não puderam ser
    importados." quando há link falho; senão nenhum erro). Com job `failed` a espera permanece e "Tentar
    falhas novamente" existe na tela e desarma. Enquanto o lote está `running` com a espera armada e ainda sem
    falha, a mensagem do lote não manda clicar em botão ausente: "... Se algum item do lote falhar, use
    "Tentar falhas novamente" ...".
  - **Cancelamento** (`_cancel_batch_rows`): lote cancelado não tem espera (`links_hold` é falso e
    `arm_links_hold` não grava em lote cancelado); o `error_code` fica como registro do motivo, como nos
    outros erros de lote cancelado, e a mensagem vira `LINKS_HOLD_CANCELLED_MESSAGE` (sem apontar botões).
  - **Fim do trecho sob a trava** (`_after_links_chunk`): a conferência da espera e a gravação do estado/erro
    do fim do trecho (`_handle_listing_error`, `_finish_listing`) acontecem sob a MESMA trava do lote; uma
    renovação recusada não consegue armar a espera entre as duas (a gravação a apagaria). Nada ali consulta o
    Instagram nem adquire o portão; a espera pela trava é interrompida pelo encerramento do app.
  - A listagem por perfil (`_refresh_page`, `_handle_listing_error`) e os outros códigos (`content_unavailable`,
    `rate_limited`, `network_error`, publicação inexistente, mídia que sumiu) não mudam. Dois lotes por link são
    isolados.
  - **Cancelamento durante o fim do trecho**: o fim do trecho espera a trava do lote, que pode durar uma
    renovação em voo; ao obtê-la o runner RELÊ o lote e, se ele foi cancelado (ou saiu de `listing`), não grava
    nada (lote cancelado segue o encerramento de cancelado de `_after_chunk`). Defesa em profundidade, para
    lote por link e por perfil: toda gravação de estado de fim de trecho passa por `_set_listing` e
    `_finish_listing`, que conferem `status = 'listing'` na MESMA transação da gravação; um lote cancelado
    nunca volta a `running`/`completed`/`paused` por uma gravação atrasada. A espera pela trava no fim do
    trecho é interrompida só pelo encerramento do app, não pelo cancelamento do lote (o `cancel` do trecho já
    foi desregistrado nesse ponto); o cancelamento sinaliza o job da renovação em voo, que solta a trava.
    `arm_links_hold` só grava em lote `listing`/`running`.
  - **Limitações conhecidas**: (-2) o runner espera uma renovação em voo do MESMO lote antes de gravar o fim
    do trecho (equivalente ao que já acontecia com o teto global de páginas: uma consulta por vez). (-1) A
    retenção de jobs (`purge_finished_before`) pode apagar o job falho de um lote concluído com a espera
    armada; o lote fica com a mensagem de "Tentar falhas novamente" sem job para repetir (a espera não é
    liberada nesse caminho; não foi alterado). (0) durante uma pausa do Instagram com a espera armada por uma renovação, o
    lote pode ficar em `paused`/`pending` com o erro visível e sem o botão "Retomar importação" até a pausa
    acabar (o runner só passa o lote a `needs_attention` quando volta a rodar); nenhuma consulta acontece nesse
    intervalo. (i) app encerrado DURANTE a consulta de um link (ou de uma renovação): resultado
    desconhecido, o link continua `pending` e é consultado de novo no próximo início. (ii) Processo morto entre a
    resposta recusada e o UPDATE que arma a espera: a recusa não fica registrada, 1 consulta extra no próximo
    início. (iii) Cancelar o job exatamente durante a consulta recusada: o job fica `cancelled` e a espera é armada
    do mesmo jeito (sem consulta extra). (iv) Provider `gallery-dl` (estratégia não padrão):
    `GalleryDlInstagramDownloader.download` renova a URL vencida por conta própria, dentro do download, sem
    passar por `pipeline._refresh_post`; essa renovação interna NÃO respeita a espera (faltou: o provider não
    recebe lote nem contexto no `download`, e a pipeline não tem como impedir a consulta sem mudar a interface
    `InstagramDownloader.download` ou o comportamento da listagem por perfil nessa estratégia) nem arma a espera
    quando é recusada. A resolução dos links pelo runner respeita e arma a espera em qualquer estratégia, e a
    renovação interna nunca usa mídia de outra publicação (`contract_changed`/`post_mismatch`).
- **Diagnóstico seguro no log** (`app.jobs.errors.safe_diagnostic`): os avisos (WARNING) de falha de link e de
  listagem (por link e por perfil) terminam com `[stage=... status=... redirect=... detail=... content_type=...
  size=... path=...]`, só com os campos presentes e válidos, numa linha só. Lista de permissão: `stage` e `detail`
  casam POR INTEIRO `[a-z][a-z0-9_]{0,63}` (começam por letra minúscula, até 64 caracteres) e não podem ter 6 ou
  mais dígitos seguidos (pareceria um id); `redirect` só se for um dos 6 valores de `web/errors.redirect_kind`;
  `status` de 100 a 599 e `size` de 0 a 10^12, só `int` exato (nem `bool` nem subclasse); content-type só da
  lista fixa (`text/html`, `application/json`, `text/javascript`, `application/x-javascript`, `text/plain`,
  `application/octet-stream`, `image/jpeg`, `image/png`, `image/webp`, `video/mp4`); `path` só quando é um dos
  paths fixos do provider web (`/`, `/graphql/query`, `/web/search/topsearch/` e os tokens `/p/` e `/reel/` do
  documento público de uma publicação, já sem o shortcode). Todo texto tem de ser `str` exato
  (subclasse é omitida). Valor fora do formato é omitido, nunca truncado; a única normalização é a do
  content-type, que perde parâmetros (`; charset=...`), espaços em volta e maiúsculas antes da comparação com a
  lista (o que sai é sempre o valor da lista). O `stage` do erro tem precedência sobre `diagnostic["stage"]`.
  Nenhum valor aceito contém espaço ou quebra de linha, e o resultado ainda passa por `redact`. `location_path`,
  query, corpo, cookies, tokens, shortcode e ids nunca entram. O link aparece pela posição no lote. Exemplo:
  `Lote 7: consulta por link recusada (access_refused); aguardando ação manual, sem nova tentativa automática
  [stage=post_document status=302 redirect=redirect_login detail=redirect_login size=0 path=/reel/]`. No lote por
  link recusado o mesmo resumo vai no fim de `error_message` (sem coluna nova),
  e também no `error_message` do link que vira `failed` na 2a recusa (`errors.message_with_diagnostic`). O texto
  final respeita o teto de 500 caracteres de `safe_message`: se não couber, o diagnóstico perde campos INTEIROS do
  fim (nunca um valor cortado ao meio) e, se nem o primeiro couber, a mensagem vai sem sufixo.
- **Link que trava o lote** (`batch_links.attention_count`): cada vez que um link leva o lote a `needs_attention`
  por erro não retryable que não é de sessão/pausa/rede, o contador do link sobe. Na 1a vez vale a regra acima
  (link pendente, nenhum outro pedido). Na 2a vez do MESMO link (`LINK_ATTENTION_LIMIT`), ele fica `failed` com esse
  erro e o lote segue para os próximos: um link que sempre devolve `contract_changed`/`internal_error` não impede
  os seguintes. `contract_changed` com `diagnostic["detail"] == "post_shape_unknown"` (o provider o levanta para UMA
  publicação) é erro DO LINK já na 1a vez. Nunca marcam o link como falho: `session_missing`, `session_invalid`,
  `session_expired`, `checkpoint_required`, `rate_limited`, `action_blocked`, `request_auth_failed`,
  `network_error`, `timeout` e qualquer erro retryable.
- **Disjuntor de links falhos seguidos** (`batches.link_fail_streak`, `LINK_FAIL_BREAKER = 3`): a leitura do
  documento público só foi verificada no Instagram real para um Reel; se o formato mudar (ou estiver errado para
  foto/carrossel), todo link viraria `contract_changed`/`post_envelope_missing` ou `content_unavailable` e o lote
  gastaria uma requisição por link marcando tudo como falho. Por isso 3 links
  SEGUIDOS falhos (sem nenhum resolvido entre eles; o contador é persistido e vale entre trechos) com links ainda
  pendentes param o trecho e deixam o lote em `needs_attention` com `error_code = links_failing`. O contador zera
  quando um link resolve, quando o disjuntor dispara e no "Retomar"; a retomada (manual ou a automática de 6 h,
  máx. 4) continua pelos pendentes, e os falhos ficam falhos. Sem link pendente depois da 3a falha, o lote conclui
  normalmente (`links_failed` se nenhum job foi criado).
- **Retomada** (reinício do app, pausa, backpressure, espera do teto de páginas): continua dos links pendentes; o
  ponto de retomada é a própria tabela `batch_links` (não há cursor). Se o app cair entre a resposta do Instagram e
  a gravação, o link é pedido de novo e o índice único dos jobs descarta os repetidos. `restart-listing` é recusado
  (409 `listing_not_resumable`).
- **Fim**: sem link pendente a listagem fica `done` (`listing_end` NULL) e o lote vai a `running` -> `completed`
  quando os jobs terminam. Parte dos links falha e ao menos um job: `error_message` "X de N links não puderam ser
  importados." (`error_code` NULL). Nenhum job criado: lote `failed`, `error_code = links_failed`.
- **Cancelamento**: igual ao do lote por perfil (jobs ociosos cancelados na transação, o trecho em andamento para na
  próxima conferência); os links ainda pendentes aparecem como `cancelled` na API (o estado gravado continua
  `pending`).
- **Deduplicação**: a de sempre (`_add_item`). Mídia já na Biblioteca -> job `completed`/`skipped` sem download;
  item em voo em outro lote -> `duplicate_in_flight`. A consulta do link acontece mesmo assim (é ela que diz quais
  mídias a publicação tem).
- **Link de download expirado** (`pipeline._download_after_expired_post`): job de lote por link não tem página de
  listagem; a renovação chama `resolve_post(shortcode, path_kind=...)` UMA vez por publicação (no provider web, o
  mesmo GET anônimo do documento público; `path_kind` vem da `source_url` do job, que o normalizador monta com
  `/reel/` quando a publicação é Reel; sem `note_auth_success`; trava em memória por app, lote e
  shortcode; intervalo mínimo `REFRESH_MIN_INTERVAL` por publicação via `media_url_refreshed_at`), grava as URLs
  novas de todos os jobs abertos da publicação no lote e tenta o download uma vez. Durante pausa do Instagram o job
  volta a `pending` sem consumir tentativa. Mídia ausente da resposta, ou URL nova também recusada ->
  `content_unavailable`. Erros da consulta seguem a classificação normal. Vale também para "repetir job" depois que
  as URLs foram apagadas. Mesmas proteções da renovação por página (seção 9, A1/A4): toda tentativa é registrada em
  `listing_pages` com `page_index = position` do link (ok/falha, código, quando); dentro de `REFRESH_MIN_INTERVAL`
  depois de uma FALHA, os itens irmãos do carrossel repetem o mesmo código sem requisição nova (retryable só para
  `RETRYABLE_REFRESH_CODES`, sem aplicar pausa de novo): carrossel de 10 itens = 1 consulta, não 10; 3 renovações
  seguidas falhas com o mesmo código pausam o lote (`BREAKER_REFRESH_FAILURES`). Lotes por perfil não mudam.
- **Renovação e `access_refused`**: a renovação usa a trava e a recusa em espera do lote descritas acima em
  "Recusa em espera" (não há estado próprio da renovação).
- **Log**: só id do lote e contagens; shortcode, link colado e URL de mídia nunca são registrados.
- **Fluxos separados (maior qualidade do vídeo)**: vídeo de link com `job_remote_refs.dash_streams` baixa o fluxo de
  vídeo e o de áudio pelo CDN e junta sem recodificar (etapa `muxing`); contrato em
  `docs/INSTAGRAM_WEB_CONTRACT.md`, "Fluxos separados". **Teto, sem laço:** no máximo **3 requisições ao CDN por chamada de download** (vídeo, áudio, MP4 direto) e, contando a renovação única, no máximo **6 requisições ao CDN e 1 consulta da publicação por tentativa do job**.
  Justificativa: a regra de não insistir protege as CONSULTAS ao Instagram (`resolve_post`, listagem); um GET
  assinado ao CDN é o que o job faria de qualquer forma. Fluxo recusado (403/404/410), status HTTP inesperado (ex.:
  206/416/5xx) e qualquer outra falha de conteúdo vão DIRETO para o MP4 direto, sem renovação e sem consulta. Só se o
  MP4 direto também vier vencido entra a renovação única de sempre; depois dela o download roda mais uma vez do
  começo (fluxos com as URLs novas, depois MP4 direto renovado) e, se ainda for recusado, o job falha e a referência
  é apagada. Falha de rede (retryable) e cancelamento sobem como hoje, sem MP4 direto.
  Sem FFmpeg/ffprobe, ou em qualquer fallback, a qualidade cai para o MP4 direto **sem aviso na interface**: o único registro é a linha de log `dash_fallback=<motivo>` do job. Os temporários (os dois fluxos, o `.part` e o arquivo juntado) ficam em `temp/job-<id>/`:
  apagados antes do fallback, ao fim do job, na nova tentativa e pela recuperação do startup; job interrompido
  recomeça do zero (sem retomada parcial). Disco: os dois fluxos + o arquivo juntado cabem na reserva de
  2 x `max_download_bytes` por download já exigida. **Download de `BaseURL` pelo app e junção NUNCA rodaram contra o CDN real; verificados só com mídias sintéticas e o manifesto real saneado.**
  Testes: `test_dash_manifest.py`, `test_normalize_dash.py`, `test_import_links_dash.py`,
  `processor/tests/test_remux.py`.
- Testes: `backend/tests/test_import_links.py`, `test_import_links_runner.py`, `test_import_links_refused.py`,
  `test_import_links_refused_diag.py`, `test_import_links_mismatch.py`, `test_import_links_refresh_refused.py`,
  `test_import_links_hold.py`, `test_import_links_public_document.py`, `test_gallerydl_post_mismatch.py`; leitura
  do documento: `test_web_post_document.py`, `test_web_provider_post.py`.

## 0. Meta de escala (REVISÃO, prevalece sobre o resto deste documento)

O produto precisa suportar lotes de 1.000, 2.000, 5.000 e 10.000 mídias por operação. Consequências:
- NÃO existe teto absoluto pequeno de páginas. A paginação para por evidência de improdutividade, não por um número
  fixo; existe só um teto de segurança proporcional e folgado (seção 1, revisada).
- Listagem incremental e retomável: a listagem roda em trechos, persiste no SQLite um checkpoint após cada página
  (cursor de retomada, contadores) e pode parar e continuar depois (backpressure, pausa, fechamento do app, reinício do
  Windows, falha de rede) sem recomeçar do zero e sem baixar de novo o que já terminou.
- Backpressure: a descoberta não pode criar milhares de jobs pendentes. O import runner para de listar quando o lote
  tem `MAX_PENDING_INGEST` (padrão 100) jobs de ingest ainda não baixados e retoma quando cair abaixo de
  `RESUME_PENDING_INGEST` (padrão 40). Assim as URLs assinadas são consumidas minutos depois de listadas.
- Nada de materializar o lote em RAM: geradores, inserts por página em uma transação, dedupe confiado ao índice único
  do SQLite entre retomadas; o conjunto de ids vistos em memória vale só dentro de um trecho de listagem.
- Instagram continua conservador: 1 listagem por vez, ritmo 2-4 s entre páginas, pausas por proteção. Concorrência de
  CDN e FFmpeg tem política própria e não aumenta chamadas ao Instagram.

### Checkpoint de listagem (interface WEB <-> PIPELINE)
```python
@dataclass(frozen=True, repr=False)
class ListingCheckpoint:             # opaco para a pipeline; repr redigido; cursor nunca logado/exposto
    resume_cursor: str | None        # retomar daqui nunca pula itens (pode repetir alguns; o índice único deduplica)
    next_page_index: int
    pages: int; posts_seen: int; items_compatible: int; unproductive_streak: int
    has_next: bool
iter_profile_media(username, content, limit, *, cancel=None, resume: ListingCheckpoint | None = None,
                   on_checkpoint: Callable[[ListingCheckpoint], None] | None = None)
```
O provider chama `on_checkpoint` depois que TODOS os itens de uma página foram entregues (cursor da próxima página) e o
consumidor persiste no lote (colunas internas novas em `batches`: `listing_cursor`, `listing_next_page`,
`listing_pages`, `listing_posts_seen`, `listing_items_compatible`, `listing_unproductive_streak`, `listing_has_next`,
`listing_state` = pending|running|waiting_backpressure|paused|done). `limit` restante = limite pedido menos jobs já
criados no lote. Ao fechar o gerador no meio de uma página, o último checkpoint persistido continua válido (a página
inteira é pedida de novo e o índice único descarta repetidos).

## 1. Orçamento de paginação (`web/budget.py`) (SUBSTITUÍDO pela seção 0.1: sem teto de páginas, sem parada por improdutividade)

Histórico (não vale mais): as regras abaixo descrevem o orçamento antigo. Hoje `web/budget.py` só guarda `PAGE_SIZE`
e a leitura validada de números do ambiente.

Regras de parada adaptativas (substituem o teto de 40 páginas):
- `UNPRODUCTIVE_PAGES_LIMIT`: páginas seguidas sem item compatível (padrão 3 para all/posts, 6 para filtros).
- Rendimento baixo: depois de `MIN_PAGES_FOR_YIELD` (20) páginas, se itens compatíveis por página < `MIN_YIELD`
  (0,25, ou seja, menos de 1 item a cada 4 páginas) -> `low_yield`.
- Teto de segurança proporcional (rede de proteção, não limite de produto):
  `ceil(limit / PAGE_SIZE) * SAFETY_FACTOR[content] + SLACK_PAGES`, com SAFETY_FACTOR all/posts 2, images 4,
  reels/videos 8 (10.000 all -> ~1.668 páginas; 10.000 reels -> ~6.668). Nunca é atingido por um perfil produtivo.
- Os contadores (streak, rendimento) persistem no checkpoint e valem entre retomadas.
Os itens abaixo marcados "ABSOLUTE_MAX_PAGES = 40" e "FILTER_FACTOR" ficam SUBSTITUÍDOS por esta seção.

### (texto original, substituído onde conflitar)

```python
PAGE_SIZE = 12                       # count observado no navegador e usado hoje
ABSOLUTE_MAX_PAGES = 40              # teto duro por listagem (2-4 s entre páginas: ~2,5 min no pior caso)
UNPRODUCTIVE_PAGES_LIMIT = 3         # páginas seguidas sem nenhum item compatível com o filtro -> parar
FILTER_FACTOR = {"all": 1, "posts": 1, "reels": 3, "videos": 3, "images": 2}
SLACK_PAGES = 2
def max_pages_for(limit: int, content: str) -> int:
    # ceil(limit / PAGE_SIZE) * FILTER_FACTOR[content] + SLACK_PAGES, limitado a ABSOLUTE_MAX_PAGES
```
Justificativa: `limit` conta mídias; um post rende >= 1 mídia (carrossel rende várias), então "all/posts" precisa de no
máximo ceil(limit/12) páginas; a folga cobre posts sem URL/não suportados. Filtros (reels/vídeos/imagens) podem
descartar a maior parte dos posts; o fator 3 cobre perfis em que ~1/3 dos posts são do tipo pedido; abaixo disso o
critério de improdutividade encerra antes. Exemplos: 10 all -> 3; 100 all -> 11; 10 reels -> 5; 100 reels -> 29.
Variáveis de ambiente opcionais: `MEDIA_TOOL_IG_ABSOLUTE_MAX_PAGES`, `MEDIA_TOOL_IG_UNPRODUCTIVE_PAGES` (validadas,
nunca acima de 100 / abaixo de 1).
Parada (ordem): quantidade atingida (imediata, sem página extra) > cancelamento > erro > cursor repetido/página
vazia/2 páginas sem nó novo (existentes) > `UNPRODUCTIVE_PAGES_LIMIT` páginas seguidas sem item compatível
(stop_reason `filter_exhausted`) > `max_pages` (stop_reason `budget_exhausted`). Parar por orçamento ou filtro NÃO é
erro: o lote termina com os itens encontrados e a mensagem diz por quê.
Métricas seguras registradas por listagem (log e batch): pages, posts_seen, items_compatible, items_skipped_filter,
duplicates, stop_reason. Nunca cursor, URL ou id.
"Todos" (limit null): desabilitado na API (422 `limit_all_disabled`, mensagem pt-BR) e na interface até política
própria. 10/20/50/100 continuam aceitos.

## 2. Referência de listagem e renovação de URL expirada

```python
@dataclass(frozen=True)
class ListingRef:                    # em downloader.py; opaco para a pipeline
    page_index: int                  # 1-based
    cursor: str | None               # cursor usado para PEDIR a página (None = 1a página); nunca logado/exposto
    page_size: int
# RemoteMedia ganha: listing_ref: ListingRef | None = None
class InstagramDownloader:
    def refresh_page(self, profile: str, ref: ListingRef, *, cancel=None) -> list[RemoteMedia]
        # padrão: InstagramError("content_unavailable"); web: bootstrap + 1 POST (after=ref.cursor, count=ref.page_size)
    def download(self, item, dest_dir, *, cancel=None, progress=None) -> Path
        # web: NÃO renova mais internamente; URL expirada -> InstagramError("media_url_expired", retryable=False,
        #      diagnostic detail http_403/http_404)
```
Pipeline (`app/jobs/pipeline.py` + repositório de jobs + migration `0003_listing_ref.sql`):
- `jobs` ganha colunas internas `listing_page_index`, `listing_cursor`, `listing_page_size`, `media_url_refreshed_at`.
  O import runner grava esses campos de `RemoteMedia.listing_ref`. Nunca saem pela API nem por log.
- Em `media_url_expired`: adquirir trava em memória por `(batch_id, listing_page_index)`; dentro dela, reler o job:
  se `media_url` mudou desde que esta tentativa começou (outro worker renovou), usar a nova; senão, se a página foi
  renovada há menos de `REFRESH_MIN_INTERVAL` (10 min) e a URL continua recusada -> falha definitiva
  `content_unavailable`; senão chamar `downloader.refresh_page(...)` UMA vez e gravar a nova `media_url` (e
  `thumbnail_url`) de TODOS os jobs não terminais do mesmo lote cujos (source_post_id, source_media_index) apareçam na
  página, marcando `media_url_refreshed_at`. Depois, UMA nova tentativa de download desta mídia. Falhou de novo ->
  `content_unavailable` (não retryable). Item não encontrado na página renovada -> `content_unavailable`.
- Erros da renovação seguem a classificação normal (429 pausa, checkpoint pausa, etc.); nada de retry extra.
  Implementado (atualizado pela seção 9, M7): 429 na renovação pausa o Instagram (10 min) e o job volta a
  `pending`/`waiting_retry` SEM consumir tentativa (`attempts` não muda, sem `next_attempt_at`); como a pausa bloqueia
  o claim de downloads, a nova tentativa só acontece depois da pausa. A renovação também não é chamada enquanto a
  pausa vale (o job volta a `pending` sem tentativa). `contract_changed`/`content_unavailable` falham sem pausa nem
  retry e contam para o disjuntor do lote (seção 9).
- Job sem referência de listagem (jobs antigos, gallery-dl) com URL recusada -> `content_unavailable` com mensagem
  pedindo nova importação (sem relistagem completa). Job repetido pela API depois de terminar já não tem URL nem
  cursor: a pipeline vai direto à renovação, que só é possível na página 1 (sem cursor); nas demais páginas o
  resultado é o mesmo `content_unavailable`.
- Jobs de biblioteca (process) não usam isso.
- Ao terminar (completed/failed/cancelled), limpar `media_url`, `thumbnail_url` e `listing_cursor` do job (só são
  necessários enquanto o download está pendente).

## 3. Classificação após a única renovação de token

`session_invalid` só com evidência explícita: `require_login: true`, `login_required`/`checkpoint`-like explícito no
JSON de erro, 401, redirect para login, sessionid apagado. Token ainda recusado após a renovação sem essa evidência:
403 -> `access_refused`; demais (ex.: 1357004 persistente, JSON inesperado) -> `contract_changed`.

## 4. Proteções

Motivos de pausa (`worker.PAUSE_REASONS`): `rate_limited` 10 min; `action_blocked` 1 h; `checkpoint_required`
24 h (mensagem: concluir a verificação no navegador e liberar a pausa); `session_invalid` (evidência confiável)
até a sessão ser salva/atualizada (limpa automaticamente ao salvar sessão ou cookies) com teto de 24 h. Todas
liberáveis manualmente (rota existente). Pausa bloqueia novas importações/prévias e downloads do Instagram; não bloqueia
processamento local. Nenhum retry automático novo.

## 5. Acabamento

Log "GraphQL página N" também para a última página quando a listagem para no limite. Docs: paginação comprovada no
real em 2026-10-03 (1 GET + 2 POST, variáveis after/before/first/last aceitas). Prévia de perfil (topsearch) segue
não verificada no real: documentar.

## 6. Escala da API e do banco (rodada de escala; fonte: auditorias SQLite e frontend/API)

API (contrato para frontend e backend):
- `GET /api/batches/{id}`: resumo SEM jobs por padrão; `?include_jobs=true` devolve no máximo 500 (mais recentes) e
  `jobs_truncated: bool`. Lista de jobs do lote = `GET /api/jobs?batch_id=`.
- Paginação keyset em `GET /api/jobs` e `GET /api/media`: parâmetro opcional `before_id` (itens com id < before_id,
  ordem id DESC) e resposta com `next_before_id` (null no fim). `offset` continua aceito por compatibilidade.
  `total` e `counts` continuam vindo; contagens por lote quando `batch_id` é passado.
- `POST /api/media/process-by-filter {view, media_type?, q?, profile?, preset_id}` -> `Batch` (202): cria os jobs no
  servidor numa transação, a partir do mesmo filtro da Biblioteca; teto `MAX_BULK_PROCESS` (10.000).
- `POST /api/batches/{id}/cancel`: um UPDATE em lote numa transação; sinaliza individualmente só jobs em execução.
- Quantidade de importação aceita pela API (revisado, seção 0.1): N inteiro de 1 a 1.000.000 ou `null` ("Todos").

Banco:
- Migration de índices: `ix_jobs_batch_cover (batch_id, status, kind, is_duplicate, stage, media_id, rendition_id)`
  (substitui `ix_jobs_batch`), `ix_jobs_status_id (status, id)`, `ix_jobs_rendition (rendition_id) WHERE rendition_id IS
  NOT NULL`, `ix_jobs_cancel (id) WHERE cancel_requested = 1`, `ix_media_profile_nocase`, e o índice de página
  `(batch_id, listing_page_index)` se ainda não existir.
- `refresh_status` com `status IN (não terminais)` usando o índice; `pending_ingest_count` com `+media_id IS NULL`;
  recovery sem OR (UNION) e com o índice de cancelamento.
- URLs assinadas e cursor numa tabela à parte `job_remote_refs(job_id PK, media_url, thumbnail_url, listing_cursor,
  listing_page_index, listing_page_size)`: linha apagada quando o job conclui; URLs apagadas quando falha/cancela
  (cursor/página mantidos para retry de job falho; apagados no cancelamento do lote).
- `PRAGMA journal_size_limit = 16 MB`; `wal_checkpoint(TRUNCATE)` na manutenção quando não há jobs ativos.
- Retenção: jobs terminais com mais de `JOB_RETENTION_DAYS` (30) são apagados, preservando o último job de cada mídia
  e jobs de lotes ainda abertos; `VACUUM` ocasional quando ocioso e o freelist for grande.
- Progresso: no máximo 1 gravação a cada 2 s por job; mudanças de etapa sempre gravadas.

## 7. Falha de autenticação de um request vs sessão inválida (prevalece sobre a seção 3)

- `request_auth_failed` (novo código, retryable só depois da pausa): um request recusado por autenticação mesmo após a
  única renovação de token: 401, `require_login`, erro 1357001/"Not Logged In"/"requires login"/`login_required` no
  JSON. Pausa `auth_check` de 15 min, estado recuperável (lote/listagem/job retomam do checkpoint).
- `session_invalid` SOMENTE com evidência forte e reproduzível: (a) Set-Cookie do servidor apagando/expirando o
  `sessionid`; (b) o documento de bootstrap redireciona para `/accounts/login/`; (c) `request_auth_failed` em 2
  verificações consecutivas separadas pela pausa, sem nenhuma resposta autenticada com sucesso entre elas (contador
  persistido em app_state, zerado por qualquer sucesso).
- Em nenhum caso o Media Tool apaga a sessão DPAPI, remove cookies, chama logout ou exige nova sessão
  automaticamente. `session_invalid` = pausa liberável (até salvar sessão, testar sessão com sucesso ou liberar
  manualmente; teto 24 h). `session_expired` segue a mesma regra. O "Testar sessão" manual continua disponível
  durante qualquer pausa pela passagem própria no portão (seção 8, "Admissão no portão").
- `contract_changed`/`access_refused` continuam como antes; nenhum deles toca a sessão.

## 8. Política de retomada automática das pausas (lotes longos sem intervenção)

Controlador único de recuperação da sessão (`app/services/instagram_recovery.py`, com relógio injetável):
- Estado persistido em app_state: motivo, escopo (`session`|`instagram`), nível de backoff, `next_attempt_at`,
  `since`. Sobrevive a reinício do app/Windows; ao iniciar, retoma a contagem a partir do estado salvo.
- Backoff por motivo (degraus; teto): `request_auth_failed` 15m, 30m, 1h, 2h, 4h (4h); `rate_limited` 15m, 30m, 1h,
  2h, 4h (6h); `action_blocked` 1h, 2h, 4h, 8h (12h); `checkpoint_required` e `session_invalid` forte: verificação a
  cada 6h (fixo), interface mostra "ação necessária".
- Durante a pausa: nenhuma tentativa ao Instagram (nem pelo worker, nem pelo import runner, nem renovação de URL).
- No vencimento: SÓ o controlador faz UMA verificação mínima (bootstrap + 1 consulta count=1, a mesma do teste de
  sessão). Sucesso: limpa a pausa, zera o backoff e o contador de falhas de autenticação, e libera a listagem para
  continuar do checkpoint. Falha do mesmo tipo: próximo degrau. Falha de tipo mais grave: troca o motivo.
  Rede indisponível na verificação: reagenda no mesmo degrau (não conta como falha de autenticação).
- Portão global de requisições ao Instagram (`InstagramRequestGate`): no máximo 1 requisição em voo no app inteiro e
  intervalo mínimo de 2-4 s entre requisições de listagem/renovação. Renovações de URL de milhares de jobs passam
  pelo portão gradualmente (sem thundering herd). Downloads de CDN e FFmpeg não passam pelo portão.
- Liberação manual e "Testar sessão" continuam disponíveis; salvar sessão limpa pausas de sessão.
- Nada apaga a sessão DPAPI; nada tenta contornar checkpoint/desafio.

### Admissão no portão (revisão de concorrência)
- Garantia: depois que a pausa é PUBLICADA (escopo `instagram` ou `session`), nenhuma requisição nova ao
  instagram.com começa. Requisição já admitida (em voo) não é cancelada: termina e seu resultado é tratado.
- Publicação: `Worker.set_pause` (caminho de `pause_for` e do controlador) chama `gate.publish_pause(scope)` de forma
  síncrona antes de retornar; `clear_pause`, a liberação manual e o vencimento sem verificação pendente chamam
  `gate.clear_pause()`; `load_pause_state` republica a pausa salva no startup.
- Admissão: dentro de `gate.slot()`, depois da vaga (1 em voo) e de qualquer espera (intervalo 2-4 s; o teto por hora
  é reservado antes, em `wait_listing_page`), imediatamente antes do envio, o portão confere a pausa publicada sob a
  MESMA trava da publicação (`_pause_lock`). Cada publicação/liberação incrementa uma época (`pause_epoch`). Com pausa
  vigente a requisição é recusada com `InstagramPaused` (`code = "instagram_paused"`, atributo `scope`), sem enviar
  nada. Os dois escopos bloqueiam a admissão (toda requisição ao instagram.com usa a sessão). Downloads de CDN e FFmpeg
  não passam pelo portão. No gallery-dl a admissão é por operação (um subprocess = uma vaga).
- Passagens explícitas (`gate.verification_pass(epoch)`), presas à thread da chamada e à época lida no início dela;
  uma pausa publicada depois invalida a passagem e as próximas requisições da chamada são recusadas:
  - verificação do controlador (`tick`, só no vencimento; época lida sob o lock do controlador junto com o estado);
  - "Testar sessão" manual (ação explícita do dono; época lida ao iniciar o teste). Sucesso continua liberando as
    pausas de escopo `session` e zerando o contador; `rate_limited`/`action_blocked` continuam.
  Nenhuma outra operação passa durante a pausa.
- Tratamento por chamador de `InstagramPaused`: listagem -> `listing_state='paused'` mantendo o checkpoint (retoma do
  cursor salvo quando a pausa termina); renovação de URL -> job volta a `pending` (`waiting_retry`) sem consumir
  tentativa e sem registrar falha de renovação nem disjuntor; verificação do controlador -> `busy` (o próximo ciclo
  decide); "Testar sessão" manual e prévia de perfil -> 409 `instagram_paused`.

## 9. Correções da revisão arquitetural de 10.000 (decisões de interface)

Listagem (C1, M1, M4, M3) — política final (re-revisão):
- Erros retryable/5xx/rede NÃO encerram a listagem: `listing_state='paused'` com backoff próprio do lote (1m, 5m,
  15m, 1h, 1h...) sem limite de tentativas (persistido em `batches.listing_retry_level`/`listing_retry_at`).
- No meio do lote (já existe checkpoint com próxima página) nenhum outro erro conclui o lote parcial nem apaga o cursor:
  `internal_error`, `session_*` (sem pausa), `profile_not_found`, `access_refused`, `contract_changed`,
  `content_unavailable` -> `listing_state='needs_attention'` mantendo checkpoint e motivo seguro. `unsupported_media`
  depois de um checkpoint não para a listagem (itens pulados e contados pelo provider). Só o fim real do perfil e o
  limite concluem (seção 0.1); antes da 1a página o erro falha o lote como antes.
- Cursor só é apagado quando o lote conclui ou é cancelado (cancelamento grava `listing_state='cancelled'`).
- Nenhum recomeço automático da página 1. Cursor recusado no início de um trecho retomado: uma nova tentativa com o
  MESMO cursor depois do backoff do lote; recusado de novo -> `needs_attention` com o checkpoint. Recomeço da página 1
  só por `POST /api/batches/{id}/restart-listing` com `{"confirm": true}` (sem confirmação: 409
  `restart_confirmation_required` com o aviso de custo). `POST /api/batches/{id}/resume-listing` retoma
  `paused`/`needs_attention` do checkpoint.
- `needs_attention` tem nova tentativa automática a cada 6 h, no máximo 4 (persistida em `listing_retry_at`/
  `listing_attention_tries`, zerada por página entregue ou ação do usuário); depois fica parada aguardando ação.
- Suspensão do Windows (builder EXECUTION): usar `BatchRepository.has_active_work()`, que exclui listagem em
  `needs_attention`, listagem pausada por backoff e lote pausado pelo disjuntor (`paused_until`) além de 30 min.
- Job interrompido 3 vezes seguidas: `interrupted_repeatedly` (feito pelo builder EXECUTION).
- Backpressure global além do por lote (`MAX_PENDING_INGEST_GLOBAL` = 200) e bloqueio de perfil que já está listando
  (409 `profile_already_importing`). Item já em download por outro lote aberto: o job novo nasce `pending` com
  `duplicate_reason = 'duplicate_in_flight'` e `jobs.primary_job_id` (migration 0009). Enquanto o primário está
  `pending`/`downloading` o dependente não é reivindicado e não conta no backpressure (por lote nem global); quando o
  primário baixa, o dependente liga à mídia pela origem (sem segundo download) e segue para o processamento se o lote
  pedir; se o primário falha ou é cancelado sem gerar a mídia, o dependente vira um job normal e baixa sozinho.
- Backpressure de processamento (lotes com processamento): o claim de NOVOS downloads do lote e a listagem param
  quando o lote tem `max_process_backlog` (50) ingests com processamento em `downloading`/`downloaded`/`processing` e
  retomam abaixo de `resume_process_backlog` (20); teto global somando os lotes com processamento
  `max_process_backlog_global` (150), retomada abaixo de `resume_process_backlog_global` (60). Configurável em
  `Settings` (env `MEDIA_TOOL_MAX_PROCESS_BACKLOG`, `MEDIA_TOOL_RESUME_PROCESS_BACKLOG`,
  `MEDIA_TOOL_MAX_PROCESS_BACKLOG_GLOBAL`, `MEDIA_TOOL_RESUME_PROCESS_BACKLOG_GLOBAL`). Estado com histerese em
  memória no worker (`process_backlog_state`); lotes sem processamento não são afetados. Limita os originais
  acumulados em disco à espera do FFmpeg.
- Processar por filtro idempotente: ignora mídias com job aberto do MESMO preset/params e as que já têm rendition do
  mesmo preset/params; índice único em renditions(media_id, preset_id, params) (`params` é JSON canônico, equivale ao
  hash).

Execução (A1, A3, A4, C2, M7, M8, B6, B7):
- Tabela `listing_pages(batch_id, page_index, last_refresh_at, last_refresh_ok, last_refresh_code, refresh_attempts)`:
  toda tentativa de renovação é registrada; com falha recente (10 min), os irmãos falham na hora com o mesmo código sem
  nova requisição.
- Disjuntor por lote: 3 falhas seguidas do mesmo código de renovação, ou 20 jobs seguidos falhando com o mesmo código,
  pausam o lote (`batches.paused_reason`, `paused_until`, retomada automática com backoff; listagem também respeita).
- Dois pools: download (I/O, `download_concurrency` padrão 2, máx 4) e processamento FFmpeg (CPU,
  `processing_concurrency` padrão 1, máx 2; a configuração existente `concurrency` passa a ser esta). Ingest com
  processamento termina em `downloaded`; o processamento é uma etapa reivindicada pelo pool de FFmpeg. Ingest
  download-only (padrão, seção 10) termina em `completed` no próprio pool de download. Claim justo por rodízio entre
  lotes.
- Disco: `services/disk.py` com `disk_ok(min_free)`; mínimo = 5 GB + 2 x max_download_bytes x download_concurrency
  (configurável), mais 2 x max_download_bytes x processing_concurrency só enquanto houver processamento em aberto
  (seção 10). Antes de cada claim de ingest e de cada trecho de listagem. Disco insuficiente: pausa local
  `disk_full` (não consome tentativa; job volta a pending), verificação a cada 1 min, exibida na interface com espaço
  livre.
- Interrupção (reinício abrupto) e pausa não consomem tentativa.
- FFmpeg em Job Object do Windows com KILL_ON_JOB_CLOSE (processor).
- Verificação automática do controlador fora da thread despachante.
- Enquanto houver lote ativo: `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` (impede suspensão; não
  impede desligar a tela).

Proteção do Instagram (A2, M2, M5):
- Backoff com janela de prova: após limpar uma pausa por verificação, o nível é mantido por 2x a última pausa; novo 429
  nessa janela sobe um degrau. Depois de um 429, ritmo da listagem 6-12 s por página por 1 h. Teto de 600 páginas por
  hora (padrão desde a seção 0.1; antes 300; configurável, ver abaixo).
- Teto por hora configurável por configuração técnica (sem botão na interface): `INSTAGRAM_PAGE_RATE_LIMIT` (ambiente
  do processo ou `.env`). Ausente ou vazia: vale a antiga `MEDIA_TOOL_IG_MAX_PAGES_PER_HOUR` ou o padrão, 600 páginas/h
  (antes 300). Inteiro de 1 a 100000: esse teto. `0`: SEM teto horário, só para POC de escala (nenhuma
  reserva nem espera do teto, nada gravado em `instagram_listing_pages`, o lote nunca fica em `waiting_rate_limit`;
  um aviso WARNING por processo registra isso). Inválido, negativo, decimal ou acima de 100000: aviso e padrão 600
  (nunca desabilita por engano). Afeta SOMENTE o teto de páginas por hora: concorrência (1 em voo), intervalo entre
  requisições, backpressure, retries, checkpoint, pausas, admissão no portão e ritmo lento depois de `rate_limited`
  continuam iguais. Com `0`, `ImportRunner.restore_rate_limit_wait` limpa na inicialização os
  campos de espera que uma execução anterior (com teto) deixou gravados. Só vale para o provider web; o gallery-dl
  nunca aplicou o teto de páginas. Testes: `backend/tests/test_page_rate_limit_config.py`.
- Estado do bootstrap reaproveitado em memória por até 10 min entre trechos e renovações (só memória).
- Composite: sem fallback quando houver `resume`.

Operação (M6, B3):
- Background sem duplicar log no console; access log do uvicorn desligado ou em arquivo rotativo; resumo de uma linha
  por job (etapas em DEBUG). `thumbs/` fatiado por id (`thumbs/<id % 256>/`), migrando os existentes.
- Interface: espaço livre em disco, estado `needs_attention` com botão "Retomar importação", pausa por lote e
  `disk_full`, dois campos de concorrência (downloads e processamento).

### Estado final da seção 9 (integração)
- Listagem: `listing_state` (`pending`, `running`, `waiting_backpressure`, `paused`, `needs_attention`, `done`; a API
  apresenta ainda `waiting_rate_limit` durante a espera pelo teto de páginas por hora, ver seção 10) com
  backoff próprio, `needs_attention` com motivo em `batches.error_code`/`error_message`, retomada por
  `POST /api/batches/{id}/resume-listing`, backpressure global e 409 `profile_already_importing`.
- Execução: `listing_pages` registra as renovações; disjuntor por lote (3 renovações ou 20 jobs seguidos com o mesmo
  código) grava a linha do lote só em falha ou mudança de estado: o sucesso só zera quando há contador ou degrau
  diferente de zero (o caminho feliz não escreve em `batches` a cada job). Dois pools (download e FFmpeg); o job
  sai dos ativos do worker antes de liberar o lease, porque o pool de FFmpeg pode reivindicar na hora o mesmo job
  `downloaded`; na etapa de FFmpeg o job não volta a `downloaded` no meio. Disco (`disk_full`), pausa e interrupção
  não consomem tentativa.
- API: `GET /api/settings` traz `concurrency` (= `processing_concurrency`, pool FFmpeg 1-2), `download_concurrency`
  (1-4) e `disk {free_bytes, required_bytes, ok, disk_full}` (o mesmo de `GET /api/settings/diagnostics`, que
  continua). `PUT /api/settings` aceita `processing_concurrency` como nome de `concurrency` (os dois juntos precisam
  ser iguais). `BatchOut` traz `listing_state`, `paused_reason` (código de erro que se repetiu) e `paused_until`,
  estes dois só enquanto a pausa do disjuntor vale; o motivo do `needs_attention` vem em `error_code`/`error_message`.
- Interface: faixa de disco (`DiskBanner`), aviso do lote com "Retomar importação" (`BatchNotice`) e dois campos de
  concorrência nas Configurações.

### Espera pelo teto de páginas por hora (visibilidade)
Só visibilidade: o teto (600 páginas por hora, janela deslizante de 1 h), os intervalos, a concorrência e o
backpressure não mudaram.
- Estado: quando o portão segura a próxima página por causa do teto, a API apresenta `listing_state =
  "waiting_rate_limit"` com `rate_limit_resume_at` (ISO UTC previsto), `rate_limit_pages_used` e
  `rate_limit_pages_max`. Não é erro, falha nem pausa: o lote continua ativo (`status = listing`, conta em
  `has_active_work`, mantém o PC acordado), retoma sozinho e não aceita `resume-listing` (409, como `running`).
  No banco a coluna `listing_state` continua `running`, porque a espera acontece dentro do trecho de listagem; o que
  marca a espera são as colunas `batches.rate_limit_resume_at`, `rate_limit_pages_used` e `rate_limit_pages_max`
  (migration 0010, só colunas novas). O presenter só apresenta `waiting_rate_limit` com `status = listing`,
  `listing_state = running` e retomada prevista no futuro; nos demais casos os três campos saem `null`.
- Quem grava: o portão avisa o início e o fim da espera (`InstagramRequestGate.listing_wait_observer`) e o import
  runner grava os campos na entrada e os limpa na saída (`ImportRunner._on_rate_limit_wait`). Uma espera fora da
  thread que lista o lote atual não mexe em lote nenhum.
- Log (`media_tool.imports`, INFO): uma linha na entrada (lote, teto atingido usadas/máximo, espera prevista e
  horário de retomada) e uma na saída (lote, quanto esperou e motivo: janela liberada, lote cancelado, encerramento
  do aplicativo, espera interrompida, ou janela liberada com o Instagram em pausa). Nada é registrado durante a
  espera. Esperas previstas abaixo de 30 s (`WAIT_NOTICE_MIN_SECONDS`, o ritmo normal de quem lista encostado no
  teto) não mudam o estado do lote nem geram log; a espera em si é a mesma.
- Cancelar o lote durante a espera interrompe a espera na hora. Pausa do Instagram publicada durante a espera: a
  espera segue até a janela liberar e a admissão da requisição é recusada como sempre (listagem `paused`, nenhuma
  página pedida).
- Persistência da janela: cada página reservada grava em `app_state` (`instagram_listing_pages`) a lista das reservas
  da última hora em relógio de parede (JSON com no máximo o teto de entradas, cerca de 10 KB com 600; uma escrita por
  página pedida, nunca em leitura). Na inicialização `ImportRunner.restore_rate_limit_wait` reconstrói a janela
  (`restore_listing_pages`): entrada com mais de 1 h é descartada; entrada no futuro ou ilegível conta como página
  usada agora; valor inteiro ilegível conta como janela cheia agora (pelo teto vigente). Em nenhum caso a espera passa de 1 h. Assim o
  mesmo teto vale através de um reinício (antes a contagem recomeçava do zero a cada início do app).
- Reconciliação na inicialização: lote com espera gravada por uma execução anterior é conferido com a janela
  reconstruída. Janela ainda cheia e lote ainda listando: continua em espera com a retomada recalculada. Janela livre
  ou lote que não está mais listando: campos limpos e o lote segue o fluxo normal. No encerramento do app durante a
  espera os campos ficam gravados de propósito, para a interface já abrir com o estado certo.
- Interface: rótulo `Aguardando limite de requisições` (tom normal da coleta) e aviso neutro com o horário de
  retomada, o tempo restante aproximado e as páginas usadas na última hora.

### Nota: página pedida de novo depois do backpressure rígido
Quando o teto de ingests pendentes (`MAX_PENDING_INGEST` ou o global) é atingido no MEIO de uma página, o runner grava
os itens que já vieram e encerra o trecho sem checkpoint dessa página; a retomada pede a mesma página de novo e o
índice único dos jobs descarta os itens repetidos. Custo: 1 requisição extra por ocorrência, sem item duplicado.
Observado no lote real de 5.000 itens: páginas 107 e 142 pedidas duas vezes (307 consultas para 305 páginas). A
parada na fronteira da página (quando a próxima página inteira não caberia) não repete nada.

### Descartes da listagem visíveis no lote
O que o provider descarta ao listar (edge/nó nulo, post sem `code`, imagem/vídeo sem URL utilizável, filho de
carrossel inválido ou faltando) fica em contadores PERSISTENTES do lote (migration 0011, só `ADD COLUMN`):
`listing_dropped_unexpected` (total), `listing_dropped_reasons` (JSON `{"unexpected": {motivo: n}, "expected": {...},
"degraded": {...}}`, só nomes fixos e contagens) e `listing_dropped_partial` (ver abaixo). A API expõe
`listing_dropped: {unexpected_total, reasons}` (docs/CONTRACTS.md) e a interface mostra um aviso de atenção
(`1 mídia não pôde ser adicionada durante a listagem`, com o detalhe por motivo) e o resumo
`... concluídos • 1 mídia descartada durante a listagem`. Regras:
- Descarte NUNCA muda o status do lote (não vira `failed`, não pausa, não gera nova tentativa); os demais downloads
  seguem. Não há retry automático da página nem tentativa de recuperar a mídia.
- Só perda inesperada entra no total. Filtros de propósito (`unsupported_type`, `duplicate`, `content_filter`) ficam
  em `expected`; `rendition_candidates_invalid` (item entregue em versão menor) fica em `degraded`. Nenhum dos dois
  aparece na API nem na interface.
- Unidade: cada motivo conta o que é (post inteiro = 1; filho de carrossel = 1; `carousel_children_missing` = filhos
  que faltaram). Com post inteiro descartado não se sabe quantas mídias ele tinha: a interface diz "pelo menos N".
- **Não duplicar:** o `ListingCheckpoint` de cada página leva `page_counts` (descartes daquela página) e o runner soma
  esses números na MESMA transação que grava os jobs e o checkpoint da página (`_flush`). Página consumida por inteiro
  soma uma única vez. Página consumida em parte (backpressure rígido no meio, queda do app, erro) não
  tem checkpoint e não soma nada em definitivo (fica só o parcial, abaixo); refeita depois, soma uma vez, no checkpoint
  dela. Retomada (`resume-listing`,
  reinício do app, backoff) continua do checkpoint e nunca reconta páginas já somadas. `restart-listing` zera os três
  campos (as páginas serão listadas e contadas de novo).
- **Limite atingido no meio da última página:** essa página nunca ganha checkpoint. Cada mídia entregue leva
  `RemoteMedia.listing_dropped` = descartes inesperados da página até o edge dela, inclusive; a cada gravação sem
  checkpoint o runner guarda o do último item gravado em `listing_dropped_partial` (é ESTADO da página aberta, não
  soma: página refeita sobrescreve, checkpoint apaga). Quando a listagem termina (`_finish_listing`), o parcial é
  somado ao total e apagado na mesma instrução, uma única vez (reinício depois disso não soma de novo). Assim entram
  os descartes ANTERIORES ao último item do lote (perdas reais dentro do recorte pedido) e ficam fora os posteriores
  (não tiraram nada do recorte). Essa é a ÚNICA situação em que um descarte fica de fora: parada pelo limite, descarte
  posterior ao último item gravado.
- **Erro numa página sem checkpoint** (ex.: paginação quebrada depois de entregar os nós, `cursor_missing_with_errors`;
  inclusive página em que NENHUM item foi entregue): o provider anexa ao erro (`exc.listing_dropped`) os descartes
  inesperados da página até o último nó consumido (a página inteira, se foi consumida até o fim) e o runner os grava
  em `listing_dropped_partial`. Enquanto a página puder ser refeita (`needs_attention`, `paused`) eles ficam como
  parcial: refeita com sucesso, o checkpoint dela substitui o parcial (sem duplicar); quebrando de novo, o parcial é
  regravado com o mesmo estado (não acumula). Se a listagem ENCERRA sem refazê-la (`done` com erro, lote `failed`), o
  parcial é somado em definitivo, uma vez.
- **Parcial visível:** a API soma o parcial ao que apresenta (`listing_dropped.unexpected_total` e `reasons`) e diz
  quanto dele ainda é da página aberta em `listing_dropped.pending`. Assim um lote parado em `needs_attention` por
  dias (inclusive com as tentativas automáticas esgotadas) já mostra o descarte na interface. O parcial também
  aparece durante um backpressure rígido no meio da página (some no checkpoint dela, que traz a contagem inteira).
- **Cancelamento:** cancelar o lote soma o parcial da página aberta ao total, uma vez (a página não será refeita; é
  perda real do que já tinha sido listado), na transação do cancelamento; o runner repete a operação ao encerrar o
  trecho cancelado (idempotente: o parcial é apagado na mesma instrução que soma).
- **JSON ilegível** em `listing_dropped_reasons`/`listing_dropped_partial` é tratado como vazio (um WARNING por
  execução); não derruba `GET /api/batches` nem a listagem. O total da coluna inteira continua valendo na API.
- Limitações conhecidas: `expected` e `degraded` só são somados em páginas com checkpoint; se o erro acontecer antes
  de qualquer nó da página ser consumido (ex.: callback `on_page` levantando) só entram os descartes de edge/nó nulo
  quando a página não tinha nenhum nó utilizável.
- Providers sem checkpoint por página (gallery-dl) não informam descartes: os campos ficam zerados.

### Filtros da importação por perfil (visualizações mínimas e período de postagem)
**Estado (2026-10-29):** só o período está ativo. O filtro de visualizações está desligado por um interruptor único
(`import_service.MIN_VIEWS_ENABLED` no backend, `VIEWS_FILTER_ENABLED` no frontend): a API recusa `min_views` com 422
`min_views_unavailable`. As regras abaixo continuam valendo para quando ele for religado.
Opcionais, só na importação por perfil e vários perfis (a importação por link não tem filtros), só no provider web
(docs/CONTRACTS.md, seção 8.1). Desligados, nada muda: o runner nem repassa os argumentos e as colunas novas
(migrations 0017 e 0019, só `ADD COLUMN` anuláveis) ficam NULL. Regras:
- **Onde filtra:** dentro do provider web (`iter_profile_media(..., min_views=N, posted_after=corte)`), depois do
  filtro de conteúdo e ANTES de contar e entregar o item: o provider só entrega o que vira job (a aritmética do
  runner continua valendo; filtrar depois cairia em `LISTING_STALLED`).
- **Período:** `posted_within_days` (1 a 3650) vira o corte `posted_after` (agora - N dias, UTC) UMA vez, na criação
  (grupo: o mesmo corte para todos os filhos), gravado no lote; retomada, reinício do app e `restart-listing` usam
  sempre o gravado. Publicação mais antiga que o corte, ou sem `taken_at`, não vira job (`out_of_period`). A listagem
  vem da mais nova para a mais antiga: a primeira publicação NÃO fixada mais antiga que o corte ENCERRA a listagem
  (checkpoint `has_next=False`, `period_end=True`, na mesma página; nenhuma página seguinte é pedida). Fixada é pulada
  sem encerrar. Sem o campo de fixação no nó (não verificado; docs/INSTAGRAM_WEB_CONTRACT.md) encerra só depois de 4
  publicações seguidas mais antigas que o corte; uma dentro do período zera a sequência; sem data não conta. O lote
  grava `listing_period_end = 1` com o checkpoint, termina com `listing_end = 'profile_end'` (CHECK da 0012) sem a
  mensagem de fim de perfil, e a API mostra `listing_end: "period_end"`.
- **Visualizações:** `play_count` da aba Reels SEM login (docs/INSTAGRAM_WEB_CONTRACT.md), só para vídeos dentro do
  período, com páginas da aba buscadas sob demanda. >= mínimo = `approved`; menor = `below`; fora da aba Reels ou
  sem contagem = `no_count`; foto/carrossel sem vídeo com o filtro ligado = `no_count` (escolha documentada; a API só
  aceita o filtro com `reels`/`videos`). "Sem contagem" nunca é baixado. "Últimos N" conta só aprovados.
- **Requisições da aba Reels:** só com o filtro de visualizações ligado; cada uma passa pelo teto de páginas por hora
  e pelo portão global (pausa publicada -> `InstagramPaused`, a listagem fica `paused`), e respeita o cancelamento.
  Erro da aba sobe como erro da listagem e segue a política existente (429/recusa -> pausa/backoff do lote, sem retry
  imediato); nunca há download sem o filtro. Formato inesperado -> `contract_changed` "Não foi possível ler as
  visualizações do perfil…": sem nenhum job o lote falha com esse motivo; no meio do lote fica `needs_attention`.
  O mapa código -> contagem, o cursor e o LSD vivem só durante o trecho: cada trecho novo relê a aba do começo até o
  ponto necessário (custo extra conhecido em perfis grandes com muitas retomadas).
- **Não duplicar (espelho dos descartes):** o `ListingCheckpoint` de cada página leva `page_views`
  ({approved, below, no_count, out_of_period}) e o runner soma em `listing_views` na MESMA transação do checkpoint;
  sem checkpoint, o estado da página aberta fica em `listing_views_partial` (página refeita sobrescreve, checkpoint
  apaga; fim da listagem e cancelamento somam uma vez). Erro numa página sem checkpoint: `exc.listing_views`.
  `restart-listing` zera `listing_views`, o parcial e `listing_period_end` (o corte não muda).
- **Duplicatas:** o filtro roda antes do dedup; aprovado que já está na Biblioteca vira job duplicado (conta em
  `approved`, não em `progress.downloaded`).
- **API:** `views_filter = {min_views, posted_within_days, posted_after, analyzed, approved, below, no_count,
  out_of_period, pending}` (`analyzed` = soma dos quatro). No grupo, soma dos filhos.
- **Logs:** só totais (`views_approved=… views_below=… views_no_count=… out_of_period=… reels_pages=…` por trecho e
  `filtros: N analisados, …` no fim). Nenhuma contagem individual, código, URL, cursor ou token.

## 10. Download-only é o padrão; processamento é opcional e separado

O Media Tool é usado principalmente para BAIXAR os originais. O processamento por FFmpeg/presets continua disponível,
mas nunca acontece sozinho depois do download.

- API: `POST /api/imports {profile, content_type, limit, preset_id?, process?}`. `process` é opcional com padrão
  `false` e `preset_id` é opcional (`null`). `process: true` continua exigindo preset (o informado, ou o preset
  padrão quando `preset_id` é `null`; 404 `preset_not_found` se o informado não existe). Nenhuma configuração
  persistida liga processamento automático; `batches.process` já nasce 0 (sem migration nova, lotes antigos intactos).
- Job de ingest download-only (`jobs.params` NULL): `pending -> downloading -> completed` logo depois de original
  salvo + validação por assinatura + SHA-256 + registro em `media` (sem miniatura, sem metadados técnicos; exceção: vídeo de lote por link juntado de fluxos separados, ver abaixo). Não passa por `downloaded`/`processing`, não
  cria job `kind = process` nem rendition, e é concluído pelo pool de download. Interrompido (encerramento, disco,
  nova tentativa) depois de registrar o original, volta a `pending` (não a `downloaded`) e o pool de download o
  conclui sem baixar de novo. Linhas antigas download-only que estejam em `downloaded` continuam sendo concluídas
  pelo pool de processamento, sem FFmpeg.
- Backpressure: o lote download-only obedece só ao de download (100 ingests pendentes por lote, 200 no app). O
  backpressure de processamento (por lote e teto global) conta apenas ingests com `params` e não segura a listagem
  nem o claim de download de um lote download-only, mesmo com a fila do FFmpeg cheia por outro lote.
- Disco: sem job aberto que vá usar o FFmpeg (`kind = process` ou ingest com `params`), o mínimo exigido é reserva +
  2 x max_download_bytes x download_concurrency; a parcela das saídas do FFmpeg (2 x max_download_bytes x
  processing_concurrency) só entra enquanto houver processamento em aberto (`JobRepository.has_processing_work`).
- Dois níveis separados. (1) Download puro (`params` NULL, o padrão): ZERO ffprobe e ZERO FFmpeg, mesmo com as
  ferramentas instaladas. ÚNICA exceção (seção 0.2, "Fluxos separados"): vídeo de lote por link que vem em dois
  arquivos (vídeo e áudio) usa o FFmpeg para juntá-los sem recodificar (etapa `muxing`) e o ffprobe para conferir a
  junção; essa mídia é registrada COM `width`/`height`/`duration`/`fps`/`has_audio`/`video_codec` (continua sem
  miniatura). Sem as ferramentas ela é baixada pelo MP4 direto, nas regras do download puro. A mídia é validada só pela assinatura do arquivo (`has_media_signature`: JPEG, PNG, GIF,
  WebP, MP4/MOV/HEIC, WebM; para ISO BMFF os 4 primeiros bytes precisam ser um tamanho de caixa plausível, então
  texto com "free"/"skip" nos bytes 4-8 é rejeitado), recebe SHA-256 e é registrada sem miniatura e sem
  `width`/`height`/`duration`/`fps`/`has_audio` (nulos; a API devolve `thumbnail_url: null` e o endpoint de miniatura
  responde 404). Arquivo vazio, HTML, JSON ou texto continuam `invalid_media`. A ausência do ffprobe é esperada
  nesse modo e não gera aviso de log. `media_type` vem do job e a extensão é normalizada (vídeo `.mp4`, imagem
  `.jpg`/`.png`/`.webp`). (2) Análise/processamento opcional: ffprobe (metadados técnicos), miniatura, FFmpeg,
  presets/Anti-duplicidade. O lote COM processamento valida por ffprobe e gera a miniatura no ingest como antes
  (sem ffprobe, avisa e cai na assinatura). Ao processar uma mídia baixada em download puro (`_ensure_metadata`), o
  job preenche os metadados técnicos que faltam e gera a miniatura; sem ffprobe/FFmpeg nesse momento o job de
  processamento falha como já falhava (`ffmpeg_not_found`), sem falha nova.
- Processar depois: `POST /api/media/process-by-filter` (idempotente por preset/params), `POST /api/media/process` e
  `POST /api/media/{id}/process` funcionam sobre mídias baixadas em download-only. Lotes antigos com processamento e
  suas renditions continuam lendo e exibindo igual.
- Testes: `backend/tests/test_download_only.py`; escala offline de 1.000 e 10.000 itens em
  `backend/tests/scale/test_scale.py` (cenário `download_only`, marcado `slow`).

## 11. Importação unificada: capas, máximo de visualizações e contadores (migration 0021)

- Capas: lote "Somente capas" (`batches.media_role = 'cover'`) lista como "Somente vídeos" e cria jobs de capa
  (`jobs.media_role = 'cover'`, `media_type = 'image'`, `media_url` = a capa). Dedup por origem inclui o papel; um job
  de capa só espera (`duplicate_in_flight`) outro job de capa. Renovação de URL expirada: o mecanismo existente
  (`refresh_page`/`resolve_post`) grava a capa renovada. Capa nunca é processada nem editada.
- Filtro de visualizações religado, com mínimo e/ou máximo gravados no lote e no grupo (`min_views`, `max_views`).
  Cada trecho da listagem (retomada, reinício do app, "retomar listagem") lê os valores GRAVADOS. Vereditos por página
  no mesmo JSON da 0017 (`above` só nos lotes com máximo). "Sem contagem" nunca é baixado com o filtro ligado. O portão
  de requisições, o teto por hora, a pausa em 429 e o tratamento de formato inesperado do `reels_tab.py`/`provider.py`
  não mudaram.
- Capa maior (lote por perfil): o job de capa lê a publicação UMA vez antes de baixar (`_largest_cover`, pelo portão
  e teto) e usa a capa dela; erro/restrição/pausa: capa da listagem e log sem URL; erro do Instagram aplica a pausa de
  sempre, sem repetir. Lote por link não faz requisição extra. Imagens em download puro ganham só largura/altura por
  um ffprobe opcional (exceção à seção 10; nunca miniatura nem FFmpeg).
- Primeira página vazia (migration 0022): 1a página com 0 publicações e sem próxima encerra o lote como antes, mas com
  `listing_empty_first_page = 1` (API: `listing_end = "empty_first_page"`) e mensagem própria no lote, não como fim do
  perfil. A linha de log "filtros:" usa os mesmos totais do `views_filter` da API (inclui "acima do máximo").
- Contadores: `progress.duplicates` (mídias já existentes, por origem ou sha256; congelado em `final_duplicates`, NULL
  = 0 nos lotes encerrados antes da 0021) e `progress.filtered` (abaixo, acima, sem contagem, fora do período e itens
  de outro tipo, `expected.content_filter`, contados por página na listagem e por publicação nos links).
