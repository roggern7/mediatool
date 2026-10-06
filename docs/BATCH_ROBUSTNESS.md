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
- **Tipos**: `all`, `reels`, `photos_carousels` (novo: fotos simples + TODOS os itens de carrosséis, inclusive
  vídeo de carrossel; exclui Reels e vídeo avulso que não é Reel) e os antigos `posts` (= `all` no provider web),
  `videos`, `images`. Em `reels` item de carrossel nunca entra. O filtro acontece na normalização, antes de criar
  jobs; o que ele ignora conta em `expected.content_filter`, fora de `listing_dropped.unexpected_total`.
  Carrossel conta por ITEM; se o limite cai no meio de um carrossel, o corte é por mídia (os primeiros itens entram).
  **Vídeo avulso que não é Reel** (legado: sem `product_type` `clips`, fora de carrossel) entra SÓ em `all` (e no
  antigo `videos`); em `reels` e `photos_carousels` é ignorado e contado como `content_filter` (esperado), nunca
  como descarte. Em qual filtro esse caso deve cair está em aberto com o dono.
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

## 10. Download-only é o padrão; processamento é opcional e separado

O Media Tool é usado principalmente para BAIXAR os originais. O processamento por FFmpeg/presets continua disponível,
mas nunca acontece sozinho depois do download.

- API: `POST /api/imports {profile, content_type, limit, preset_id?, process?}`. `process` é opcional com padrão
  `false` e `preset_id` é opcional (`null`). `process: true` continua exigindo preset (o informado, ou o preset
  padrão quando `preset_id` é `null`; 404 `preset_not_found` se o informado não existe). Nenhuma configuração
  persistida liga processamento automático; `batches.process` já nasce 0 (sem migration nova, lotes antigos intactos).
- Job de ingest download-only (`jobs.params` NULL): `pending -> downloading -> completed` logo depois de original
  salvo + validação por assinatura + SHA-256 + registro em `media` (sem miniatura, sem metadados técnicos). Não passa por `downloaded`/`processing`, não
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
  ferramentas instaladas. A mídia é validada só pela assinatura do arquivo (`has_media_signature`: JPEG, PNG, GIF,
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
