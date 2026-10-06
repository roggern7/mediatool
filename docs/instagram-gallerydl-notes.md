# Instagram via gallery-dl: notas de investigação (gallery-dl 1.32.14)

Fonte: leitura de `gallery-dl/gallery_dl/extractor/instagram.py`, `extractor/common.py`, `cookies.py`,
`job.py`, `cache.py` e `exception.py`, mais os testes offline do adapter. Nenhum segredo neste arquivo.

## Cookies realmente necessários

- O extractor só verifica `sessionid` (`cookies_names = ("sessionid",)`, domínio `.instagram.com`). Sem ele,
  `login()` apenas marca `_logged_in = False` e segue sem erro; as chamadas que exigem login falham depois.
- `csrftoken`: em `_init()` o gallery-dl gera um token aleatório, grava no cookie `csrftoken` (sobrescrevendo o
  carregado da config) e envia o mesmo valor em `X-CSRFToken`. O runner (`_use_session_csrftoken`), quando o
  `csrftoken` da sessão é informado, volta a usá-lo no cookie e no header, coerente com a sessão do navegador.
- `ds_user_id`, `mid`, `ig_did`, `rur`: o gallery-dl não exige. O dict de config é carregado inteiro no domínio
  `.instagram.com` (`cookies_update_dict`), então eles são enviados. O adapter repassa se existirem e usa
  `ds_user_id` (ou o prefixo numérico do `sessionid`, antes de `%3A`) só para o teste de sessão.
- O valor do `sessionid` deve ser enviado como está no navegador (com `%3A`, não decodificado).

## O que significa `GET /api/v1/feed/user/... -> HTTP 302 -> /`

> SUPERADA (2026-10-03): a leitura abaixo ("pedido tratado como deslogado", "sessão invalidada") foi
> escrita antes das sondagens e da evidência do navegador. O 302 para `/` sem apagar o `sessionid` prova só que a
> chamada `feed/user` foi recusada para este cliente. Ver "Reinterpretação após evidência do navegador".

`InstagramExtractor.request()` inspeciona redirects: URL final com `/accounts/login/` vira "login page",
com `/challenge/` vira "challenge page" e `https://www.instagram.com/` (comprimento 25 a 27, termina em `/`)
vira "home page"; todos levantam `AbortExtraction("HTTP redirect to <x> page (...)")`.
A leitura mais provável é que o Instagram tratou o pedido ao feed como **deslogado** e redirecionou em vez de
devolver JSON. O id do usuário já tinha sido resolvido antes (estratégias `search` e `web` do gallery-dl), por
isso o erro aparece só no feed. (Inferência pelo código e pelo comportamento conhecido; não reproduzido aqui.)

Causas possíveis, em ordem de probabilidade para o teste do dono (cookies do Brave no Windows):

1. **O `sessionid` não foi enviado.** Navegadores Chromium recentes no Windows (Chrome 127+, Brave
   equivalente) cifram cookies com App-Bound Encryption (prefixo `v20`). O `WindowsChromiumCookieDecryptor`
   do gallery-dl 1.32.14 só conhece `v10`; qualquer outro prefixo vai para DPAPI, que falha e devolve `None`,
   e o `.decode()` seguinte lança `AttributeError`. `cookies_load` captura a exceção, registra
   `cookies: 'NoneType' object has no attribute 'decode'` como aviso e **nenhum cookie é carregado**.
   Com o Brave aberto, o banco `Cookies` também pode estar bloqueado para cópia. O resultado é uma
   execução anônima que termina no 302 para `/`. (Conclusão por leitura de código; não reproduzido com o
   navegador do dono, de propósito.)
2. **Sessão invalidada**: logout no navegador, troca de senha ou invalidação pelo Instagram por uso em outro
   cliente/IP. O sintoma é o mesmo (302 para `/` ou para `/accounts/login/`).
3. **User-Agent**: não explica o 302. O gallery-dl usa UA de Chrome no Windows (`util.USERAGENT_CHROME`);
   UA não-Chrome só gera o aviso de qualidade de vídeo reduzida.

Por isso o adapter não lê cookies de navegador: o dono informa o `sessionid` localmente
(arquivo protegido por DPAPI ou `INSTAGRAM_SESSIONID`) e o runner o passa ao gallery-dl como **dict de config**
(`extractor.instagram.cookies`). O runner chama `config.clear()` e nunca `config.load()`, então um
`%APPDATA%\gallery-dl\config.json` do usuário apontando `"cookies": ["brave"]` não interfere.

## Endpoints usados (API REST, `api: "rest"`, padrão)

| Operação do runner | Endpoint | Observação |
|---|---|---|
| `profile` / início de `list` | `GET /web/search/topsearch/?query=` (`user_by_search`); se 200 sem o usuário, `GET /<user>` como documento HTML (`user_by_web`, extrai `profile_id`); último recurso `GET /api/v1/users/web_profile_info/` | mesma ordem do `user-strategy` padrão do gallery-dl 1.32.14 (`["search", "web"]`; o upstream deixou `web_profile_info` por causa do 429, CHANGELOG #8978). Os métodos do upstream convertem qualquer falha em `NotFoundError`; o runner registra a 1a falha HTTP/redirect e a relança (429, login, home, 404 classificados na hora, sem tentar a estratégia seguinte) |
| `list` com `posts`, `videos`, `images`, `all` | `GET /api/v1/feed/user/<id>/` (`count=30`, paginação `max_id`) | URL interna `https://www.instagram.com/id:<id>/posts/` evita nova busca do id |
| `list` com `reels` | `POST /api/v1/clips/user/` (`page_size=50`) | `InstagramReelsExtractor` |
| `resolve_post` | `GET /api/v1/media/<media_pk>/info/` | `InstagramPostExtractor` com `/p/<shortcode>/` |
| `test_session` | `GET /api/v1/feed/user/<ds_user_id>/?count=1` (feed da própria conta; sem `ds_user_id`, prefixo numérico do `sessionid`) | a 1a chamada do download que exige login; UMA requisição, sem seguir redirect, sem nova tentativa, sem fallback; username do JSON (`user` ou 1o item), se houver |

`api: "graphql"` usa `query_hash` antigos e não suporta várias operações; não é usado.

## Opções de config aplicadas pelo runner

- `cookies`: dict (nunca arquivo, nunca argv). `cookies-update: false` (nada é gravado de volta).
- `cache.file: ":memory:"`: o cache de usuários do gallery-dl não vai para `%APPDATA%\gallery-dl\cache.sqlite3`.
- `videos: "merged"`: URL MP4 progressiva (vídeo com áudio). Com o padrão (`true`), vídeos com manifesto DASH
  viram `ytdl:<post_url>` e exigiriam yt-dlp.
- `previews: false`, `audio: false`: sem arquivos extras de prévia/música.
- `retries` e `sleep-429` por operação: `list` 2 tentativas extras, `profile` e `resolve_post` 1; em 429 o
  gallery-dl espera `sleep-429 = 30 s` (o padrão dele, `request_interval_429`, é 60 s). Depois o runner devolve
  `rate_limited` e a fila aplica o próprio backoff.
- `test_session`: `retries: 0`, `sleep-429: 0` (não dorme em 429), `sleep-request: 1` (a única requisição de um
  processo novo não espera), `timeout: 15` s.
- `sleep-request`: **padrão do extractor mantido** (`request_interval = (6.0, 12.0)` s entre requisições,
  compartilhado na classe). O runner só aceita aumentar esse intervalo, nunca reduzir.
- `include` só vale para o `InstagramUserExtractor` (dispatch para posts/reels/tagged/stories...); o adapter
  chama os subextractors diretamente.

## Metadados por arquivo (mensagem `Url`)

Do post: `post_id`, `post_shortcode`, `post_url` (`/reel/<code>/` quando o post tem um único vídeo, senão
`/p/<code>/`), `type` (`reel`/`post`), `username`, `fullname`, `owner_id`, `date`/`post_date` (datetime UTC),
`description`, `tags`, `likes`, `pinned`, `count`, `sidecar_media_id`/`sidecar_shortcode` em carrosséis.
Do arquivo: `num` (1-based), `media_id`, `shortcode` (do item), `display_url` (imagem ou capa do vídeo),
`video_url` (ou `None`), `width`, `height`, `width_original`, `height_original`, `extension`, `tagged_users`.
**Não vêm** no kwdict: `video_duration` e `product_type`. O runner envolve `_parse_post_rest` da instância
(sem alterar o upstream) para copiá-los do JSON bruto.

Mapeamento para `RemoteMedia`: `source_post_id = post_shortcode`, `source_media_index = num - 1`,
`media_type = video` se `video_url`, `media_url = video_url` ou `display_url`, `thumbnail_url = display_url`
só para vídeo, `is_reel` = vídeo com `product_type == "clips"` ou vindo do endpoint de reels do perfil
(vídeo único de feed não é reel, mesmo com `type == "reel"` do gallery-dl), `source_url` =
`/reel/<code>/` para reels e `/p/<code>/` para o resto (o `post_url` do gallery-dl é ignorado),
`taken_at` = `date` em ISO 8601 com `Z` (`dt.NONE`, sem data, vira `null`).

No processo pai, `timeout` (padrão 300 s) é o tempo máximo **sem nenhuma linha** do runner (stdout ou stderr);
o tempo que o consumidor leva para processar os itens não conta. `max_total_time` é um teto total opcional
(padrão: nenhum na listagem; 600 s em `resolve_post`). Operações usadas por endpoints síncronos têm teto curto:
`test_timeout` (padrão 30 s) no teste de sessão, que vira `result: timeout`, e `profile_timeout` (padrão 45 s)
em `get_profile`, que vira `InstagramError("timeout")` (504 na prévia). Em qualquer teto o
filho é morto (`kill` + `wait`). Linhas do stdout acima de 2 MB são descartadas.

## Classificação de erros (runner)

| Sinal do gallery-dl / Instagram | Código |
|---|---|
| sem `sessionid` no pedido | `session_missing` (o adapter recusa antes de iniciar o runner) |
| `AbortExtraction` redirect para login | `session_expired` (sem sessão: `session_missing`) |
| `AbortExtraction` redirect para home (`/`) | `access_refused` (não retryable; prova só que a chamada REST do gallery-dl foi recusada, não sessão inválida nem restrição da conta; sem pausa; sem sessão: `session_missing`) |
| `AbortExtraction` redirect para challenge, corpo com `checkpoint_required`/`challenge_required` | `checkpoint_required` |
| HTTP 429 | `rate_limited` |
| `require_login: true` ou `login_required` (mesmo com "Please wait a few minutes", que é o 401 de quem não está logado) | `session_invalid` (sem sessão: `session_missing`) |
| `feedback_required`, `"spam": true`, `restriction_enrollment_data` ou chaves `feedback_*` sem `require_login` (aviso de restrição nesta chamada) | `action_blocked` (não retryable; pausa protetora do Media Tool de 1 h, liberável em Configurações) |
| "Please wait a few minutes" sem `require_login` | `rate_limited` |
| HTTP 401/403, `AuthRequired`, resposta não JSON | `session_invalid` |
| HTTP 404 / `NotFoundError` / `web_profile_info` sem usuário | `profile_not_found` (em `resolve_post`: `content_unavailable`) |
| perfil `is_private` com `followed_by_viewer` falso ou `friendship_status.following` falso (e não é a própria conta) | `profile_private` antes de listar; sem nenhum dos dois campos a listagem tenta (o upstream só avisa) e o erro real é classificado |
| erro de conexão/timeout, HTTP 5xx | `network_error` |
| estrutura inesperada | `internal_error` |

## Teste de sessão: resultado estruturado

`SessionTestResult` tem `ok`, `code` (códigos antigos), `message` (pt-BR), `username`, `result`, `http_status`,
`duration_ms`, `detail`, `session_recognized` e `listing_api`. O runner emite uma linha
`{"type": "session_test", result, code, message, username, http_status, detail, session_recognized, listing_api}`.
O teste separa dois fatos: `session_recognized` (`true` = a resposta não encerrou a sessão; `false` = Set-Cookie
apagou/expirou o `sessionid`, redirect para login ou `require_login`/401/403; `null` = rede/timeout/5xx) e
`listing_api` (`ok` = 200 com items; `refused` = redirect que não é login ou 400/403 com campos de restrição;
`rate_limited` = 429; `unknown`). A deleção do `sessionid` é detectada no Set-Cookie só pelo nome do cookie e pelos
atributos (`Max-Age<=0`, `Expires` no passado, valor vazio ou `""`); o valor nunca é registrado. `detail` só em redirect
(`redirect_home`, `redirect_login`, `redirect_challenge`, `redirect_checkpoint`, `redirect_consent`, `redirect_other`;
3xx sem `Location` = `redirect_other`).

| Resposta (pedido sem seguir redirect) | `result` | `code` |
|---|---|---|
| 200 com `items` (lista) ou `user.username` (verificado antes de tudo; a bio pode conter qualquer texto) | `valid` | `ok` |
| 3xx para `/challenge/` ou `/checkpoint/`; `checkpoint_required`/`challenge_required`/`checkpoint_url` | `checkpoint_required` | `checkpoint_required` |
| 3xx para `/accounts/login/` | `expired` | `session_expired` |
| Set-Cookie apagando/expirando o `sessionid` (fora login/checkpoint/429) | `invalid` | `session_invalid` |
| 3xx para `/` sem apagar o `sessionid` | `api_refused` (`detail` `redirect_home`; "Sessão reconhecida; listagem recusada") | `access_refused` |
| 3xx para consentimento, outra página ou sem `Location`, sem apagar o `sessionid` | `api_refused` (`detail` `redirect_consent`/`redirect_other`) | `access_refused` |
| `require_login: true` ou `login_required` (exceto 429); 401/403 | `invalid` | `session_invalid` |
| `feedback_required`, `spam: true`, `restriction_enrollment_data` ou chaves `feedback_*` de topo **sem** `require_login` (exceto 429) | `restricted` (aviso de restrição nesta chamada) | `action_blocked` |
| HTTP 429; "Please wait a few minutes" **sem** `require_login` | `rate_limited` | `rate_limited` |
| erro de DNS, conexão ou SSL | `network_error` | `network_error` |
| timeout HTTP (15 s) ou teto total do pai (30 s) | `timeout` | `network_error` |
| 5xx, 404, JSON sem usuário, não JSON | `instagram_error` | `internal_error` |
| sem sessão salva | `session_missing` | `session_missing` |

Critério (decisão do orquestrador): `expired` só para redirect para `/accounts/login/`. `require_login` e 401/403
viram `invalid`/`session_invalid`. Redirect para a página inicial sem apagar o `sessionid` NÃO é sessão inválida
nem restrição da conta: no teste vira `api_refused` com `detail=redirect_home` e nas outras operações vira
`access_refused` (não repetível, sem pausa). Ver "Reinterpretação após evidência do navegador". Os
marcadores só são procurados nos campos estruturados do JSON (`status`, `message`, `error_type`,
`checkpoint_url`, `require_login`, `spam`), ou no texto quando o corpo não é JSON. Atenção: o 401 de quem não
está logado traz a frase "Please wait a few minutes before you try again." junto com `require_login: true`. Por
isso `require_login` tem precedência sobre a frase, que sozinha não significa rate limit. Não há fallback (o antigo
`users/<id>/info/` dava 429; ver a sondagem de 2026-10-03 01:07).

## Investigação real com sessão (2026-10-03)

Sessão salva pelo dono às 00:03:53 (horário local). O teste da UI terminou como `rate_limited` às 00:06:00,
depois que o frontend já tinha desistido aos 90 s.

Causa mecânica: o teste usava `GET /api/v1/users/<id>/info/` (id tirado do prefixo do `sessionid`), com
`retries: 2` e a espera padrão de 60 s em 429. Duas esperas de 60 s dão cerca de 2 min até o `rate_limited`.

Arquivo de sessão: só a chave `sessionid` (sem `ds_user_id`, `csrftoken`, `mid`, `ig_did`, `rur`). O formato bate
com o de um navegador: prefixo numérico e 4 segmentos separados por `%3A`, sem espaços ou aspas.

Diagnóstico in-process, uma requisição por vez, sem seguir redirect, `retries 0`, `sleep-429 0`, timeout 15 s,
com os mesmos headers do `InstagramRestAPI._call`. Total de 6 requisições, espaçadas de 20 s ou mais, com pausa
de mais de 3 min depois de cada 429:

| # | Hora | Endpoint | Cookie | Resposta |
|---|---|---|---|---|
| 1 | 00:09:14 | `current_user/?edit=true` | sessão real | **302 → `https://www.instagram.com/`**, 0,8 s; cookie não apagado |
| 2 | 00:09:44 | `users/<id próprio>/info/` | sessão real | **429**, 0,5 s, corpo não JSON |
| 3 | 00:13:09 | `current_user/?edit=true` | nenhum | 401 JSON `status: fail`, `require_login: true`, `message: "Please wait a few minutes before you try again."` |
| 4 | 00:13:54 | `current_user/?edit=true` | `sessionid` FALSO | 401 JSON igual ao #3; servidor **apagou** o cookie `sessionid` |
| 5 | 00:14:26 | `web_profile_info/?username=instagram` | sessão real | **429**, 0,3 s, `text/plain`, corpo vazio |
| 6 | 00:17:54 | `users/<id próprio>/info/` | `sessionid` FALSO | 302 → `/`; servidor apagou o cookie |

Conclusões:

- Nenhum redirect para `/accounts/login/` ou `/challenge/` e nenhum `checkpoint_required`/`require_login` com a
  sessão real.
- O Instagram **reconhece** o `sessionid` do dono: responde diferente do anônimo e do falso, e não apaga o
  cookie. Mas **não serve a API** para ele: `current_user` redireciona para a página inicial, e
  `users/<id>/info/` e `web_profile_info` dão 429 imediato, de corpo vazio.
- O 429 do teste original **não** é a resposta de sessão inválida: um `sessionid` desconhecido recebe
  302/401 e tem o cookie apagado, não 429. É um limite real, ligado a esta sessão ou a este IP. Já houve 429 no
  `web_profile_info` anônimo em 2026-10-02, então o IP também está limitado nesse endpoint.
- Não foi possível confirmar se o 302 para `/` em `current_user` com sessão reconhecida é bloqueio temporário
  da conta, aviso pendente na conta (consentimento ou "foi você?") ou exigência de cookies de dispositivo
  (`mid`/`ig_did`/`datr`) da mesma sessão do navegador. O gallery-dl 1.32.14 exige apenas `sessionid` e gera o
  `csrftoken` sozinho. Não houve teste com os outros cookies, porque eles não estão no arquivo e cookies do
  navegador não são usados. Na época o teste classificava esse caso como `invalid` (SUPERADO: ver
  "Reinterpretação após evidência do navegador").
- Teste real depois da correção (`GalleryDlInstagramDownloader.test_session()`, 00:21:51): `result: invalid`,
  `code: session_invalid`, `http_status: 302`, `username: null`, `duration_ms: 840`.

## Diagnóstico sem rede: inspect_request (2026-10-03, sessão real com cookies extras)

Operação `inspect_request` do runner: monta as requisições do teste de sessão e da 1a chamada da listagem,
captura a PreparedRequest num adapter falso (nada é enviado) e emite só nomes e headers não sensíveis.
Com a sessão real (sessionid + ds_user_id, csrftoken, mid, ig_did salvos pelo dono):

- As duas requisições levam os 5 cookies no header Cookie; no jar todos têm domínio `.instagram.com` e path `/`;
  `X-CSRFToken` == cookie `csrftoken` (o da sessão, não o gerado). gallery-dl 1.32.14.
- Teste: `GET /api/v1/accounts/current_user/` (query `edit`), `allow_redirects=False`. Listagem:
  `GET /api/v1/users/web_profile_info/` (query `username`), `allow_redirects=True`.
- Headers: UA Chrome 153 do gallery-dl (`util.USERAGENT_CHROME`), `Accept: */*`, `Accept-Language: en-US,en;q=0.5`,
  `Referer: https://www.instagram.com/`, `X-IG-App-ID: 936619743392459`, `X-ASBD-ID: 129477`, `X-IG-WWW-Claim: 0`,
  `X-Requested-With: XMLHttpRequest`, `Sec-Fetch-Dest/Mode/Site: empty/cors/same-origin`. Não há `Origin` nem
  client hints `sec-ch-ua*`. Diferenças em relação ao navegador onde a sessão foi criada (UA, client hints,
  `X-IG-WWW-Claim`) são hipóteses ainda não testadas para o 302 para `/`.

(Histórico, SUPERADO pela reinterpretação: hoje 302 para `/` sem apagar a sessão dá `api_refused`.) Teste de sessão: redirect para `/`, consentimento ou outra página era `instagram_error` com
`detail` = `redirect_home`/`redirect_consent`/`redirect_other`, sem fallback, até a investigação concluir.
`redirect_login` = `expired`; `redirect_challenge`/`redirect_checkpoint` = `checkpoint_required`.
Operação `probe` (preparada, não executada): UMA requisição GET/POST a `/api/v1/...`, sem redirect, sem nova
tentativa, sem dormir em 429; emite status, Location sem query, tipo de redirect, tamanho do corpo, content-type e
chaves de topo do JSON.

## Sondagem real do fluxo do upstream (2026-10-03, 01:07-01:08, sessão real com 5 cookies)

Script de diagnóstico fora do repositório, in-process, com a config do runner (`retries 0`, `sleep-429 0`,
timeout 15 s), instrumentando `session.send`. Teto de 5 requisições ao Instagram, intervalo mínimo de 20 s entre
elas e parada em qualquer 429. Nenhum 429.

| # | Hora | Requisição | Status | Location / observação |
|---|---|---|---|---|
| R1 | 01:07:28 | `GET /web/search/topsearch/` (`query=instagram`), headers de `_call`, sem seguir redirect | **200** JSON, 8 KB | `users` com 5 itens; `instagram` presente com `pk` e `id`; resposta traz `x-ig-set-www-claim` e Set-Cookie `csrftoken`, `ds_user_id`, `rur` |
| R2 | 01:07:50 | `GET /instagram/` como documento (headers de `_user_by_web_impl`), sem seguir redirect | **200** HTML, 147 KB | **sem** `"profile_id"` no corpo (o `user_by_web` do upstream falharia para um perfil que existe) |
| R3a | 01:08:11 | Job real `https://www.instagram.com/instagram/posts/`: `GET /web/search/topsearch/` | **200** JSON | user-strategy `["search", "web"]`; id resolvido na 1a estratégia |
| R3b | 01:08:33 | `GET /api/v1/feed/user/25025320/` (`count=30`), com o `X-IG-WWW-Claim` e o `csrftoken` devolvidos pelo R3a | **3xx → `/`** | código exato não registrado (o requests seguiu o redirect) |
| R3c | 01:08:33 | redirect seguido automaticamente: `GET /` | 200 HTML | o gallery-dl levantou `AbortExtraction("HTTP redirect to home page")` → `session_invalid`; nenhum item, nada baixado |

R3c foi o salto automático do redirect do R3b, no mesmo segundo, não uma requisição espaçada.

Conclusões:

- Os 429 vinham dos endpoints que o runner chamava fora do fluxo do upstream (`web_profile_info`,
  `users/<id>/info/`). O `topsearch`, usado pelo gallery-dl, responde 200 com a mesma sessão e o mesmo IP.
- O 302 para `/` do antigo teste (`current_user`) **não** era falso negativo: a primeira chamada do download que
  exige login (`feed/user`) também é redirecionada para a página inicial, mesmo para um perfil público e com o
  claim e o csrftoken renovados pelo servidor. Endpoints `/web/...` e o HTML respondem, mas a API `/api/v1/`
  recusa esta sessão neste cliente. A causa (bloqueio da conta para a API, impressão digital do cliente como UA e
  client hints, ou outra) **não foi determinada**.
- Por isso o teste de sessão passou a ser o `feed/user` da própria conta (resultado igual ao do download) e a
  resolução de perfil segue o upstream, com `web_profile_info` só como último recurso.
- Como o R2 mostrou HTML sem `profile_id` para um perfil existente, "HTML sem `profile_id`" não basta para
  `profile_not_found`. Nesse caso o runner tenta `web_profile_info` como último recurso: se ele vier sem usuário,
  o resultado é `profile_not_found`; um 404 de qualquer estratégia também dá `profile_not_found`.

## Sondagem R4: impressão digital do cliente (2026-10-03, 01:16)

Mesmas regras (fora do repositório, `retries 0`, `sleep-429 0`, sem seguir redirect, teto de 2 requisições,
intervalo mínimo de 30 s). Alvo: `GET /api/v1/feed/user/<id próprio>/?count=1`, o mesmo pedido do teste de sessão.

| # | Hora | Config | Headers enviados (nomes) | Status | Location | Set-Cookie |
|---|---|---|---|---|---|---|
| R4a | 01:16:02 | `browser: "chrome"` (cifras Chromium + headers de navegador do `_init_session`) | UA Chrome 138, `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`, `Upgrade-Insecure-Requests`, mais todos os de `_call` (`X-IG-App-ID`, `X-ASBD-ID`, `X-CSRFToken`, `X-IG-WWW-Claim`, `X-Requested-With`, `Sec-Fetch-*`), `Cookie` | **302** (0,5 s) | `/` | só `rur`; `sessionid` não apagado |
| R4b | 01:16:33 | padrão do runner (sem `browser`) | UA Chrome 153, sem `sec-ch-ua*`, mais os de `_call` e `Cookie` | **302** | `/` | só `rur`; `sessionid` não apagado |

As duas respostas são iguais: corpo vazio, `text/html`, headers `x-ig-*` (`x-ig-set-www-claim`, `x-ig-push-state`,
`x-ig-origin-region`...) e nenhum `x-ig-set-*` além do claim. Conclusão: a impressão digital do cliente (TLS do
Chromium, client hints, UA) **não** muda a recusa. A API `/api/v1/` continua redirecionando esta sessão para a
página inicial, sem apagar o cookie, então o servidor não trata a sessão como desconhecida. O runner não foi
alterado (sem `browser`).

## Sondagem R5: GraphQL do upstream (2026-10-03, 01:17:44)

Uma requisição, mesmas regras. `api: "graphql"`, `InstagramGraphqlAPI._call` com o `query_hash` de `user_feed`
(`69cba403...`), `variables` `{"id": <id próprio>, "first": 1}`, headers oficiais (`X-Instagram-AJAX`,
`X-ASBD-ID 198387`, `X-IG-App-ID`, `X-CSRFToken`, `X-IG-WWW-Claim`, `X-Requested-With`), sem seguir redirect.

| # | Hora | Endpoint | Status | Location | Content-Type | Chaves de topo do JSON |
|---|---|---|---|---|---|---|
| R5 | 01:17:44 | `GET /graphql/query/` (`query_hash`, `variables`) | **400** (0,5 s, 363 bytes) | nenhum | `application/json` | `feedback_action`, `feedback_appeal_label`, `feedback_message`, `feedback_title`, `feedback_url`, `message`, `restriction_enrollment_data`, `spam`, `status` (`status: fail`, sem `require_login`, sem `data`) |

Set-Cookie: `csrftoken`, `rur`. Nenhum redirect e nenhum `require_login`. O formato é o da resposta de **bloqueio de
ação/restrição** (`feedback_required`/"Tente novamente mais tarde", com link de contestação e dados de
restrição), não de sessão inválida nem de 429. Os valores não foram registrados, de propósito, então o tipo exato
da restrição não está confirmado. A leitura feita na época (conta restrita para acesso automatizado) foi revista:
ver "Reinterpretação após evidência do navegador". Este 400 prova só que a chamada GraphQL **legada** do gallery-dl
(`GET /graphql/query/?query_hash=...`) foi recusada. O runner não foi alterado.

### Conclusão (R4 + R5) e mudança aplicada (SUPERADA)

Esta conclusão foi substituída pela seção "Reinterpretação após evidência do navegador": a afirmação de restrição
da conta e as pausas de 6 h não valem mais. Texto original mantido como histórico.

Cookies, headers e impressão digital foram descartados com evidência. A causa é restrição de atividade
automatizada da conta/sessão pelo Instagram (bloqueio de ação). Mudanças no app, sem novas requisições:

- Novo `result` `restricted` ("Atividade restringida pelo Instagram"), `code` `action_blocked`, não retryable:
  resposta com `feedback_required`, `spam: true`, `restriction_enrollment_data` ou chaves `feedback_*` (antes caía em
  `rate_limited`). A mensagem pede pelo menos 24 horas sem novas tentativas e conferir/contestar o aviso no navegador.
- Listagem/importação com `action_blocked`: o lote falha com essa mensagem, sem retry curto; jobs com esse código
  também falham sem retry. Como `rate_limited`, ele pausa novos downloads do Instagram, mas por 6 horas
  (`action_blocked_pause_seconds = 21600`); o teste de sessão com `restricted` aplica a mesma pausa.
- 302 para `/` deixou de dizer "causa em investigação": o teste (`instagram_error`, `detail` `redirect_home`) e as
  outras operações dizem que o Instagram reconheceu a sessão e recusou o acesso automatizado, que não é falta de
  cookies. Nas operações (perfil, listagem, resolve_post) com sessão o code é `access_refused` (não retryable; pausa
  de 6 h), não mais `session_invalid`.
- A detecção de bloqueio de ação é uma só (`is_action_blocked`), usada pelo teste de sessão e pelas operações: no
  JSON só contam campos de topo (`feedback_*` aninhado não conta em nenhum dos dois); corpo não JSON usa busca
  textual.
- 3xx sem `Location` é `redirect_other`, não `redirect_home`. A op de diagnóstico `probe` só aceita GET.

## Reinterpretação após evidência do navegador (2026-10-03)

Evidência nova do dono (Chrome real, mesma conta, sem o Media Tool): ao abrir o próprio perfil, o Instagram Web fez
**0** requisições a `/api/v1/feed/user/` e **14** requisições `POST /graphql/query` (com `doc_id`), todas HTTP 200.
Cada fato prova só o que está abaixo:

- **A) `feed/user` → 302 para `/` sem apagar o `sessionid`** (R3b, R4a, R4b): prova apenas que ESSA chamada, usada
  pelo gallery-dl no modo REST, foi recusada para este cliente. NÃO prova sessão inválida nem restrição da conta.
- **B) GraphQL legado do gallery-dl** (`GET /graphql/query/?query_hash=...`) → 400 com `feedback_*`, `spam` e
  `restriction_enrollment_data` (R5): prova apenas que essa chamada legada foi recusada com um aviso de restrição.
  NÃO prova restrição da conta inteira.
- **C) GraphQL do navegador** (`POST /graphql/query` com `doc_id`) funciona com a mesma conta. O Instagram Web atual
  não usa `feed/user` para carregar perfis.

Conclusões retiradas: "restrição de atividade automatizada da conta" e "aguarde 24 horas" não têm base nesses fatos.
Mudanças no app (sem novas requisições ao Instagram):

- Teste de sessão: continua com UMA requisição ao endpoint que o downloader usa (`feed/user`), mas o resultado
  separa `session_recognized` e `listing_api`. 302 para `/` sem apagar o `sessionid` = `api_refused` ("Sessão
  reconhecida; listagem recusada", `code` `access_refused`); com o `sessionid` apagado = `invalid`.
- Operações (perfil, listagem, resolve_post): 302 para `/` continua `access_refused` (não retryable), com mensagem
  que só afirma o que foi provado, e **sem pausa**. Uma pausa antiga com motivo `access_refused` gravada em
  `app_state` é descartada no startup.
- Campos explícitos de restrição (`action_blocked`/`restricted`): mensagem "O Instagram respondeu a esta chamada com
  um aviso de restrição (campos feedback/restriction)."; a pausa vira uma proteção do Media Tool de **1 hora**
  (`action_blocked_pause_seconds = 3600`), liberável em Configurações. `rate_limited` (429) continua com 10 min.
- A listagem por perfil ainda não funciona com esta sessão: o gallery-dl só oferece `feed/user` (REST) e o GraphQL
  legado (`query_hash`), e os dois foram recusados. Suportar o GraphQL com `doc_id` seria uma mudança separada.

## Comportamento real observado (2026-10-02, sem sessão)

- Não há sessão configurada nesta máquina (`data\secrets\instagram-session.bin` ausente, `INSTAGRAM_SESSIONID`
  não definida). A POC terminou em `session_missing` sem fazer requisição.
- Uma requisição anônima pelo runner (`op: profile`, `instagram`) a `web_profile_info` recebeu **HTTP 429**
  imediatamente; o gallery-dl esperou 60 s e tentou de novo, recebendo 429 outra vez, e o processo foi
  encerrado após 120 s. Sem sessão, nem o perfil é acessível desta rede no momento.

## Riscos e limites

- O Instagram muda endpoints e respostas sem aviso; o adapter depende do extractor upstream.
- Usar a sessão em outro IP/cliente pode disparar checkpoint; o adapter nunca tenta contornar.
- Ritmo: 6 a 12 s por requisição. Uma página de posts traz até 30 posts e uma de reels até 50; listar 5000
  mídias pode levar dezenas de minutos.
- URLs de CDN expiram (parâmetros `oe=`); o download renova via `resolve_post` uma vez.


## Provider moderno

As seções acima são o histórico da investigação com o gallery-dl e continuam valendo para o provider legado. A
listagem por `POST /graphql/query` com `doc_id` (fato C) virou o provider `web`, que é o padrão; o gallery-dl fica
como método `gallery_dl` ou como fallback de `auto` (só em `contract_changed`/`unsupported_media`). Camadas, erros,
`detail`, ritmo, teste de sessão, `doc_id` e como atualizar quando o Instagram mudar:
[INSTAGRAM_WEB_CONTRACT.md](INSTAGRAM_WEB_CONTRACT.md).
