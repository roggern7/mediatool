# Contrato: provider moderno do Instagram Web (GraphQL persistido)

Complementa `docs/CONTRACTS.md` (seções 3, 4 e 8) e `docs/instagram-gallerydl-notes.md` (histórico). Código em
`backend/app/integrations/instagram/web/` e `backend/app/integrations/instagram/composite.py`. O gallery-dl upstream
não é modificado.

## Por que existe

O gallery-dl 1.32.14 lista posts por `GET /api/v1/feed/user/<id>/` (REST, recebeu 302 para `/`) ou por
GraphQL legado `GET /graphql/query/?query_hash=...` (recebeu 400). O Instagram Web atual (evidência do Chrome do
dono, mesma conta e IP) carrega o perfil por `POST /graphql/query` com `doc_id` persistido; a consulta de posts é
`PolarisProfilePostsQuery` e responde `data.xdt_api__v1__feed__user_timeline_graphql_connection` com `edges[].node`
e `page_info`, já contendo URLs de mídia (`image_versions2`, `video_versions`, `carousel_media`).

## Camadas (um dono por arquivo)

```
InstagramSessionProvider (existente, DPAPI)
  -> web/http.py        InstagramWebHttp       cliente httpx em memória, host fixo, sem redirect automático
  -> web/errors.py      InstagramWebError      classificação de respostas (classify_response) e mensagens
  -> web/bootstrap.py   WebBootstrap           GET do documento, extrai fb_dtsg/lsd (viewer só como pista), calcula jazoest
  -> web/queries.py     QuerySpec + registro   doc_id versionado e centralizado, overrides, variáveis por consulta
  -> web/graphql.py     InstagramWebGraphQL    POST mínimo, paginação, ritmo, detecção de contrato quebrado
  -> web/normalize.py   node -> RemoteMedia    imagem, vídeo/reel, carrossel (contrato interno existente)
  -> web/provider.py    InstagramWebDownloader implementa InstagramDownloader (downloader.py)
  -> composite.py       CompositeInstagramDownloader  estratégia web (padrão) | gallery_dl | auto
  -> pipeline existente (cdn_downloader -> temp -> SHA-256 -> dedupe -> originals -> SQLite -> fila -> FFmpeg)
```
`app/main.py` cria, por operação, um `InstagramWebDownloader` (com `doc_id_overrides` do setting e `env=Settings.env`)
e um `GalleryDlInstagramDownloader`, e devolve o `CompositeInstagramDownloader` com a estratégia efetiva. Os objetos
http/bootstrap/graphql do provider web também são criados por operação e descartados (o cliente HTTP é fechado) no
fim dela.

Donos: `builder-web-core` = http.py, bootstrap.py, errors.py + testes/fixtures deles;
`builder-web-graphql` = queries.py, graphql.py, normalize.py + testes/fixtures deles;
`builder-web-provider` = provider.py, composite.py, `downloader.py` (códigos e SessionTestResult), main.py/config/
settings/schemas/settings_service (estratégia e teste de sessão), testes de integração;
`frontend` = exibição do teste de sessão e do método de importação.

## Regras de segredo (todas as camadas)

Valores de `sessionid`, `csrftoken`, `ds_user_id`, `mid`, `ig_did`, `fb_dtsg`, `lsd`, `jazoest`, claims e qualquer
token: só em memória; nunca em logs, stdout/stderr, exceções, SQLite, respostas de API, argv, arquivos, fixtures.
Objetos que carregam tokens têm `__repr__` redigido e não são serializáveis. Corpo HTML/JSON de resposta nunca é
logado; diagnósticos usam apenas: status, path sem query, content-type, tamanho, nomes de chaves, nomes de
marcadores encontrados/ausentes, códigos de erro do Instagram e um `detail` curto (ver "Detail codes"). Valores de
tokens extraídos e de cookies do jar são registrados em `app.core.logging.register_secret` para redação defensiva.
Fixtures usam valores obviamente falsos (`FAKE_DTSG_...`, `FAKE_LSD_...`). O `doc_id` não é segredo.

## Mensagens ao usuário

Mensagens em português simples, sem jargão (sem "token", "bootstrap", "GraphQL", "doc_id"); o detalhe técnico vai só
em `diagnostic["detail"]` e nos logs. Exemplos de `web/errors.py` e `web/bootstrap.py`:
- `contract_changed`: "O Instagram mudou a forma de responder e o Media Tool precisa ser atualizado." (no bootstrap:
  "O Instagram mudou a forma de carregar a página e o Media Tool precisa ser atualizado.").
- `access_refused`: "O Instagram recusou esta consulta. Isso não indica sessão inválida."
- `session_invalid`: "O Instagram não reconheceu a sessão salva. Salve uma sessão nova em Configurações."

As mensagens de `web/graphql.py` seguem a mesma regra: `contract_changed` usa o texto acima e a recusa da sessão
após a única renovação diz "O Instagram não aceitou a sessão salva para consultar publicações. Salve uma sessão nova
em Configurações." (detail `web_token_rejected`).

## web/errors.py (builder-web-core)

```python
class InstagramWebError(InstagramError):   # subclasse do erro existente; pipeline já entende .code/.retryable
    def __init__(self, code: str, message: str, *, retryable: bool = False, stage: str = "",
                 diagnostic: Mapping[str, object] | None = None)
    code: str          # preservado mesmo fora de ERROR_CODES
    stage: str         # bootstrap, document, graphql, topsearch, http, normalize
    diagnostic: dict   # só dados seguros; strings redigidas e truncadas, listas limitadas

def classify_response(resp: WebResponse, *, stage: str) -> InstagramWebError | None   # None = resposta utilizável
def redirect_kind(location_path: str | None) -> str
def loads_json(text: str) -> object       # aceita o prefixo anti-hijacking "for (;;);"
DOCUMENT_STAGES = frozenset({"bootstrap", "document"})   # stages que esperam HTML; os outros esperam JSON
```
Códigos (os novos estão em `downloader.ERROR_CODES`): `contract_changed` (novo; formato esperado sumiu),
`unsupported_media` (novo; só tipos de mídia que o app não baixa), `session_invalid`, `session_expired`,
`checkpoint_required`, `rate_limited` (retryable), `action_blocked`, `access_refused`, `network_error` (retryable),
`timeout` (retryable), `content_unavailable`, `profile_not_found`, `profile_private`.

Ordem de `classify_response`: checkpoint/challenge > 429 > login/require_login/sessionid apagado/"please wait a few
minutes" > feedback/restriction (campos de topo) > outros 3xx > 401/403 > 5xx > 2xx com JSON inválido quando o stage
espera JSON (`contract_changed`) > qualquer outro status (`contract_changed`, ex.: 400, 404). Corpo HTML nunca é
varrido em busca de marcadores (o JavaScript embutido daria falso positivo).

### Detail codes (`diagnostic["detail"]`; também vão para `last_test.detail`)

| Origem | code | detail |
|---|---|---|
| errors.py | `checkpoint_required` | `redirect_challenge`, `redirect_checkpoint`, `checkpoint_marker` |
| errors.py | `rate_limited` | `http_429`, `please_wait` |
| errors.py | `session_expired` | `redirect_login` |
| errors.py | `session_invalid` | `require_login`, `sessionid_deleted`, `http_401` |
| errors.py | `action_blocked` | `restriction_fields` |
| errors.py | `access_refused` | `redirect_home`, `redirect_consent`, `redirect_other`, `http_403` |
| errors.py | `network_error` | `http_<5xx>` |
| errors.py | `contract_changed` | `invalid_json`, `http_<status>` |
| bootstrap.py | `contract_changed` | `token_marker_missing` |
| graphql.py | `session_invalid` | `web_token_rejected` |
| graphql.py | `profile_private` / `content_unavailable` | `root_null_private` / `root_null_authenticated` |
| graphql.py | `contract_changed` | `invalid_json`, `not_object`, `persisted_query_error`, `missing_data`, `missing_root_field`, `root_not_object`, `edges_not_list`, `page_info_missing`, `repeated_cursor`, `cursor_missing_with_errors` |
| errors.py (documento público da publicação, stage `post_document`) | `access_refused` | `redirect_login`, `redirect_challenge`, `redirect_checkpoint`, `redirect_consent`, `redirect_home`, `redirect_other`, `http_401`, `http_403` |
| errors.py (documento público) | `rate_limited` / `network_error` | `http_429`, `please_wait` / `http_<5xx>` |
| errors.py (documento público) | `content_unavailable` / `contract_changed` | `post_http_404` / `document_not_html`, `http_<status>` |
| http.py (documento público) | `contract_changed` | `document_too_large` |
| provider.py | `unsupported_media` | `unsupported_media` |
| provider.py (post) | `content_unavailable` / `contract_changed` | `post_no_media`, `post_gated_logged_out`, `post_gated_session_off`, `post_gated_with_session` / `post_shape_unknown`, `post_envelope_missing`, `post_mismatch` |
| errors.py (documento da publicação com a sessão, stage `session_document`; 2xx não-HTML, inclusive JSON, é `document_not_html`) | `checkpoint_required` / `rate_limited` / `request_auth_failed` / `session_invalid` / `action_blocked` / `access_refused` / `network_error` / `content_unavailable` / `contract_changed` | os de `classify_response` (`redirect_challenge`, `redirect_checkpoint`, `checkpoint_marker`, `http_429`, `please_wait`, `redirect_login`, `require_login`, `http_401`, `sessionid_deleted`, `restriction_fields`, `redirect_*`, `http_403`, `http_<5xx>`, `http_<status>`) / `post_http_404` / `document_not_html` |

`timeout` e `network_error` do cliente HTTP (stage `http`) não têm `detail`; o diagnóstico traz método, path e o nome
da exceção. Fim anômalo da paginação (`contract_changed`, graphql.py): `detail` em `empty_page`, `cursor_missing`,
`cursor_missing_with_errors`, `repeated_cursor`, `no_new_items`, `has_next_invalid`
(`downloader.LISTING_ANOMALY_DETAILS`). Só nos logs (não é erro): `max_pages`.

## web/http.py (builder-web-core)

```python
@dataclass(frozen=True)
class WebHttpConfig:
    timeout: float = 15.0                 # prazo total por requisição; retries 0
    user_agent: str = <USERAGENT_CHROME do gallery-dl, ou um Chrome fixo se ausente>
    app_id: str = "936619743392459"
    asbd_id: str = "359341"               # aceito no real (2026-10-03); necessidade não isolada; configurável
    accept_language: str = "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"

class WebResponse:      # classe com __slots__ (sem __dict__): vars/asdict não a expõem
    __slots__ = ("status", "location_path", "content_type", "elapsed_ms", "set_cookie_names",
                 "sessionid_deleted", "text", "path")
    text: str           # NUNCA logar; repr não mostra
    path: str           # path da requisição, sem query (diagnósticos)
    def json(self) -> object   # aceita "for (;;);"; ValueError se inválido
    # repr/str/format redigidos; pickle e copy/deepcopy levantam TypeError

class InstagramWebHttp:
    BASE = "https://www.instagram.com"
    def __init__(self, session: InstagramSession, config: WebHttpConfig = WebHttpConfig(), *,
                 transport: httpx.BaseTransport | None = None)    # transport para testes
    def get_document(self, path: str, *, referer: str | None = None) -> WebResponse   # headers de navegação
    def get_api(self, path: str, params: Mapping[str, str] | None = None, *, referer: str | None = None) -> WebResponse
    def post_form(self, path: str, form: Mapping[str, str], headers: Mapping[str, str]) -> WebResponse
    def csrftoken(self) -> str | None      # do jar (pode ser renovado por Set-Cookie); nunca logado
    def account_id(self) -> str | None     # cookie ds_user_id do jar; nunca logado, registrado no redator
    def close(self) -> None
```
Sempre `follow_redirects=False`, host fixo (path deve começar com `/`, sem `//`, `\`, esquema, host ou caracteres de
controle), `trust_env=False`, cookies da sessão no jar com domínio `.instagram.com` e path `/`; Set-Cookie de resposta
atualiza só o jar em memória (um cookie por nome; Set-Cookie que apaga o `sessionid` remove todas as cópias e marca
`sessionid_deleted`). Cancelamento é verificado entre requisições pelo chamador; cada requisição tem prazo total
(checado entre chunks).

## web/bootstrap.py (builder-web-core)

```python
class WebState:         # classe com __slots__ (sem __dict__): vars/asdict não a expõem
    __slots__ = ("fb_dtsg", "lsd", "jazoest", "viewer_id", "app_id", "created_at")
    # viewer_id: viewer do HTML quando real (!= "0"), senão None; NÃO prova sessão (só pista)
    # repr/str/format redigidos ("WebState(<redacted>, html_viewer=...)"); pickle e copy/deepcopy levantam TypeError

@dataclass
class BootstrapParse:              # seguro para log: só nomes/booleanos
    markers_found: list[str]; markers_missing: list[str]
    html_viewer_hint: str          # "real" | "zero" | "absent" (pista, nunca o valor)

def parse_bootstrap(html: str) -> tuple[WebState | None, BootstrapParse]
def compute_jazoest(fb_dtsg: str) -> str   # "2" + soma dos códigos dos caracteres

class WebBootstrap:
    def __init__(self, http: InstagramWebHttp, *, ttl: float = 900.0, path: str = "/")
    def get(self, *, force: bool = False) -> WebState   # 1 GET do documento; cache em memória com TTL
    def invalidate(self) -> None
```
Marcadores aceitos (vários formatos, aspas opcionalmente escapadas, primeiro que casar): fb_dtsg em
`"DTSGInitialData",[],{"token":"..."}`, `"DTSGInitData",[],{"token":"..."}`, `{"name":"fb_dtsg","value":"..."}` (em
qualquer ordem), `"dtsg":{"token":"..."}`; lsd em `"LSD",[],{"token":"..."}` ou `"lsd":"..."`; viewer em
`"USER_ID":"..."`, `"viewerId":"..."`, `"actorID":"..."`; app id (opcional) em `"APP_ID"` ou `"appId"`.

Erros, nesta ordem: status/redirect via `classify_response` (stage `bootstrap`; login, `require_login`,
`sessionid` apagado etc.); sem fb_dtsg ou lsd -> `contract_changed` (`token_marker_missing`). O diagnóstico traz
`markers_found`, `markers_missing`, `size` e `html_viewer_hint`.

**O viewer do HTML inicial NÃO classifica a sessão.** Evidência do navegador do dono, com sessão LOGADA: o HTML
inicial de instagram.com trouxe `"USER_ID":"0"` e, no mesmo navegador, `PolarisProfilePostsQuery` respondeu HTTP 200
com `data.xdt_api__v1__feed__user_timeline_graphql_connection.edges` preenchido e `data.xdt_viewer.user.id` presente
(a identidade chega depois, na hidratação/respostas GraphQL). A POC real também recebeu documento 200 com
DTSGInitialData, LSD, USER_ID `"0"` e APP_ID. Por isso viewer `"0"` ou ausente com fb_dtsg+lsd presentes é um
`WebState` normal (`viewer_id` None); o viewer fica só como pista (`html_viewer_hint`) no diagnóstico e no log.

## web/queries.py (builder-web-graphql)

```python
@dataclass(frozen=True)
class QuerySpec:
    name: str              # fb_api_req_friendly_name
    doc_id: str
    root_field: str        # chave esperada dentro de data
    observed_at: str       # data da evidência
    relay_providers: Mapping[str, bool]

PROFILE_POSTS = QuerySpec("PolarisProfilePostsQuery", "28991540097136703",
    "xdt_api__v1__feed__user_timeline_graphql_connection", "2026-10-03",
    {"__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider": False,
     "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False,
     "__relay_internal__pv__PolarisReelsRecoDebugOverlayEnabledrelayprovider": False})

# Consulta desativada: só o nome continua aceito (configuração salva antes da remoção); o valor é ignorado.
DISABLED_QUERY_NAMES: tuple[str, ...] = ("PolarisPostActionLoadPostQueryQuery",)

KNOWN_QUERIES: tuple[str, ...] = (PROFILE_POSTS.name, *DISABLED_QUERY_NAMES)   # aceitos em instagram_web_doc_ids
DOC_ID_RE = re.compile(r"[0-9]{5,25}")                    # só dígitos ASCII, com fullmatch
ENV_PREFIX = "MEDIA_TOOL_IG_DOC_"
ROOT_ENV_PREFIX = "MEDIA_TOOL_IG_ROOT_"                   # override do campo raiz, só por env
ROOT_FIELD_RE = re.compile(r"[a-z][a-z0-9_]{2,119}")      # nome de campo seguro, com fullmatch

def resolve_spec(spec: QuerySpec, overrides: Mapping[str, str], env: Mapping[str, str] | None = None) -> QuerySpec
def profile_posts_variables(spec: QuerySpec, username: str, count: int, after: str | None) -> dict
```
`queries.py` é o único arquivo de código com `doc_id`.

> **A consulta de post isolado por POST GraphQL foi REMOVIDA (2026-10-10).** Ela nunca foi verificada no Instagram
> real (nome, `doc_id`, campo raiz e variáveis vinham de conhecimento público) e a única POC real terminou em
> `access_refused`. A observação de 2026-10-10 mostrou que a página da publicação não faz nenhuma consulta de API
> para trazer a publicação: os dados vêm embutidos no HTML (ver "Documento público da publicação"). Não existe mais
> `POST_BY_SHORTCODE`, `post_variables` nem `post_by_shortcode` no código. Só o NOME
> `PolarisPostActionLoadPostQueryQuery` continua em `KNOWN_QUERIES` (via `DISABLED_QUERY_NAMES`), para o setting
> `instagram_web_doc_ids` e os envs `MEDIA_TOOL_IG_DOC_POLARISPOSTACTIONLOADPOSTQUERYQUERY` /
> `MEDIA_TOOL_IG_ROOT_POLARISPOSTACTIONLOADPOSTQUERYQUERY` já salvos continuarem válidos (sem migração): o valor é
> ignorado e `resolve_spec` registra uma vez por processo `override para consulta desativada ignorado` (sem nome nem
> valor).

Campo raiz por env: `MEDIA_TOOL_IG_ROOT_<NAME>` (lido de `Settings.env`, vale após reiniciar o backend; não há setting)
troca o `root_field` da consulta `<NAME>` quando o valor é um nome de campo seguro (`ROOT_FIELD_RE`); valor inválido é
ignorado com aviso que não o mostra. Serve para quando o `doc_id` trocado responde em outra raiz (ex.:
`PolarisPostRootQuery`, raiz `xdt_api__v1__media__shortcode__web_info`, formato v1 `{"items": [...]}`, que
`normalize_post` já entende). O nome enviado em `fb_api_req_friendly_name` continua o embutido.

Precedência do `doc_id` (por nome, sem diferenciar maiúsculas): setting `instagram_web_doc_ids` > env
`MEDIA_TOOL_IG_DOC_<NAME>` (ex.: `MEDIA_TOOL_IG_DOC_POLARISPROFILEPOSTSQUERY`), lida de `Settings.env`, que junta o
ambiente do processo e o `.env` (o ambiente vence o `.env`; `os.environ` não é lido direto) > valor embutido.
Override inválido (fora de `[0-9]{5,25}`) cai para a próxima fonte, com aviso que não mostra o valor. Nomes fora de
`KNOWN_QUERIES` são ignorados com aviso que não mostra nome nem valor. A API (`PUT /api/settings`) já rejeita nome
desconhecido, nome repetido e `doc_id` inválido com 422.

Variáveis da 1a página exatamente como observado: `data={count, include_reel_media_seen_timestamp: true,
include_relationship_info: true, latest_besties_reel_media: true, latest_reel_media: true}`, `username`, e os
relay providers. Páginas seguintes: acrescentar `after=<cursor>`, `before=None`, `first=<count>`, `last=None`
(padrão Relay de conexão; **verificado no Instagram real em 2026-10-03**: página 2 HTTP 200 com itens novos e sem
duplicatas).

## web/graphql.py (builder-web-graphql)

```python
DEFAULT_PAGE_DELAY = (2.0, 4.0)   # segundos entre páginas (a partir da 2a), sorteado no intervalo
NO_NEW_PAGES_LIMIT = 2            # páginas seguidas só com nós já vistos antes de parar
TOKEN_ERROR_CODES = frozenset({1357001, 1357004})

class InstagramWebGraphQL:
    def __init__(self, http: InstagramWebHttp, bootstrap: WebBootstrap, *, overrides: Mapping[str, str] = {},
                 env: Mapping[str, str] | None = None, page_delay: tuple[float, float] = DEFAULT_PAGE_DELAY,
                 sleep: Callable[[float], None] | None = None)
    def query(self, spec: QuerySpec, variables: Mapping[str, object], *, referer_path: str = "/") -> dict
        # retorna data[spec.root_field]
    def iter_profile_posts(self, username: str, *, max_media: int | None, page_size: int = 12,
                           max_pages: int | None = 500, cancel: threading.Event | None = None,
                           on_page: Callable[[dict], None] | None = None) -> Iterator[dict]   # None = sem teto
        # gera nós sob demanda; max_media só reduz o count da 1a página; quem conta mídias e para é o chamador
    last_viewer_id: str | None                 # xdt_viewer.user.id da última resposta com dados
    listing_account_confirmation: str | None   # estado da conta na 1a resposta da última listagem

def account_confirmation(viewer_id: object, account_id: object) -> str
    # "confirmed" | "different_account" | "not_confirmed"
```
Formulário mínimo do POST `/graphql/query` (cada campo justificado; nada de dezenas de `__*`):
`av` (id da conta: viewer do HTML quando real, != `"0"`; senão o `ds_user_id` dos cookies da sessão,
`InstagramWebHttp.account_id()`; senão `"0"`), `__user`=`0`, `__a`=`1`, `__comet_req`=`7`, `fb_dtsg`, `jazoest`, `lsd`,
`fb_api_caller_class`=`RelayModern`, `fb_api_req_friendly_name`, `variables` (JSON compacto),
`server_timestamps`=`true`, `doc_id`. Headers: `Content-Type: application/x-www-form-urlencoded`,
`X-CSRFToken` (cookie `csrftoken`, se houver), `X-IG-App-ID` (do documento, senão o de `WebHttpConfig`), `X-FB-LSD`,
`X-FB-Friendly-Name`, `X-ASBD-ID` (de `WebHttpConfig`), `X-Root-Field-Name`, `Origin: https://www.instagram.com`,
`Referer: https://www.instagram.com/<username>/`, `Sec-Fetch-*` de cors same-origin.

**Situação dos campos no Instagram real (2026-10-03):** as variáveis de paginação (`after`, `before`, `first`,
`last`) foram verificadas (POC de paginação e POCs de 10/20). `av` (= ds_user_id), `__a`, `__comet_req`, o header
`X-Root-Field-Name` e o valor de `X-ASBD-ID` foram ACEITOS nas requisições reais bem-sucedidas, mas a necessidade de
cada um não foi isolada (não testamos removê-los), e o valor exato de `av` que o navegador envia não foi capturado.

Re-bootstrap: no máximo **1 por operação** (não por página; a listagem inteira compartilha o mesmo controle). Dispara
quando a resposta indica token/sessão web recusado (códigos 1357001/1357004, mensagem de `errors` com
`fb_dtsg`/`csrf`/`lsd`/`login`, ou 403 em JSON com esses termos em `message`/`errorSummary`/`errorDescription`;
403 com corpo HTML ou texto nunca conta e segue `access_refused` sem re-bootstrap) e a classificação foi "utilizável",
`session_invalid`, `contract_changed` ou `access_refused` vindo de 403. Se recusar de novo: `session_invalid`
(`web_token_rejected`). Só é avaliado quando `data[root_field]` útil está ausente: com dados, `errors` parciais
(mesmo citando login) só geram aviso no log e os dados são usados. Resposta 200 com raiz null/ausente cujo
`data.xdt_viewer.user.id` é igual ao `ds_user_id` da sessão (sessão comprovadamente autenticada) não é token/sessão
recusada: vira `profile_private` (`root_null_private`, se `errors` cita privado) ou `content_unavailable`
(`root_null_authenticated`), sem re-bootstrap.

Contrato quebrado (`contract_changed`): JSON inválido ou não-objeto, JSON sem `data`, `data` sem `root_field`,
`errors` com mensagem de documento/consulta persistida inexistente (só sem dados úteis), raiz não-objeto, `edges`
não-lista, `page_info` ausente, cursor repetido. `errors` parcial junto com dados úteis só gera aviso no log.
Diagnóstico seguro: status, chaves de topo, chaves de `data`, códigos e mensagens curtas de `errors` (sem trechos
entre aspas, sem tokens).

Paginação e ritmo: nunca espera antes da 1a página; entre páginas espera de 2 a 4 s (sorteado; acorda na hora se
`cancel` for acionado); `cancel` checado antes de cada requisição. **Só `has_next_page` false (resposta válida) é fim do perfil.** Com
`has_next_page` true, qualquer resposta sem continuação utilizável é FIM ANÔMALO e sobe `contract_changed` depois de
entregar os nós da página, sem checkpoint dela e sem requisição extra: sem cursor (`cursor_missing`, ou
`cursor_missing_with_errors` quando há `errors`), página vazia (`empty_page`), cursor repetido (`repeated_cursor`),
**2 páginas seguidas sem nós novos** (`no_new_items`); `has_next_page` que não é booleano -> `has_next_invalid`. Ids
(`pk`/`id`) já vistos são pulados. `max_pages`: None na listagem do app (sem teto de páginas; o construtor do provider
aceita um teto por TRECHO só para testes/POC, e atingi-lo não é fim do perfil); 1 no teste de sessão.

Prova de autenticação pela resposta: `InstagramWebGraphQL.last_viewer_id` = `data.xdt_viewer.user.id` da última
resposta com dados (None se `xdt_viewer` faltar, for null, `user` for null ou o id for vazio/`"0"`). Nunca é logado;
fica registrado no redator.

Estado semântico da conta (`account_confirmation(viewer_id, account_id)`, função única usada pela listagem, pelo
`on_page` e pelo teste de sessão): compara `str(...).strip()` de `xdt_viewer.user.id` com o `ds_user_id` da sessão.
`confirmed` = iguais; `different_account` = ambos presentes e diferentes; `not_confirmed` = algum ausente, null, vazio
ou `"0"`. Só o estado vai para log/saída; os ids nunca. Na listagem, `listing_account_confirmation` é calculado da
**primeira** resposta de `iter_profile_posts` (sem chamada extra; None antes dela).

Observação de páginas (`on_page`, opcional; sem custo quando None): chamado após cada resposta válida, antes de os nós
da página serem entregues (também é chamado quando o consumidor para no meio da página), com metadados seguros:
`page_index` (1..), `http_status`, `edges_count`, `has_next_page`, `end_cursor_present` (bool; nunca o valor),
`cursor_changed` (bool: cursor presente e diferente do `after` enviado), `variables_keys` (nomes das variáveis
enviadas; na página 2 inclui `after`, `before`, `first`, `last` do padrão Relay, verificado no real), `new_nodes`, `duplicate_nodes`
(ids já vistos na listagem, descartados) e `account_confirmation` da página.

### Documento público da publicação (observado 2026-10-10)

A publicação de um link é lida do **documento HTML público** da publicação, por um cliente **anônimo** (sem nenhum
cookie da sessão do dono). Módulos: `web/post_document.py` (leitura do envelope), `errors.classify_public_document`
(classificação do GET) e `InstagramWebHttp.anonymous` (cliente com jar vazio).

**O que foi observado no Instagram real** (2026-10-10, Chrome anônimo, contexto isolado, UM Reel público):
- `GET https://www.instagram.com/reel/<código>/` responde 200 `text/html` (~750 KB), sem redirect e sem tela de
  login bloqueante.
- Nenhuma consulta de API traz a publicação. Os dados vêm embutidos num `<script type="application/json" data-sjs>`
  (há ~40 desses na página). Caminho dentro do JSON do script:
  `require[0] = ["ScheduledServerJS", "handle", null, [{"__bbox": {"require": [...]}}]]` e, dentro desse `require`,
  `["RelayPrefetchedStreamCache", "next", [], ["adp_PolarisLoggedOutDesktopWWWPostRootContentQueryRelayPreloader_<hash>",
  {"__bbox": {"complete": true, "result": {"data": {"xig_polaris_media": {...}}}, "sequence_number": 0}}]]`.
- `data.xig_polaris_media`: `{__typename: "XIGPolarisVideoMedia", pk, code, gating_ruling: null, id,
  comments_connection, if_not_gated_logged_out: {...a mídia...}}`.
- A mídia (`if_not_gated_logged_out`) vem no formato v1 que `normalize_node` já entende: `code`, `pk`, `taken_at`
  (epoch), `media_type: 2`, `product_type: "clips"`, `original_width`/`original_height` (1080x1920),
  `user.username`, `image_versions2.candidates` (só `{url, width, height}`; maior observado 640x1136) e
  `video_versions` (3 entradas `{type: 101|102|103, url}`, **sem** `width`/`height`).

**O que NÃO foi observado** (o código aceita a forma v1, mas nada disso está verificado; se vier diferente, falha de
forma segura, sem inventar):
- `GET /p/<código>/` (nenhuma observação); foto (`media_type 1`) e carrossel (`media_type 8`, `carousel_media`)
  neste envelope.
- Publicação restrita/privada: a forma de `gating_ruling` quando não é nulo e o que vem no lugar da mídia.
- Qual `type` de `video_versions` é a melhor qualidade: como as versões vêm sem largura/altura, `_largest` fica com
  a primeira que tem URL `https://` (empate de área 0) e `width`/`height` do item saem de
  `original_width`/`original_height`.
- Qualidade de foto sem login: a capa do Reel veio com no máximo 640 px de largura (o original tem 1080). Uma foto
  importada por link pode vir abaixo de 1080 px; a decisão do dono foi aceitar isso nesta versão. `width`/`height`
  reais da versão escolhida ficam gravados.
- Host do CDN das URLs sem login: se não for `.cdninstagram.com`/`.fbcdn.net`, o download falha com `invalid_url`.
- Tela de login respondida com 200 (viraria `post_envelope_missing`), redirect `/p/<código>/` -> `/reel/<código>/`
  e o caminho `/p/` aberto com a sessão (o documento logado de um Reel foi verificado em 2026-10-29: ver "2a
  tentativa de uma publicação restrita").

**Requisição**: UM `GET /<tipo>/<shortcode>/` por publicação, com os headers de documento do `InstagramWebHttp`
(`Sec-Fetch-Site: none`, sem `Referer`), sem `Cookie`, sem seguir redirect, pelo mesmo portão global (uma vaga por
requisição) e contando como uma página no teto por hora. `<tipo>` é `p` ou `reel` e vem de quem chama (o link colado
ou a URL de origem do job; `validation.post_path_kind`): o provider nunca escolhe sozinho e não tem valor padrão. O
corpo é limitado a `MAX_DOCUMENT_BYTES` (4 MiB). O mesmo IP faz as requisições anônimas e as da sessão do dono; o
ritmo global é o mesmo (portão e teto por hora), mas o Instagram vê o mesmo IP nos dois casos.

**Leitura** (`web/post_document.py`; sem regex sobre o HTML, `html.parser` da biblioteca padrão):
```python
OBSERVED_AT = "2026-10-10"
CACHE_MARKER = "RelayPrefetchedStreamCache"; ROOT_FIELD = "xig_polaris_media"
UNGATED_FIELD = "if_not_gated_logged_out"; GATING_FIELD = "gating_ruling"
OBSERVED_PRELOADER_PREFIX = "adp_PolarisLoggedOutDesktopWWWPostRootContentQuery"   # só sinal para o log
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024; MAX_SCRIPTS = 200; MAX_SCRIPT_BYTES = 2 * 1024 * 1024; MAX_DEPTH = 64

@dataclass
class PostDocumentParse:      # seguro para log: contagens, booleanos e tokens fixos
    scripts: int; json_scripts: int; envelopes: int; preloader_observed: bool
    outcome: str              # "ok" | "no_scripts" | "envelope_missing" | "mismatch" | "gated"
    typename: str | None      # só se casar [A-Za-z]{1,40}

def iter_json_scripts(html, *, max_scripts=MAX_SCRIPTS, max_script_bytes=MAX_SCRIPT_BYTES) -> Iterator[str]
def find_media_envelopes(scripts: Iterable[str]) -> list[tuple[str | None, dict]]
def select_media_node(envelopes, shortcode: str) -> tuple[str, Mapping | None]
def parse_post_document(html: str, shortcode: str) -> tuple[Mapping | None, PostDocumentParse]
```
- Só entram scripts com `type="application/json"` **e** o atributo `data-sjs`; script que não é JSON válido é contado
  e ignorado; a caminhada pelo JSON para em `MAX_DEPTH` níveis. O envelope só é reconhecido no caminho observado
  (lista cujo primeiro elemento é `RelayPrefetchedStreamCache` e cujo último traz `__bbox.result.data.xig_polaris_media`).
  O nome do preloader não é exigido (só informa `preloader_observed`).
- `ok`: envelope com `code` EXATAMENTE igual ao shortcode pedido (diferencia maiúsculas), `gating_ruling` nulo e
  `if_not_gated_logged_out` objeto que não declara outro `code`. `gated`: o envelope do shortcode existe, mas
  `gating_ruling` não é nulo ou a mídia não é objeto. `mismatch`: só há envelope de outra publicação.
  `envelope_missing`/`no_scripts`: nenhum envelope / nenhum script.
- Nada do HTML, das URLs, do `code` ou do nome do preloader vai para log, exceção ou `PostDocumentParse`. Log:
  `Publicação (documento público): outcome=... scripts=N envelopes=N preloader_observado=... typename=...`.

**Mapa de erros** (stage `post_document`; `diagnostic["path"]` é o token fixo `/p/` ou `/reel/`, nunca o path com o
shortcode; o caminho do `Location` não entra no diagnóstico):

| Condição | code | detail | retryable | Efeito no lote por link |
|---|---|---|---|---|
| 3xx para `/accounts/login` | `access_refused` | `redirect_login` | não | arma a recusa em espera do lote; link pendente |
| 3xx para challenge/checkpoint/consent/home/outro | `access_refused` | `redirect_challenge`/`redirect_checkpoint`/`redirect_consent`/`redirect_home`/`redirect_other` | não | idem (não vira `checkpoint_required`: não há conta nesta requisição) |
| 429, ou corpo não-HTML com "please wait a few minutes" | `rate_limited` | `http_429`/`please_wait` | sim | pausa do Instagram; link pendente |
| 401 / 403 | `access_refused` | `http_401`/`http_403` | não | recusa em espera do lote |
| 404 | `content_unavailable` | `post_http_404` | não | o link falha; o lote segue |
| 5xx | `network_error` | `http_<status>` | sim | link pendente; backoff do lote |
| outro status | `contract_changed` | `http_<status>` | não | erro do lote (não do link) |
| 2xx que não é HTML | `contract_changed` | `document_not_html` | não | erro do lote |
| corpo acima de 4 MiB | `contract_changed` | `document_too_large` | não | erro do lote |
| HTML sem script `data-sjs` ou sem o envelope | `contract_changed` | `post_envelope_missing` | não | erro DO LINK; sem fallback em `auto`; 3 seguidos param o lote (`links_failing`) |
| envelope só de outra publicação | `contract_changed` | `post_mismatch` (stage `normalize`) | não | erro do link |
| `gating_ruling` não nulo ou mídia ausente | (2a tentativa com a sessão, abaixo) | | | sem sessão salva: `content_unavailable` `post_gated_logged_out`, o link falha (não arma a espera); mensagem: "A publicação exige login ou está restrita, e não há sessão do Instagram salva para tentar abri-la. Salve a sessão em Configurações e importe este link de novo." |
| nó só com tipo não suportado / sem código / sem URL | `unsupported_media` / `contract_changed` `post_shape_unknown` / `content_unavailable` `post_no_media` | | não | erro do link |
| cancelado antes do GET | devolve `[]` | | | |

Só a primeira linha de sucesso (200 HTML com o envelope de um Reel) foi observada no real; as demais seguem o
comportamento HTTP padrão. **Redirect nunca é seguido**: qualquer 3xx, inclusive `/p/<código>/` -> `/reel/<código>/`,
é `access_refused`. O GET anônimo **nunca** levanta `session_missing`, `session_invalid`, `session_expired`,
`request_auth_failed`, `checkpoint_required` nem `action_blocked`: não há sessão nem conta na requisição (um
`Set-Cookie` apagando `sessionid` numa resposta anônima é ignorado). Por isso um problema de UMA publicação pública
não mexe no status da sessão do dono.

#### 2a tentativa de uma publicação restrita: o mesmo documento com a sessão

Pedido do dono (2026-10-29): quando o documento público vem restrito (`gated`), e SÓ nesse caso, `resolve_post` faz
UMA 2a tentativa abrindo o documento da publicação com a sessão salva, como o navegador logado abriria. Nunca
POST/GraphQL (a consulta recusada pelo Instagram em 2026-10-09 continua removida). Links públicos continuam sem
sessão: a sessão salva nem é lida.

**VERIFICADO no Instagram real (2026-10-29, sessão do dono, UM Reel restrito sem login; sem download):**
- `GET /reel/<código>/` anônimo -> documento público restrito (`gated`).
- `GET /reel/<código>/` com a sessão -> 302 para `/reels/<mesmo código>/`, corpo vazio, sem `Set-Cookie` de
  `sessionid`.
- `GET /reels/<código>/` com a sessão -> 200 `text/html` (~730 KB). No mesmo caminho do público
  (`ScheduledServerJS` -> `__bbox.require` -> `RelayPrefetchedStreamCache` -> `[preloader, {__bbox: {result: {data:
  ...}}}]`), `data.xig_fetch_media` = o nó da mídia no formato v1 (`code` igual ao pedido, `pk`, `media_type` 2,
  `product_type` "clips", `user`, `image_versions2`, `video_versions` com `width`/`height`, `video_dash_manifest`,
  `carousel_media` nulo, `original_width`/`original_height`, `taken_at`...; sem `__typename`). Outro script, mesmo
  envelope, traz `data.xdt_api__v1__clips__home__connection_v2.edges[i].node.media`: o feed de Reels (o 1o edge é
  a mesma publicação, os seguintes são OUTRAS). Não há `xig_polaris_media`, `if_not_gated_logged_out` nem
  `gating_ruling` na página logada.
- NÃO VERIFICADO: o caminho `/p/` com a sessão, foto/carrossel, publicação que continua restrita com a sessão (forma
  suposta: `xig_fetch_media` nulo), qualquer resposta de recusa.

**Requisições** (cliente autenticado da importação por perfil, `http_factory`, padrão `InstagramWebHttp` com os
cookies da sessão: headers de documento de navegador, `Sec-Fetch-Mode: navigate`, sem `Referer`, sem
`X-CSRFToken`/`X-IG-App-ID`; cada GET passa pelo portão e conta uma página no teto por hora; teto de 4 MiB; fechado ao
fim, cookies só no jar em memória):
- Link de Reel: direto `GET /reels/<código>/` (o endereço do navegador logado). Redirect não é seguido.
- Link `/p/`: `GET /p/<código>/`; um redirect (sem `sessionid` apagado, `Location` relativo ou com origem exatamente `https://www.instagram.com`/
  `https://instagram.com`; outro host é recusa `redirect_other`) cujo caminho é `/reels/`, `/reel/` ou `/p/`
  do MESMO código (comparação exata) é seguido UMA vez (no máximo 2 GETs com a sessão por link). Qualquer outro
  redirect, inclusive um 2o, vai para a classificação, sem retry.

**Pré-condições**, nesta ordem: `use_session` (quem chama passa `False` depois que a sessão foi desligada no lote)
-> senão `content_unavailable` `post_gated_session_off`; cancelamento (cancelado -> lista vazia, a sessão nem é
lida); sessão salva com `sessionid` -> senão `content_unavailable` `post_gated_logged_out`; portão sem pausa
publicada (`paused_scope()`) -> senão `InstagramPaused` sem requisição. O cancelamento é conferido de novo depois de
cada espera do teto.

**Classificação**: `errors.classify_session_document`, a `classify_response` da importação por perfil com stage
`session_document` (login/401/`require_login` -> `request_auth_failed`, não evidência forte), menos: só 2xx HTML é
documento utilizável; qualquer outro 2xx (inclusive JSON válido) é `contract_changed` `document_not_html`; 404 é
`content_unavailable` `post_http_404`; o caminho do `Location` não entra no diagnóstico.

**Leitura**: `post_document.parse_session_post_document` (mesmos tetos e a mesma caminhada sem regex do público; o
parser anônimo não mudou): só `data.xig_fetch_media` cujo `code` é EXATAMENTE o pedido vira mídia, pelo mesmo
`normalize_post` (a maior `video_versions` por área; DASH pela regra de sempre). O feed de Reels nunca é lido, nem
como reserva. Só nós de outra publicação -> `post_mismatch`; campo nulo -> `post_gated_with_session`; nada ->
`post_envelope_missing`.

| Resultado da 2a tentativa | code | detail | Efeito no lote por link |
|---|---|---|---|
| nó da publicação pedida | (mídias) | | importada como as outras; log `Publicação (documento com a sessão): outcome=ok ... via=session_document` e `Publicação resolvida (web): ... via=session_document` |
| `xig_fetch_media` nulo | `content_unavailable` | `post_gated_with_session` | o link falha ("A publicação continua indisponível mesmo com a sessão: pode ser de conta privada que você não segue, removida ou restrita por região/idade."); sessão desligada no lote; não conta no disjuntor |
| sem o nó / só de outra publicação / nó sem código | `contract_changed` | `post_envelope_missing` / `post_mismatch` / `post_shape_unknown` | o link falha; sessão desligada no lote já na 1a; não conta no disjuntor `links_failing` |
| 404 / sem mídia utilizável | `content_unavailable` / `unsupported_media` | `post_http_404` / `post_no_media`... | o link falha (conta no disjuntor, como no documento público) |
| recusa, limite, verificação, rede, 2xx não-HTML | o da classificação | o da classificação | sem nova tentativa; a política de sempre (pausa por `rate_limited`/`checkpoint_required`/`request_auth_failed`/`session_invalid`/`action_blocked`, recusa em espera por `access_refused`, backoff por rede; link `pending`); sessão desligada no lote |

Todos os erros da 2a tentativa trazem `diagnostic["via"] = "session_document"`; os do GET têm stage
`session_document` (o `safe_diagnostic` registra `stage=session_document`); o `path` de todas é o caminho realmente pedido
(`/reels/` ou `/p/`). `session_off =
True` na exceção marca os casos que desligam a sessão (`AppContext.links_session_off`, só memória;
`app.jobs.pipeline.note_links_session_refusal` e `links_resolve_options`): o runner e a renovação de URL passam
`use_session=False` nos pedidos seguintes do lote e um link restrito falha com `content_unavailable`
`post_gated_session_off` ("A publicação exige login ou está restrita. Nesta importação o Instagram recusou a
tentativa com a sessão salva (ou ela falhou), então o Media Tool não usa mais a sessão para os links restritos deste
lote. Para tentar de novo com a sessão, importe este link em uma nova importação."); essas falhas também não contam
no disjuntor `links_failing` (os links públicos seguem). O link que provocou uma recusa fica `pending` e, depois da pausa/espera, é pedido de novo só pelo
documento público. Um reinício do app desfaz o desligamento. `resolve_post_authenticated` continua `False`: quem
chama não registra `note_auth_success` nem quando a publicação veio pela sessão.

#### Fluxos separados (`video_dash_manifest`): maior qualidade no modo "Por link"

Módulo: `web/dash_manifest.py` (`select_dash_streams`). Só o modo "Por link" lê o manifesto (`normalize_post` chama
`normalize_node(..., dash=True)`); a listagem de perfil continua ignorando o campo (nunca foi observada com manifesto
real).

**O que foi observado no real** (2026-10-10, o MESMO Reel público; só a leitura do nó, nenhum download de fluxo):
- `video_versions`: 3 entradas `{type: 101|102|103, url}` SEM largura/altura. A POC real baixou a primeira: MP4
  progressivo H.264 720x1280, 30 fps, com áudio HE-AAC. É a única versão H.264.
- `original_width/height`: 1080x1920; `has_audio: true`; `is_dash_eligible: 1`; `number_of_qualities: 9`.
- `video_dash_manifest`: string XML (MPD, ~19 KB): raiz `MPD` no namespace `urn:mpeg:dash:schema:mpd:2011`, perfil
  `isoff-on-demand`, `type="static"`, `mediaPresentationDuration="PT69.288345S"`, 1 `Period`.
  - 1 `AdaptationSet contentType="video"` com 9 `Representation`, TODAS VP9 (`codecs="vp09..."`, `mimeType="video/mp4"`):
    540x960 (5), 720x1280 (2), 1080x1920 a 30 fps (rótulo "960p") e 1080x1920 a 59 fps (rótulo "1080p", 3,28 Mb/s).
    Atributos: `id`, `bandwidth`, `codecs`, `mimeType`, `width`, `height`, `frameRate` (fração, ex.: `15360/512`),
    `FBContentLength` (bytes do arquivo), `FBQualityLabel`.
  - 1 `AdaptationSet contentType="audio"` com 1 `Representation` `codecs="mp4a.40.5"`, `mimeType="audio/mp4"`.
  - Cada `Representation` tem UMA `<BaseURL>` https absoluta (arquivo fMP4 inteiro, host `*.fbcdn.net`, assinatura
    na query) e um `<SegmentBase>` com `<Initialization>`: um arquivo por representação, sem lista de segmentos.
- Fixture: `backend/tests/fixtures/instagram_web/dash/observed_sanitized.mpd` (o manifesto real com URLs, ids e
  identificador trocados por valores falsos).

**Forma aceita** (qualquer outra coisa cai no MP4 direto, sem erro e sem requisição): até 512 KiB; sem `<!`
(DOCTYPE/ENTITY/CDATA/comentário) e sem `&` que não seja uma das cinco entidades predefinidas ou referência numérica
(`unsafe_xml`); raiz `MPD` no namespace DASH com `type` ausente ou `static`; exatamente 1 `Period`; nenhum
`SegmentTemplate`/`SegmentList`; nenhuma `BaseURL` fora de `Representation` nem relativa; no máximo 1 `AdaptationSet`
de vídeo e 1 de áudio (por `contentType`, ou `mimeType` quando ele falta) e nenhum de outro tipo;
`mediaPresentationDuration` `PT[nH][nM][n(.n)S]` (ausente = duração desconhecida). Uma `Representation` é utilizável
com `mimeType` `video/mp4`/`audio/mp4`, `codecs` `vp09.`/`avc1.`/`avc3.` (vídeo) ou `mp4a.40.` (áudio), exatamente 1
`BaseURL` aprovada por `validate_cdn_url`, `SegmentBase`, `bandwidth` inteiro e, no vídeo, `width`/`height` inteiros
(`frameRate` `n/d` ou `n`, opcional). As demais representações são ignoradas. `DashParse.outcome`: `ok`, `absent`,
`too_large`, `unsafe_xml`, `xml_error`, `unsupported_form`, `no_usable_video`, `no_usable_audio` (só o token vai para
o log, com contagens).

**Escolha**: vídeo de maior área (largura x altura), depois mais quadros por segundo, depois maior `bandwidth`
(empate: a primeira); áudio de maior `bandwidth`. No Reel observado: 1080x1920 a 59 fps (VP9) + HE-AAC.

**Quando os fluxos separados são usados** (as três condições): (a) o manifesto tem a forma aceita; (b) a área
escolhida é MAIOR que a declarada pela versão de `video_versions` escolhida (sem largura/altura conta 0); (c) se
`original_width/height` existem, a área escolhida ALCANÇA a do original. Senão, MP4 direto (um DASH máximo de 720p com
original de 1080p não troca H.264 por VP9 sem ganho provado). `RemoteMedia.media_url` continua sendo SEMPRE o MP4
direto (fallback); os fluxos vão em `dash_video_url`, `dash_audio_url`, `dash_video_bytes`, `dash_audio_bytes`,
`dash_width`, `dash_height`, `dash_duration`.

**Download e junção** (`jobs/pipeline.py`, `_download_dash`; `media_processor/remux.py`): dois GETs pelo
`cdn_download` de sempre (vídeo, depois áudio; `InstagramDownloader.download_url`), junção com
`ffmpeg -i <vídeo> -i <áudio> -map 0:v:0 -map 1:a:0 -c copy -fflags +bitexact -flags:v +bitexact -flags:a +bitexact
-movflags +faststart -f mp4` (nenhuma recodificação, corte, filtro ou preset; mesma entrada e mesma versão do ffmpeg
= mesmos bytes) e verificação por ffprobe (vídeo e áudio presentes, dimensões do manifesto, codec `vp9`/`h264` +
`aac`, duração dentro de max(1 s, 2%)). O original fica VP9 em MP4 (`media.video_codec = "vp9"`).

| situação nos fluxos separados | resultado |
|---|---|
| FFmpeg/ffprobe ausente; tamanhos declarados acima de `max_download_bytes` | MP4 direto, sem pedir nenhum fluxo |
| fluxo grande demais, não é mídia, status HTTP inesperado (nem 200 nem 403/404/410; ex.: 206/416/5xx) | MP4 direto |
| tamanho diferente de `FBContentLength`; junção falhou; verificação reprovada | MP4 direto (temporários apagados antes) |
| 403/404/410 em qualquer das duas URLs | MP4 direto, SEM renovação e sem consulta ao Instagram (falha de conteúdo como as demais) |
| os fluxos falharam e o MP4 direto também veio 403/404/410 | `media_url_expired` -> a renovação única de sempre (1 consulta da publicação); depois dela o download roda mais UMA vez do começo (fluxos separados com as URLs novas, se a publicação renovada trouxer manifesto; recusados de novo, MP4 direto renovado; MP4 direto ainda recusado = job falha, referência apagada) |
| falha de rede (retryable) | sobe como hoje (retry controlado do job), sem MP4 direto |
| cancelamento | job cancelado, temporários apagados |

**Teto de requisições, sem laço**: no máximo **3 requisições ao CDN por chamada de download** (vídeo, áudio, MP4 direto) e, contando a renovação única, no máximo **6 requisições ao CDN e 1 consulta da publicação por tentativa do job** (3 antes da renovação + 3 depois; a renovação acontece no máximo uma vez
e só quando o MP4 direto vem vencido).

Sem FFmpeg/ffprobe, ou em qualquer fallback, a qualidade cai para o MP4 direto **sem aviso na interface**: o único registro é a linha de log `dash_fallback=<motivo>` do job.

**Números hostis ou corrompidos**: duração só com dígitos limitados e entre 0 e 48 h (fora disso o manifesto é
`unsupported_form`); representação com `width`/`height` acima de 16384, `frameRate` acima de 1000, `bandwidth` acima
de 10^9 ou `FBContentLength` acima de 2^40 é ignorada (nada é baixado para ser reprovado depois).
`job_remote_refs.dash_streams` só recebe JSON válido: valor não finito ou fora de faixa = sem fluxos separados (MP4
direto), e um valor malformado já gravado é ignorado na leitura.

**NÃO verificado** (o código não inventa: tudo isto cai no MP4 direto ou falha de forma segura): se a CDN entrega o
arquivo inteiro de uma `BaseURL` num GET simples (o navegador usa `Range`); manifesto sem áudio, com vários
`Period`, com `SegmentTemplate`/`SegmentList`, com `BaseURL` relativa, com H.264 ou AV1 (H.264 é aceito por
inferência; AV1 é ignorado); vídeo dentro de carrossel e vídeo que não é Reel; `video_dash_manifest` nulo ou ausente;
as dimensões reais das outras `video_versions`; sincronia A/V da junção de arquivos reais (edit lists).
**Download de `BaseURL` pelo app e junção NUNCA rodaram contra o CDN real; verificados só com mídias sintéticas e o manifesto real saneado.**

### Respostas 200 com `errors`

**Aceito (sem falha, sem nova tentativa):** HTTP 200 com `data[root_field]` objeto, `edges` lista e `page_info`
objeto, mesmo com `errors` junto (em qualquer formato: lista, objeto, texto, itens não-objeto). Os dados recebidos são
usados, o cursor de `page_info` é seguido e o checkpoint da página é gravado normalmente. Sem dados úteis nada muda:
`data` nulo/ausente, raiz ausente ou não-objeto, `edges` não-lista e `page_info` ausente continuam `contract_changed`
(ou `profile_private`/`content_unavailable` com a conta confirmada), sem checkpoint da página.

**Fim anômalo da paginação (revisado; prevalece):** `has_next_page` true sem `end_cursor` (com ou sem `errors`),
página vazia com `has_next_page` true, cursor repetido e 2 páginas seguidas sem nó novo -> `contract_changed` com o
`detail` correspondente, depois de entregar os nós da página e sem checkpoint dela. NUNCA é fim do perfil. No lote
(import runner): com checkpoint (meio do lote, inclusive na 1a página de um trecho retomado) a listagem vai direto
para `needs_attention` com o checkpoint anterior (sem a nova tentativa de 60 s do "cursor recusado": nenhuma
requisição extra; depois, 1 tentativa automática a cada 6 h, no máximo 4, ou `resume-listing`). Na 1a página do lote
(sem checkpoint): sem nenhum item o lote termina `failed` com o erro visível; com itens já listados eles viram jobs e
a listagem fica `needs_attention` (a nova tentativa pede a página 1 de novo e o índice único descarta os repetidos).
Na estratégia `auto` o detalhe `cursor_missing_with_errors` não aciona o fallback para o gallery-dl
(`composite.NO_FALLBACK_DETAILS`). O aviso de log é `GraphQL: página N com fim anômalo da paginação (<detail>); não é
fim do perfil`. O contador `cursor_missing_end_of_profile` deixou de existir.

**Contado e registrado:** uma linha WARNING por página que veio com `errors` ou teve descarte inesperado (emitida ao
fim da página, também quando o consumidor para no meio dela):
`GraphQL <consulta>: página N com errors parciais|descartes inesperados: errors=N groups=[{... count=N edges_at=...}
{...}] [other_groups=K other_errors=M] requested=N edges=N new_nodes=N media_normalized=N has_next_page=<bool>
cursor_present=<bool> dropped_unexpected={motivo:n,...} skipped_expected={motivo:n,...}
dropped_at={motivo:[posições],...}`. `requested` é o `count` pedido, só informativo:
`edges < requested` sozinho não gera aviso (no real as páginas trazem menos que o pedido normalmente).

**Erros agrupados (sem corte nos 5 primeiros):** TODOS os itens de `errors` são examinados (custo linear) e agrupados
pela chave já sanitizada; cada grupo sai uma vez com `count=` (ocorrências) e, quando o `path` cita `edges`,
`edges_at=` com as posições dos edges afetados em faixas (`0-4,7`; posição na página, não é id). O teto é de
**grupos distintos** (12 por página, e 2.400 caracteres para a lista), nunca de ocorrências. Quem entra, em ordem de
prioridade: (1) todo grupo cujo `edges_at` coincide com a posição de um descarte inesperado da página (`dropped_at`)
e o grupo com mais ocorrências: nunca são omitidos (passam do teto se preciso) e saem na frente, na ordem de
chegada; (2) um grupo por `path` distinto, do path mais raro para o dominante (o campo que mais falha não esconde os
raros); (3) o resto por ocorrências. Depois dos de (1), a lista sai por ocorrências decrescentes. O que não couber
aparece explicitamente como `other_groups=K other_errors=M other_paths=[a,b,c]`: quantos tipos e ocorrências ficaram
fora e os paths sanitizados distintos deles (`-` = erro sem path; até 8 paths/300 caracteres, o resto como `+N`).
Exemplo com 14 tipos (fictício): `... {keys=message,path path=edges[].node.owner message=[rare last failed] count=1
edges_at=9}] other_groups=2 other_errors=6 other_paths=[edges[].node.kilo,edges[].node.lima] requested=12 ...`.
A linha inteira tem corte duro de 3.000 caracteres com o marcador
` …[truncated]`. `dropped_at` traz a posição do edge de cada descarte inesperado, para cruzar na mesma linha o erro
(`edges_at`) com a mídia descartada. Exemplo (valores fictícios):
`errors=10 groups=[{severity=ERROR keys=message,path,severity path=edges[].node.location.profile_pic_url message=[A
server error field_exception occured. Check server logs for details.] count=9 edges_at=0-8} {severity=ERROR
keys=message,path,severity path=edges[].node.image_versions2 message=[...] count=1 edges_at=5}] requested=12 edges=10
new_nodes=10 media_normalized=9 has_next_page=True cursor_present=True dropped_unexpected={image_no_url:1}
skipped_expected={} dropped_at={image_no_url:[5]}`. A chave do grupo é calculada DEPOIS da sanitização (valores
sensíveis diferentes caem no mesmo grupo e não vazam pela quantidade de grupos) e traz só:
`code`/`api_error_code`/`error_code`/`severity`/`classification` do erro e de `extensions` quando são enum em
MAIÚSCULAS e `_` (até 3 dígitos só no fim, até 40 caracteres) ou inteiro de até 7 dígitos; `keys` (nomes das chaves
do erro); `path` só com nomes de campo, sem o campo raiz da consulta e com todo índice de lista como `[]`
(`edges[].node.carousel_media[].image_versions2`; valor vira `<?>`, no máximo 8 segmentos); `message` como abaixo. Na CHAVE todo trecho citado da message vira `"…"` (30 erros
com um nome citado diferente cada são um grupo só); o texto exibido é o do primeiro erro do grupo, com a message
sanitizada de sempre. Números curtos (até 4 dígitos) ficam na chave: `returned 404` e `returned 500` são grupos
diferentes (números longos já viram `<id>`/`<num>` na sanitização). Palavra NÃO citada com cara de nome de campo que
mude a cada erro ainda cria um grupo por valor; o teto de grupos e `other_*` limitam o efeito. Item de `errors` que
não é objeto vira `invalid=<tipo>`. A `message` sai em uma linha de até 160 caracteres, por **lista de
permissão**: ficam
só palavras alfabéticas de até 19 letras (com maiúsculas no meio, até 8), nomes de campo em minúsculas com `_`, enums
em MAIÚSCULAS, números de até 4 dígitos e pontuação básica; URL vira `<url>`, valor citado vira `"…"` (só nome de
campo citado fica), dígitos em grupos que somam 9+ viram `<num>` e todo o resto vira `<id>` (e-mail, token, base64,
cookie com `:`, percent-encoding depois de até 3 decodificações, shortcode, id). Caracteres de controle, de largura
zero, de direção de texto e BOM são removidos; o texto também passa pelo `redact`. Motivos inesperados
(`dropped_unexpected`): `edge_null`, `edge_not_object`, `node_null`, `node_not_object` (GraphQL) e `node_no_code`,
`media_type_missing`, `carousel_no_children`, `carousel_child_invalid`, `carousel_children_missing` (menos filhos em
`carousel_media` que `carousel_media_count`; soma a diferença), `video_no_url`, `image_no_url` e
`rendition_candidates_invalid` (item entregue numa versão menor porque uma versão declarada maior veio sem URL
utilizável; candidato nulo não declara tamanho e não é detectado) (normalização). De propósito (`skipped_expected`):
`duplicate`, `unsupported_type`, `content_filter`. Totais do trecho de listagem: `InstagramWebGraphQL.listing_totals`
(só o que o GraphQL vê) e `InstagramWebDownloader.last_listing_anomalies` (`pages_with_errors`, `errors`,
`dropped_unexpected`, `skipped_expected` com os três motivos de propósito), com a
linha `Listagem web (este trecho): ...` no fim do trecho quando há `errors` ou descarte inesperado.
Esses totais são por trecho; o total do LOTE fica nas colunas `batches.listing_dropped_*` (migration 0011), somado
por página junto do checkpoint: cada `ListingCheckpoint` leva `page_counts` (`unexpected`/`expected`/`degraded` da
página que ele encerra) e cada `RemoteMedia` leva `listing_dropped` (descartes inesperados da página até o nó dela),
ver docs/BATCH_ROBUSTNESS.md, "Descartes da listagem visíveis no lote". A linha da página sai também quando o
callback `on_page` levanta. A renovação de página (`profile_page`) e `query()` registram os mesmos grupos de erros,
sem os dados de paginação.

**Nunca registrado:** cookies, tokens, headers, URLs (assinadas ou não), ids de conta/mídia/post, shortcodes,
cursores e valores de variáveis ou de campos da resposta. Do payload só saem nomes de campo/chave, códigos tipo enum
e os trechos da `message` que passam pela lista de permissão acima (palavras comuns); valores nunca.

## web/normalize.py (builder-web-graphql)

```python
@dataclass
class NormalizeStats:   # contadores seguros para log
    nodes: int; items: int; skipped_no_url: int; skipped_unsupported: int; skipped_invalid_node: int
    reasons: dict[str, int]   # os mesmos descartes por motivo (ver "Respostas 200 com `errors`")

def normalize_node(node: Mapping, *, profile: str, from_reels: bool = False,
                   stats: NormalizeStats | None = None) -> list[RemoteMedia]
def filter_by_content(items: list[RemoteMedia], content: ContentType) -> list[RemoteMedia]
def legacy_to_v1(node: Mapping) -> dict          # nó GraphQL "legado" -> nó v1 (puro; só troca nomes de campo)
def normalize_post(root: Mapping, *, stats: NormalizeStats | None = None) -> list[RemoteMedia]
```
`normalize_post` (nó de UMA publicação; hoje o do documento público, que chega no formato v1 e foi verificado no
real para um Reel; o formato "legado" abaixo nunca foi verificado nem observado no documento público, mas ainda é aceito se vier) devolve todas as
mídias da publicação (carrossel inteiro, na ordem, índice 0-based), sem filtro de conteúdo, reaproveitando
`normalize_node`. Aceita os dois formatos plausíveis: v1 (`{"items": [nó]}`, usando o primeiro item, ou um nó direto
com `media_type` numérico) e GraphQL "legado", convertido por `legacy_to_v1`: `__typename`
`[XDT]GraphImage`/`GraphVideo`/`GraphSidecar` -> `media_type` 1/2/8 (sem `__typename`: `edge_sidecar_to_children` ->
8, senão `is_video`; nome desconhecido -> tipo não suportado), `shortcode` -> `code`, `id` -> `pk`,
`display_resources[{src, config_width, config_height}]` + `display_url` (com `dimensions`) -> `image_versions2`,
`video_url` (com `dimensions`) -> `video_versions`, `taken_at_timestamp` -> `taken_at`,
`edge_sidecar_to_children.edges[].node` -> `carousel_media` (filho inválido fica como `null` para os índices não
mudarem). `source_profile` = dono da publicação em minúsculas (`user.username`, senão `owner.username`; `""` se
ausente ou fora do formato de nome de usuário).

Listagem de perfil (`normalize_node`):
`media_type` 1 imagem, 2 vídeo, 8 carrossel (`carousel_media` em ordem, índice 0-based). Imagem: maior candidato
de `image_versions2.candidates` por largura*altura. Vídeo: maior de `video_versions` por largura*altura (empate:
primeiro); thumbnail = maior candidato de imagem; `duration` de `video_duration` se houver; `has_audio` não está
no RemoteMedia (o ffprobe mede depois). `is_reel` = post `product_type == "clips"` e item vídeo. `source_url`
`/reel/<code>/` para reels, `/p/<code>/` para o resto. `taken_at` ISO UTC com `Z`. `post_id`=`pk` do post,
`media_id`=`pk`/`id` do item. Só URLs `https://` contam. Nó sem `code` -> pulado. Item sem URL utilizável -> pulado
com contagem (não derruba a listagem); `media_type` desconhecido -> pulado e contado em `skipped_unsupported`.
Conteúdo: reels = só itens `is_reel`; videos = itens vídeo; images = itens imagem; posts/all = tudo.

### Contagem de visualizações (filtro de visualizações mínimas): aba Reels SEM login

A listagem com a sessão (`PolarisProfilePostsQuery`) NÃO traz contagem de visualizações (VERIFICADO 2026-10-28:
`play_count`/`ig_play_count` ausentes, `view_count` nulo). A fonte é a aba Reels do perfil aberta SEM login
(`web/reels_tab.py`, `ReelsViews`), **VERIFICADA no Instagram real em 2026-10-29** (só nomes):

- `GET /<usuario>/reels/` pelo cliente ANÔNIMO (`InstagramWebHttp.anonymous`, jar vazio, mesmo portão e teto de
  páginas por hora; nenhum cookie da sessão viaja). O HTML embute num `<script type="application/json" data-sjs>` o
  envelope `RelayPrefetchedStreamCache` com `data.xig_user_by_username` (`id`, `pk`, `polaris_clips_connection`
  {`edges[].node`, `page_info` {`end_cursor`, `has_next_page`}}) e o marcador `LSD` (lido pela mesma regra do
  bootstrap). A resposta define cookies anônimos (`csrftoken` etc.), guardados só no jar em memória.
- Nó (`XIGPolarisVideoMedia`): `code`, `pk`, `id`, `play_count` (int; a contagem usada), `like_count`,
  `comment_count`, `product_type`, `like_and_view_counts_disabled`, `clips_tab_pinned_user_ids`, `display_uri`,
  `user`, `seo_canonical_url`. Sem `taken_at`/`media_type`/`video_versions`.
- Páginas seguintes: `POST /api/graphql` form-urlencoded com `fb_api_req_friendly_name`
  `PolarisLoggedOutDesktopWWWProfileReelsTabContentQuery_connection` e `doc_id` `27420265870989234`
  (`web/queries.py`, `REELS_TAB_CONNECTION`; override por `instagram_web_doc_ids`/`MEDIA_TOOL_IG_DOC_<NOME>` como as
  demais), `variables={"after", "first", "id"}`, `server_timestamps`, `fb_api_caller_class=RelayModern`, `lsd`;
  headers `X-FB-LSD`, `X-CSRFToken` (cookie anônimo), `X-IG-App-ID`, `X-FB-Friendly-Name`, `X-ASBD-ID`,
  `Origin`/`Referer` da aba. Resposta: `data.node.polaris_clips_connection`. O Media Tool pede `first=12` (o
  navegador usa 4; 12 NÃO verificado); sem a conexão na resposta, a MESMA página é pedida uma vez com 4 e o tamanho
  fica valendo para o resto do trecho.

Regras: contagem = `play_count` `int` (não `bool`) >= 0; qualquer outra coisa é "sem contagem". Casamento pelo
`code` da publicação. Páginas sob demanda: para um vídeo da listagem cujo código ainda não está no mapa, a próxima
página da aba é pedida até achar o código, até `has_next_page` false ou até o menor `pk` NÃO fixado já visto na aba
ser menor que o `pk` do item (a aba vai do mais novo para o mais antigo: o item não está nela -> "sem contagem").
Reel fixado na aba (`clips_tab_pinned_user_ids` não vazio) não entra nesse menor `pk`. Erros: GET classificado por
`classify_public_document` (3xx/401/403 -> `access_refused`, 429 -> `rate_limited`, 5xx -> `network_error`); POST por
`classify_response`, com códigos de sessão/autenticação trocados por `access_refused` (não há sessão na requisição).
Formato inesperado (sem envelope, sem conexão, `page_info` inválido, cursor ausente com próxima página, JSON inválido,
404) -> `contract_changed` com a mensagem "Não foi possível ler as visualizações do perfil…" e detail `reels_tab_*`.
Nada de cookie, token, cursor, código, id ou contagem em log ou diagnóstico (path fixo `/reels/`).

NÃO VERIFICADO: `first=12`; aba de perfil privado, sem Reels ou restrito sem login (falha como formato inesperado e o
lote para); se a aba contém TODOS os Reels do perfil (Reel fora dela vira "sem contagem").

**Teste real de 2026-10-29 (segunda rodada; só nomes e ids de consulta):**
- 1a página anônima (`GET /<usuario>/reels/`, envelope `xig_user_by_username`): FUNCIONA (12 Reels, `play_count`
  inteiro).
- Paginação anônima `PolarisLoggedOutDesktopWWWProfileReelsTabContentQuery_connection` (doc `27420265870989234`, POST
  `/api/graphql`) a partir do Python: HTTP 429.
- Consulta LOGADA da aba Reels `PolarisProfileReelsTabContentQuery` (doc `39368442709468666`, variáveis
  `{data: {include_feed_video, page_size, target_user_id}, user_id}` + relay providers): `field_exception` em
  `/graphql/query` e HTTP 429 em `/api/graphql`.
- Conclusão: hoje o filtro de visualizações só enxergaria os 12 Reels mais recentes. Ele está DESLIGADO
  (`import_service.MIN_VIEWS_ENABLED = False`: a API recusa `min_views` com 422 `min_views_unavailable`; e
  `VIEWS_FILTER_ENABLED = false` no frontend esconde o campo e os contadores). O código da aba Reels continua no
  lugar e testado com o interruptor ligado.

### Top vídeos de um perfil: `web/top_reels.py`

Lê a mesma aba Reels SEM login (`_ProfileReelsTab`, subclasse de `ReelsViews`; mesmas requisições, `before_request`
e portão) do mais novo para o mais antigo, até `scan_limit` Reels ou `has_next_page` false, e ordena por `play_count`.
Campos do nó usados além dos da contagem: `display_uri` (miniatura; só `https://`, validada de novo pelo downloader de
CDN, que aceita só `*.cdninstagram.com`/`*.fbcdn.net`; ausente -> sem miniatura), `like_and_view_counts_disabled`
(contagem oculta: fora do ranking) e `pk` (horário estimado, `reel_taken_at_from_pk`). Diferente do filtro da
importação, aqui o perfil pode não existir: 404 -> `profile_not_found`; documento sem a conexão é lido pela regra de
`profile_snapshot.parse_profile_document` (rota de erro -> `profile_not_found`; privado ou escondido de visitantes ->
`profile_private`); o resto continua `contract_changed`, com mensagem própria. NÃO VERIFICADO no real: `display_uri`
(existe no nó observado em 2026-10-29, mas a URL não foi baixada), aba de perfil privado/restrito e paginação longa
(a paginação anônima recebeu 429 no teste de 2026-10-29: buscas acima de 12 Reels podem falhar com `rate_limited`).

### Período de postagem e publicações fixadas

O período usa `taken_at` do nó da listagem (verificado presente em 2026-10-28). Fixação no perfil:
`timeline_pinned_user_ids` (lista não vazia = fixada) — **NÃO verificado** na resposta real da listagem (inferido do
formato v1). Sem o campo, o provider não sabe se a publicação é fixada e só encerra depois de 4 publicações seguidas
mais antigas que o corte (o Instagram fixa no máximo 3).

## web/provider.py (builder-web-provider)

`InstagramWebDownloader(InstagramDownloader)`, construtor com `session_provider`, `max_download_bytes`,
`cdn_downloader`, `doc_id_overrides`, `env`, `page_delay`, `http_config`, `transport`, `page_size` (12), fábricas
injetáveis para testes, `on_page` e `max_pages` (teto de páginas por trecho, só testes/POC; None = sem teto, o
padrão do app), ambos repassados a
`iter_profile_posts` na listagem. Sem `sessionid` na sessão:
`session_missing`.
- `get_profile(username)`: `GET /web/search/topsearch/?query=<username>` (stage `topsearch`), casa o username exato;
  JSON inválido ou sem `users` -> `contract_changed`; sem resultado -> `profile_not_found`. **Não verificado no
  real:** a prévia de perfil por topsearch só tem testes com respostas falsas; a importação não depende dela.
- `iter_profile_media(username, content, limit, cancel)`: bootstrap (sem olhar o viewer do HTML) +
  `iter_profile_posts`; só falha pelas respostas reais; normaliza; filtra;
  conta mídias (limit None = teto 5000); para de pedir páginas ao atingir o limite. Se nenhuma mídia foi entregue e
  houve itens de tipo desconhecido: `unsupported_media` (stage `normalize`). Ao fim da listagem,
  `last_account_confirmation` (atributo público; `confirmed`/`different_account`/`not_confirmed`; None se a listagem
  não chegou à consulta) traz o estado da conta pela 1a resposta, e o log seguro
  `Listagem web de <perfil>: N posts, N mídias normalizadas, N entregues, N sem URL, N não suportadas, conta <estado>`
  conta como "entregues" os itens efetivamente entregues ao consumidor, inclusive quando ele para no limite e fecha
  o gerador.
- `download(item, dest_dir, ...)`: `cdn_download` existente (sem cookies, allowlist de CDN, SSRF); se
  `forbidden/not_found` (URL expirada), refaz UMA listagem do `source_profile` (no máximo 3 páginas) para achar o
  mesmo `source_post_id` (por `media_id`, senão índice e tipo) e tenta de novo uma vez; não achou ->
  `content_unavailable`.
- `resolve_post(shortcode, *, cancel=None, path_kind=None, use_session=True) -> list[RemoteMedia]`: UM GET
  **anônimo** do documento público da publicação (`/<path_kind>/<shortcode>/`) + `parse_post_document` +
  `normalize_post` (ver "Documento público da publicação"). O cliente vem de `public_http_factory` (padrão:
  `InstagramWebHttp.anonymous`, jar vazio, mesmo portão) e é fechado ao fim; não há bootstrap nem GraphQL. Só uma
  publicação restrita no documento público ganha a 2a tentativa com a sessão (`/reels/<código>/`, ou `/p/<código>/`
  com no máximo um redirect para a mesma publicação, pelo cliente de `http_factory`; ver "2a tentativa de uma
  publicação restrita"); `use_session=False` a desliga. Atributo de
  classe `resolve_post_authenticated = False`: quem chama não registra `note_auth_success` (a resposta anônima não
  prova a sessão). `path_kind` precisa ser `"p"` ou `"reel"`; qualquer outro valor, inclusive `None`, é
  `invalid_input` sem requisição. Devolve TODAS as mídias da publicação (carrossel inteiro, na ordem,
  `source_media_index` 0-based), sem filtro de conteúdo, com `listing_ref=None` e `source_profile` = dono da
  publicação (`""` se ausente). Antes do GET aplica o teto global de páginas por hora (`wait_listing_page` do portão
  com `page_rate_limit`, quando o portão tem o método e o teto não é 0): cada post resolvido conta como uma página.
  `cancel` vai para o cliente HTTP (a espera pela vaga no portão o respeita) e é conferido antes de começar e depois
  da espera do teto: cancelado antes do GET -> lista vazia. Shortcode inválido -> `invalid_input` (sem requisição).
  Sem nenhuma mídia: só itens de tipo não suportado -> `unsupported_media`; objeto sem código de publicação ->
  `contract_changed` (`post_shape_unknown`); demais casos (itens sem URL `https://` utilizável) ->
  `content_unavailable` (`post_no_media`). Verificado no real só para um Reel público em `/reel/` (2026-10-10). Log só
  com contagens: `Publicação resolvida (web): N mídias, N sem URL, N não suportadas`.
- `test_session()`: nunca lança; sem download. Etapas:
  1. `session_present`: há sessão salva com `sessionid`? Senão `session_missing` (demais etapas `unknown`).
  2. `bootstrap` (1 GET de `/`): `ok` quando fb_dtsg e lsd foram extraídos; o viewer do HTML não conta.
  3. `graphql`: `PolarisProfilePostsQuery` com `count=1` (1 página) do perfil público **`instagram`**
     (`TEST_PROFILE`), sempre esse perfil. `ok` quando HTTP 200 com o campo raiz (também quando a falha foi só
     `edges_not_list`/`page_info_missing`).
  4. `listing_api`: `ok` quando `edges` é lista; `rate_limited` em 429; `refused` em `access_refused`/
     `action_blocked` na etapa GraphQL; senão `unknown`.
  5. `session_recognized` (via `account_confirmation`: `confirmed` -> `true`, `different_account` -> `false`,
     `not_confirmed` -> `null`): `true` se `last_viewer_id` == `ds_user_id` da sessão; `false` se os dois existem e
     diferem (outra conta) ou se houve evidência confiável de deslogado (redirect para login, 401, `require_login`,
     `sessionid` apagado, token recusado depois do re-bootstrap único: `session_invalid`/`session_expired`); `null`
     quando não deu para confirmar (resposta sem `xdt_viewer`, sem `ds_user_id` nos cookies, ou outras falhas).
  `valid` só quando a consulta moderna funcionou (listagem `ok`). **Decisão:** com a listagem `ok`, `result` é
  `valid` (`code` `ok`) nos três casos de `session_recognized`, porque a operação pode ser feita; a mensagem diz o que
  falta: `true` -> "Sessão funcionando: ..."; `null` -> "A consulta de publicações funcionou, mas o Instagram não
  confirmou a conta da sessão."; `false` (outra conta) -> "A sessão salva pertence a outra conta: ...".
  `session_invalid` só sai com evidência confiável de que a operação não pode ser feita. Mapeamento de `code` -> `result`: `session_invalid`=`invalid`,
  `session_expired`=`expired`, `checkpoint_required`, `rate_limited`, `action_blocked`=`restricted`,
  `access_refused`=`api_refused`, `timeout`, `network_error`, `session_missing`; qualquer outro (incluindo
  `contract_changed`) = `instagram_error` com o `code` original. `http_status` e `detail` vêm do diagnóstico do erro.

| Situação | session_present | session_recognized | bootstrap | graphql | listing_api |
|---|---|---|---|---|---|
| Sem sessão | false | null | unknown | unknown | unknown |
| Falha no bootstrap (login/sessionid apagado/require_login) | true | false | failed | unknown | unknown |
| Falha no bootstrap (outro motivo) | true | null | failed | unknown | unknown ou rate_limited |
| Falha na consulta (sessão recusada) | true | false | ok | failed | unknown |
| Falha na consulta (outro motivo) | true | null | ok | failed | refused, rate_limited ou unknown |
| Sucesso, conta confirmada (`xdt_viewer.user.id` == `ds_user_id`) | true | true | ok | ok | ok |
| Sucesso, conta não confirmada | true | null | ok | ok | ok |
| Sucesso, outra conta | true | false | ok | ok | ok |

## composite.py (builder-web-provider)

`CompositeInstagramDownloader(primary=web, fallback=gallery_dl, strategy)`. Estratégia efetiva: setting
`instagram_provider` gravado > env `MEDIA_TOOL_INSTAGRAM_PROVIDER` (`Settings.instagram_provider`, inclui o `.env`) >
`web`. Valor desconhecido vira `web`.
- `web` (**padrão**): só o provider web, sem fallback.
- `gallery_dl`: só o provider legado.
- `auto` (opcional, explícito): usa o web e cai para o gallery-dl SOMENTE quando o web responde `contract_changed`
  ou `unsupported_media` (`FALLBACK_CODES`), uma vez por operação e com log. Na listagem, só cai se o web ainda não
  entregou nenhum item (depois disso o erro sobe e o lote fica parcial). Nunca há fallback em 429, checkpoint,
  restrição, sessão inválida/expirada, rede ou timeout: esses erros sobem como estão (pausas do worker continuam
  valendo). `resolve_post` (publicação por link) não tem fallback nenhum, ver abaixo.
- `test_session()`: em `web` e `auto` testa o provider web (sem trocar o resultado pelo do gallery-dl); em
  `gallery_dl`, o teste legado (etapas web ficam `null`).
- `resolve_post(shortcode, *, cancel=None, path_kind=None, use_session=True)`: repassa `cancel` e `path_kind` ao
  provider só quando informados, e `use_session=False` só ao provider web (em `gallery_dl` é descartado). Propriedade `resolve_post_authenticated`: a do provider que atende a estratégia (`gallery_dl` -> True;
  `web`/`auto` -> False). **Exceção à regra de `auto`: `resolve_post` NUNCA faz fallback.** Em `auto` a publicação
  por link é atendida só pelo provider web; nenhum erro dele (nem `contract_changed`, nem `unsupported_media`) vira
  uma segunda consulta pelo gallery-dl com a sessão do dono: o erro sobe como está. Só a estratégia `gallery_dl`
  explícita resolve publicação pelo legado.

`contract_changed` e `unsupported_media` não são retryable e não pausam o worker. Na prévia de perfil
(`POST /api/instagram/profile`) eles respondem 502.

## Prova de conceito real (`scripts/poc-instagram-web.ps1`)

`backend/tools/poc_instagram_web.py --profile <perfil> [--content all] [--process]`, chamado pelo script
`scripts\poc-instagram-web.ps1 -Username <perfil> [-Content all] [-Process]`. Usa o SQLite e o storage reais e o
provider web direto (sem composite). Recusa rodar se o backend responde na porta configurada (`app_running`) ou se há
lote listando/job aberto (`queue_busy`). Cria UM lote (limit 1, process false), roda a listagem e UMA ingestão; com
`--process`, processa com o preset "Anti-duplicidade" e confere que o SHA-256 do original não mudou. Para no primeiro
erro, sem nova tentativa, e imprime relatório JSON sem segredos (`code`, `message`, `stage`, `detail` seguros).
Saída: 0 ok, 1 falha, 2 pré-condição (app aberto, fila ocupada, preset ausente).

## POC de paginação (`scripts/poc-instagram-pagination.ps1`)

`backend/tools/poc_instagram_pagination.py --profile <perfil> [--page-size 1] [--limit 2]`, chamado por
`scripts\poc-instagram-pagination.ps1 -Username <perfil> [-PageSize 1] [-Limit 2]`. **Só listagem**: sem banco, sem
download, sem processamento. Usa a sessão salva (`LocalInstagramSessionProvider`) e o `InstagramWebDownloader` real
com conteúdo `all`, retries 0 e sem re-bootstrap (token recusado vira `session_invalid`/`web_token_rejected` na hora).
O padrão (page_size 1, limit 2) força exatamente 2 páginas com o mínimo de tráfego (1 GET `/` + 2 POST
`/graphql/query`). Teto duro `max_pages=2`: nunca mais de 2 POSTs, mesmo com posts sem URL/não suportados. Se só
houve 1 página (ex.: carrossel na 1a página já atinge o limite), `outcome` = `inconclusive_single_page`, sem requisição
adicional; se o limite não foi atingido em 2 páginas, `inconclusive_limit_not_reached` (ambos saída 3). Ctrl+C ->
`interrupted` (saída 130), ainda com o JSON. `profile` no relatório é o username normalizado (null se a entrada for
inválida; a entrada crua nunca é impressa). Token recusado não refaz o bootstrap nem anuncia isso no log. O override de `doc_id` gravado no banco (setting) não é lido; vale o env/`.env` ou o valor embutido.
Recusa rodar com o app escutando na porta (`app_running`, saída 2). Para no primeiro erro (`rate_limited`,
`checkpoint_required`, `action_blocked`, `access_refused`, `contract_changed`, `session_invalid` ou qualquer exceção,
saída 1) com só `code`/`message`/`stage`/`detail` seguros. Relatório JSON (stdout): `requests` (todas as requisições
ao instagram.com: método, path sem query, status), `instagram_requests`, `cdn_requests` (deve ser 0),
`other_requests`, `pages` (metadados do `on_page`), `items` (na ordem: `shortcode`, `media_type`, `is_reel`,
`source_media_index`, `taken_at`, `cdn_url_valid` = a URL normalizada passa `validate_cdn_url`, `cdn_domain_suffix` =
sufixo permitido do host ou null), `duplicates_across_pages` (soma de `duplicate_nodes` das páginas 2+),
`stopped_at_limit` (atingiu o limite sem pedir página extra) e `account_confirmation`. Nunca imprime ids, cookies,
tokens, cursores ou URLs.

### Paginação comprovada no real (2026-10-03)

A POC acima rodou contra o Instagram real em 2026-10-03 e comprovou a paginação: 1 GET `/` + 2 POST
`/graphql/query`, com a 2a página pedida pelo cursor da 1a. As variáveis Relay `after`, `before`, `first` e `last`
foram aceitas como descritas em `web/queries.py`. O orçamento de paginação, o checkpoint de retomada e a renovação de
URL por página estão em [BATCH_ROBUSTNESS.md](BATCH_ROBUSTNESS.md). A prévia de perfil (`topsearch`) segue não
verificada no real.

## POC do documento público da publicação (`scripts/poc-instagram-post.ps1`)

**A nova versão ainda NÃO foi executada no real; só deve rodar com autorização do dono.** Serve para conferir os
casos não verificados (foto, carrossel, caminho `/p/`, publicação restrita, redirect).
`backend/tools/poc_instagram_post.py --url <link>`, chamado por `scripts\poc-instagram-post.ps1 -Url <link>` (`/p/`,
`/reel/` ou `/reels/`). **Só leitura**: sem banco, sem download, sem processamento e **sem sessão** (a sessão salva
não é lida). Usa o `InstagramWebDownloader` real (`resolve_post`, mesma classificação de erros do app) sobre um
cliente anônimo, retries 0 e sem seguir redirect. Tráfego: exatamente 1 GET do documento da publicação. Recusa rodar
com o app escutando na porta (`app_running`, saída 2). Link inválido -> `invalid_input` sem nenhuma requisição (saída
1). Para no primeiro erro (saída 1) com só `code`/`message`/`stage`/`detail` seguros e, quando o erro traz, `status`,
`redirect` (tipo, ex. `redirect_login`), `content_type` (só `tipo/subtipo`) e `size` (pela lista de permissão de
`jobs.errors.safe_diagnostic`). Ctrl+C -> `interrupted` (saída 130). Relatório JSON (stdout): `source`
(`public_document`), `verified_at`, `session_used` (sempre `false`), `link_kind` (`p`/`reel`), `requests` (método,
tipo do caminho `/p/` ou `/reel/`, status), `instagram_requests`, `cdn_requests` (deve ser 0), `other_requests`,
`cookies_sent` (deve ser 0), `document` (`status`, `redirect_kind`, `content_type`, `size`), `parse` (o
`PostDocumentParse`), `node` (forma da mídia: `media_type`, `product_type`, quantidade de candidatos de imagem, maior
largura/altura, quantidade e `type` das versões de vídeo, `video_versions_have_size`, filhos do carrossel),
`media_count`, `media_types` e `items` (na ordem: `media_type`, `is_reel`, `source_media_index`, `width`, `height`,
`taken_at`, `cdn_url_valid`, `cdn_domain_suffix`, `shortcode_matches`, `owner_present`, `thumbnail_present`). Nunca
imprime URL, HTML, shortcode, ids, cookies ou tokens.

Como ler o resultado: `ok` com `media_count` igual ao número de itens da publicação e `cdn_url_valid` true em todos
comprova o caso testado (registre-o então nesta página, em "O que foi observado"). `post_envelope_missing` com
`parse.scripts` > 0 indica que o envelope mudou de lugar ou de nome; `access_refused` com `redirect` mostra o tipo do
redirect; `cdn_url_valid` false indica host de CDN fora da lista aceita; `max_image_width` mostra a qualidade de foto
disponível sem login.

**Qualidade baixada pela POC real** (2026-10-10): o download da POC pegou a primeira `video_versions`, um MP4
progressivo H.264 de 720x1280 a 30 fps, enquanto o original declarado era 1080x1920. Os fluxos de 1080p só existem
no `video_dash_manifest` (seção "Fluxos separados"); o download deles nunca foi feito no real.

## Como atualizar quando o Instagram mudar

Sintoma: operações ou o teste de sessão terminam em `contract_changed`. Leia o `detail` (no cartão do teste de sessão,
em `last_test.detail`, no relatório da POC ou nos logs) para saber qual parte mudou:
- `token_marker_missing`: o HTML do documento mudou (sem fb_dtsg ou lsd). Ajuste os marcadores em
  `web/bootstrap.py` (`_FB_DTSG_MARKERS`, `_LSD_MARKERS`; `_VIEWER_MARKERS` é só pista) e adicione uma fixture nova em
  `backend/tests/fixtures/instagram_web/` (valores falsos) com o caso em `tests/test_web_bootstrap.py`.
- `persisted_query_error`, `missing_root_field`, `missing_data`, `http_400`/`http_404`: provavelmente o `doc_id` ou
  o nome da consulta mudou. Pegue o `doc_id` atual no DevTools do navegador (aba Network, `POST /graphql/query` com
  `fb_api_req_friendly_name=PolarisProfilePostsQuery`; copie só `doc_id`, nunca `fb_dtsg`/`lsd`/cookies).
- `edges_not_list`, `page_info_missing`, `root_not_object`, `repeated_cursor`: o formato da resposta ou a paginação
  mudou; ajuste `web/graphql.py` e/ou `profile_posts_variables` em `web/queries.py`.

Trocar o `doc_id` sem nova versão (vale a primeira fonte válida):
1. Setting (aplica na próxima operação, sem reiniciar): `PUT /api/settings` com header `X-Media-Tool-Client: panel`
   e corpo `{"instagram_web_doc_ids": {"PolarisProfilePostsQuery": "<doc_id>"}}`. `{}` ou `null` remove o override.
2. Env ou `.env` (vale após reiniciar o backend): `MEDIA_TOOL_IG_DOC_POLARISPROFILEPOSTSQUERY=<doc_id>`.
3. Valor embutido: edite `PROFILE_POSTS` em `web/queries.py` (atualize também `observed_at`) e ajuste os testes que
   conferem o valor: `tests/test_web_queries.py`, `tests/test_web_graphql.py` e
   `tests/test_review_web_pipeline_e2e.py`.

### Documento público da publicação (modo "Por link")

Sintoma: links falham com `contract_changed` e detail `post_envelope_missing` (o HTML veio, mas sem o envelope
conhecido) ou `post_shape_unknown` (o envelope veio, mas a mídia mudou de forma).
1. Abra uma janela anônima do navegador (sem login), abra o DevTools (F12) e carregue o link da publicação.
2. Na resposta do documento (aba Network, a primeira requisição), procure os `<script type="application/json"
   data-sjs>` e, dentro deles, o objeto que traz a publicação (hoje `xig_polaris_media`, dentro de uma entrada
   `RelayPrefetchedStreamCache`). Anote só NOMES de chaves e a estrutura; nunca copie URLs de mídia, cookies ou
   tokens.
3. Ajuste as constantes de `web/post_document.py` (`CACHE_MARKER`, `ROOT_FIELD`, `UNGATED_FIELD`, `GATING_FIELD`) e
   `OBSERVED_AT`, e a fixture `backend/tests/post_document_fixtures.py` (valores falsos) com os testes
   `tests/test_web_post_document.py` e `tests/test_web_provider_post.py`. Forma nova da mídia: ajuste
   `normalize_node`/`normalize_post` em `web/normalize.py`.
   Forma nova do `video_dash_manifest`: os vídeos continuam sendo baixados pelo MP4 direto (o log da publicação
   mostra `dash=<outcome>:<n>`); ajuste `web/dash_manifest.py` e a fixture `tests/dash_fixtures.py` só com NOMES de
   elementos/atributos observados (testes: `tests/test_dash_manifest.py`, `tests/test_normalize_dash.py`,
   `tests/test_import_links_dash.py`).
4. Se a página passar a exigir login (redirect ou tela de login em 200), não há ajuste de constante que resolva: o
   documento anônimo não traz a publicação e o Media Tool só usa a sessão salva na 2a tentativa de uma publicação
   que o documento público declara restrita (`gated`). Registre o caso e decida com o dono.
5. Confira com a POC do documento público (1 GET anônimo), com autorização do dono.

Se o root field mudar, edite `root_field` em `PROFILE_POSTS` e as fixtures/testes que o usam
(`tests/test_web_queries.py`, `tests/test_web_normalize.py`, `tests/test_review_web_pipeline_e2e.py` e as fixtures
em `backend/tests/fixtures/instagram_web/graphql/`). Consulta nova: crie o `QuerySpec` em `web/queries.py` e inclua o
nome em `KNOWN_QUERIES` (o schema de settings valida contra essa lista). Depois de qualquer ajuste: rode a suíte
offline do backend e, só então, a POC com 1 mídia.

### Documento da publicação `/p/` aberto COM a sessão (VERIFICADO 2026-10-29)

`/p/<código>/` logado (foto e carrossel) responde 200 SEM `xig_fetch_media`. O nó vem em
`require > [0:"ScheduledServerJS"] > [3] > [0] > __bbox > require > [0:"RelayPrefetchedStreamCache"] > [3:<preloader>] >
[1] > __bbox > result > data > xdt_api__v1__media__shortcode__web_info > items > [0]`: nó v1 com `code` (igual ao
pedido), `media_type` inteiro, `carousel_media` (lista no carrossel, null na foto), `carousel_media_count`,
`image_versions2`, `video_versions` (null na foto), `taken_at`, `user`. `parse_session_post_document` aceita os dois
envelopes (`xig_fetch_media` dos Reels e `items` deste campo) e só o item com `code` EXATAMENTE igual ao pedido vira
mídia; `items` vazio ou o campo nulo = "gated". Teste com fixture sintética: `tests/test_unified_import.py`.

### Capa e visualizações (VERIFICADO 2026-10-29)

- Capa: `RemoteMedia.thumbnail_url` (maior `image_versions2.candidates` do vídeo) baixa SEM login (1080x1920 JPEG,
  ~250 KB) sem baixar o vídeo. "Somente capas" baixa só essa URL.
- Visualizações: a aba Reels ANÔNIMA (`web/reels_tab.py`, `ReelsViews`) paginou 5 páginas seguidas (60 Reels,
  `play_count` inteiro em todos, ~7 s/página, sem 429): filtro religado (`MIN_VIEWS_ENABLED = True`,
  `VIEWS_FILTER_ENABLED = true`) com mínimo e máximo (`max_views`; acima do máximo = "above"). O documento por post não
  traz visualizações; a consulta LOGADA da aba Reels dá field_exception e não é usada. Curtidas e comentários não são
  usados.

### Análise de Perfis: `web/profile_snapshot.py` (VERIFICADO 2026-10-31/11-01)

UM GET ANÔNIMO de `/<usuario>/reels/` (`InstagramWebHttp.anonymous`, jar vazio) traz tudo, sem requisição extra, com
`max_bytes=MAX_DOCUMENT_BYTES`, `diagnostic_path="/reels/"` e `classify_public_document(resp, stage="profile_snapshot")`.
Nunca usa sessão: com ela, `GET /api/v1/users/web_profile_info/` deu 429 e `GET /api/v1/feed/reels_media/` deu 500, e
por isso não há fallback nem uso dessas rotas. Só nomes de campos e números públicos ficam registrados aqui.

Observado (200 HTML, ~600 KB; `_load_scripts(iter_json_scripts(html), "xig_user_by_username")` devolve 2 envelopes):

1. Usuário: `pk`, `username`, `is_private`, `is_unpublished`, `latest_reel_media` (int), `full_name`, `is_verified`,
   `all_media_count` (SEMPRE null sem login), `id`, `lox_highlights_connection` {`edges`, `page_info`}, `has_any_clips`,
   `is_memorialized`.
2. `{pk, polaris_clips_connection: {edges: [{node: {code, pk, play_count, like_and_view_counts_disabled,
   clips_tab_pinned_user_ids, ...}}], page_info}}`: 12 Reels na 1a página.
3. Meta tags com entidades HTML: `og:description` e `description`, por exemplo `686M seguidores, seguindo 163, 8,630 posts
   &#x2014; ...`. Número em formato inglês, arredondado em contas grandes (`33K posts`).
4. `latest_reel_media`: 0 (sem Stories) e timestamps de 4,9 a 14,6 h atrás, honesto sem login. `lox_highlights_connection`
   com 7 e 5 edges, e com `edges: []` e `page_info` presente num perfil cuja página confirma que não há destaques.
5. Perfil inexistente: HTTP **200**, ~508 KB, SEM o envelope, com `"canonicalRouteName":"comet.igweb.PolarisErrorRoute"` e
   `"tracePolicy":"polaris.httpErrorPage"`.

NÃO observado (tratado por regra, com falha segura; o comportamento real destes casos não foi verificado): perfil privado,
perfil com restrição de idade, redirect para login anônimo, `like_and_view_counts_disabled` verdadeiro.

| Campo | Regra |
|---|---|
| `account_status` | `active`: envelope lido, `username` confere (sem diferenciar maiúsculas), `is_private` e `is_unpublished` False. `private`: `is_private` True. `unavailable`: marca de rota de erro sem envelope (com todos os scripts legíveis), HTTP 404 ou `is_unpublished` True. `unverified`: todo o resto (sem envelope nem marca, JSON quebrado, outro `username`, flag não booleana, 3xx, 401/403, 429 esgotado, 5xx, timeout, rede). Nunca vira `unavailable` por falha. |
| `stories` | `latest_reel_media` int>0 com idade < 24 h ou no futuro: `yes`; idade >= 24 h: `no`; 0: `no` (privado: `unverified`); ausente/não int/bool/negativo: `unverified`. |
| `highlights` | Mapping com `edges` lista: não vazia `yes`; vazia `no` (privado: `unverified`); resto: `unverified`. |
| `posts` | Meta tag, só antes do travessão (depois dele há texto livre). Inteiro: `posts_exact=1`. K/M com até 2 casas: aproximado, `posts_exact=0`, `posts_text` original. Sem meta/ilegível: `unverified`. `all_media_count` só se vier int > 0 no futuro, como exato. |
| `views` | Soma dos `play_count` int>=0 da 1a página, sem paginar. `known` com ao menos 1 somado; `none` só com `has_any_clips` False e conexão ausente ou `edges == []`; `unverified` nos demais casos (inclusive conexão malformada) e em privado. Conexão de outro `pk` é ignorada. Nós sem contagem ou com `like_and_view_counts_disabled` ficam fora de `views_reels`. |

Valor fora de int64 (Posts, Views, `latest_reel_media`) é `unverified`. A rota de erro também poderia ser servida a um IP
bloqueado (NÃO observado): por isso `unavailable` não conta como sucesso e passa pelo canário (`docs/CONTRACTS.md`, seção
8.8). A consulta não usa o teto de páginas por hora, só o portão.

Um envelope válido do usuário tem prioridade sobre as marcas de rota de erro, e o corpo HTML só é varrido por essas duas
marcas exatas. `full_name` é dado não confiável: perde controles e surrogates soltos e é truncado. Nada do corpo ou de
cookies vai para log, exceção ou `ProfileSnapshot`. Regras de execução (backoff local): `docs/CONTRACTS.md`, seção 8.8.

### Análise de Perfis: observado em 200 perfis públicos (2026-11-01, sem sessão)

- Ritmo: uma consulta por vez, 3,4 a 3,5 s por perfil, sem nenhum 429 em 16 minutos.
- Perfil escondido sem login (@ambev, restrição de idade; outras contas com 0 seguidores, algumas removidas horas depois): `canonicalRouteName` da aba Reels, meta tag com a contagem de posts e
  `"xig_user_by_username":null`. Status `restricted`, com uma releitura antes de aceitar.
- Resposta passageira: @cbf_futebol (conta real, 0 posts) saiu sem leitura uma vez e foi lida normalmente depois.
- Rota de erro: 5 perfis reconsultados depois de minutos deram o mesmo resultado (inexistência estável).
- Não observado: 429 anônimo nesta rota, redirect para login e limite em volume acima de 200 perfis.
