# Media Tool: contratos compartilhados (V1)

Fonte da verdade para todos os módulos. Quem precisar mudar um contrato avisa o orquestrador antes.

Direção da V1 (revisada): importação por perfil do Instagram usando uma sessão autenticada fornecida localmente
pelo usuário, gallery-dl local encapsulado por adapter, download em massa, fila persistente, MediaProcessor/FFmpeg,
biblioteca. A extensão Chromium NÃO faz parte da V1 (nenhum código dela foi criado).

## 1. Layout e ownership

```
media-tool\   (raiz do repositório)
  .venv\                 Python 3.13 (já existe)
  gallery-dl\            upstream 1.32.14, git próprio, IGNORADO pelo nosso git. Não editar.
  backend\               FastAPI (pacote `app`), testes em backend\tests
    requirements.txt
    app\main.py          create_app(settings=None) + app = create_app()
    app\core\            config, logging (com redação de segredos), security (middleware, validadores), paths
    app\db\              conexão SQLite, migrations SQL numeradas, repositórios
    app\schemas\         Pydantic de entrada/saída
    app\services\        storage, cdn_downloader, diagnostics, system_open, import_service,
                         export_service, export_destination, export_names (exportar para pasta, seção 8.2)
    app\jobs\            fila persistente, worker, import runner, recovery, export runner
    app\integrations\instagram\
        session.py       InstagramSession, InstagramSessionProvider, LocalInstagramSessionProvider, dpapi
        validation.py    parse_profile_input, validate_shortcode, validate_instagram_url
        downloader.py    InstagramDownloader (ABC), RemoteMedia, ProfileInfo, SessionTestResult, InstagramError
        gallerydl.py     GalleryDlInstagramDownloader (fala com o runner via subprocess)
        gallerydl_runner.py  processo filho: lê pedido JSON do stdin, roda gallery-dl in-process, escreve JSON lines
    app\api\             routers (prefixo /api)
  processor\             pacote Python `media_processor` (pyproject, instalado editable no .venv)
  frontend\              React + Vite + TypeScript; build em frontend\dist (servido pelo backend em /)
  storage\originals  storage\processed  storage\temp  storage\thumbs
  data\media-tool.db     data\secrets\ (ignorado pelo git)
  logs\
  scripts\setup.ps1 start.ps1 stop.ps1 set-instagram-session.ps1 poc-instagram.ps1
  docs\
```

Ownership (um dono por arquivo; outros só leem):
- agente `instagram-adapter`: app/integrations/instagram/{validation,downloader,gallerydl,gallerydl_runner}.py, prova de
  conceito (backend/tools/poc_instagram.py + scripts/poc-instagram.ps1), testes do adapter.
- agente `secrets-security`: app/integrations/instagram/session.py, app/core/security.py, app/core/logging.py,
  app/services/cdn_downloader.py, app/services/diagnostics.py, app/services/system_open.py,
  scripts/set-instagram-session.ps1, testes de segurança/segredos.
- agente `backend-core`: todo o resto de backend/app (config, db, jobs, api, schemas, services/storage,
  services/import_service, main), backend/requirements.txt, testes de banco/fila/API.
- agente `processor`: processor/.
- agente `frontend`: frontend/.
- agente `scripts-docs`: scripts/setup.ps1, start.ps1, stop.ps1, README.md, .env.example.
- orquestrador: .gitignore, docs/CONTRACTS.md.

## 2. Execução

- Backend: `.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765`
- Painel: backend serve `frontend\dist` em `http://127.0.0.1:8765/` (fallback SPA para rotas fora de /api).
- Frontend dev: Vite em `http://127.0.0.1:5173`, proxy `/api` -> `http://127.0.0.1:8765`.
- API docs: `http://127.0.0.1:8765/docs`.

Ambiente (opcionais; defaults relativos à raiz do projeto; `.env` na raiz é lido se existir, nunca versionado):
`MEDIA_TOOL_ROOT`, `MEDIA_TOOL_DATA_DIR` (data), `MEDIA_TOOL_STORAGE_DIR` (storage), `MEDIA_TOOL_LOG_DIR` (logs),
`MEDIA_TOOL_SECRETS_DIR` (data\secrets), `MEDIA_TOOL_HOST` (127.0.0.1), `MEDIA_TOOL_PORT` (8765),
`MEDIA_TOOL_FFMPEG`, `MEDIA_TOOL_FFPROBE`, `MEDIA_TOOL_CONCURRENCY` (1), `MEDIA_TOOL_MAX_DOWNLOAD_BYTES` (2147483648),
`MEDIA_TOOL_DISABLE_WORKER` (testes), `INSTAGRAM_SESSIONID` (fallback; o caminho principal é o arquivo protegido),
`MEDIA_TOOL_TIMEZONE` (fuso IANA da data nos nomes exportados; padrão: fuso do sistema),
`MEDIA_TOOL_INSTAGRAM_PROVIDER` (`web` padrão, `auto`, `gallery_dl`), `MEDIA_TOOL_IG_DOC_<NOME>` (override de doc_id;
ver `docs/INSTAGRAM_WEB_CONTRACT.md`).

## 3. Sessão do Instagram (segredo)

`sessionid` é credencial. Nunca: hardcode, commit, SQLite, logs, exceptions, respostas de API, testes (use valores
falsos óbvios tipo `FAKE-SESSION-0000`), prompts, relatórios, argv de processo.

```python
class InstagramSession:            # opaco; __repr__/__str__ = "InstagramSession(<redacted>)"; não serializável por json
    def cookies(self) -> dict[str, str]   # ÚNICO acesso aos valores; só o adapter chama
class SessionStatus(BaseModel-like): configured: bool; source: Literal["file","env"] | None; updated_at: str | None
class InstagramSessionProvider(ABC):
    def status(self) -> SessionStatus
    def get(self) -> InstagramSession | None
    def save(self, sessionid: str, extra: dict[str, str] | None = None) -> None   # extra só chaves permitidas:
        # csrftoken, ds_user_id, mid, ig_did, rur (validadas: [A-Za-z0-9%:_\-.]+, tamanho limitado)
    def clear(self) -> None
class LocalInstagramSessionProvider(InstagramSessionProvider):
    def __init__(self, secrets_dir: Path, env: Mapping[str, str] = os.environ)
```
Armazenamento: `data\secrets\instagram-session.bin`, conteúdo JSON cifrado com Windows DPAPI (CryptProtectData, escopo
usuário atual, via ctypes; sem dependência nova). Escrita atômica. Fora do Windows: fallback arquivo com permissão 0600
e aviso em diagnóstico. Precedência: arquivo; senão `INSTAGRAM_SESSIONID` do ambiente.
Logging: filtro global que redige valores de segredo conhecidos e padrões `sessionid=...`, `"sessionid": "..."`, cookies.
Acréscimos entregues: `SessionStatus.error: str|None` (motivo seguro, ex.: arquivo ilegível); `SessionValidationError`
(`.field`, `.message`, nunca contém o valor). `install_security(..., allow_testserver=False)`; códigos de erro HTTP
`invalid_host` (400), `client_header_required` (403), `origin_not_allowed` (403), `internal_error` (500).
`build_diagnostics(settings_like=...)` aceita mas não lê o argumento.
Último teste de sessão (sem segredo) fica em `app_state` key `instagram_session_last_test` como JSON
`{ok, result, code, message, username|null, http_status|null, duration_ms, detail|null, session_recognized|null,
listing_api|null, session_present|null, bootstrap|null, graphql|null, tested_at}`. `session_present` (bool),
`bootstrap` e `graphql` (`ok`|`failed`|`unknown`) são preenchidos pelo provider web; ficam `null` em registros antigos
e no teste do gallery-dl. Valores fora do esperado são normalizados para `null` na leitura.
O provider web (seção 4) usa a mesma `InstagramSession`: os cookies vão só para o jar em memória do cliente HTTP, e os
tokens extraídos da página (`fb_dtsg`, `lsd`, `jazoest`) também ficam só em memória, em objetos com `__slots__`,
`repr` redigido e sem serialização.

## 4. Adapter Instagram

```python
ContentType = Literal["reels", "posts", "videos", "images", "all", "photos_carousels"]   # ver seção 8.1
def parse_profile_input(value: str) -> str     # "@user", "user", "https://www.instagram.com/user/..." -> "user"; ValueError
                                               # username: ^[A-Za-z0-9._]{1,30}$, sem "..", não reservado (p, reel, explore...)
@dataclass class ProfileInfo: username; user_id: str|None; full_name: str|None; avatar_url: str|None;
    post_count: int|None; is_private: bool|None; is_verified: bool|None
@dataclass class RemoteMedia: source_platform="instagram"; source_profile: str; source_post_id: str  # shortcode
    post_id: str|None; media_id: str|None; source_media_index: int  # 0-based dentro do post/carrossel
    media_type: Literal["video","image"]; is_reel: bool; source_url: str  # https://www.instagram.com/p/<code>/ ou /reel/
    media_url: str; thumbnail_url: str|None; width: int|None; height: int|None; duration: float|None
    taken_at: str|None; extension: str|None
@dataclass class SessionTestResult: ok: bool; code: str; message: str; username: str|None
    result: str   # valid|invalid|expired|checkpoint_required|rate_limited|restricted|api_refused|timeout|network_error|instagram_error|session_missing
    http_status: int|None; duration_ms: int|None   # sem `result` no construtor, ele é derivado de ok/code
    session_recognized: bool|None   # web: True se data.xdt_viewer.user.id == ds_user_id; False se outra conta ou
                                    # evidência confiável de deslogado (401/require_login, redirect login, sessionid
                                    # apagado); None se não confirmável. USER_ID do HTML inicial não conta.
                                    # gallery-dl: True quando a resposta não encerrou a sessão.
    listing_api: str|None   # ok (200 com items) | refused (redirect não-login, 400/403 com feedback_*) |
                            # rate_limited (429) | unknown
    detail: str|None   # gallery-dl: só em redirect: redirect_home|redirect_login|redirect_challenge|redirect_checkpoint|
                       # redirect_consent|redirect_other (destino pelo caminho do Location, sem query);
                       # provider web: detail code do erro (lista em INSTAGRAM_WEB_CONTRACT.md)
    session_present: bool|None      # provider web: há sessão salva com sessionid; None no gallery-dl
    bootstrap: str|None             # provider web: ok|failed|unknown (abertura da página inicial); None no gallery-dl
    graphql: str|None               # provider web: ok|failed|unknown (consulta count=1); None no gallery-dl
    # result -> code: valid=ok, invalid=session_invalid, expired=session_expired, checkpoint_required,
    # rate_limited, restricted=action_blocked, api_refused=access_refused, timeout=network_error, network_error,
    # instagram_error=internal_error, session_missing
class InstagramError(Exception): code: str; message: str; retryable: bool
    # codes: session_missing, session_invalid, session_expired, checkpoint_required, profile_not_found,
    # profile_private, rate_limited, action_blocked (campos explícitos de restrição nesta chamada, não retryable),
    # access_refused (302 para "/" com sessão: a chamada REST do gallery-dl foi recusada; não prova sessão inválida
    # nem restrição da conta; não retryable, sem pausa), content_unavailable, network_error,
    # download_failed, invalid_input,
    # internal_error, timeout (gallery-dl: só get_profile, teto total estourado; web: prazo da requisição),
    # contract_changed (provider web: o formato esperado do Instagram sumiu; o Media Tool precisa de
    # atualização; não retryable, sem pausa), unsupported_media (provider web: o perfil só trouxe tipos de
    # mídia que o app não baixa; não retryable, sem pausa)
class InstagramDownloader(ABC):
    def test_session(self) -> SessionTestResult
    def get_profile(self, username: str) -> ProfileInfo
    def iter_profile_media(self, username: str, content: ContentType, limit: int | None, *,
                           cancel: threading.Event | None = None) -> Iterator[RemoteMedia]
    resolve_post_authenticated: bool = True     # False no provider web (documento público, sem sessão)
    def resolve_post(self, shortcode: str, *, cancel: threading.Event | None = None,
                     path_kind: str | None = None) -> list[RemoteMedia]
        # todas as mídias da publicação; renova URLs expiradas. path_kind: "p" | "reel" (tipo do link de quem chama;
        # obrigatório no provider web, ignorado no gallery-dl). Só o provider web (e o composite, que repassa) aceita
        # também `use_session: bool = True`: False = sem a 2a tentativa com a sessão numa publicação restrita
    def download(self, item: RemoteMedia, dest_dir: Path, *, cancel=None, progress=None) -> Path  # em dest_dir (temp)
class GalleryDlInstagramDownloader(InstagramDownloader):
    def __init__(self, session_provider: InstagramSessionProvider, *, python_executable: str = sys.executable,
                 temp_dir: Path, cdn_downloader=cdn_download, timeout: float = ..., sleep_request: tuple|None = None)
```
Construtor entregue também aceita `max_download_bytes`, `backend_dir`, `max_total_time: float|None`,
`test_timeout: float = 30.0` e `profile_timeout: float = 45.0`; `timeout` é tempo de INATIVIDADE do runner (padrão
300s), não tempo total. `test_session()` nunca lança: termina em até `test_timeout` (filho morto, `result: timeout`);
no runner usa `retries 0`, sem espera em 429, timeout HTTP 15 s e UMA requisição, a primeira chamada do download
que exige login: `GET /api/v1/feed/user/<ds_user_id>/?count=1` da própria conta (sem `ds_user_id`, o prefixo numérico
do `sessionid`; sem nenhum dos dois = `invalid`), sem seguir redirect e sem fallback. Critério do teste: 200 com
`items` ou `user` = `valid` (antes de qualquer marcador; username do JSON se houver); redirect para
`/accounts/login/` = `expired`; para `/challenge/`/`/checkpoint/` = `checkpoint_required`; Set-Cookie que apaga ou
expira o `sessionid` (Max-Age<=0, Expires no passado, valor vazio ou `""`; só nome e atributos são lidos, nunca o
valor) = `invalid`; para `/`, consentimento ou outra página (ou 3xx sem `Location`, `redirect_other`) sem apagar o
`sessionid` = `api_refused` (`code` `access_refused`, `session_recognized: true`, `listing_api: refused`) com
`detail` (`redirect_home`...); `require_login` e 401/403 = `invalid`
(`session_invalid`); 429 = `rate_limited`; `feedback_required`, `spam: true`, `restriction_enrollment_data` ou
chaves `feedback_*` de topo = `restricted` (`action_blocked`; aviso de restrição nesta chamada, não restrição
comprovada da conta), mesma detecção usada nas operações.
Nas operações, redirect para `/` com sessão = `access_refused` (sem pausa).
Resolução do perfil (`profile` e início de `list`): estratégia do gallery-dl 1.32.14 (`user-strategy` padrão):
`GET /web/search/topsearch/?query=<user>`; se responder 200 sem o usuário, a página HTML `/<user>` (`profile_id`); só
então, como último recurso, `GET /api/v1/users/web_profile_info/`. Qualquer falha HTTP/redirect numa estratégia (429,
login, home, 404...) é classificada na hora, sem passar para a seguinte. Listagem: `GET /api/v1/feed/user/<id>/`
(posts/videos/images/all) e `POST /api/v1/clips/user/` (reels), pelos extractors do upstream com URL `id:<id>`.
`get_profile()` (prévia) termina em até
`profile_timeout` (filho morto, `InstagramError("timeout")`). Em perfil/post o runner espera 30 s em 429 com 1 nova
tentativa; na listagem, 2. 401 com `require_login` (mesmo com "Please wait a few minutes") vira `session_invalid`, não
`rate_limited`. Cookies extras da sessão (ds_user_id, csrftoken, mid, ig_did, rur) seguem ao gallery-dl; o
`csrftoken` informado substitui o gerado no cookie e no header `X-CSRFToken`. `is_reel` = `product_type == "clips"` ou item vindo
do endpoint de reels; `source_url` `/reel/<code>/` para reels, `/p/<code>/` para o resto. `taken_at` ausente = null.
`limit` conta mídias (itens de arquivo), não posts. `None` = todos (com teto de segurança 5000).
Runner: `python -m app.integrations.instagram.gallerydl_runner` com cwd backend; pedido JSON (incluindo cookies) pelo
**stdin**; saída JSON lines `{"type":"item"|"profile"|"error"|"done", ...}` no stdout; stderr do gallery-dl é
redigido antes de qualquer log. Segredo nunca em argv nem em arquivo em texto puro.
O download do arquivo usa a URL de CDN da listagem via `cdn_downloader` (sem cookies); se expirar (403/404/410),
`resolve_post` e tenta de novo uma vez. Respeitar o intervalo de requisição do gallery-dl (não reduzir agressivamente).

### Provider web e estratégia

```python
class InstagramWebDownloader(InstagramDownloader)        # app/integrations/instagram/web/provider.py
class CompositeInstagramDownloader(InstagramDownloader):  # app/integrations/instagram/composite.py
    def __init__(self, primary: InstagramDownloader, fallback: InstagramDownloader, strategy: str = "web")
INSTAGRAM_PROVIDERS = ("auto", "web", "gallery_dl")       # app/core/config.py
```
`InstagramWebDownloader` lista posts como o Instagram Web atual (1 GET da página inicial + `POST /graphql/query`
com `doc_id` persistido) e baixa pela mesma `cdn_downloader`. Contrato completo (camadas, erros, `detail`, ritmo,
precedência de `doc_id`, teste de sessão): [INSTAGRAM_WEB_CONTRACT.md](INSTAGRAM_WEB_CONTRACT.md).
`app.main` monta, por operação, o composite com `primary` = web e `fallback` = gallery-dl. Estratégia efetiva
(`settings_service.instagram_provider`): setting `instagram_provider` > env `MEDIA_TOOL_INSTAGRAM_PROVIDER` (ambiente ou
`.env`) > `web`. `web` (padrão): só o provider web, sem fallback. `gallery_dl`: só o legado. `auto` (opcional): web,
com fallback ao gallery-dl SOMENTE em `contract_changed` ou `unsupported_media`, uma vez por operação, com log (na
listagem, só antes do primeiro item); nunca em 429, checkpoint, restrição, sessão, rede ou timeout, e nunca em
`resolve_post` (publicação por link: em `auto` só o web atende, sem fallback). O teste de sessão
usa o web em `web` e `auto`, e o gallery-dl em `gallery_dl`.
Teste de sessão do provider web: sessão presente -> 1 GET da página inicial (só tokens; o viewer do HTML não conta) ->
consulta `count=1` do perfil público `instagram`, sem download; `valid` só se a consulta funcionou; `contract_changed`
vira `result: instagram_error` com `code: contract_changed`.

## 5. CDN downloader seguro (`app/services/cdn_downloader.py`)

```python
class DownloadError(Exception): code: Literal["invalid_url","forbidden","not_found","too_large","network_error","invalid_media","cancelled"]
def validate_cdn_url(url: str) -> str
def cdn_download(url: str, dest_dir: Path, *, max_bytes: int, timeout: float = 60, cancel=None,
                 progress: Callable[[int, int|None], None] | None = None) -> DownloadResult(path, size, content_type, suffix)
```
Só `https`, porta 443, host com sufixo `.cdninstagram.com` ou `.fbcdn.net`, sem userinfo; DNS resolvido e IPs
privados/loopback/link-local/reservados rejeitados; redirects manuais revalidados (máx 3); streaming para arquivo
temporário em `dest_dir`; limite de tamanho; sem cookies; magic bytes (MP4/MOV, WebM, JPEG, PNG, WebP, HEIC).

## 6. Banco SQLite

`data\media-tool.db`, WAL, `foreign_keys=ON`. Migrations `backend\app\db\migrations\NNNN_nome.sql`, tabela
`schema_migrations(version, name, applied_at)`. Datas ISO 8601 UTC com `Z`.

- `presets(id, name UNIQUE, is_builtin, params json, created_at, updated_at)`; seed `Padrão` e `Anti-duplicidade`
  (builtin, editáveis, não excluíveis). `Padrão` tem os 6 params padrão; `Anti-duplicidade` tem os mesmos 6 mais
  `zoom: 3`, `brightness: 0.01`, `contrast: 1.02`, `saturation: 1.03`, `trim_end: 0.3` (migration 0015, que só
  atualiza a linha embutida se ela ainda tiver exatamente os 6 params originais; renditions, jobs e lotes não mudam).
- `media(id, source_platform, source_profile, source_url, source_post_id, source_media_index DEFAULT 0, post_id, media_id_remote,
  media_type, original_filename, original_path /*relativo a storage*/, original_sha256 UNIQUE, file_size, duration, width,
  height, fps, has_audio, mime_type, thumbnail_path, taken_at, created_at, updated_at, video_codec /*migration 0016;
  codec de vídeo do original pelo ffprobe, ex. "vp9"/"h264"; NULL quando não analisado*/)`;
  índice único parcial `(source_platform, source_post_id, source_media_index)` quando `source_post_id` não nulo.
- `renditions(id, media_id FK CASCADE, preset_id FK SET NULL, preset_name, params json, job_id, processed_path,
  processed_sha256, file_size, duration, width, height, fps, has_audio, created_at /*processed_at*/)`
- `batches(id, source /*instagram_import|library*/, source_profile, content_type, requested_limit, preset_id, preset_name,
  process, status /*listing|running|completed|failed|cancelled*/, found, error_code, error_message, created_at,
  updated_at, finished_at)`
- `jobs(id, batch_id FK, media_id FK SET NULL, rendition_id, kind /*ingest|process*/, preset_id, preset_name, params json,
  status, stage, progress, error_code, error_message, attempts, max_attempts DEFAULT 3, next_attempt_at, cancel_requested,
  is_duplicate, duplicate_reason /*source_id|sha256|null*/, source_platform, source_profile, source_url, source_post_id,
  source_media_index, post_id, media_id_remote, media_type, media_url, thumbnail_url, taken_at, temp_path, created_at,
  started_at, finished_at, updated_at, heartbeat_at)`; único `(batch_id, source_post_id, source_media_index)` para ingest.
- `import_groups(id, content_type, requested_limit, preset_id, preset_name, params json, process, created_at)`
  (migration 0012): importação de vários perfis; só guarda o pedido, o estado é derivado dos filhos. `batches` ganha
  `group_id` (FK, NULL em lotes avulsos/antigos), `group_position` (1-based) e `listing_end`
  (`profile_end`|`limit`|NULL).
- Importação por link (migration 0014; seção 8.3): `batches` ganha `import_mode` (NULL = por perfil; `'links'` = por
  link, com `source='instagram_import'`, `source_profile` NULL, `content_type='all'`, `requested_limit` NULL) e
  `batch_links(batch_id FK CASCADE, position /*1-based, ordem colada*/, shortcode, url /*normalizada*/,
  status /*pending|resolved|failed*/, error_code, error_message, items_found, profile, resolved_at,
  attention_count /*vezes em que o link deixou o lote em needs_attention*/, PK (batch_id, position),
  UNIQUE (batch_id, shortcode))`; `batches.link_fail_streak` (links seguidos falhos; disjuntor de 3). A renovação
  de link de download de uma publicação é registrada em `listing_pages` com `page_index = position` do link.
- Filtro de visualizações mínimas (migration 0017; seção 8.1): `import_groups.min_views` e `batches.min_views`
  (INTEGER >= 1; NULL = sem filtro), `batches.listing_views` (JSON `{"approved", "below", "no_count"}` somado por página
  no checkpoint) e `batches.listing_views_partial` (mesmo formato, página aberta sem checkpoint). Só colunas
  anuláveis; lotes antigos e lotes criados sem o filtro ficam com NULL nas quatro.
- `exports(id, kind /*batch|group|library*/, batch_id FK SET NULL, group_id FK SET NULL, selection json /*só library*/,
  dest_path, dest_created /*1 = pasta padrão criada pelo servidor para esta exportação*/,
  mode /*folder|zip*/, zip_path, zip_name, zip_token, zip_bytes, zip_expires_at, zip_downloaded_at,
  status /*pending|running|completed|cancelled|failed*/, total, copied, skipped_existing, renamed, missing,
  failed, cancel_requested, error_code, error_message, created_at, started_at, finished_at)` e
  `export_items(export_id FK CASCADE, media_id, PK (export_id, media_id))` (migration 0013; seção 8.2): em `library` por
  ids, `export_items` guarda os ids escolhidos; por filtro, guarda os ids EXCLUÍDOS e `selection` guarda o filtro
  congelado (`max_id`). Para lote e grupo nada é materializado (as mídias saem dos jobs de ingest ou process
  concluídos, unidos a `batch_media`).
- `profile_analyses(id, status /*pending|running|waiting|completed|cancelled|interrupted*/, total, invalid_count,
  duplicate_count, invalid_lines json, cancel_requested, waiting_until, wait_reason, wait_level, resume_after,
  final_stats json, error, created_at, started_at, finished_at)` e `profile_analysis_items(id, analysis_id FK CASCADE, position, username,
  state /*pending|running|done|cancelled*/, attempts, account_status, display_name, posts_state, posts_count, posts_exact,
  posts_text, views_state, views_total, views_reels, stories, highlights, error_code, error_message, checked_at,
  UNIQUE (analysis_id, username))`, índices `(analysis_id, state, position)`, `(analysis_id, position)` e parcial
  `(analysis_id, position) WHERE state IN ('pending', 'running')` (migration 0025; seção 8.8).
- `batch_media(batch_id FK CASCADE, media_id FK CASCADE, PK (batch_id, media_id))` (migration 0013): relação durável
  lote -> mídia, preenchida pelo backfill da migration e pelo purge de jobs antes de apagar (seção 8.2).
- `settings(key PK, value json, updated_at)`; `app_state(key PK, value, updated_at)` (`export_last_folder`: última pasta
  de exportação usada, só para abrir o diálogo nela).
Nenhuma tabela guarda sessão/cookies.

## 7. Estados

Job `status`: `pending | downloading | downloaded | processing | completed | failed | cancelled`.
Ingest download-only (padrão): `pending -> downloading -> completed`. Ingest com `process: true`:
`pending -> downloading -> downloaded -> processing -> completed`. Process: `pending -> processing -> completed`.
`stage`: `queued, waiting_retry, downloading, muxing, hashing, probing, thumbnail, processing, finalizing, done, skipped`
(`muxing`: junção de vídeo e áudio baixados separados, só no lote por link; seção 8.3).
`progress`: 0..1 ou null (nunca inventado).
Deduplicação em dois níveis: (1) antes de baixar, mídia com mesmo (platform, post_id/shortcode, index) já existe ->
não baixa, `is_duplicate=1, duplicate_reason=source_id`; (2) após baixar, SHA-256 igual a mídia existente -> descarta
temp, liga à existente, `duplicate_reason=sha256`. Se o lote pede processamento e a mídia já tem rendition do mesmo
preset com os mesmos params, não reprocessa (completed, stage `skipped`); senão processa o original existente.
Retry controlado: erros `retryable` (network_error, rate_limited, download_failed transitório) voltam a `pending`
com `next_attempt_at` (30s, 2min, 10min), `attempts+1`; esgotado `max_attempts` -> `failed`. Nunca retry infinito.
Pausa do Instagram: `rate_limited` pausa todo o tráfego automático ao Instagram por 10 min; `action_blocked`
(campos explícitos de restrição), não retryable, falha o job/lote sem retry e aplica uma pausa de proteção do Media
Tool de 1 hora (`action_blocked_pause_seconds = 3600`), liberável em Configurações. `access_refused` (302 para `/`)
é não retryable e NÃO pausa; o teste de sessão com `api_refused` também não pausa. O teste com `restricted` aplica a
pausa de `action_blocked`; com `rate_limited`, a de 10 min.
Uma pausa nova nunca encurta a vigente. Persistência em `app_state`: `instagram_downloads_paused_until` (ISO UTC) e
`instagram_pause_reason` (`rate_limited|action_blocked`); uma pausa persistida com motivo antigo `access_refused` é
descartada no startup. Durante a pausa: ingests que precisam
baixar não são reivindicados, imports e prévia de perfil respondem 409 `instagram_paused`, e lotes em `listing`
esperam (não iniciam a listagem até a pausa acabar ou ser liberada). Processamento local e teste de sessão seguem.
Batch `status`: `listing` (runner coletando via adapter; uma importação por vez) -> `running` (jobs criados) ->
`completed` quando todos os jobs são terminais (mesmo com falhas parciais) | `failed` (listagem falhou sem itens)
| `cancelled`. Progresso do batch: `{found, downloaded, processed, skipped, failed, pending, active}`.
Lote por link (`import_mode = 'links'`, seção 8.3): fica em `listing` enquanto há link `pending` (um link consultado
por vez, na mesma fila de listagem); link `pending -> resolved | failed`. Sem link pendente segue para `running` ->
`completed`; se nenhum link gerou job, `failed` com `error_code = links_failed`.
Recovery no startup: jobs `downloading` -> `pending`; `processing` -> `downloaded`; `attempts+1`, `error_code=interrupted`
registrado; esgotado -> `failed`. Batches `listing` reiniciam a listagem (jobs já criados não duplicam). Temporários de
jobs não ativos em `storage\temp` removidos (somente dentro de temp). Nunca marcar `completed` sem arquivo verificado.

## 8. API REST (prefixo `/api`)

Mutações exigem header `X-Media-Tool-Client` não vazio (frontend envia `panel`). Host aceito: `127.0.0.1:<porta>`,
`localhost:<porta>`. CORS só para `http://127.0.0.1:5173`, `http://localhost:5173`. Erros `{detail, code}`.

| Método | Caminho | Corpo / query | Resposta |
|---|---|---|---|
| GET | /api/health | | `{status:"ok", version, time}` |
| GET | /api/diagnostics | | `Diagnostics` |
| POST/GET | /api/profile-analyses | seção 8.8 | Análise de Perfis |
| GET | /api/presets | | `Preset[]` |
| POST | /api/presets | `{name, params}` | `Preset` 201 |
| GET/PUT/DELETE | /api/presets/{id} | PUT `{name?, params?}` | `Preset` / 204 (409 builtin) |
| POST | /api/presets/{id}/duplicate | `{name?}` | `Preset` 201 |
| POST | /api/instagram/profile | `{profile}` | `ProfileInfo` (preview); prazo 50 s no backend: 504 `timeout`; 429 `rate_limited`; 403 `action_blocked`/`access_refused`; 502 `contract_changed`/`unsupported_media`/`network_error`; 409 `profile_preview_in_progress`; 409 `instagram_paused` |
| POST | /api/imports | `{profile, content_type, limit: 1..1000000\|null, preset_id?\|null, process?: bool}` (`limit: null` = "Todos", até o fim real do perfil; `content_type`: `all`\|`reels`\|`photos_carousels` e os antigos `posts`\|`videos`\|`images`; `process` padrão `false` = download-only; `preset_id` padrão `null`; `process: true` sem `preset_id` usa o preset padrão; `edit?: EditSettings\|null` = "Editar após baixar", seção 8.4) | `Batch` 202; 409 `session_missing`; 409 `instagram_paused`; 409 `profile_already_importing`; 422 `invalid_input`\|`edit_with_preset`\|`edit_no_changes` |
| POST | /api/import-groups/preview | `{entries?: string[], text?: string}` | `ProfilesPreview` (sem efeitos, sem Instagram); 422 `too_many_profiles` |
| POST | /api/import-groups | `{profiles: string[], content_type, limit: 1..1000000\|null, preset_id?\|null, process?: bool, edit?: EditSettings\|null}`, query `children_limit` | `ImportGroupDetail` 202 (com `skipped`, `duplicates`); 422 `invalid_profiles`\|`no_profiles`\|`too_many_profiles`\|`edit_with_preset`\|`edit_no_changes`; 409 `profile_already_importing`\|`session_missing`\|`instagram_paused`; 404 `preset_not_found` |
| GET | /api/import-groups | `limit`<=200, `before_id` | `{items: ImportGroup[], total, next_before_id}` |
| GET | /api/import-groups/{id} | `children_limit`<=500 (100), `after_position` | `ImportGroupDetail`; 404 `group_not_found` |
| POST | /api/import-groups/{id}/cancel | query `children_limit` | `ImportGroupDetail`; 409 `group_finished`; 404 `group_not_found` |
| POST | /api/import-links/preview | `{entries?: string[], text?: string}` | `LinksPreview` `{valid: [{shortcode, url}], invalid: [{entry, reason}], duplicates}` (sem efeitos, sem Instagram); 422 `too_many_links` (seção 8.3) |
| POST | /api/import-links | `{links: string[], preset_id?\|null, process?: bool, edit?: EditSettings\|null}` | `Batch` 202 (com `duplicates`); 422 `invalid_links`\|`no_links`\|`too_many_links`\|`edit_with_preset`\|`edit_no_changes`; 409 `session_missing`\|`instagram_paused`; 404 `preset_not_found` (seção 8.3) |
| POST | /api/imports/history/hide | `{batch_ids?: id[] (lotes avulsos), group_ids?: id[]}` OU `{all_finished: true}`, mais `delete_media?: bool`, `dry_run?: bool` (seção 8.1, "Histórico") | `{batches, groups, video_count, total_bytes, shared_count, dry_run, deleted: MediaDeleteResult\|null}` (`shared_count`: vídeos que também estão em outra importação ainda visível); 409 `import_in_progress`; 422 `batch_in_group`\|`validation_error`; 404 `batch_not_found`\|`group_not_found` |
| GET | /api/batches | `limit, offset, source?=instagram_import\|library, group_id?, ungrouped?=true` | `{items: Batch[], total}` (padrão: todos os lotes, inclusive filhos de grupo) |
| GET | /api/batches/{id} | | `Batch` + `jobs: Job[]` |
| GET | /api/batches/{id}/links | `limit`<=500 (200), `offset` | `{items: BatchLink[], total}`; 404 `batch_not_found` (seção 8.3) |
| POST | /api/batches/{id}/cancel | | `Batch` |
| POST | /api/batches/{id}/retry-failed | | `Batch` |
| GET | /api/media | `view=all\|originals\|processed`, `media_type`, `profile`, `q`, `limit`<=200, `offset` | `{items: Media[], total}` |
| GET | /api/media/{id} | | `Media` |
| GET | /api/media/{id}/thumbnail | | image/jpeg |
| GET | /api/media/{id}/original | | arquivo |
| GET | /api/renditions/{id}/file | | arquivo |
| POST | /api/media/{id}/process | `{preset_id}` | `Job` 202 |
| POST | /api/media/process | `{media_ids, preset_id}` | `Batch` 202 |
| POST | /api/edits | `{media_ids: id[] (1..500), settings: EditSettings, trims?: EditTrim[]}` (seção 8.4) | `Batch` 202 (`preset_id: null`, `preset_name: "Edição"`); 404 `media_not_found`; 422 `edit_requires_video`\|`edit_no_changes`\|`edit_trim_out_of_range`\|`validation_error` |
| DELETE | /api/renditions/{id} | | 204 |
| DELETE | /api/media/{id} | `?delete_original=true` obrigatório | 204 |
| POST | /api/media/{id}/open | `{target:"file"\|"folder", rendition_id?}` | `{ok}` |
| POST | /api/library/open-folder | | `{ok}` (seção 8.2) |
| POST | /api/exports/pick-folder | | `PickFolder` (seção 8.2) |
| POST | /api/exports | `ExportRequest` | `Export` 202 (seção 8.2) |
| GET | /api/exports | `batch_id?`, `group_id?`, `limit` | `{items: Export[]}` |
| GET | /api/exports/{id} | | `Export` |
| GET | /api/exports/{id}/download | `token` | arquivo `.zip` (seção 8.2, Baixar ZIP) |
| POST | /api/exports/{id}/cancel | | `Export` |
| POST | /api/exports/{id}/open | | `{ok}` |
| GET | /api/jobs | `status` csv, `batch_id`, `ids` csv, `limit`, `offset`, `before_id`, `created_from`, `created_to` | `{items, total, counts, next_before_id}` (período abaixo da tabela) |
| GET | /api/jobs/{id} | | `Job` |
| POST | /api/jobs/{id}/cancel | | `Job` |
| POST | /api/jobs/{id}/retry | | `Job` |
| GET | /api/settings | | `Settings` (nunca contém segredo) |
| PUT | /api/settings | `{ffmpeg_path?, ffprobe_path?, concurrency?, default_preset_id?, instagram_provider?, instagram_web_doc_ids?}` (`null` volta ao padrão; nome de consulta desconhecido, repetido ou `doc_id` fora de 5 a 25 dígitos = 422) | `Settings` |
| PUT | /api/settings/instagram/session | `{sessionid, csrftoken?, ds_user_id?, mid?, ig_did?}` | `InstagramStatus` (sem segredo) |
| PATCH | /api/settings/instagram/session/cookies | `{csrftoken?, ds_user_id?, mid?, ig_did?}` (vazio/null remove; ao menos um) | `InstagramStatus`; 409 `session_not_editable` |
| DELETE | /api/settings/instagram/session | | `InstagramStatus` |
| DELETE | /api/settings/instagram/pause | | `InstagramStatus` (libera a pausa do Instagram manualmente) |
| POST | /api/settings/instagram/session/test | | `InstagramStatus` com `last_test` atualizado; prazo 40 s no backend; 409 `session_test_in_progress` se já houver um teste em andamento |

**Fila: período (`GET /api/jobs`).** `created_from` e `created_to` (opcionais, ISO 8601 COM fuso, ex.: `2026-10-28T03:00:00.000Z` ou `2026-10-28T00:00:00-03:00`; até 64 caracteres) filtram `created_from <= created_at < created_to` (início inclusivo, fim exclusivo). O backend converte cada limite para UTC no formato de `jobs.created_at` (milissegundos, `Z`; fração abaixo do milissegundo arredonda para cima) e compara como texto. Sem fuso, data inválida, fora do intervalo representável ou `created_from` depois de `created_to` = 422 `validation_error`; vazio = ausente; início igual ao fim = lista vazia. O período vale para `items`, `total` e `counts` (os chips de status) e combina com `status`, `batch_id` e `ids`; a paginação keyset (`before_id`/`next_before_id`) segue igual dentro do período. Sem índice novo: a contagem lê as linhas do filtro (medido com 100 mil jobs: cerca de 45 ms por consulta; 10 mil: cerca de 3 ms; a retenção apaga jobs encerrados há mais de 30 dias).
A página Fila mostra o período (Hoje, Ontem, Últimos 7 dias, Últimos 30 dias, Tudo, Personalizado com data inicial e final inclusivas) no fuso local do computador: manda o início do dia local e o início do dia seguinte em UTC (`toISOString`). "Últimos N dias" inclui hoje. Padrão "Hoje" na Fila geral (o último período escolhido ali fica no `localStorage`, chave `media-tool.queue.period`); aberta pelo link de um lote (`?lote=`), começa em "Tudo". A lista (ordem id DESC) ganha um separador antes dos jobs de cada dia local: "Hoje", "Ontem" ou `dd/mm/aaaa`.

```ts
PresetParams = { metadata_cleanup: bool, trim_start: number /*0..30*/, mirror: bool, speed: number /*0.5..2*/,
  auto_adjust: bool, reencode: bool,       // os 6 acima são obrigatórios; chaves extras rejeitadas
  // Ajustes opcionais (ausente = neutro = desligado):
  zoom?: number /*0..10, % de zoom central; neutro 0*/, brightness?: number /*-0.1..0.1; neutro 0*/,
  contrast?: number /*0.9..1.1; neutro 1*/, saturation?: number /*0.8..1.2; neutro 1*/,
  trim_end?: number /*0..30 s cortados do final; neutro 0*/, pitch_shift?: number /*-3..3, % de tom; neutro 0*/,
  grain?: number /*0..4, granulação; neutro 0*/ }
// Ajuste opcional com valor neutro nunca é gravado nem devolvido: o backend omite a chave (presets e, por cópia,
// jobs, lotes e renditions). Assim os params de um preset sem ajustes opcionais são exatamente as 6 chaves de
// sempre e a dedupe de renditions por `canonical_json(params)` continua casando com as já existentes.
Preset = { id, name, is_builtin, params, created_at, updated_at }
ProfileInfo = { username, user_id|null, full_name|null, avatar_url|null, post_count|null, is_private|null, is_verified|null }
Rendition = { id, media_id, preset_id|null, preset_name|null, job_id|null, processed_sha256, file_size, duration|null,
  width|null, height|null, fps|null, has_audio|null, created_at, file_url }
Media = { id, source_platform, source_profile|null, source_url|null, source_post_id|null, source_media_index, media_type,
  original_filename|null, original_sha256, file_size, duration|null, width|null, height|null, fps|null, has_audio|null,
  video_codec|null, mime_type|null, taken_at|null, created_at, thumbnail_url|null, original_url, renditions: Rendition[], last_job: Job|null }
Job = { id, batch_id|null, media_id|null, rendition_id|null, kind, preset_id|null, preset_name|null, status, stage|null,
  progress|null, error_code|null, error_message|null, attempts, max_attempts, next_attempt_at|null, is_duplicate,
  duplicate_reason|null, source_platform|null, source_profile|null, source_url|null, source_post_id|null,
  source_media_index|null, media_type|null, thumbnail_url|null /*local se existir, senão null*/, created_at,
  started_at|null, finished_at|null, updated_at, elapsed_seconds|null }
Batch = { id, source, source_profile|null, content_type|null, requested_limit|null, preset_id|null, preset_name|null,
  process, status, found, error_code|null, error_message|null, created_at, updated_at, finished_at|null,
  progress: {found, downloaded, processed, skipped, failed, pending, active, duplicates, filtered /*seção 8.6*/},
  counts: {<status>: n}, jobs?: Job[],
  listing_state: "pending"|"running"|"waiting_backpressure"|"waiting_rate_limit"|"paused"|"needs_attention"|"done"|
    "cancelled"|null,
  rate_limit_resume_at: string|null /*ISO UTC*/, rate_limit_pages_used: number|null, rate_limit_pages_max: number|null,
  listing_dropped: { unexpected_total: number, reasons: {<motivo>: n}, pending: number },
  listing_end: "profile_end"|"limit"|null,   // por que a listagem terminou (seção 8.1); null enquanto lista/erro/antigo
  import_mode: "profile"|"links"|null,   // "links" = importação por link (seção 8.3); null em lotes de Biblioteca
  links: { total: number, pending: number, resolved: number, failed: number }|null,   // só em lote por link
  group_id: number|null, group_position: number|null /*1-based*/,
  group_state: "waiting"|"active"|"attention"|"completed"|"failed"|"cancelled"|null /*só em filhos de grupo*/,
  views_filter: ViewsFilter|null /*filtro de visualizações mínimas (seção 8.1); null = sem filtro*/,
  last_export: LastExport|null /*última exportação para pasta concluída do lote; seção 8.2*/ }
  // pending: parte de unexpected_total/reasons que ainda é da página ABERTA da listagem (sem checkpoint; ex.: lote
  // em needs_attention ou parado no meio da página). Já está somada no total para não ficar invisível; vira
  // definitiva se a listagem encerrar ou o lote for cancelado, e é substituída (sem duplicar) se a página for refeita.
  // listing_dropped (GET /api/batches e /api/batches/{id}): mídias/posts que a listagem NÃO pôde adicionar ao lote
  // (inesperado). Não muda `status` (o lote não vira `failed`; os demais downloads seguem). `reasons` só traz nomes
  // fixos: edge_null, edge_not_object, node_null, node_not_object, node_no_code, media_type_missing,
  // carousel_no_children, carousel_child_invalid, carousel_children_missing, video_no_url, image_no_url. Não entram:
  // filtros de propósito (unsupported_type, duplicate, content_filter) nem rendition_candidates_invalid (item
  // entregue em versão menor). Lote antigo, de Biblioteca ou sem descarte: { unexpected_total: 0, reasons: {} }.
  // Post inteiro descartado conta 1 (mínimo: não se sabe quantas mídias tinha). Regras de contagem sem duplicar:
  // docs/BATCH_ROBUSTNESS.md, "Descartes da listagem visíveis no lote".
  // waiting_rate_limit: a listagem espera o teto de páginas por hora; o lote continua ativo e retoma sozinho (não é
  // erro nem pausa). Os três campos rate_limit_* só vêm preenchidos nesse estado (docs/BATCH_ROBUSTNESS.md, seção 9).
InstagramStatus = { session_configured: bool, source: "file"|"env"|null, updated_at|null, extra_keys: string[],
  paused_until: string|null /*ISO UTC*/, pause_reason: "rate_limited"|"action_blocked"|null,
  last_test: {ok, result, code, message, username|null, http_status|null, duration_ms, detail|null,
    session_recognized: bool|null, listing_api: "ok"|"refused"|"rate_limited"|"unknown"|null,
    session_present: bool|null, bootstrap: "ok"|"failed"|"unknown"|null, graphql: "ok"|"failed"|"unknown"|null,
    tested_at}|null }
  // session_present/bootstrap/graphql: etapas do provider web; null em registros antigos e no gallery-dl
  // code inclui os novos contract_changed e unsupported_media (result instagram_error)
  // result: valid|invalid|expired|checkpoint_required|rate_limited|restricted|api_refused|timeout|network_error|instagram_error|session_missing
Settings = { ffmpeg_path|null, ffprobe_path|null, concurrency /*1..4*/, default_preset_id|null,
  instagram_provider: "auto"|"web"|"gallery_dl" /*efetivo: setting > env MEDIA_TOOL_INSTAGRAM_PROVIDER > web*/,
  instagram_web_doc_ids: {[nome da consulta]: doc_id} /*só overrides gravados; nomes de KNOWN_QUERIES*/,
  effective: {ffmpeg_path|null, ffprobe_path|null}, storage: {originals, processed, temp, thumbs}, database_path,
  instagram: InstagramStatus }
Diagnostics = { generated_at, overall: "ok"|"warn"|"error", checks: [{id, label, status:"ok"|"warn"|"error", detail}],
  python:{version, executable}, sqlite:{version, path, schema_version}, ffmpeg:{found, path, version},
  ffprobe:{found, path, version}, gallery_dl:{found, version, path}, storage:{<nome>:{path, exists, writable}},
  worker:{running, concurrency, active_jobs}, instagram:{session_configured}, backend:{host, port, version} }
```
URLs de arquivo na API são relativas (`/api/media/1/thumbnail`). Thumbnails remotas do Instagram não são expostas ao
frontend (o painel só mostra thumbnails locais geradas após o download; preview de perfil pode usar `avatar_url`).

Storage: original promovido de temp por rename atômico para
`storage\originals\<yyyy>\<mm>\<sha256[:2]>\<sha256>.<ext>`; processado em
`storage\processed\<yyyy>\<mm>\<media_id>_<job_id>_<sha8>.<ext>`; thumb `storage\thumbs\<media_id>.jpg`.
Nunca sobrescrever; nome interno nunca vem da origem.

### 8.1 Importação: "Todos", tipo de mídia e vários perfis (grupos)

Contrato para o frontend. Regras de execução completas: `docs/BATCH_ROBUSTNESS.md`, seção 0.1.

**Quantidade (`limit`)**: inteiro de 1 a 1.000.000 ("Últimos N") ou `null` ("Todos"). Fora disso: 422 (validação).
`null` percorre o perfil até o fim real (`has_next_page=false`); não é um número grande. N conta mídias do TIPO
pedido ("100 + Reels" = 100 Reels; a listagem continua até achar 100 ou o perfil acabar). O código
`limit_all_disabled` não existe mais.

**Tipo (`content_type`)**, três opções na interface:

| Interface | `content_type` | O que entra |
|---|---|---|
| Todos | `all` | todos os tipos suportados |
| Reels | `reels` | só publicações classificadas como Reel (item de carrossel nunca entra) |
| Publicações do feed | `photos_carousels` | tudo o que não é Reel: fotos simples, vídeos avulsos antigos (não-Reel) e TODOS os itens dos carrosséis (inclusive vídeo de carrossel) |

O valor `photos_carousels` foi mantido na API por compatibilidade; o rótulo da interface é "Publicações do feed".
`reels` e `photos_carousels` são complementares: toda mídia suportada cai em exatamente um dos dois.

**Vídeo avulso que não é Reel** (publicação de vídeo legada, sem `product_type` `clips` e fora de carrossel): entra
em `all`, em `photos_carousels` e no antigo `videos`. Em `reels` é ignorado como filtro esperado
(`content_filter`), nunca como descarte.

**Método de importação**: "Todos" (`limit: null`), N acima de 5.000 e `photos_carousels` exigem o método web. Com
`instagram_provider = "gallery_dl"` configurado, `POST /api/imports` e `POST /api/import-groups` respondem 422
`not_supported_by_provider` (mensagem em pt-BR) e nada é criado. Em `auto` o pedido é aceito e nunca cai no
gallery-dl: o erro do web fica visível no lote.

Valores antigos ainda aceitos: `posts` (igual a `all`), `videos`, `images`. Carrossel conta por item; se o limite cai
no meio de um carrossel, entram só os primeiros itens dele. O que o filtro ignora NÃO aparece em `listing_dropped`.

**Visualizações mínimas (`min_views`, opcional)**: inteiro estrito de 1 a 10.000.000.000 em `POST /api/imports` e
`POST /api/import-groups` (POR PERFIL). Omitido ou `null` = sem filtro, e tudo fica exatamente como antes (o frontend
OMITE a chave quando o filtro está desligado). `0`, negativo, decimal (`1.5`, `5e5`), texto (`"500000"`) e booleano:
422 (validação). Só vale para vídeo: com `content_type` diferente de `reels` ou `videos`, 422
`min_views_requires_video` (pt-BR) e nada é criado. Só existe no método web: com `gallery_dl`, 422
`not_supported_by_provider`; em `auto` não há fallback. A interface oferece Sem filtro, 100.000, 250.000, 500.000,
1.000.000 e Personalizado, e só com o conteúdo Reels (a interface não oferece "Vídeos"; ao trocar o conteúdo o
filtro volta a "Sem filtro").

Regra: vídeo com contagem de visualizações >= `min_views` é aprovado (igual ao mínimo aprova) e segue o fluxo normal;
abaixo do mínimo ou sem contagem disponível é ignorado ANTES do download (não vira job). Com o filtro, "Últimos N"
conta só vídeos aprovados: a listagem continua até achar N aprovados ou o perfil acabar. Duplicata: vídeo aprovado que
já está na Biblioteca vira job duplicado como sempre (por isso `approved` pode ser maior que `progress.downloaded`);
vídeo abaixo do mínimo não vira job e a mídia da Biblioteca não é tocada. Leitura da contagem e tipos de publicação:
`docs/INSTAGRAM_WEB_CONTRACT.md`, "Contagem de visualizações" (campos NÃO verificados contra uma resposta real da
listagem). Regras de contagem sem duplicar: `docs/BATCH_ROBUSTNESS.md`, "Filtro de visualizações mínimas".

```ts
ViewsFilter = { min_views: number, analyzed: number /*= approved + below + no_count*/, approved: number,
  below: number /*abaixo do mínimo*/, no_count: number /*sem contagem disponível*/,
  pending: number /*parte de analyzed que ainda é da página aberta; no grupo é sempre 0 (já somada)*/ }
```

**Filtro de visualizações DESLIGADO (2026-10-29):** enquanto `import_service.MIN_VIEWS_ENABLED` for `False`, qualquer
`min_views` não nulo em `POST /api/imports` ou `POST /api/import-groups` é 422 `min_views_unavailable` ("O filtro de
visualizações ainda não está disponível.") e nada é criado; `min_views: null` ou omitido segue aceito. Motivo e
evidência: docs/INSTAGRAM_WEB_CONTRACT.md ("Teste real de 2026-10-29").

**Período de postagem (`posted_within_days`, opcional)**: inteiro estrito de 1 a 3650 em `POST /api/imports` e
`POST /api/import-groups` (o mesmo para todos os perfis); omitido ou `null` = qualquer data. Fora disso 422. Só no
método web (com `gallery_dl`, 422 `not_supported_by_provider`). Não existe em `POST /api/import-links`: `min_views` ou
`posted_within_days` não nulos lá = 422 `filters_profile_only` ("O filtro de período/visualizações vale só para
importação por perfil."). O corte `posted_after` (agora - N dias, UTC, `AAAA-MM-DDTHH:MM:SSZ`) é calculado na criação e
gravado (migration 0019: `posted_within_days`, `posted_after` em `import_groups` e `batches`; `batches.listing_period_end`);
retomadas usam sempre esse corte. Publicação mais antiga que o corte não é baixada (`out_of_period`) e a listagem
termina ao chegar nelas (`listing_end: "period_end"`). As visualizações (`min_views`) vêm da aba Reels do perfil SEM
login (docs/INSTAGRAM_WEB_CONTRACT.md); vídeo fora da aba ou sem contagem não é baixado. `views_filter` (lote e grupo)
passa a ser `{min_views: number|null, posted_within_days: number|null, posted_after: string|null, analyzed, approved,
below, no_count, out_of_period, pending}`, presente quando qualquer um dos dois filtros está ligado.
Interface (linha do lote e do grupo): "Mín. 500.000 visualizações: N analisados • N aprovados • N baixados • N abaixo
do mínimo • N sem contagem disponível", com "baixados" = `progress.downloaded` (contagem já existente).

**`Batch.listing_end`**: `"limit"` (achou a quantidade pedida), `"profile_end"` (o perfil acabou; pode ter menos que
o pedido: mostrar "perfil chegou ao fim: `found` de `requested_limit`"; nesse caso `error_message` traz um texto
informativo e `error_code` é `null`), `null` (ainda listando, erro, cancelado ou lote antigo).

**Fim anômalo da paginação** (não é fim do perfil): o lote fica `status: "listing"`, `listing_state:
"needs_attention"`, `error_code: "contract_changed"`, `error_message` em pt-BR, `listing_end: null`. O lote NÃO
conclui; há nova tentativa automática a cada 6 h (até 4) e o botão "Retomar importação"
(`POST /api/batches/{id}/resume-listing`). Na 1a página, sem nenhum item: `status: "failed"` com o mesmo erro.

**Estados apresentados** de um lote/filho: `status` (`listing`|`running`|`completed`|`failed`|`cancelled`) e
`listing_state` (`null`/`pending`|`running`|`waiting_backpressure`|`waiting_rate_limit`|`paused`|`needs_attention`|
`done`|`cancelled`). Em filhos de grupo, `group_state` resume para a lista: `waiting` (na fila; a listagem ainda não
começou, `listing_state` nulo ou `pending`), `active` (listando ou baixando, inclusive `waiting_rate_limit`,
`waiting_backpressure` e `paused`: use `listing_state` para o rótulo), `attention` (`needs_attention`),
`completed`, `failed`, `cancelled`.

```ts
ProfilesPreview = { valid: string[] /*usernames normalizados, minúsculas, na ordem, sem repetição*/,
  invalid: { entry: string /*até 200 caracteres*/, reason: string /*pt-BR*/ }[], duplicates: number }
ImportGroup = { id, content_type, requested_limit: number|null /*POR PERFIL*/, preset_id|null, preset_name|null,
  process: boolean, created_at, finished_at: string|null /*só com todos os filhos encerrados*/,
  status: "active"|"attention"|"completed"|"cancelled",
  attention: boolean /*algum filho em attention ou failed; vale também com status active/completed*/,
  profiles: { total, waiting, active, completed, attention, failed, cancelled } /*os seis somam total*/,
  profile_end_count: number /*filhos que chegaram ao fim REAL do perfil (listing_end = profile_end, sem os do período)*/,
  period_end_count: number /*filhos cuja listagem terminou pelo corte do período de postagem (listing_end = period_end)*/,
  progress: { found, downloaded, processed, skipped, failed, pending, active, duplicates, filtered }
    /*soma das mídias dos filhos; duplicates/filtered: seção 8.6*/,
  listing_dropped_total: number /*soma de listing_dropped definitivo dos filhos; o detalhe fica em cada filho*/,
  views_filter: ViewsFilter|null /*soma dos filhos (com a página aberta de cada um); null = sem filtro*/,
  last_export: LastExport|null /*última exportação para pasta concluída do grupo inteiro; seção 8.2*/ }
ImportGroupDetail = { group: ImportGroup, children: Batch[] /*ordem group_position*/,
  next_after_position: number|null,
  skipped?: { profile: string, reason: "profile_already_importing" }[] /*só na criação*/,
  duplicates?: number /*só na criação*/ }
```
`ImportGroup.status` é derivado dos filhos: `active` se algum filho está `waiting` ou `active`; senão `attention` se
algum está em `attention` (grupo parado aguardando ação ou a nova tentativa de 6 h); senão `cancelled` se todos foram
cancelados; senão `completed` (todos encerrados; confira `attention`/`profiles.failed` para falhas).

**Entradas de perfil**: UM PERFIL POR LINHA (vírgula e ponto e vírgula também separam; ESPAÇO NÃO separa). Cada
entrada: `usuario`, `@usuario` ou URL do perfil (`http`/`https`, com ou sem `www`, barra final, query string e
fragmento). URLs aceitas: `instagram.com/usuario`, `/usuario/` e as abas do perfil `/usuario/reels/` e
`/usuario/tagged/`. Normalização = a de `parse_profile_input` (minúsculas). Linhas vazias são ignoradas e espaços nas
pontas aparados; repetidas (depois de normalizar) são removidas e contadas em `duplicates`. Inválidas (reportadas com
motivo): entrada com espaço interno (`nome com espaço` é UMA entrada inválida, não três perfis), URL que não é de
perfil (`/p/...`, `/reel/...`, `/reels/...`, `/explore/...`, `/stories/...`, `/accounts/...`, `/direct/...`,
`/usuario/<outro caminho>`), outro domínio, caracteres não permitidos, nome reservado (`p`, `reel`, `reels`,
`explore`, `stories`, `accounts`, `direct`, `tv`, `about`, `legal`, `developer`, `api`...) ou fora de
`^[A-Za-z0-9._]{1,30}$`. Teto de sanidade: 10.000 perfis por requisição
(422 `too_many_profiles`).

**Fluxo recomendado**: (1) `POST /api/import-groups/preview` com o texto colado; mostrar `invalid` e `duplicates`;
(2) `POST /api/import-groups` com `profiles: preview.valid`. A criação REJEITA entrada inválida (422
`invalid_profiles`, sem criar nada) e devolve no erro `invalid`, `valid` e `duplicates` para reenviar só as válidas.

`POST /api/import-groups/preview` (sem efeitos; nenhuma requisição ao Instagram):
```json
{ "text": "usuario_um\n@Usuario_Dois\nhttps://www.instagram.com/usuario.tres/?hl=pt\n\nusuario_um\nhttps://www.instagram.com/p/ABCDE12345/" }
```
```json
{ "valid": ["usuario_um", "usuario_dois", "usuario.tres"],
  "invalid": [{ "entry": "https://www.instagram.com/p/ABCDE12345/", "reason": "a URL não aponta para um perfil" }],
  "duplicates": 1 }
```

`POST /api/import-groups` -> 202:
```json
{ "profiles": ["usuario_um", "usuario_dois", "usuario.tres"], "content_type": "reels", "limit": 1000,
  "preset_id": null, "process": false }
```
```json
{ "group": { "id": 7, "content_type": "reels", "requested_limit": 1000, "preset_id": null, "preset_name": null,
             "process": false, "created_at": "2026-10-06T12:00:00.000Z", "finished_at": null,
             "status": "active", "attention": false,
             "profiles": { "total": 2, "waiting": 2, "active": 0, "completed": 0, "attention": 0, "failed": 0,
                           "cancelled": 0 },
             "profile_end_count": 0,
             "progress": { "found": 0, "downloaded": 0, "processed": 0, "skipped": 0, "failed": 0, "pending": 0,
                           "active": 0 },
             "listing_dropped_total": 0 },
  "children": [
    { "id": 41, "source": "instagram_import", "source_profile": "usuario_um", "content_type": "reels",
      "requested_limit": 1000, "preset_id": null, "preset_name": null, "process": false, "status": "listing",
      "found": 0, "error_code": null, "error_message": null, "created_at": "2026-10-06T12:00:00.000Z",
      "updated_at": "2026-10-06T12:00:00.000Z", "finished_at": null, "listing_state": null,
      "rate_limit_resume_at": null, "rate_limit_pages_used": null, "rate_limit_pages_max": null,
      "paused_reason": null, "paused_until": null,
      "listing_dropped": { "unexpected_total": 0, "reasons": {}, "pending": 0 }, "listing_end": null,
      "group_id": 7, "group_position": 1, "group_state": "waiting",
      "progress": { "found": 0, "downloaded": 0, "processed": 0, "skipped": 0, "failed": 0, "pending": 0,
                    "active": 0 },
      "counts": { "pending": 0, "downloading": 0, "downloaded": 0, "processing": 0, "completed": 0, "failed": 0,
                  "cancelled": 0 }, "jobs": null, "jobs_truncated": null },
    { "id": 42, "source_profile": "usuario.tres", "group_id": 7, "group_position": 2, "group_state": "waiting",
      "...": "demais campos de Batch" }
  ],
  "next_after_position": null,
  "skipped": [{ "profile": "usuario_dois", "reason": "profile_already_importing" }],
  "duplicates": 0 }
```
- Um lote filho por perfil criado, na ordem da lista (`group_position` 1..n entre os criados). `limit` é POR PERFIL.
- Perfil que já tem importação ativa (lote em `listing`) não derruba o pedido: volta em `skipped` e os demais são
  criados. Se TODOS forem pulados: 409 `{code: "profile_already_importing", detail, skipped: [...]}`, nada criado.
- `children` traz a primeira página (`children_limit`, padrão 100, máx. 500); as seguintes por
  `GET /api/import-groups/{id}?after_position=<next_after_position>`.
- Erros: 422 `invalid_profiles` `{detail, code, invalid: [{entry, reason}], valid: [...], duplicates}`; 422
  `no_profiles` (lista só com vazios); 422 `too_many_profiles`; 422 `not_supported_by_provider` ("Todos", N > 5.000 ou
  `photos_carousels` com o método gallery-dl configurado; vale também em `POST /api/imports`); 409
  `session_missing`; 409 `instagram_paused`; 404 `preset_not_found`.

`GET /api/import-groups?limit=50&before_id=` -> `{ "items": [ImportGroup, ...], "total": 12, "next_before_id": 3 }`
(ordem id DESC; `next_before_id` `null` no fim). Custo fixo para a página inteira (lista, agregados dos filhos e
`last_export`), independente do número de grupos e de filhos.

`GET /api/import-groups/{id}?children_limit=100&after_position=100` -> `ImportGroupDetail` (sem `skipped`/
`duplicates`). Cada filho é um `Batch` completo: `source_profile`, `group_position`, `status`, `listing_state`
apresentado (inclusive `waiting_rate_limit` com os campos `rate_limit_*`, e `waiting_backpressure`), `group_state`,
`found`, `progress`, `requested_limit`, `listing_end`, `listing_dropped`, `error_code`/`error_message`. Custo
independente do número de filhos (sem consulta por filho). Exemplo de filho em andamento e de filho com atenção:
```json
{ "id": 41, "source_profile": "usuario_um", "group_position": 1, "group_state": "active", "status": "listing",
  "listing_state": "waiting_rate_limit", "rate_limit_resume_at": "2026-10-06T13:02:11.000Z",
  "rate_limit_pages_used": 600, "rate_limit_pages_max": 600, "found": 3120, "requested_limit": null,
  "listing_end": null, "progress": { "found": 3120, "downloaded": 3051, "processed": 0, "skipped": 0, "failed": 2,
  "pending": 60, "active": 2 }, "...": "demais campos de Batch" }
{ "id": 43, "source_profile": "usuario_quatro", "group_position": 3, "group_state": "attention",
  "status": "listing", "listing_state": "needs_attention", "error_code": "contract_changed",
  "error_message": "O Instagram interrompeu a paginação do perfil antes do fim (resposta sem continuação utilizável). A importação não foi dada como concluída e o ponto de retomada foi mantido.",
  "found": 240, "listing_end": null, "...": "demais campos de Batch" }
```

`POST /api/import-groups/{id}/cancel` -> `ImportGroupDetail`: cancela todos os filhos ainda abertos (`listing` ou
`running`) numa transação só (nenhum filho da fila começa a listar depois); o filho que está listando/baixando para
de forma cooperativa. Os concluídos/falhos ficam como estão. 409 `group_finished` se não há filho aberto. Cancelar, retomar
(`resume-listing`), recomeçar (`restart-listing`) ou repetir falhas (`retry-failed`) de UM filho: rotas de lote
existentes, com o `id` do filho.

`GET /api/batches`: continua listando todos os lotes, inclusive filhos de grupo (compatibilidade), agora com
`group_id`/`group_position`/`group_state`/`listing_end` em cada item (lotes antigos: `null`). Para a lista de
"importações recentes" sem poluição use `?ungrouped=true` (só lotes avulsos e antigos) junto com
`GET /api/import-groups`; `?group_id=<id>` devolve só os filhos de um grupo (paginação por `offset`).

Fila do grupo: um perfil por vez na listagem, na ordem `group_position`; os demais ficam `group_state: "waiting"`.
O filho seguinte começa quando o da vez terminou de listar (os downloads dele seguem em paralelo) ou ficou parado por
conta própria (`attention`, pausa do lote). Erro em um perfil não afeta os outros. Pausa global do Instagram
(`GET /api/settings` -> `instagram.paused_until`) segura todos e a listagem retoma do checkpoint. Depois de reiniciar
o app a fila continua de onde parou, na mesma ordem.

#### Histórico de "Importações recentes" (remover e limpar)

`POST /api/imports/history/hide` tira itens da lista de importações recentes SEM apagar o lote: grava
`history_hidden_at` (migration 0018, aditiva: `batches.history_hidden_at` e `import_groups.history_hidden_at`, NULL =
visível; não existe 0017 neste código). `GET /api/batches?history=true` e `GET /api/import-groups?history=true` (usados
pela página Importar em "recentes" e "ver todas") deixam de fora os ocultos, inclusive no `total`; sem `history` as
listas, `GET /api/batches/{id}`, a Fila, exportações (antigas e novas), "Abrir pasta" e as pastas da página Editar
(derivadas dos lotes) continuam iguais.
- Item: `batch_ids` (lote avulso, `source = instagram_import`, sem grupo; filho de grupo -> 422 `batch_in_group`) e/ou
  `group_ids` (o grupo inteiro). Em andamento (lote `listing`/`running`, inclusive pausado ou aguardando ação; grupo
  com algum filho assim) -> 409 `import_in_progress` ("Cancele ou aguarde terminar"), nada muda. Cancelado, concluído
  ou falho pode sair. Repetir num item já oculto é aceito.
- `all_finished: true`: todos os lotes avulsos e grupos terminados ainda visíveis; os em andamento ficam.
- Contagem: `video_count`/`total_bytes` (original + versões) dos VÍDEOS dessas importações, pela mesma regra das
  pastas da página Editar. `dry_run: true` só devolve a contagem (a confirmação da tela mostra quantos vídeos e o
  espaço).
- `delete_media: true` (desmarcado por padrão na tela): depois de ocultar, apaga esses vídeos com a mesma rotina de
  `POST /api/media/delete` (item a item; job ou exportação em andamento -> pulado com o motivo em `deleted.skipped`).
  Fotos não são apagadas. Um vídeo que também está em outra importação some dela também.

### 8.2 Exportar para pasta

Camada de exportação amigável sobre a Biblioteca. O armazenamento interno (`storage\originals`, SHA-256, dedupe,
histórico) não muda e continua sendo a fonte da verdade: exportar é COPIAR os originais para UMA pasta plana escolhida
pelo usuário (sem subpastas; vários perfis ficam juntos). Nunca move, nunca cria hardlink/symlink/junção, nunca
sobrescreve. Apagar a pasta exportada depois não afeta a Biblioteca. Por padrão exporta o ORIGINAL; com
`content: "edited"` exporta as versões editadas/processadas (renditions) e com `content: "both"` os dois (abaixo,
"Conteúdo").

**Destino autorizado (segurança).** Nenhuma rota aceita caminho vindo do cliente. O destino só existe de duas formas:
1. `POST /api/exports/pick-folder` (sem corpo): o BACKEND abre o diálogo nativo "Selecionar pasta" do Windows num
   processo filho curto, sem shell, com lista de argumentos fixa, ambiente filtrado, sem janela de console e prazo de
   10 min (estourou = cancelado). Dois métodos, nesta ordem: (a) `python -m app.services.folder_picker_child`
   (tkinter; janela dona `Tk()` com `-topmost`, `lift()`, `focus_force()` e `update()` antes de
   `askdirectory(parent=root, mustexist=True)`; diretório inicial pelo stdin; resposta em JSON só com ASCII);
   (b) se o tkinter/Tcl não inicializar (erro de import, `TclError`, processo que não inicia ou resposta inválida):
   `powershell.exe -NoProfile -NonInteractive -STA -Command <script fixo>` com
   `System.Windows.Forms.FolderBrowserDialog` (diretório inicial pela variável `MEDIA_TOOL_PICKER_INITIAL` só do
   filho, passada apenas se for diretório absoluto existente; resposta em Base64 de UTF-8). Cancelar o diálogo não
   passa para o método seguinte. Nenhum método funcionou (ou fora do Windows): 501 `picker_unavailable`, com
   mensagem orientando a usar a pasta padrão. O caminho escolhido chega íntegro com acentos, Unicode e espaços,
   sem depender do codepage do console. Um diálogo por vez (segundo pedido: 409 `picker_busy`). A pasta escolhida é validada e fica guardada NO SERVIDOR, ligada a um token
   aleatório (`secrets.token_urlsafe(32)`) de USO ÚNICO e validade de 10 min. Diretório inicial do diálogo: a última
   pasta usada, senão `<Documentos>\Media Tool\Exports` (criada sob demanda).
2. `destination: {default: true}`: o servidor cria `<Documentos>\Media Tool\Exports\Importacao-AAAA-MM-DD-HHMM`
   para lote/grupo e `...\Biblioteca-AAAA-MM-DD-HHMM` para seleção da Biblioteca (sufixo `-2`, `-3`... se já
   existir). No painel, a Biblioteca só oferece essa opção quando o seletor responde `picker_unavailable`.

Validação do destino, feita na escolha E de novo quando a cópia começa: caminho absoluto e sem `..`; existente e
pasta; resolvido (links e junções seguidos, então um atalho para dentro do storage é recusado); não é raiz de unidade
nem de compartilhamento; não está dentro (nem é pai) das pastas gerenciadas pelo Media Tool (pasta do projeto,
storage, data com banco/backups/secrets, logs, inclusive quando configuradas fora do projeto); não está dentro de
`%SystemRoot%`, `Program Files`, `Program Files (x86)` ou `ProgramData`; gravável. Unidade local, removível e de rede
(UNC ou unidade mapeada) são aceitas: é um destino do usuário, escolhido no diálogo nativo desta máquina.
"Dentro" e "pai" são decididos por IDENTIDADE de arquivo (volume + número do arquivo, `os.stat`), além do texto do
caminho: para o destino e cada ancestral dele, a identidade é comparada com a de cada pasta gerenciada e do sistema; e
a identidade do destino é comparada com a de cada ancestral das pastas gerenciadas. Assim um caminho alternativo
para a mesma pasta (`\\localhost\C$\...`, `\\?\C:\...`, unidade mapeada para um compartilhamento desta máquina,
junção) é recusado igual. `\\?\` é normalizado e `\\.\` é recusado. A mesma verificação roda na escolha, no início
da cópia e no "Abrir pasta"; durante a cópia, antes de cada arquivo, o executor confere que a pasta de destino continua
sendo a MESMA pasta validada (se foi trocada, para com `export_folder_missing`). Caminhos com mais de 260 caracteres
são aceitos (checagens e I/O com o prefixo de caminho longo); se ainda assim não for utilizável: `export_dest_too_long`.

| Método | Caminho | Corpo / query | Resposta |
|---|---|---|---|
| POST | /api/exports/pick-folder | (sem corpo; qualquer corpo é ignorado) | `PickFolder`; 409 `picker_busy`; erros de destino abaixo; 501 `picker_unavailable` |
| POST | /api/exports | `ExportRequest` | `Export` 202 (`status: "pending"`, `total` já contado); 404 `batch_not_found`\|`group_not_found`; 409 `export_token_invalid`\|`export_empty`\|`export_already_running` (corpo com `export_id` da existente)\|`export_queue_full`; erros de destino; 422 `validation_error` (qualquer campo extra, como `path`, é rejeitado) |
| GET | /api/exports | `batch_id?`, `group_id?`, `kind?=batch\|group\|library`, `limit`<=50 (10) | `{items: Export[]}` (id DESC) |
| GET | /api/exports/{id} | | `Export` (progresso); 404 `export_not_found` |
| POST | /api/exports/{id}/cancel | | `Export`; 409 `export_finished`; 404 `export_not_found` |
| POST | /api/exports/{id}/open | (sem corpo) | `{ok}`: abre no Explorer a pasta do REGISTRO no banco, revalidada; 409 `export_folder_missing`; 422 `export_dest_forbidden`; 404 `export_not_found`; 501 `unsupported` |
| POST | /api/library/open-folder | (sem corpo) | `{ok}`: abre no Explorer a pasta física dos originais (`Settings.storage.originals`); 501 `unsupported` |

Todas as rotas POST acima são mutações: valem as proteções da seção 8 (Host local, header `X-Media-Tool-Client`,
`Origin` permitido).

```ts
PickFolder = { cancelled: boolean, token: string|null, path_display: string|null }   // cancelled: true = diálogo fechado
ExportSource =
  | { kind: "batch", id: number }                                 // lote avulso ou UM filho de grupo
  | { kind: "group", id: number }                                 // importação de vários perfis inteira
  | { kind: "library", selection: ExportSelection }
ExportSelection =
  | { media_ids: number[] /*sem teto de quantidade; limite real = corpo de 1 MB*/ }
  | { filter: { view?: "all"|"originals"|"processed", media_type?: "video"|"image", q?: string, profile?: string },
      exclude_ids?: number[] }                     // "todos os resultados do filtro", resolvido no servidor
ExportRequest = { source: ExportSource, destination: { token: string } | { default: true } | { zip: true },
  content?: "originals" | "edited" | "both" /*padrão "originals"; ausente = pedido de antes*/ }
Export = { id, kind: "batch"|"group"|"library", batch_id|null, group_id|null, dest_path /*só exibição*/,
  status: "pending"|"running"|"completed"|"cancelled"|"failed",
  total, processed /*copied + skipped_existing + missing + failed*/,
  copied /*gravadas na pasta, inclui renamed*/, skipped_existing /*arquivo idêntico já estava na pasta*/,
  renamed /*gravadas com outro nome por colisão*/, missing /*arquivo (original ou versão) ausente no storage*/, failed,
  error_code|null, error_message|null, created_at, started_at|null, finished_at|null,
  content: "originals"|"edited"|"both" }
LastExport = { id, dest_path, status, total, copied, skipped_existing, renamed, missing, failed, finished_at|null }
```
`Batch.last_export` e `ImportGroup.last_export`: última exportação CONCLUÍDA daquele lote/grupo (ou `null`); é ela
que o "Abrir pasta" da importação abre (`POST /api/exports/{last_export.id}/open`). `null` = orientar a usar
"Exportar para pasta". A exportação de um grupo aparece no grupo, não nos filhos.

Exemplos:
```json
POST /api/exports/pick-folder          -> { "cancelled": false, "token": "q3J...43 caracteres...", "path_display": "D:\\Fotos\\Minha pasta" }
POST /api/exports/pick-folder          -> { "cancelled": true, "token": null, "path_display": null }
POST /api/exports  { "source": { "kind": "group", "id": 7 }, "destination": { "token": "q3J..." } }
POST /api/exports  { "source": { "kind": "batch", "id": 41 }, "destination": { "default": true } }
POST /api/exports  { "source": { "kind": "library", "selection": { "media_ids": [12, 15, 18] } },
                     "destination": { "token": "q3J..." } }
POST /api/exports  { "source": { "kind": "library", "selection": { "filter": { "view": "all", "media_type": "video",
                     "q": "perfil" }, "exclude_ids": [15] } }, "destination": { "token": "q3J..." } }
```
```json
{ "id": 3, "kind": "group", "batch_id": null, "group_id": 7, "dest_path": "D:\\Fotos\\Minha pasta",
  "status": "running", "total": 10000, "processed": 3482, "copied": 3470, "skipped_existing": 12, "renamed": 3,
  "missing": 0, "failed": 0, "error_code": null, "error_message": null,
  "created_at": "2026-10-06T12:00:00.000Z", "started_at": "2026-10-06T12:00:00.412Z", "finished_at": null }
```

**O que entra.** `batch`: toda mídia ligada a um job de ingest ou de process CONCLUÍDO do lote (process: lote de
Edição ou de processamento da Biblioteca; num lote de importação os dois apontam para as mesmas mídias), inclusive os concluídos como
duplicata (`source_id` ou `sha256`), que apontam para a mídia que já existia na Biblioteca, UNIDA à relação durável
`batch_media` (abaixo); cada mídia uma vez.
`group`: a união de todos os filhos, cada mídia uma vez (mídia compartilhada entre perfis entra uma vez). `library`:
os ids pedidos (ids desconhecidos ou repetidos são ignorados) ou todas as mídias do filtro menos `exclude_ids`; o
filtro é congelado no pedido (mídias importadas depois não entram). Sem nenhuma mídia: 409 `export_empty`, nada é
criado. Não há teto numérico de ids: o limite é o corpo de 1 MB da requisição; a seleção por filtro não manda ids.

**Conteúdo (`content`).** Guardado no JSON `exports.selection` (chave `content`; sem migração). `"originals"` (padrão)
não grava nada: o registro, o total, os nomes e o ZIP são os de antes; registro antigo sem a chave = `originals`.
`"edited"`: só as versões (renditions); `"both"`: o original de cada mídia e depois as versões dela; `total` = arquivos
(mídias + versões). Quais versões entram: `batch` = as das mídias do lote com `renditions.preset_id IS lote.preset_id`
e `renditions.params` = params do job `process` daquela mídia no lote (o primeiro); sem o job (apagado pela
retenção), os guardados em `batch_media.params` (migration 0020: o purge copia os params do job `process` concluído
junto com o par lote -> mídia); sem nenhum dos dois, `lote.params` (nos lotes sem recorte manual os três são iguais;
na Edição com recorte o job de um vídeo recortado tem params próprios, seção 8.4) (JSON canônico dos dois lados; são as saídas daquele lote, inclusive as
reaproveitadas de um job anterior; uma por mídia, a de MENOR id, a mesma que a dedupe do processamento
(`find_rendition`) devolve e para a qual o job reaproveitado aponta); `group` = a mesma regra em cada filho;
`library` = TODAS as versões das mídias selecionadas (edições e presets), em ordem de id. Uma versão é lida só pelo
`processed_path` dela (nunca pelo original); arquivo ausente conta `missing` e a exportação segue. `edited` sem
nenhuma versão: 409 `export_empty` ("Este lote não tem versões editadas ou processadas."; grupo/Biblioteca com texto
equivalente); lote só de download com `both`: só os originais. A regra "uma exportação aberta por origem e modo" vale
independentemente de `content`. A pasta e o ZIP continuam PLANOS também em `both`; cada arquivo do storage entra uma
vez. ZIP só de versões: `editados-AAAA-MM-DD-HHMM.zip`; lote de Edição da Biblioteca com originais ou os dois:
`edicao-AAAA-MM-DD-HHMM.zip` (importação com "Editar após baixar" continua `importacao-...zip`). Mudança de comportamento: um lote "Processar com preset" da Biblioteca (só jobs
`process`) antes dava 409 `export_empty` e agora exporta (os originais das mídias processadas, ou as versões com
`content`).

**Relação durável lote -> mídia (`batch_media`).** A retenção (jobs encerrados há mais de 30 dias) apaga jobs
antigos; para a exportação continuar representando o lote inteiro meses depois, a migration 0013 cria
`batch_media(batch_id, media_id)` com backfill dos ingests concluídos existentes, e
`JobRepository.purge_finished_before` (a única rotina que apaga jobs) grava ali, na MESMA transação e ANTES do
`DELETE`, os pares (lote, mídia) dos ingests concluídos do trecho que vai apagar. Pipeline, downloader e dedupe não
foram alterados (a purga grava os pares de ingests E de process concluídos, então um lote de Edição antigo continua
exportando). A consulta da exportação de lote/grupo é a união `jobs concluídos do lote ∪ batch_media`, paginada
por keyset sobre `media.id`, sem materializar a lista. Mídia excluída da Biblioteca (ou lote apagado) sai da relação
por `ON DELETE CASCADE`.

**Nomes dos arquivos** (`app/services/export_names.py`): `{perfil}_{AAAA-MM-DD}_{codigo_do_post}[_{n}].{ext}`. Data =
a de `taken_at` no fuso do Media Tool (`app/services/export_time.py`: `MEDIA_TOOL_TIMEZONE` (IANA, via `zoneinfo`
+ `tzdata`) > fuso do sistema (no Windows com as regras de horário de verão do ano do instante, via
`SystemTimeToTzSpecificLocalTimeEx`; regras históricas a partir do primeiro ano registrado no Windows, 2004) > UTC;
resolvido uma vez por processo (mudar a variável ou o fuso do sistema exige reiniciar); `taken_at` guardado não
muda; o mtime das entradas do ZIP usa a hora local do mesmo fuso e o mtime do arquivo copiado para pasta é o
instante da publicação; data fora do calendário representável -> nome sem data); `n` (1-based) só em item de carrossel (post com mais de um item na Biblioteca, ou item de índice
maior que zero); sem código do post: `{perfil}_{AAAA-MM-DD}_{id da mídia}`; sem perfil: prefixo `midia`; sem perfil
nem código: `midia_{12 primeiros caracteres do SHA-256}`. Vídeo enviado do PC (`source_platform = 'local'`): o nome
original enviado (`original_filename`) sem a extensão, sanitizado (dois `clip.mp4` -> `clip.mp4` e `clip (2).mp4`
pela colisão comum); sem nome enviado, a regra acima. Extensão = a do arquivo no storage (da versão, para uma
versão). Versão editada/processada: o nome base ganha a marca da versão antes do sufixo de colisão e da extensão
(`{nome}{marca}[ (n)].{ext}`): Edição (`preset_id` nulo E nome "Edição") = `_editado` + `_{início}-{fim}` (recorte
manual: `trim_start` e `cut_end` com até 3 casas, sem zeros à direita, ex. `_3.25-12.8`, `_2.5-7.125`) ou `_{N}s` (limite de duração sem a margem de
0,03 s) + `_{V}x` (velocidade diferente de 1) + `_nitido`, ex. `_editado_15s_1.5x`; preset =
`_{nome do preset sanitizado}` (sem nome: `_processado`; vale também para preset excluído, cujo `preset_id` virou
nulo); até 40 caracteres; o corte de 120 caracteres é no nome
base, nunca na marca. Duas versões com a mesma marca caem na colisão comum (` (2)`). Sanitização: Unicode
NFC; `<>:"/\|?*` e caracteres de controle viram `_`; sem ponto/espaço nas pontas; nome reservado do Windows (`CON`,
`PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`, com ou sem extensão) ganha `_` na frente; nome inteiro limitado a
120 caracteres (o corte é no nome base, nunca na extensão). O I/O usa o prefixo `\\?\` (caminhos longos).

**Colisão** (nome já existe na pasta; diferença só de maiúsculas/minúsculas conta como colisão no Windows):
arquivo IDÊNTICO (mesmo tamanho e mesmo SHA-256 do original; o hash do arquivo da pasta só é lido quando o tamanho
bate) -> não copia, conta `skipped_existing`; arquivo DIFERENTE -> grava com sufixo ` (2)`, ` (3)`... e conta
`renamed` (o arquivo que estava lá nunca é tocado). Reexportar para a mesma pasta é idempotente.

**Execução** (`app/jobs/export_runner.py`): thread própria, UMA exportação por vez (as demais ficam `pending`, em
fila, na ordem de criação); não usa o pool de download/FFmpeg. Mídias lidas em páginas de 500 por keyset (`id`
crescente); cada original é copiado em blocos de 1 MiB para um temporário
`.<nome>.<id da exportação>.<8 hex aleatórios>.mtpart` na pasta de destino, o tamanho é conferido e o temporário é
renomeado sem sobrescrever (se outro programa criar o arquivo final entre a cópia e o rename, ele fica intacto e a
mídia sai com sufixo). **Regra dos temporários:** a exportação só remove arquivos que ESTA execução criou. O
temporário nasce com abertura exclusiva (nome já existente: sorteia outro, nunca apaga), fica registrado em memória
e só os registrados são removidos. Depois de um reinício nada é apagado por padrão de nome: um `.mtpart` pode
sobrar na pasta e pode ser apagado à mão. Progresso gravado no banco a cada 200 arquivos ou
1 s (não a cada arquivo). Cancelamento (`POST /api/exports/{id}/cancel`): `pending` vira `cancelled` na hora;
`running` para entre arquivos ou no próximo bloco de um arquivo grande, remove o temporário e mantém o que já foi
copiado. Original ausente no storage: conta `missing` e segue. Falha ao gravar um arquivo: conta `failed` e segue;
25 falhas seguidas param a exportação (`export_write_failed`). Disco de destino cheio: para com `export_disk_full`.
Pasta de destino sumiu: `export_folder_missing` (durante a cópia) ou `export_dest_missing` (antes de começar).
Startup: exportação que ficou `running` vira `failed` com `error_code: "interrupted"` (não é retomada sozinha; basta
exportar de novo para a mesma pasta); as `pending` continuam na fila.
Limites: uma exportação aberta (`pending`/`running`) por origem (mesmo lote, mesmo grupo, ou qualquer exportação da
Biblioteca): a segunda recebe 409 `export_already_running`; no máximo 20 `pending` na fila (409 `export_queue_full`).
O token da pasta só é gasto quando a exportação é criada (pedido vazio ou recusado não obriga a escolher a pasta de
novo). Ao concluir, `processed == total`: mídia excluída durante a cópia conta em `missing`; mídia com dado inesperado
conta em `failed` e a exportação segue. Pasta padrão criada pelo servidor para a exportação é removida se ficar VAZIA
(só `rmdir`) quando a exportação é cancelada, falha, é interrompida ou nem chega a ser criada.

#### Baixar ZIP (`mode: "zip"`)

Variante da exportação: mesma origem (lote, grupo, Biblioteca), mesma consulta de itens (inclui `batch_media` e as
duplicatas do lote), mesma regra de nomes e o mesmo executor/fila. Em vez de copiar para uma pasta, o servidor gera UM
`.zip` PLANO (todas as entradas na raiz, sem diretórios) e o painel inicia o download normal do navegador.

- Pedido: `POST /api/exports` com `destination: {"zip": true}` (sem token de pasta, sem caminho). Resposta 202 com
  `mode: "zip"`, `status: "pending"`, `zip_name` (`importacao-AAAA-MM-DD-HHMM.zip`; Biblioteca:
  `biblioteca-AAAA-MM-DD-HHMM.zip`). Uma exportação aberta por (origem, modo): ZIP e pasta da mesma origem podem
  conviver na fila; o segundo ZIP da mesma origem recebe 409 `export_already_running`.
- Espaço: antes de criar, o servidor soma `file_size` dos itens (consulta agregada) + margem de 1% + 64 MiB e compara
  com o espaço livre do volume do temporário, descontada a reserva que os downloads já exigem
  (`services.disk.disk_ok`, só lida) quando o temporário está no mesmo volume do storage. Sem espaço: 507
  `export_no_space`, com mensagem dizendo quanto precisa e quanto há; nada é criado. `ENOSPC` durante a escrita:
  `failed` com `export_disk_full` e o temporário removido. O painel avisa que o navegador precisará do mesmo espaço
  de novo na pasta de Downloads (o app não consegue verificar isso).
- Geração (`ExportRunner._run_zip`): `zipfile.ZipFile(..., "w", ZIP_STORED, allowZip64=True)`; cada entrada é aberta
  com `force_zip64=True` e escrita em blocos de 1 MiB lidos do original (nunca o arquivo inteiro em memória; o que
  cresce em memória é só a ficha do diretório central, da ordem de 1 KB por entrada). Nomes: os de `export_names`,
  em UTF-8, únicos sem diferenciar maiúsculas/minúsculas (colisão resolvida no nome com ` (2)`, ` (3)`..., contada em
  `renamed`). Data da entrada = `taken_at` (UTC) quando houver, senão a do arquivo. Original ausente: `missing`, sem
  entrada. Ao final confere que o ZIP abre e que o número de entradas é igual a `copied`, sem repetição.
- Temporário: `<data>\exports-tmp\export-<16 hex>.zip.part` durante a preparação, renomeado para `.zip` ao ficar
  pronto. Pasta exclusiva do servidor, fora de `storage` (a limpeza de `storage\temp` do startup não a alcança),
  ignorada pelo git. O caminho interno nunca sai pela API (`dest_path` de um ZIP é o `zip_name`).
- `Export` ganha: `mode: "folder"|"zip"`, `zip_name|null`, `zip_bytes|null` (só pronto), `zip_ready: boolean`,
  `zip_expires_at|null`, `download_url|null` (só pronto; traz o token de download). `status` ganha `expired`.
  `LastExport` ganha `mode` e continua sendo a última exportação PARA PASTA concluída (ZIP não entra; "Abrir pasta"
  de um ZIP responde 409 `export_not_folder`).
- Download: `GET /api/exports/{id}/download?token=<token>` -> o arquivo em streaming (`FileResponse`),
  `Content-Type: application/zip`, `Content-Length`, `Content-Disposition: attachment; filename="<zip_name>"`,
  `Range` aceito (206). A rota é GET (sem o guarda de mutação): o token aleatório por exportação impede que outra
  página adivinhe ou dispare o download, e a validação de Host continua valendo. Sem token ou token errado: 404
  `export_not_found`; ainda não pronto: 409 `export_zip_not_ready`; removido/vencido: 410 `export_zip_expired`.
- Limpeza: o ZIP pronto vale 24 h (`zip_expires_at`). Ele só conta como BAIXADO quando uma resposta que vai até o
  último byte (200 completo, ou `Range` que termina no fim) acaba de ser transmitida; resposta parcial ou
  interrompida não conta. A partir do FIM dessa transmissão o ZIP vale no máximo mais 1 h ("Baixar novamente"
  funciona enquanto existir). Vencido: o arquivo é removido e SÓ ENTÃO o registro vira `expired`; se a remoção
  falhar (arquivo em uso por um download em andamento, que não é interrompido), o registro continua `completed` e
  a varredura seguinte tenta de novo. Varredura a cada 10 min no executor e no startup: vencidos; arquivos com o
  padrão do servidor sem registro vivo; e, com o disco abaixo do mínimo dos downloads, ZIPs prontos que JÁ foram
  baixados (nunca os ainda não baixados). Cancelamento e falha removem o `.part` na hora. Startup: ZIP `running`
  vira `failed`/`interrupted` e o `.part` daquele registro é removido.
- Segurança da pasta de temporários: só se opera se `<data>\exports-tmp` for um diretório REAL dentro de `<data>`
  (junção, symlink ou outro reparse point: nada é criado, servido nem apagado, e o pedido de ZIP responde 500
  `internal_error`). Só são apagados/servidos arquivos regulares cujo nome casa exatamente com
  `export-<16 hex>.zip[.part]` e cujo caminho resolvido fica dentro da pasta; qualquer outro arquivo que esteja
  ali não é tocado. Os originais nunca são tocados.
- Espaço com fila: na criação, além do ZIP pedido, entram na conta os bytes que os ZIPs `pending`/`running`
  ainda vão escrever (reserva lógica). No INÍCIO da preparação a conta é refeita (sem a fila); sem espaço: `failed`
  com `export_no_space`, sem criar o `.part`. Garantia real: o ZIP não COMEÇA se for deixar o disco abaixo do
  mínimo dos downloads; se outro programa consumir o disco durante a escrita, a preparação pode levar o disco
  abaixo desse mínimo até falhar com `export_disk_full` (e o temporário é removido).
- Download interrompido: a resposta (`ZipFileResponse`) conta os bytes enviados e observa `http.disconnect`; só
  marca como baixado se a faixa foi até o último byte, todos os bytes foram enviados e o cliente não desconectou.
- Falhas por mídia (pasta e ZIP): nada pôde ser gravado -> `failed` com `export_write_failed` ou
  `export_read_failed` e a causa predominante; parte gravada -> `completed` com `error_code`/`error_message`
  informativos (ex.: "3 arquivos não puderam ser gravados na pasta de destino: acesso negado...").
- Outros desfechos: nenhum original disponível (todos ausentes) -> `failed` com `export_empty`, sem ZIP vazio;
  erro de leitura no meio de um original -> `failed` com `export_read_failed` (a entrada já começou e não pode
  ser retirada: basta pedir de novo); data fora do intervalo do formato ZIP (1980 a 2107) é limitada ao
  intervalo; cabeçalho que não pode ser montado para UMA mídia conta `failed` e o ZIP segue.
- Retomada no painel: ao abrir a tela, `GET /api/exports` da origem (`batch_id`, `group_id` ou `kind=library`)
  decide: exportação em andamento -> acompanha; ZIP pronto e ainda válido -> oferece "Baixar novamente" (sem
  download automático); senão nada. 409 `export_already_running` -> o painel acompanha o `export_id` do corpo.
- Sem limite arbitrário de quantidade: não há teto de mídias na exportação nem no ZIP (só disco e tamanho dos
  arquivos). `media_ids`/`exclude_ids` não têm mais teto numérico: o limite real é o corpo de 1 MB da API (dezenas de
  milhares de ids); o painel nunca manda mais que os ids de uma página, porque a seleção ampla ("todos os resultados")
  vai como filtro, resolvido no servidor. O teto de 20 exportações `pending` NÃO é limite de mídias: é proteção da
  fila contra abuso.

```json
POST /api/exports  { "source": { "kind": "group", "id": 7 }, "destination": { "zip": true } }
{ "id": 9, "kind": "group", "group_id": 7, "mode": "zip", "status": "completed", "dest_path": "importacao-2026-10-06-1143.zip",
  "total": 10000, "processed": 10000, "copied": 10000, "renamed": 0, "missing": 0, "failed": 0,
  "zip_name": "importacao-2026-10-06-1143.zip", "zip_bytes": 19756849562, "zip_ready": true,
  "zip_expires_at": "2026-10-07T14:43:10.000Z", "download_url": "/api/exports/9/download?token=q3J...", "...": "demais campos" }
```

**Códigos de erro** (mensagens em pt-BR):

| Código | HTTP | Quando |
|---|---|---|
| `picker_busy` | 409 | já há um diálogo de pasta aberto |
| `picker_unavailable` | 501 | nenhum método de diálogo funcionou neste ambiente (ou fora do Windows); use a pasta padrão |
| `export_token_invalid` | 409 | token inexistente, expirado (10 min) ou já usado |
| `export_dest_invalid` | 422 | caminho vazio, relativo ou com `..` |
| `export_dest_missing` | 409 | a pasta não existe (mais) ou não é pasta |
| `export_dest_drive_root` | 422 | raiz de unidade ou de compartilhamento |
| `export_dest_too_long` | 422 | caminho longo demais para ser usado neste computador |
| `export_already_running` | 409 | a mesma origem já tem exportação `pending`/`running` (`export_id` no corpo) |
| `export_queue_full` | 409 | já há 20 exportações `pending` (proteção da fila; não é limite de mídias) |
| `export_no_space` | 507 | sem espaço livre para o ZIP temporário (a mensagem traz quanto precisa e quanto há) |
| `export_zip_not_ready` | 409 | download pedido antes de o ZIP ficar pronto |
| `export_zip_expired` | 410 | o ZIP já foi removido (prazo vencido) |
| `export_not_folder` | 409 | "Abrir pasta" de uma exportação em modo ZIP |
| `export_dest_forbidden` | 422 | dentro (ou pai) das pastas do Media Tool, ou dentro das pastas do sistema |
| `export_dest_not_writable` | 409 | sem permissão de escrita (ou a pasta padrão não pôde ser criada) |
| `export_empty` | 409 | a seleção não tem nenhuma mídia |
| `export_not_found` | 404 | exportação inexistente |
| `export_finished` | 409 | cancelar exportação já encerrada |
| `export_folder_missing` | 409 | "Abrir pasta" de exportação cuja pasta não existe mais |

Só no registro da exportação (`Export.error_code`, com `status: "failed"`): `export_disk_full`,
`export_write_failed`, `export_folder_missing`, `interrupted`, `internal_error` e os `export_dest_*` da revalidação.

### 8.3 Importação por link

Contrato para o frontend. Regras de execução: `docs/BATCH_ROBUSTNESS.md`, seção 0.2.

**Como a publicação é consultada**: no método web, cada link é UM `GET` **anônimo** do documento público da
publicação (`/p/<código>/` ou `/reel/<código>/`, conforme o link colado), sem nenhum cookie da sessão do dono e sem
seguir redirect; os dados vêm embutidos no HTML (`docs/INSTAGRAM_WEB_CONTRACT.md`, "Documento público da
publicação"). A consulta por POST GraphQL que existia antes foi removida. A criação do lote continua exigindo sessão
configurada (409 `session_missing`), como antes.

**Publicação restrita: 2a tentativa com a sessão** (automática). Só quando o documento público vem restrito (o que
antes terminava em `post_gated_logged_out`), o Media Tool abre o documento da publicação com a sessão salva, como o
navegador logado abriria: Reel -> `GET /reels/<código>/` (o `/reel/` logado responde 302 para ele, verificado em
2026-10-29); `/p/` -> `GET /p/<código>/`, seguindo no máximo UM redirect para a mesma publicação. É o cliente
autenticado da importação por perfil (mesmos headers de navegador, mesmo portão, mesmo teto de páginas por hora;
nunca POST/GraphQL); a mídia vem do nó `xig_fetch_media` da página logada (o feed de Reels dela é ignorado). Links
públicos continuam sem sessão. Só tenta com sessão salva e sem pausa do Instagram (com pausa nada é enviado e o link
segue `pending`). Resultado: publicação entregue -> importada como as outras (o log da publicação traz
`via=session_document`); ainda restrita -> link `failed` com `content_unavailable` ("A publicação continua
indisponível mesmo com a sessão: pode ser de conta privada que você não segue, removida ou restrita por
região/idade."); página logada num formato que o Media Tool não lê -> link `failed` (`contract_changed`); nesses dois
casos a sessão é desligada no resto do lote e a falha não conta no disjuntor `links_failing` (nem as dos
restritos seguintes, com a sessão desligada); recusa (redirect para login/challenge/checkpoint, 401/403, 429, restrição, `sessionid` apagado) ou
falha de rede -> sem nova tentativa, a política de sempre (`rate_limited`/`checkpoint_required`/
`request_auth_failed`/`session_invalid`/`action_blocked` pausam o Instagram; `access_refused` arma a recusa em
espera; rede -> backoff; o link segue `pending`; o diagnóstico traz `stage=session_document`) e a sessão deixa de
ser usada no resto do lote: os links seguintes continuam pelo documento público e os restritos falham com
`content_unavailable` ("A publicação exige login ou está restrita. Nesta importação o Instagram recusou a tentativa
com a sessão salva (ou ela falhou), então o Media Tool não usa mais a sessão para os links restritos deste lote.
Para tentar de novo com a sessão, importe este link em uma nova importação."). Esse desligamento fica só em memória
(um reinício do app o desfaz). Sem sessão salva, a publicação restrita falha com `content_unavailable` ("A
publicação exige login ou está restrita, e não há sessão do Instagram salva para tentar abri-la. Salve a sessão em
Configurações e importe este link de novo."). NÃO VERIFICADO no Instagram real: o caminho `/p/` com a sessão, foto e
carrossel pela sessão e as respostas de recusa.

**Ressalva**: isso foi verificado no Instagram real só para UM Reel público sem login (2026-10-10). NÃO foram
verificados: foto, carrossel, o caminho `/p/`, publicação restrita/privada, a melhor versão de vídeo (as versões
vêm sem largura/altura; fica a primeira) e a qualidade de foto sem login (a capa do Reel veio com no máximo 640 px
de largura; uma foto pode vir abaixo de 1080 px). O restante foi testado só offline, com respostas falsas; a prova
desses casos depende de uma execução autorizada pelo dono (`scripts\poc-instagram-post.ps1`). Por isso existem as
proteções abaixo (disjuntor `links_failing` e link que repete erro). Publicação que exige login só é importada
pela 2a tentativa com a sessão descrita acima.

O usuário cola um ou vários links de publicações (um por linha) e o app baixa TODAS as mídias de cada publicação
(foto, Reel, vídeo, carrossel inteiro). Vários links formam UM lote comum (`Batch` com `import_mode: "links"`,
`source: "instagram_import"`, `source_profile: null`, `content_type: "all"`, `requested_limit: null`): ele aparece em
`GET /api/batches` (inclusive com `?ungrouped=true`), tem jobs em `GET /api/jobs?batch_id=`, aceita `cancel`,
`retry-failed`, `resume-listing` e exportação (`POST /api/exports` com `source: {kind: "batch", id}`, pasta ou ZIP),
igual a um lote de um perfil. Erro em um link não interrompe os outros.

**Links aceitos**: `/p/<código>/`, `/reel/<código>/` e `/reels/<código>/`, também com o usuário na frente
(`/<usuario>/p/<código>/`), com `http`/`https` ou sem esquema (`instagram.com/...`, `www.instagram.com/...`), com ou
sem `www`, barra final, query string e fragmento (descartados). O shortcode identifica a publicação: o mesmo código
em `/p/` e `/reel/` é o mesmo link (fica o primeiro). URL normalizada: `https://www.instagram.com/p/<código>/` ou
`https://www.instagram.com/reel/<código>/`. Lista colada: um link por linha; vírgula, ponto e vírgula e espaço em
branco também separam; linhas vazias são ignoradas. Teto de sanidade: 500 links por lote (`too_many_links`).

**Fluxo recomendado**: (1) `POST /api/import-links/preview` com o texto colado; mostrar `invalid` e `duplicates`;
(2) `POST /api/import-links` com `links: preview.valid.map(v => v.url)`; (3) acompanhar por `GET /api/batches/{id}` e
`GET /api/batches/{id}/links`.

`POST /api/import-links/preview` (sem efeitos; nenhuma requisição ao Instagram). Corpo
`{ "entries"?: string[], "text"?: string }` (os dois podem vir juntos):
```json
{ "text": "https://www.instagram.com/p/ABCDE12345/\ninstagram.com/reel/FGHIJ67890/?igsh=x\nhttps://www.instagram.com/reel/ABCDE12345/\nnao-e-link" }
```
```json
{ "valid": [ { "shortcode": "ABCDE12345", "url": "https://www.instagram.com/p/ABCDE12345/" },
             { "shortcode": "FGHIJ67890", "url": "https://www.instagram.com/reel/FGHIJ67890/" } ],
  "invalid": [ { "entry": "nao-e-link", "reason": "informe o link completo da publicação" } ],
  "duplicates": 1 }
```
`entry` vem cortado em 200 caracteres. Entrada inválida repetida conta em `duplicates` (aparece uma vez em
`invalid`). 422 `too_many_links` acima de 500 entradas.

`POST /api/import-links` -> 202. Corpo `{ "links": string[] (1 ou mais), "preset_id"?: number|null,
"process"?: boolean }` (`process` padrão `false` = download-only; `process: true` sem `preset_id` usa o preset
padrão). Cada item de `links` pode ser um link ou um texto com vários (mesmos separadores da prévia). Resposta: o
`Batch` criado mais `duplicates` (repetições removidas; só nesta resposta):
```json
{ "id": 41, "source": "instagram_import", "import_mode": "links", "source_profile": null, "content_type": "all",
  "requested_limit": null, "status": "listing", "listing_state": null, "found": 0,
  "links": { "total": 2, "pending": 2, "resolved": 0, "failed": 0 }, "duplicates": 1,
  "progress": { "found": 0, "downloaded": 0, "processed": 0, "skipped": 0, "failed": 0, "pending": 0, "active": 0 } }
```
Erros (nesta ordem de conferência): 422 `invalid_links` `{detail, code, invalid: [{entry, reason}], valid:
[{shortcode, url}], duplicates}` (nada é criado; reenviar só as válidas); 422 `no_links` (lista só com vazios); 422
`too_many_links`; 409 `session_missing`; 409 `instagram_paused`; 404 `preset_not_found`. Não há restrição de método
de importação (web ou gallery-dl) nem `profile_already_importing`.

`GET /api/batches/{id}/links?limit=200&offset=0` (`limit` 1..500) -> links do lote na ordem colada:
```json
{ "items": [
    { "position": 1, "url": "https://www.instagram.com/p/ABCDE12345/", "shortcode": "ABCDE12345",
      "status": "resolved", "error_code": null, "error_message": null, "items_found": 4, "profile": "dono_da_publicacao" },
    { "position": 2, "url": "https://www.instagram.com/reel/FGHIJ67890/", "shortcode": "FGHIJ67890",
      "status": "failed", "error_code": "content_unavailable", "error_message": "Conteúdo indisponível.",
      "items_found": null, "profile": null } ],
  "total": 2 }
```
```ts
BatchLink = { position /*1-based*/, url, shortcode, status: "pending"|"resolved"|"failed"|"cancelled",
  error_code: string|null, error_message: string|null, items_found: number|null /*mídias da publicação*/,
  profile: string|null /*dono, minúsculas*/ }
```
- `pending`: ainda não consultado no Instagram. `resolved`: os jobs das mídias foram criados (o download segue pelos
  jobs do lote). `failed`: erro só deste link (`content_unavailable`, `profile_private`, `profile_not_found`,
  `unsupported_media`, `invalid_input`); não é tentado de novo. `cancelled`: link que ainda estava pendente quando o
  lote foi cancelado (derivado na apresentação; em `Batch.links` ele continua contado em `pending`).
- 404 `batch_not_found`. Lote que não é por link: `{ "items": [], "total": 0 }`.

**Campos do `Batch`** (em todas as respostas com `Batch`): `import_mode` (`"links"`, `"profile"` para os demais lotes
de importação, `null` para lotes de Biblioteca) e `links: {total, pending, resolved, failed}` (só em lote por link;
`null` nos demais). `listing_end` fica `null` em lote por link.

**Estados do lote por link** (mesmos campos do lote por perfil):
- `status: "listing"` enquanto há link pendente; `listing_state` segue a listagem: `running` (consultando),
  `waiting_backpressure` (aguardando downloads), `waiting_rate_limit` (teto de páginas por hora; `rate_limit_*`),
  `paused` (pausa do Instagram ou falha de rede com nova tentativa automática; o motivo vem em
  `error_code`/`error_message`) e `needs_attention` (erro do ambiente: sessão, verificação, `contract_changed`,
  `internal_error`; nova tentativa automática a cada 6 h, no máximo 4, e botão **Retomar** =
  `POST /api/batches/{id}/resume-listing`). Nesses estados o link da vez continua `pending` e nenhum outro é pedido.
- Consulta recusada (`error_code: "access_refused"`) e a **recusa em espera** do lote: qualquer `access_refused`
  numa consulta de publicação do lote (na resolução de um link OU na renovação de um link de download vencido;
  no documento público: qualquer redirect, inclusive para a tela de login, e HTTP 401/403; na 2a tentativa com a
  sessão de uma publicação restrita: redirect que não é para login/challenge/checkpoint e HTTP 403)
  arma UMA espera para o lote inteiro, gravada no próprio lote (`error_code: "access_refused"`; sem campo novo).
  Enquanto ela vale, o Media Tool não faz NENHUMA consulta ao Instagram por esse lote: o próximo link não é pedido e
  nenhum link de download é renovado; não há nova tentativa automática (nem a de 6 h, nem no reinício do app, nem
  depois de pausa/backpressure) e o lote não é pausado nem retomado sozinho. Só uma ação explícita do dono desarma:
  **Retomar importação** (`POST /api/batches/{id}/resume-listing`, com o lote ainda em `listing`) ou **Tentar
  falhas novamente** (`POST /api/batches/{id}/retry-failed`, com o lote já fora de `listing`; repetir um job falho
  pela fila, `POST /api/jobs/{id}/retry`, também vale nesse caso). Depois de desarmada, no máximo UMA consulta
  recusada acontece, no lote inteiro e em qualquer dos dois caminhos, antes de a espera se armar de novo (as
  consultas do lote são feitas uma por vez). Outro lote por link não é afetado; lote por perfil não muda.
  - Lote ainda em `listing`: `listing_state: "needs_attention"` sem `listing_retry_at`; o link da vez continua
    `pending`. `error_message` quando a recusa foi na resolução de um link: "O Instagram recusou a consulta desta
    publicação. Isso não indica sessão inválida. Não haverá nova tentativa automática: use "Retomar importação"
    para tentar de novo (uma consulta por vez) ou cancele o lote.", seguida, quando o erro trouxe diagnóstico, de
    ` (diagnóstico: stage=post_document status=403 detail=http_403 ...)`. Quando foi numa renovação: "O Instagram recusou
    a renovação de um link de download deste lote. Isso não indica sessão inválida. Não haverá nova tentativa
    automática: use "Retomar importação" para tentar de novo (uma consulta por vez) ou cancele o lote.".
  - Cada **Retomar importação** faz UMA consulta; recusado pela 2a vez, o MESMO link vira `failed` com
    `access_refused` (o `error_message` do link leva o mesmo sufixo ` (diagnóstico: ...)`) e o lote continua
    parado (o próximo link só é pedido por outra ação). Se esse era o último link pendente, o lote sai de `listing`
    e a espera NÃO é liberada enquanto houver job que ainda possa precisar renovar o link de download: o lote segue
    para `running` com `error_code: "access_refused"` e `error_message` "O Instagram recusou uma consulta deste
    lote. Isso não indica sessão inválida. Não haverá nova tentativa automática. Se algum item do lote falhar,
    use "Tentar falhas novamente" para tentar de novo (uma consulta por vez).". Sem nenhum job criado, o lote
    falha com `links_failed`.
  - Fim do lote com a espera armada: quando todos os jobs terminam SEM nenhum `failed` (nada que o dono possa
    repetir), a espera é desarmada na conclusão e o lote fica `completed` com o estado normal: `error_code:
    null` e `error_message` "X de N links não puderam ser importados." quando há link falho (senão `null`).
    Com job `failed`, o lote conclui mantendo `error_code: "access_refused"` e a mensagem acima, e **Tentar
    falhas novamente** (visível porque há falhas) desarma e vale uma consulta.
  - Lote cancelado: a espera deixa de existir; `error_code: "access_refused"` fica como registro do motivo e
    `error_message` vira "O Instagram recusou uma consulta deste lote antes do cancelamento. Isso não indica
    sessão inválida.".
  - Lote já fora de `listing` (todos os links resolvidos/falhos) com uma renovação recusada: `status` segue
    `running` -> `completed`, com `error_code: "access_refused"` e a mesma mensagem (o job recusado fica
    `failed`, então o botão existe).
  - Job que precisa renovar o link de download com a espera armada: NÃO consulta; fica `failed` com
    `error_code: "access_refused"` e `error_message` "O Instagram recusou uma consulta deste lote e o link de
    download desta mídia precisa ser renovado. Não haverá nova tentativa automática: use "Tentar falhas novamente"
    no lote para tentar de novo (uma consulta por vez)." ou, com o lote ainda em `listing` (onde `retry-failed`
    responde 409 `batch_listing`), "... use "Retomar importação" no lote; quando a importação dos links terminar,
    "Tentar falhas novamente" repete este item.". Job com link de download ainda válido baixa normalmente.
  - **Cancelar** funciona como nos demais estados.
- Diagnóstico (formato do sufixo e do log): `chave=valor` separados por espaço, numa linha só, nesta ordem e só com
  os campos presentes e válidos: `stage` e `detail` (por inteiro `[a-z][a-z0-9_]{0,63}`, sem 6 ou mais dígitos
  seguidos), `status` (inteiro de 100 a 599), `redirect` (só `redirect_login`, `redirect_challenge`,
  `redirect_checkpoint`, `redirect_consent`, `redirect_home`, `redirect_other`), `content_type` (só `text/html`,
  `application/json`, `text/javascript`, `application/x-javascript`, `text/plain`, `application/octet-stream`,
  `image/jpeg`, `image/png`, `image/webp`, `video/mp4`; parâmetros como `; charset=...` e maiúsculas são
  descartados antes da comparação), `size` (inteiro de 0 a 10^12) e `path` (só `/`,
  `/graphql/query`, `/web/search/topsearch/`, `/p/`, `/reel/`; os dois últimos são o tipo do link, nunca o
  código da publicação). Valor fora disso é omitido, nunca truncado. `error_message` com o
  sufixo nunca passa de 500 caracteres: se não couber, o diagnóstico perde campos inteiros do fim (nenhum valor
  cortado ao meio) e, se nem o primeiro couber, a mensagem vai sem sufixo.
- Resposta de OUTRA publicação: o shortcode que a resposta declara tem de ser EXATAMENTE o pedido (maiúsculas e
  minúsculas contam). Se não for, nada é gravado (nenhum job, nenhuma mídia), o link vira `failed` com
  `error_code: "contract_changed"` (detalhe `post_mismatch`; `error_message` "O Instagram respondeu com outra
  publicação, diferente da pedida. Nada foi importado deste link.") e o lote segue para o próximo link. Vale igual
  na renovação de link de download (todos os itens da resposta são conferidos; inclusive na renovação interna
  do provider `gallery-dl`): o job falha com `contract_changed` e nenhum job recebe URL de outra publicação. Resposta sem shortcode nenhum continua como antes: não gera mídia e o link vira `failed` com
  `contract_changed` (detalhe `post_shape_unknown`).
- Página da publicação sem os dados no formato conhecido (detalhe `post_envelope_missing`): o link vira `failed` com
  `error_code: "contract_changed"` e `error_message` "O Instagram abriu a página da publicação, mas sem os dados
  dela no formato que o Media Tool conhece. Nada foi importado deste link."; o lote segue para o próximo link (3
  seguidos param o lote com `links_failing`). Publicação restrita (detalhes `post_gated_logged_out` sem sessão
  salva, `post_gated_with_session` restrita mesmo com a sessão, `post_gated_session_off` sessão já recusada no lote)
  e inexistente (HTTP 404, detalhe `post_http_404`): link `failed` com `error_code: "content_unavailable"`; não armam
  a recusa em espera.
- Limitações conhecidas da recusa em espera: (i) se o aplicativo é encerrado DURANTE a consulta de um link
  (requisição enviada, resposta ainda não recebida), o resultado é desconhecido e o link continua `pending`: ele é
  consultado de novo no próximo início (o índice único dos jobs descarta mídias repetidas); o mesmo vale para uma
  renovação interrompida no meio. (ii) Se o processo morre entre a resposta recusada e a gravação da espera (um
  único UPDATE logo depois da resposta), a recusa não fica registrada e pode haver 1 consulta extra no próximo
  início. (iii) Cancelar o job (ou o trecho) exatamente durante a consulta recusada: o job fica `cancelled`, mas a
  espera é armada do mesmo jeito; não há consulta extra. (iv) Estratégia não padrão `gallery-dl`: a renovação do
  link de download vencido acontece DENTRO do download desse provider e não passa pela espera do lote, então nessa
  estratégia cada job com link vencido ainda pode fazer uma consulta mesmo com a espera armada (a resolução dos
  links pelo runner respeita a espera em qualquer estratégia). A checagem de shortcode vale também ali.
- Link que repete erro do ambiente: na 1a vez o lote fica `needs_attention` com o link `pending`; se o MESMO link
  levar o lote a `needs_attention` pela 2a vez (depois de **Retomar** ou da nova tentativa automática), o link vira
  `failed` com esse erro e o lote segue para os próximos. Publicação com formato desconhecido (`contract_changed`
  com detalhe `post_shape_unknown`) vira `failed` já na 1a vez. Erros de sessão (`session_missing`,
  `session_invalid`, `session_expired`, `checkpoint_required`), pausas do Instagram e falhas de rede NUNCA marcam o
  link como falho.
- Disjuntor `links_failing`: 3 links SEGUIDOS falhos (nenhum resolvido entre eles) com links ainda pendentes param o
  lote em `listing_state: "needs_attention"` com `error_code: "links_failing"` e `error_message` "3 links seguidos
  não puderam ser importados. Confira os links; se eles abrem no navegador, o Media Tool pode precisar de
  atualização. "Retomar importação" continua pelos próximos links.". **Retomar** (`resume-listing`) continua pelos links
  pendentes com o contador zerado (os falhos ficam falhos); a nova tentativa automática de 6 h (máx. 4) vale igual.
  Se os links acabam junto com a 3a falha (ex.: lote de 1 a 3 links, todos falhos), o lote conclui normalmente.
- `POST /api/batches/{id}/restart-listing` responde 409 `listing_not_resumable` em lote por link (não existe "página
  1"; use `resume-listing`).
- Sem link pendente: `status: "running"` até os jobs terminarem e então `completed`. Com parte dos links falha:
  `error_code: null` e `error_message: "X de N links não puderam ser importados."` (o lote conclui normal). Se
  nenhum link gerou mídia: `status: "failed"`, `error_code: "links_failed"`, `error_message` "Nenhum dos N links pôde
  ser importado." (ou "O link não pôde ser importado.").
- Mídia que já está na Biblioteca não é baixada de novo: o job nasce `completed`/`skipped`
  (`is_duplicate`, `duplicate_reason: "source_id"`) e entra na exportação do lote.

**Maior qualidade do vídeo (fluxos separados)**. Quando o documento público traz um `video_dash_manifest` na forma
observada e a maior representação alcança a resolução original, o vídeo é baixado como DOIS arquivos (vídeo e áudio)
e juntado sem recodificar; senão, e em qualquer falha de conteúdo, vale o MP4 direto de sempre. Regra de escolha,
forma aceita e mapa de fallback: `docs/INSTAGRAM_WEB_CONTRACT.md`, "Fluxos separados". Para o frontend:
- `Job.stage` ganha `muxing` ("Juntando vídeo e áudio"), entre `downloading` e `hashing`. Durante `downloading` o
  `progress` é a soma dos dois arquivos.
- `Media.video_codec` (`string|null`): codec de vídeo do original (`"vp9"`, `"h264"`...); `null` em fotos e em mídias
  não analisadas (download puro do MP4 direto, mídias antigas). O detalhe da Biblioteca mostra "Codec" quando há valor.
- **Compatibilidade**: o original de 1080p vem em **VP9 dentro de MP4**. Chromium e Firefox reproduzem; Safari,
  QuickTime e alguns editores podem não abrir. Não há opção de configuração nesta versão e nada é recodificado no
  download; processar com um preset que recodifica gera H.264.
- `job_remote_refs.dash_streams` (migration 0016, TEXT JSON): URLs assinadas e tamanhos/dimensões esperados dos dois
  fluxos. **Tão sensível quanto `media_url`**: nunca sai pela API, por log, mensagem de erro ou diagnóstico, e é
  apagado nos mesmos pontos (job falho/cancelado: NULL; job concluído e lote concluído/cancelado: a linha some). A
  renovação de URL regrava `media_url`, `thumbnail_url` e `dash_streams` juntos (publicação renovada sem manifesto:
  `dash_streams` NULL e o job baixa o MP4 direto).
- Teto por tentativa do job: no máximo **3 requisições ao CDN por chamada de download** (vídeo, áudio, MP4 direto) e, contando a renovação única, no máximo **6 requisições ao CDN e 1 consulta da publicação por tentativa do job**. Fluxo recusado
  (403/404) vai direto para o MP4 direto, sem renovação.
- Sem FFmpeg/ffprobe, ou em qualquer fallback, a qualidade cai para o MP4 direto **sem aviso na interface**: o único registro é a linha de log `dash_fallback=<motivo>` do job.
- Mídia já na Biblioteca nunca é rebaixada nem substituída: reimportar o link continua duplicata por origem, sem
  download. Lotes por perfil não usam fluxos separados.
- **Download de `BaseURL` pelo app e junção NUNCA rodaram contra o CDN real; verificados só com mídias sintéticas e o manifesto real saneado.**

### 8.4 Edição de vídeos

`POST /api/edits` edita vídeos da Biblioteca em lote. Não há tabela nem migração nova: o lote é um lote comum
(`source = "library"`, `process = true`, `preset_id = null`, `preset_name = "Edição"`, `params` = params da edição) e
cada mídia vira um job `process` com `preset_id = null` e `preset_name = "Edição"`. A saída é uma rendition normal da
mídia (`renditions.preset_id = null`, mesmo nome de arquivo `<media_id>_<job_id>_<sha8>.mp4` em `processed/AAAA/MM/`);
o original nunca é alterado. Progresso e erros por item: `GET /api/batches/{id}` (`jobs` com `progress`, `stage`,
`error_message`, `rendition_id`) e `GET /api/jobs?ids=` quando `jobs_truncated`.

```
EditSettings = {
  duration_mode: "original" | "15" | "30" | "60" | "custom",
  custom_seconds: number | null,   // > 0 e <= 7200; obrigatório só em "custom" (ignorado nos outros modos)
  speed: number,                   // 1.0 ..= 1.5 (vídeo e áudio juntos, sincronizados)
  sharpen: boolean,                // nitidez leve
  output_type: "mp4" | "auto"      // "mp4": sempre recodifica (H.264 + AAC); "auto": copia os fluxos quando nada exige recodificar
}
EditTrim = {                       // recorte manual de UM vídeo (editor de recorte da página Editar)
  media_id: id,                    // precisa estar em media_ids; no máximo um recorte por vídeo
  start: number,                   // segundos do original, 0..86400
  end: number                      // segundos do original, 0..86400; end - start >= 0,1; fim exclusivo [start, end)
}
```

- Params gravados (validados por `PresetParams`, neutros omitidos como nos presets): `metadata_cleanup: false`,
  `trim_start: 0`, `mirror: false`, `speed`, `auto_adjust: false`, `reencode: output_type == "mp4"`,
  `max_duration` = limite + **0,03 s** (`EDIT_DURATION_MARGIN`, somado no backend; 15 -> 15,03) quando há limite e
  `sharpen` quando ligado, e sempre `audio_sync_start: true`. `auto_adjust: false` preserva dimensões, SAR e rotação (metadado de exibição); a única
  mudança de resolução possível é o recorte de 1 px em largura/altura ímpar exigido pelo yuv420p ao recodificar.
- Recorte manual (`trims`, opcional; ausente ou `[]` = pedido de antes). Vídeo com recorte: o job dele grava os params
  acima SEM `max_duration` (a duração geral é ignorada para ele) e com `trim_start` = `start` e `cut_end` = `end`
  (segundos, arredondados ao milissegundo; `trim_start` pode passar dos 30 s dos presets porque é gravado depois da
  validação do `PresetParams`; `cut_end` não existe em presets). O corte é exato, sem a margem de 0,03 s; velocidade e
  nitidez gerais valem sobre o trecho: duração final = `(end - start) / speed`. Vídeos sem recorte e os params do
  lote seguem iguais aos de antes. Com recorte, `edit_no_changes` não se aplica (recortar já é uma edição).
  Validação: `start`/`end` fora de 0..86400, `end - start < 0,1`, recorte de vídeo fora de `media_ids`, dois
  recortes do mesmo vídeo, chave extra ou tipo errado -> 422 `validation_error`; `end` maior que a duração conhecida
  da mídia + 1 ms -> 422 `edit_trim_out_of_range` (lista até 20 ids; duração desconhecida não é checada). Nada é
  criado em nenhum desses erros.
- Erros: mídia inexistente -> 404 `media_not_found` (lista até 20 ids); mídia que não é vídeo -> 422
  `edit_requires_video` (lista até 20 ids, nada é criado); `output_type: "auto"` com duração original, velocidade 1,0
  e sem nitidez -> 422 `edit_no_changes` ("Nenhuma edição selecionada."). Só "MP4" sem outras edições é válido
  (normalização para H.264/AAC). Chave extra, tipo errado (strict) ou fora da faixa -> 422 `validation_error`.
- Ids repetidos contam uma vez. Falha de um item (ex.: duração final fora da tolerância) não interrompe o lote.
- Dedupe: mesma mídia com os mesmos params reaproveita a rendition existente (job termina `skipped` com o
  `rendition_id` dela, sem arquivo novo); configuração diferente gera outra rendition. O recorte faz parte dos params:
  o mesmo recorte (igual ao milissegundo) reaproveita; outro recorte, ou o vídeo sem recorte, gera outra versão.
- Duração: com limite, a saída fica em limite + 0,03 s (vídeo quantizado por quadro, ver seção 9); vídeo cuja duração
  de saída (depois da velocidade) já cabe no limite não é cortado nem prolongado; vídeo sem áudio continua sem áudio.
- Exportar editados: pelo fluxo da seção 8.2 com `source: {kind: "batch", id: <lote da edição>}` e
  `content: "edited"` (ZIP `editados-...zip` ou pasta; nomes `..._editado_15s_1.5x.mp4`, com recorte
  `..._editado_3.25-12.8_1.05x.mp4`). Na página Editar, o painel
  do lote encerrado com saídas oferece "Baixar ZIP dos editados" e "Exportar editados para pasta"; não há exportação
  automática ao concluir.

#### Editor de recorte (página Editar)

Cada vídeo da lista "Selecionados" (e a prévia dele) tem o botão "Recortar", que abre o editor em tela cheia: player
grande, linha do tempo com régua, tira de 10 a 20 miniaturas geradas no navegador (`<video>` escondido + canvas, um
quadro por vez), região do recorte com puxadores de início e fim, agulha arrastável (varredura: posição lida uma vez
por quadro de tela e um seek por vez, sempre para o último ponto pedido) e zoom de 1x a 16x. Tempos em `mm:ss.mmm`
(milissegundo para baixo); campos de início e fim aceitam `mm:ss.mmm`, `h:mm:ss.mmm` ou segundos (vírgula ou ponto).
Atalhos: Espaço tocar/pausar, I/O marcar início/fim na agulha, ←/→ 1 quadro (1/fps da mídia ou 1/30 s), Shift+←/→
1 s, Home/End início/fim do recorte, Esc fecha sem aplicar; nos puxadores focados, ←/→ movem o próprio puxador.
Mínimo de 0,1 s, puxadores não se cruzam, tudo em `[0, duração]` (a menor entre a do banco e a do navegador). Com fps
conhecido, agulha e puxadores ficam na grade de quadros e o vídeo é levado ao meio do quadro (primeiro quadro do
recorte ao mexer no início, último ao mexer no fim). "Aplicar" guarda o recorte só naquele vídeo (linha "Recorte
00:03.250 → 00:12.800 (9,55 s)" com "Editar recorte"/"Remover recorte"; recorte do vídeo inteiro = sem recorte); a
coluna "Prevista" mostra `(fim - início) / velocidade` e, com duração geral escolhida, a linha avisa que ela é
ignorada. O pedido manda `trims` com início e fim no milissegundo para baixo; vídeo que sai da seleção perde o recorte.

#### Editar após baixar (importação)

`POST /api/imports`, `/api/import-groups` e `/api/import-links` aceitam `edit?: EditSettings | null` (mesmo schema e
mesmas validações de `POST /api/edits`). Ausente ou `null`: nada muda (download puro ou preset, como antes). Com
`edit`, o lote (e, no grupo, o `import_groups` e TODOS os filhos) é gravado como lote de Edição nas colunas que já
existiam, sem migração: `preset_id = null`, `preset_name = "Edição"`, `params` = params da edição (os mesmos de
`POST /api/edits` para as mesmas configurações) e `process = true`. A resposta mostra `process: true` e
`preset_name: "Edição"`.
- Exclusivo: `edit` junto com `process: true` ou com `preset_id` -> 422 `edit_with_preset` (nada é criado). O corpo
  esperado é `{..., process: false, preset_id: null, edit: {...}}`. Nenhuma mudança (`auto`, original, 1,0x, sem
  nitidez) -> 422 `edit_no_changes`; chave extra/fora da faixa -> 422 `validation_error`.
- Cada VÍDEO listado vira um ingest com `preset_id = null`, `preset_name = "Edição"` e os params da edição: depois do
  download o pool de FFmpeg gera a versão editada pelo mesmo caminho do job `process` (mesmas opções, mesmo comando
  FFmpeg, mesma verificação de duração; dedupe por mídia + `preset_id` nulo + params, então editar depois pela
  página Editar com as mesmas configurações reaproveita a versão). FOTO (e qualquer item que a listagem não diga ser
  vídeo) é só baixada: ingest download-only sem preset (`preset_name = null`, `params = null`); não falha o lote.
- Progresso, fila, cancelar, repetir falhos, retomar listagem e reinício funcionam como no lote com preset (a
  configuração fica no lote e em cada job). Exportar `content: "edited"` do lote ou do grupo leva as versões editadas
  daquele lote (mesma regra `preset_id IS lote.preset_id AND params = lote.params`).

#### Pastas da página Editar (somente leitura)

`GET /api/edit/folders` -> `{items: EditFolder[]}`: os vídeos da Biblioteca separados por vez que o dono importou ou
enviou. Sem tabela nem migração nova; nada é gravado.

```
EditFolder = {
  key: string,                 // "group:<id>" | "batch:<id>" | "local:<aaaa-mm-dd>" | "other"
  kind: "group" | "batch" | "local" | "other",
  import_mode: "profile" | "links" | null,   // null em local/other
  profiles: string[],          // grupo: perfis na ordem do grupo; lote por perfil: [perfil]; link/local/other: []
  created_at: string | null,   // grupo/lote: criação (UTC ISO); local/other: o envio/registro mais recente
  day: string | null,          // só em local: dia local do envio
  video_count: number,         // vídeos (media_type = "video") da pasta
  rendition_count: number,     // versões (renditions) desses vídeos
  total_bytes: number,         // originais + versões, em bytes
  shared_count: number         // vídeos desta pasta que também estão em outra pasta (reimportados)
}
```

- `group:<id>`: um grupo de importação (vários perfis). `batch:<id>`: um lote de importação (`source =
  "instagram_import"`, perfil ou link) sem grupo, inclusive cancelado com mídias. Lote -> mídia pela mesma regra da
  exportação (jobs `ingest`/`process` concluídos com `media_id` UNIDOS a `batch_media`). Lotes `library` (Edição,
  processamento) não viram pasta.
- `local:<dia>`: mídias `source_platform = "local"` (envios do computador, que não criam lote) agrupadas pelo dia
  local de `created_at` (`date(created_at, 'localtime')`, fuso do computador onde o app roda).
- `other`: vídeos que não são envio local nem estão em nenhum lote de importação.
- Pastas sem vídeo são omitidas. Ordem: `created_at` mais recente primeiro; `other` sempre por último. Um vídeo
  importado de novo em outra vez (duplicata) aparece nas duas pastas.
- Uma consulta agregada para todas as pastas e três leituras pequenas (grupos, perfis, lotes): sem N+1.

`GET /api/edit/folders/{key}/media?q=&limit=&before_id=` -> `MediaList`: vídeos da pasta, id DESC, mesma paginação
keyset de `GET /api/media` (`limit` 1..200, padrão 50; `next_before_id`). `q` busca no código do post, perfil e nome do
arquivo, como na Biblioteca. Chave fora do formato -> 422 `validation_error`; pasta inexistente -> lista vazia.

O frontend usa `original_url` (`GET /api/media/{id}/original`, com suporte a `Range` -> 206) para a miniatura
(`<video preload="metadata">` em `#t=0.5`, carregado quando a linha aparece) e para o player da prévia.

#### Abrir a página Editar com vídeos (`/editar?ids=`)

`/editar?ids=1,2,3` (ex.: "Recortar no Media Tool" da extensão) abre a página Editar com essas mídias na seleção. O
frontend lê o parâmetro uma vez ao abrir: só inteiros positivos, sem repetir, até 500 (o resto é ignorado); lê as
mídias por `GET /api/media?ids=1,2,3` -> `MediaList` (os mesmos itens de `GET /api/media/{id}`, na ordem pedida, sem as
inexistentes; `total` = itens devolvidos, `next_before_id` null; com `ids` os filtros e a paginação não se aplicam;
fora do formato, 0, ou mais de 500 ids -> 422 `validation_error`); adiciona à seleção só os VÍDEOS (capas e fotos
ficam de fora), avisa quantos itens foram ignorados (não são vídeo, não existem mais ou id inválido) e tira o `ids`
da URL (`history.replaceState`; recarregar não adiciona de novo). Não há outro editor: é a mesma página.

#### Apagar vídeos (página Editar)

`POST /api/media/delete` com `{media_ids: number[] (1..500)}` OU `{folder: <key de EditFolder>}`, e
`delete_original: true` -> 200 `{deleted, deleted_ids, skipped: [{id, code, message}], failed: [{id, code, message}]}`.

- `delete_original` ausente ou `false` -> 400 `confirmation_required` (nada é apagado). Estrito: só o booleano JSON
  `true` confirma; `"true"`, `1`, `"yes"` -> 422 `validation_error`. Os dois (`media_ids` e `folder`) ou nenhum -> 422
  `validation_error`. Mais de 500 ids -> 422; o frontend divide a seleção em blocos de 500 e soma os resumos.
- `folder`: o servidor apaga todos os vídeos da pasta (imagens do mesmo lote ficam). A pasta some da lista quando fica
  sem vídeos. Um vídeo que também está em outra pasta (`shared_count`) sai das duas; a confirmação avisa.
- Item a item com o mesmo serviço de `DELETE /api/media/{id}?delete_original=true` (original, versões e thumbnail;
  arquivos preparados para descarte antes do banco e devolvidos se o banco falhar; jobs pendentes da mídia
  cancelados). Ids repetidos contam uma vez.
- Pulados (`skipped`, a mídia continua): `media_not_found`, `media_busy` (job baixando/processando),
  `export_in_progress` (mídia numa exportação `pending`/`running`, pasta ou ZIP). Falhas (`failed`): `file_in_use` e
  erros inesperados (`delete_failed`). Um item com problema não interrompe os outros.
- A checagem de exportação aberta é feita por item, sob o mesmo lock da criação de exportação
  (`export_create_lock`): uma exportação pedida durante a exclusão espera, ou, se já existir, a mídia é pulada.
- `DELETE /api/media/{id}?delete_original=true` (exclusão individual) tem a mesma proteção: mídia numa exportação
  aberta -> 409 `export_in_progress`.
- Só as versões editadas de um vídeo: `DELETE /api/renditions/{id}` para cada versão (o original fica). Recusado com
  409 `media_busy` se o vídeo tem job baixando/processando e com 409 `export_in_progress` se a versão está numa
  exportação aberta que leva versões (`content` `edited`/`both`, pasta ou ZIP); mesma checagem sob o lock.
- 404 de mídia/versão: "Mídia não encontrada." / "Versão processada não encontrada." (códigos inalterados).

### 8.5 Upload de vídeo local

`POST /api/library/uploads` registra na Biblioteca um vídeo enviado do computador (botão ou arrastar e soltar na
página Editar). Multipart/form-data, **um arquivo por requisição** no campo `file`; outros campos são ignorados.

```
POST /api/library/uploads   (multipart/form-data; campo file)
-> 200 { media: MediaOut, duplicate: boolean }
```

- Rota isenta do limite de 1 MB do corpo (só este caminho exato, sem barra final nem subcaminhos,
  `BodySizeLimitMiddleware(exempt_paths=...)`); o teto do arquivo é `settings.max_download_bytes` (2 GB por padrão) e
  o do corpo inteiro (partes extras, cabeçalhos e epílogo incluídos, com ou sem Content-Length) é esse teto + 256 KB.
  Continua sujeita às proteções de mutação (Host, `X-Media-Tool-Client`, Origin permitida).
- O corpo é lido em streaming (parser mínimo próprio, sem `python-multipart`) para
  `temp/upload-<uuid>/upload.bin`, contando bytes; a pasta é apagada em qualquer desfecho (sucesso, erro, cliente
  desconectado) e, se o processo cair no meio, na partida (`cleanup_orphan_temps` só preserva `job-<id>` de jobs
  ativos). Cliente que desconecta no meio recebe (para ninguém) 400 `client_disconnected`, só com log informativo.
- Depois do corpo: SHA-256; conteúdo já existente devolve a mídia existente com `duplicate: true` (nenhuma linha
  nova). Senão ffprobe obrigatório, exige vídeo com stream de vídeo e dimensões; promove o original como no download
  (`originals/AAAA/MM/<sha2>/<sha256><ext>`, sob `ingest_lock`) e insere `media` com `source_platform = "local"`,
  `source_profile/source_url/source_post_id = null`, `taken_at = null` e `original_filename` = nome enviado saneado
  (só o último componente, NFC, sem caracteres das categorias Unicode Cc, Cf (inclui bidi como U+202E e largura zero),
  Cs, Co, Cn, Zl e Zp, até 255; nunca vira caminho). Thumbnail best-effort (falha só vai para o log).
- Extensão e `mime_type` vêm do container real (ffprobe), nunca do nome enviado: MP4 -> `.mp4` (`video/mp4`); MP4
  com marca `qt` -> `.mov` (`video/quicktime`); Matroska/WebM com VP8/VP9/AV1 e áudio Opus/Vorbis (ou sem áudio) ->
  `.webm` (`video/webm`), senão `.mkv` (`video/x-matroska`). Qualquer outro container (GIF, AVI, TS, FLV...) ->
  422 `unsupported_media` ("Formato não suportado. Envie vídeos MP4, MOV, WebM ou MKV."). GIF é recusado de propósito:
  é imagem animada sem áudio; gravado como vídeo ficaria com extensão e tipo falsos.
- Integridade (barata e determinística): depois do ffprobe, o FFmpeg do processor provider decodifica com `-v error`
  do quadro-chave anterior aos últimos 3 s do stream de vídeo até o fim (vídeo e o primeiro áudio). Código de saída
  diferente de 0, nenhum quadro decodificado ou qualquer erro de leitura/demux/decodificação -> 422 `invalid_media`,
  sem nada em `originals/`. Os avisos de referência que o decodificador imprime logo depois do salto quando o ponto
  de entrada não é um IDR (open-GOP, intra-refresh: "mmco: unref short failure", "number of reference frames ...
  exceeds max", "Could not find ref with POC"...) e as repetições deles são ignorados. Pega arquivo truncado (com ou
  sem faststart) e dados zerados/corrompidos no trecho decodificado; dano só antes desse trecho não é detectado.
- Nome com extensão de vídeo conhecida precisa ser da mesma família do container (`.mp4`/`.m4v`/`.mov` para MP4/MOV,
  `.mkv`/`.webm` para Matroska/WebM); um container disfarçado (ex.: MKV renomeado para `.mp4`) -> 422
  `unsupported_media`. Sem extensão ou com extensão desconhecida, vale o container.
- Erros: corpo acima do teto -> 413 `payload_too_large` (pelo Content-Length declarado, antes de ler, ou durante a
  leitura); não multipart, sem campo `file` ou mais de um arquivo -> 422 `invalid_upload`; arquivo que não é mídia,
  truncado ou corrompido -> 422 `invalid_media`; imagem -> 422 `unsupported_media` ("Só vídeos podem ser editados.");
  FFmpeg/ffprobe ausente -> 503 `ffmpeg_missing`; disco abaixo do mínimo dos downloads (`worker.disk_check`) -> 507
  `disk_full` com `free_bytes`/`required_bytes`; mais de 2 envios simultâneos -> 429 `too_many_uploads` (sem fila no
  servidor; o painel envia no máximo 2 por vez).
- A checagem de integridade tem prazo de 120 s (`INTEGRITY_TIMEOUT`). Estourado (ex.: vídeo válido com um único GOP
  muito longo, que obriga a decodificar quase tudo) -> 422 `integrity_timeout` ("Não foi possível verificar o arquivo
  a tempo. Tente um arquivo menor ou com mais quadros-chave."), sem afirmar que o arquivo é inválido; temporário
  apagado e vaga liberada.
- O espaço em disco é verificado antes de aceitar o corpo; disco cheio durante a gravação (ENOSPC / Windows 39 e 112)
  também responde 507 `disk_full` (sem `free_bytes`) e o temporário é apagado.
- A vaga de um envio só é liberada quando a análise termina (SHA-256, ffprobe, integridade), mesmo se o cliente
  desistiu nessa fase; o painel trata 429 tentando de novo após 1, 2, 4, 8 e 15 s.

### Extensão do Chrome (`/api/ext/*`)

Visão geral, instalação e permissões em `docs/EXTENSION.md`. Segurança (core/security.py + app/api/extension.py):
origem `chrome-extension://<32 letras a-p>` só em `/api/ext/*` (fora dela: 403 `origin_not_allowed` em qualquer
método, inclusive nas rotas de gerenciamento abaixo); CORS e preflight `OPTIONS` (204, com
`Access-Control-Allow-Private-Network: true` quando pedido) só para essa origem e só em `/api/ext/*`; demais origens
que não sejam do app: 403. Token obrigatório (`Authorization: Bearer <token>` ou `X-Media-Tool-Token`) em todas as
rotas, exceto `POST /api/ext/pair`; ausente/inválido/revogado: 401 `extension_unauthorized`. Mutações continuam
exigindo `X-Media-Tool-Client`. Sem migração: `app_state.extension_pairing` (hash do código, validade, tentativas,
bloqueio) e `app_state.extension_devices` (id, nome, `sha256(token)`, `created_at`, `last_used_at`).

- `POST /api/ext/pair` `{code (1..32), device_name (1..60)}` -> 200 `{token, device_id}` (`Cache-Control: no-store`;
  única resposta com o token). Código: 8 caracteres de `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, maiúsculas/minúsculas,
  espaços e hífen ignorados; uso único; 5 min. Erros: 400 `pairing_code_invalid`, 400 `pairing_code_expired`
  (expirado, já usado ou nenhum gerado), 429 `pairing_locked` (5 erros seguidos: bloqueio de 5 min e o código vigente
  deixa de valer; gerar um código novo libera).
- `GET /api/ext/status` -> `{ok: true, app: {version}, instagram: {session_configured, paused, pause_reason,
  paused_until, message}, defaults: {media, limit, posted_within_days, post_processing, auto_export, activity_limit,
  library_limit}, features: [...]}`. `message`: texto do app para pausa vigente ou sessão ausente; null se nada
  impede. `features`: capacidades desta versão (seção 8.7).
- `POST /api/ext/page-info` `{url (1..2048)}` -> `{kind: "post"|"reel"|"profile"|"other", shortcode?, username?,
  canonical_url?}`, sem rede. `/p/` -> post; `/reel/`, `/reels/`, `/tv/` -> reel (`/tv/` tem `canonical_url` em
  `/p/<código>/`); `/<usuario>/p/<código>/` traz `username`; perfil: `/<usuario>/`, `/<usuario>/reels/`,
  `/<usuario>/tagged/`; o resto é `other`. Publicação: + `library: {in_library, items: [{media_id, media_type, role,
  index, has_edited}], source_profile}` (mídias do shortcode já na biblioteca; `source_profile` = dono conhecido em
  minúsculas ou null); perfil: + `library: {media_count, videos, photos,
  covers}` (só o banco local).
- `POST /api/ext/post-owner` `{url}` -> `{username, shortcode}`. Com o @ na URL, sem consulta; senão `resolve_post`
  pelo downloader do app (portão, prazo e trava da prévia de perfil). Erros: 422 `invalid_input` (não é publicação),
  404 `owner_not_found`, 409 `instagram_paused`/`profile_preview_in_progress`, 504 `timeout` e os códigos do
  Instagram com os mesmos status de `POST /api/instagram/profile`.
- `POST /api/ext/start` -> 202: mesmo corpo (`UnifiedImportRequest`, com `media` e `auto_export` da seção 8.6) mais
  `client_request_id` opcional (1..64, `[A-Za-z0-9_.:-]`) e mesma resposta de `POST /api/imports/start` (chama
  `import_service.start_unified`), mais `open_url` (`http://127.0.0.1:<porta>/fila?lote=<id>` para um lote; `/` para
  grupo ou vários lotes). Idempotência: a mesma `client_request_id` do mesmo dispositivo em até 10 min devolve a mesma
  resposta (+ `replayed: true`) sem criar nada, inclusive depois de um reinício do app; erro não é guardado. Mesmos
  erros. Persistência: `app_state` (sem migração), uma chave `extension_start_requests:<id do dispositivo>` com
  `{<client_request_id>: {expires_at (epoch, +10 min), response}}`; só o id do pedido, a validade e a resposta pública
  (ids, contagens, `open_url`), nada de token ou cookie. Leitura e gravação sob a mesma trava do start; cada acesso
  remove as entradas vencidas do dispositivo (até 200 por dispositivo, as mais antigas saem) e cada gravação apaga as
  chaves de outros dispositivos sem gravação há mais de 10 min.
- `GET /api/ext/progress?batch_ids=1,2&group_id=3` (até 50 lotes; pelo menos um dos dois; 422 `validation_error`) ->
  `{batches: [{id, status, profile, found, downloaded, duplicates, filtered, skipped, failed, active, finished,
  error_message, open_url}], group: {id, status, profiles, found, downloaded, duplicates, filtered, skipped, failed,
  active, finished, open_url} | null, finished, missing: [ids não encontrados], open_url, library_url}`.

Gerenciamento (só pelo app, guarda atual; origem de extensão recebe 403):
- `POST /api/settings/extension/pairing-code` -> `{code, expires_at, ttl_seconds}` (`Cache-Control: no-store`;
  substitui o código anterior e libera o bloqueio de tentativas).
- `GET /api/settings/extension/devices` -> `{items: [{id, name, created_at, last_used_at}]}` (sem hash).
- `DELETE /api/settings/extension/devices/{id}` (`id`: 16 hex) -> `{items}`; 404 `extension_device_not_found`. O token
  revogado recebe 401 na próxima chamada.

### 8.6 Importação unificada (publicações e/ou perfis)

Builder A. Migration **0021** (aditiva). Serviço: `app.services.import_service.start_unified(ctx, req)` e
`classify_inputs(entries)`; o endpoint da extensão (`/api/ext/start`) chama `start_unified` com o mesmo modelo.

- `POST /api/imports/classify` (200, sem efeitos, sem Instagram): corpo `inputs` (lista) ou `text` (um dos dois).
  Separadores: quebra de linha, vírgula e ponto e vírgula. Espaço só separa quando TODOS os pedaços do trecho são URLs
  do Instagram ou `@usuario`; senão o trecho inteiro é UMA entrada inválida (motivo "entrada com espaço: use uma por
  linha"; ex.: `lixo com espaço` nunca vira os perfis "lixo" e "com"). Resposta `{profiles: [usuario], links: [{shortcode,
  url, kind: "p"|"reel"}], invalid: [{entry, reason}], duplicates}`. Publicação: `/p/`, `/reel/`, `/reels/` e `/tv/`
  (só aqui; vira `/p/<código>/`). Perfil: URL do perfil, `@usuario` ou `usuario`. Tetos: 10.000 perfis
  (`too_many_profiles`) e 500 publicações (`too_many_links`).
- `POST /api/imports/start` (202): `inputs`|`text`, `media` ("videos" padrão | "photos" | "covers" | "videos_covers" |
  "all"), `limit` (POR PERFIL, 1..1.000.000; conta ITENS: o vídeo e a capa dele contam um; `null` = todas e exige
  `confirm_all: true` quando há perfil, senão 422 `confirm_all_required`), `posted_within_days`, `min_views`,
  `max_views` (>= mínimo; só perfis; com `media` "photos" ou "all" E perfis na entrada = 422 `views_filter_media`; só
  links: aceito, filtros ignorados com aviso em `notes`), `auto_export` (`null` | `{content: "originals"|"edited"|
  "both"}`: exportação ZIP automática ao concluir, seção 8.7; "edited"/"both" com `post_processing: "none"` = 422
  `auto_export_needs_processing`),
  `post_processing` ("none"|"preset"|"edit"), `preset_id` (só com "preset"), `edit` (obrigatório com "edit").
  1 perfil -> lote avulso (`create_import`); 2+ perfis -> grupo (`create_import_group`); publicações -> um lote de links
  (`create_import_links`). Tudo é conferido antes de criar o primeiro lote (sessão 409 `session_missing`, pausa,
  provider, preset, edição). A criação do lote/grupo de perfis e do lote de links é UMA transação: falha na 2a
  criação desfaz a 1a. Resposta `{batch_ids, group_id, links_batch_id, profiles, links, invalid, duplicates,
  skipped, notes}` (`skipped`: perfis do grupo já em andamento; `notes`: avisos sem erro, ex.: filtros de perfil
  ignorados nas publicações por link; campos extras opcionais). Período e visualizações valem só para perfis
  (ignorados nos links). "Somente capas" só baixa (o pós-processamento não se aplica às capas).
- Tipo de mídia (migration **0023**: `batches.media_set`/`import_groups.media_set`; NULL nos lotes antigos = regra
  antiga por `content_type`/`media_role`): "videos" -> `content_type = 'videos'`, job original; "photos" -> `'images'`
  (fotos avulsas e de carrossel), job original; "covers" -> `'videos'` + `media_role = 'cover'`, só o job da capa;
  "videos_covers" -> `'videos'`, job original + job da capa de cada vídeo; "all" -> `'all'`, TODOS os itens originais
  (carrossel inteiro, na ordem) + o job da capa de cada vídeo. Um vídeo pode gerar dois jobs no mesmo lote (índice
  `ux_jobs_batch_source` com `media_role`, 0023); dedup por (origem, índice, papel). Nos lotes de links o tipo vale
  sobre as mídias resolvidas (itens de outro tipo contam em `filtered`); lotes de links antigos (`all`) continuam
  baixando tudo. Tipos com capa ("covers", "videos_covers", "all") e o gallery-dl: 422 `not_supported_by_provider`
  (só o método web entrega a capa sozinha). Pós-processamento (preset/edição) só no vídeo original: fotos e capas são
  só baixadas.
- Causa do antigo "Baixar tudo da publicação" sem capa: `media: "all"` virava `(content_type "all", role
  "original")` e o lote só tinha UM papel, então o Reel vinha só com o vídeo. Corrigido pelo `media_set` acima.
- Capas (`media.role = 'cover'`): o job (`jobs.media_role = 'cover'`) baixa só `thumbnail_url` (a maior capa do vídeo)
  como `media_type = 'image'`; o vídeo nunca é baixado. Dedup por origem inclui o papel (`ux_media_source` com `role`):
  a capa nunca é duplicata do vídeo (e vice-versa); sha256 continua único. URL da capa expirada: a renovação de sempre
  (página ou publicação) grava a capa renovada como `media_url` do job.
- Capa de lote por PERFIL: antes de baixar, o job lê a publicação UMA vez (`resolve_post`: documento público anônimo
  e, só se restrita, 2a tentativa com a sessão salva; pelo portão e pelo teto por hora) e usa a `thumbnail_url` dela
  (a maior; a da listagem vem menor, ex.: 640x1136 contra 1080x1920). Falha, restrição ou pausa: fica a capa da
  listagem, com log sem URL; erro do Instagram aplica a pausa de sempre, sem nova tentativa. Lotes por link não fazem
  requisição extra. Imagens (capas e fotos) ganham `width`/`height` pelo ffprobe no download (opcional; sem ffprobe
  ficam NULL).
- Primeira página vazia (migration **0022**, `batches.listing_empty_first_page`): a 1a página da listagem de um perfil
  com 0 publicações e sem próxima página encerra o lote com `listing_end: "empty_first_page"` (não `profile_end`) e
  `error_message` "O Instagram não devolveu nenhuma publicação deste perfil agora (perfil sem posts, privado ou limite
  temporário). Tente de novo mais tarde." (`error_code` null). Não conta em `profile_end_count` do grupo.
- Lote/grupo: `media` (o `media_set`; nos lotes anteriores à 0023 "videos"|"all"|"covers"; null em lotes antigos de
  outro conteúdo), `auto_export` (seção 8.7; null sem ela; filho de grupo: null), `progress.duplicates`
  (jobs `is_duplicate`; congelado em `final_duplicates`) e `progress.filtered` (abaixo/acima/sem contagem/fora do
  período + `listing_dropped_reasons.expected.content_filter`). `views_filter` ganha `max_views` e `above`.
- Biblioteca: `GET /api/media?role=cover|original` (omitido = todas), o mesmo `role` no filtro da exportação
  (`LibraryFilter`) e em `process-by-filter`; `MediaOut.role`. A view "Originais" continua sendo "sem versão
  processada" (capas entram nela). A página Editar continua só com vídeos (`media_type = 'video'`).
- Exportação: o MESMO fluxo (lote, grupo, biblioteca por seleção ou filtro; pasta ou ZIP). Capa = original; o nome
  ganha `_capa` antes da extensão (`perfil_AAAA-MM-DD_codigo[_n]_capa.jpg`), na pasta e no ZIP (plano, único).

### 8.7 Extensão como central de operações

Migration **0023** (aditiva). Rotas finas em `app/api/extension.py` sobre os serviços existentes (`import_service`,
`edit_service`, `export_service`, repositórios); helpers em `app/services/extension_ops.py`. Todas exigem o token e a
origem da extensão (como o resto de `/api/ext/*`); nenhuma rota administrativa (sessão, cookies, configurações,
exclusões). Erros `{detail, code}`. Respostas sem caminhos de arquivo, URLs de mídia, cookies ou tokens (exceção: o
`download_url` de um ZIP pronto, que é o link de download do app com o token próprio dele).

- Exportação automática (`app/services/auto_export.py`): `batches`/`import_groups` ganham `auto_export_content`,
  `auto_export_state` ('waiting' | 'created' | 'failed'), `auto_export_id` e `auto_export_error`. Lote avulso
  `completed` (ou grupo sem filho aberto e com algum filho `completed`) e nenhuma mídia dele com job aberto de
  processamento -> UMA exportação ZIP pelo `export_service.create_export` (fonte = o lote ou o grupo; filhos de grupo
  não exportam sozinhos). Disparo no fim de cada job que conclui o lote e na manutenção do worker (30 s). Transição
  'waiting' -> 'created'/'failed' uma vez só (idempotente). `export_already_running`/`export_queue_full`: continua
  esperando; outros erros (ex.: `export_empty`, `export_no_space`): 'failed' com o motivo. Lote/grupo cancelado ou falho
  sem nada concluído: 'failed'. API: `BatchOut.auto_export`/`ImportGroupOut.auto_export` = `{content, status
  ("waiting"|"failed"|estado da exportação), export_id, error_message, zip_ready, download_url, request_id}`.
  Pedido com perfis E links no mesmo start (migration 0024, `import_requests`): UM ZIP por pedido. A exportação
  automática fica no pedido (as partes — lote avulso ou grupo de perfis e lote de links — ficam sem
  `auto_export_content` e apontam para ele por `batches.request_id`/`import_groups.request_id`; filhos de grupo não); dispara uma vez quando TODAS as partes encerraram, ao menos um lote concluiu e nenhuma mídia
  delas tem processamento pendente, com fonte `library` + `media_ids` (união das mídias das partes, mesma regra da
  exportação de um lote). Todas as partes cancelaram/falharam: 'failed'. As partes apresentam o `auto_export` do pedido
  (mesmo `export_id`, `request_id` = id do pedido); pedido de uma parte só: `request_id` null. A exportação e o
  registro do id dela no lote/grupo/pedido são gravados na MESMA transação (`create_export(..., on_created)`): uma
  queda entre os dois não deixa nascer um segundo ZIP.
- `POST /api/ext/post-preview {url}` -> `{shortcode, username, kind: "reel"|"photo"|"carousel"|"video", items: [{index,
  media_type, has_cover, width, height}], counts: {videos, photos, covers}}`. UMA leitura pelo `resolve_post` do app
  (portão, teto, pausa, trava da prévia); cache em memória de 10 min por shortcode (também usado por `post-owner`;
  é só cache: perder num reinício custa no máximo uma leitura nova, por isso não é persistido).
  Erros do Instagram classificados como em `post-owner`, sem nova tentativa; 422 `invalid_input` fora de publicação;
  404 `content_unavailable` sem mídias.
- `GET /api/ext/activity?limit=1..50 (20)` -> `{items: [{kind: "batch"|"group", id, title, created_at, status, state,
  message, media_set, filters: {limit, posted_within_days, min_views, max_views}, progress, auto_export, open_url}]}`:
  grupos e
  lotes avulsos (inclusive Edição/processamento da Biblioteca), mais recentes primeiro. `state`: "waiting" (na fila,
  pausado, aguardando ação) | "analyzing" (listando) | "downloading" (há downloads pendentes) | "processing" (só
  processamento) | "done" | "partial" (concluído com falhas) | "failed" | "cancelled". `progress`: `files` (jobs/arquivos),
  `found` (`files` + filtrados), `analyzed` (publicações vistas; links resolvidos + falhos), `downloaded`,
  `duplicates`, `filtered_period` (fora do período), `filtered_views` (abaixo + acima), `no_count` (sem contagem),
  `failed`, `processed`, `processing` (jobs `downloaded`/`processing`), `pending` (`pending`/`downloading`).
  `auto_export`: `{content, status, export_id, error_message, request_id, download_url (absoluto)}` | null.
  `message`: aviso do item ou null — 1a página vazia (o Instagram não devolveu publicações), "precisa de atenção" com o
  motivo, falha (`error_message` do lote), pausa do lote por falhas seguidas, espera do teto de páginas, coleta em
  pausa e, em item ativo sem outro aviso, a pausa geral do acesso ao Instagram (mesmos textos do app). Grupo: os avisos
  dos perfis (`@perfil: texto`, até 3, separados por "; ").
- `GET /api/ext/batches/{id}/items?before_id&limit=1..200 (50)` -> `{items: [{id, media_id, media_type, role, index,
  source_post_id, status, stage_label, error_message?}], total, next_before_id}` (id DESC). `stage_label`:
  "aguardando" | "analisando" | "baixando vídeo" | "baixando foto" | "baixando capa" | "processando" | "concluído" |
  "duplicado" | "falhou" | "cancelado". 404 `batch_not_found`.
- `GET /api/ext/groups/{id}/children?after_position&limit=1..100 (20)` -> `{group_id, items: [item de lote da
  Atividade (kind "batch", title "@perfil", state, message, progress, filters, open_url...) + position], total,
  next_after_position}`: os perfis (lotes filhos) do grupo em ordem de `group_position`, keyset `after_position`
  (`BatchRepository.group_children`, o mesmo de `GET /api/import-groups/{id}`); só filhos deste grupo. Cada
  filho expande por `GET /api/ext/batches/{id}/items`. 404 `group_not_found`.
- `POST /api/ext/batches/{id}/cancel`, `POST /api/ext/groups/{id}/cancel` -> `{ok: true}` (mesmo cancelamento de
  `/api/batches/{id}/cancel` e `/api/import-groups/{id}/cancel`; mesmos erros 404/409).
- `GET /api/ext/library?before_id&limit=1..50&media_type&role&view&batch_id|group_id&profile&q` -> `{items: [{id,
  media_type, role, source_profile, source_post_id, index, taken_at, created_at, width, height, duration, has_edited,
  renditions, processing: "none"|"pending"|"processing"|"failed", has_thumbnail}], total, next_before_id}` (keyset id
  DESC; lote/grupo pela relação durável `batch_media`). `batch_id` e `group_id` juntos: 422.
- `GET /api/ext/library/ids?batch_id|group_id&media_type&role` -> `{ids, items: [{id, media_type, role}], truncated}`
  (até 5.000, id DESC; `items` na mesma ordem de `ids`, para o resumo da seleção). Sem lote nem
  grupo: 422.
- `GET /api/ext/media/{id}/thumbnail` -> imagem (`image/jpeg` da miniatura; gera e grava como a pipeline se faltar e
  houver FFmpeg; imagem sem FFmpeg: a própria imagem). Vídeo sem miniatura possível: 404 `thumbnail_unavailable` (nunca
  o vídeo).
- `GET /api/ext/edit/options` -> `{edit: {duration_modes: [{value, label}] ("original", "15", "30", "60", "custom"),
  custom_seconds_max: 7200, duration_margin: 0.03, speed: {min: 1.0, max: 1.5, step: 0.01, presets: [1, 1.05, 1.1,
  1.15, 1.2, 1.25, 1.3, 1.5]}, sharpen: {label, description}, output: "mp4", max_items: 500}, presets: [{id, name,
  summary}], defaults: {duration_mode, custom_seconds, speed, sharpen, output_type, preset_id}}`.
- `POST /api/ext/edits {media_ids (1..500), settings}` (sem recorte; `settings.output_type` só "mp4") -> 202 `{batch_id,
  created, skipped: [{media_id, reason: "not_video"|"cover"|"not_found"}], open_url}` (chama `edit_service.start` só com
  os vídeos originais). Nenhum vídeo: 422 `edit_requires_video` (com `skipped`).
- `POST /api/ext/process {media_ids (1..500), preset_id}` -> 202 `{batch_id, created, skipped, open_url}`
  (`import_service.process_media_bulk`, o mesmo de `POST /api/media/process`, só com vídeos originais). Nenhum vídeo:
  422 `process_requires_video`.
- `POST /api/ext/exports {source: {kind: "batch"|"group", id} | {kind: "library", media_ids (1..5000)}, content}` -> 202
  `{id, status}`: sempre UM ZIP por pedido (`export_service.create_export` com `{zip: true}`; capas com `_capa`). Mesmos
  erros (`export_already_running`, `export_queue_full`, `export_empty`, `export_no_space`).
- `GET /api/ext/exports/{id}` -> `{id, status, content, processed, total, zip_ready, zip_bytes, download_url (absoluto,
  só com o ZIP pronto), error_message}`.

### 8.8 Análise de Perfis

Migration **0025** (estritamente aditiva; reversão manual por `app.db.migrate.revert_migration` com
`migrations/down/0025_profile_analyses.down.sql`, só da ÚLTIMA versão aplicada e nunca no startup). Rotas em
`app/api/profile_analyses.py`; regras de leitura em `docs/INSTAGRAM_WEB_CONTRACT.md`, `web/profile_snapshot.py`. Não usa a
sessão salva em nenhum ponto. A extensão não acessa estas rotas (origem de extensão recebe 403 fora de `/api/ext/*`).

| Método | Caminho | Corpo / query | Resposta |
|---|---|---|---|
| POST | /api/profile-analyses | `{text: string}` (corpo até 1 MiB; acima: 413 `payload_too_large`) | `Analysis` 202; 400 `no_profiles`\|`too_many_profiles` (mais de 10.000 perfis únicos) |
| GET | /api/profile-analyses | `limit` 1..100 (20), `offset` | `{items: Analysis[], total, open_id}` (id DESC; `open_id`: análise que o executor atende agora) |
| GET | /api/profile-analyses/{id} | | `Analysis`; 404 `analysis_not_found` |
| GET | /api/profile-analyses/{id}/items | `limit` 1..200 (50), `offset` | `{items: Item[], total}` (por `position`); 404 |
| POST | /api/profile-analyses/{id}/cancel | | `Analysis`; idempotente em aberta; terminal volta como está; 404 |
| POST | /api/profile-analyses/{id}/resume | | `Analysis` 200; 409 `analysis_not_resumable` fora de `resumable` (inclusive o 2o clique); 404 |
| GET | /api/profile-analyses/{id}/report | | ZIP (`application/zip`, `attachment; filename="relatorio-perfis-AAAA-MM-DD.zip"`, data local do início); 409 `analysis_running` com a análise aberta; 404 |

`text`: uma entrada por linha (@, usuário ou link, com ou sem esquema/`www`, barra final, query), `parse_profile_input`.
Linha em branco é ignorada; duplicata é comparada depois de normalizar e mantém a ordem da 1a aparição.

```
Analysis: {id, status: "pending"|"running"|"waiting"|"completed"|"cancelled"|"interrupted", total, done,
  pending /*itens pending + running*/, running_username, invalid_count, duplicate_count,
  invalid_lines: [{line /*até 120 caracteres*/, reason}] /*no máximo 50*/, waiting_until,
  wait_reason: null|"rate_limited"|"too_many_failures"|"instagram_pause", error, created_at, started_at, finished_at,
  created_date /*"31/10/2026"*/, created_time /*"23:58:01"*/, finished_time, report_ready /*análise terminal*/,
  resumable /*cancelled ou interrupted com itens não consultados*/,
  summary: {active, private, unavailable, unverified, cancelled, stories_yes, highlights_yes, views_total}}
Item: {position, username, state: "pending"|"running"|"done"|"cancelled", account_status: null|"active"|"private"|
  "unavailable"|"unverified", display_name, posts_state: null|"known"|"unverified", posts_count,
  posts_exact: null|1|0 /*0 = aproximado*/, posts_text /*"33K"*/, views_state: null|"known"|"none"|"unverified",
  views_total, views_reels /*Reels somados*/, stories, highlights /*null|"yes"|"no"|"unverified"*/, error_code,
  error_message, checked_at, checked_date, checked_time, attempts}
```

`done + pending + summary.cancelled = total`. Datas de apresentação no fuso do servidor
(`export_time.resolve_zone(settings.timezone)`), o mesmo na tela e nos relatórios. `checked_at` = instante UTC logo depois
de ESTE perfil responder (com várias tentativas, o da que definiu o resultado).

Item `cancelled`: com `error_code` nulo foi cancelado pelo usuário; com `error_code` (`rate_limited` ou
`too_many_failures`) a análise foi interrompida antes dele. Nos dois casos o perfil não foi consultado: `account_status`
e `checked_at` nulos. `summary.cancelled` conta os dois.

Regras: `account_status`, Stories, Destaques, Posts e Views seguem o contrato de `web/profile_snapshot.py`; ausência de
dado é `unverified`, nunca `no`. Em `unavailable`/`unverified` os campos ficam `unverified`/nulos (a tela mostra "—" em
Indisponível; o relatório, Não verificado). Views é a soma dos Reels da 1a página lida (até 12), não o total da vida da
conta.

Limites: 10.000 perfis por análise, 1.000.000 de caracteres no texto; 10.000 links completos dão cerca de 580 KB e cabem
no limite de corpo. No ritmo adaptativo, 10.000 perfis levam cerca de 11 h (cerca de 28 h depois de um sinal de limite). Agregados: `summary` e os contadores vêm de
`final_stats` (gravado ao terminar, zerado ao retomar); só análise aberta varre os itens.

Execução (`app/jobs/profile_analysis_runner.py`): uma análise por vez, FIFO, retomada depois de um reinício. Erros:
`rate_limited` e o disjuntor (`BREAKER_LIMIT` = 5 perfis `unverified` seguidos, ignorando `unavailable`; não abre sem perfil
restante) abrem o backoff LOCAL de 15, 30 e 60 min, sem efeito em pausa global, `slow_listing` ou `app_state`; só `active` e
`private` zeram disjuntor e degrau. Esgotado, a análise fica `interrupted`, salvo cancelamento já pedido, que prevalece.
`timeout`/`network_error`: uma nova tentativa do item (`MAX_ATTEMPTS` = 2), depois `unverified`. Item em voo num reinício
volta para a fila e é consultado de novo, com a tentativa contada: `attempts` vale entre reinícios, então um ciclo de
quedas faz no máximo `MAX_ATTEMPTS + 1` consultas, e o item fecha `unverified` com `internal_error`. Demais erros: `unverified`
com `error_code`/`error_message` e segue. Pausa global: `waiting` com `wait_reason = "instagram_pause"`, sem consumir
tentativa.

Canário: a cada `CANARY_EVERY` = 10 perfis `unavailable` em sequência (derivada do banco por `position`), UMA consulta de
controle a `CANARY_PROFILE` = `instagram`, sem criar item. `active`: a sequência vale e a contagem recomeça. Qualquer outra
resposta: os itens da sequência voltam a `pending`, sem resultado nem `checked_at`, e a análise entra no mesmo backoff, com
`wait_reason` `rate_limited` se o canário levou 429 e `too_many_failures` nos demais casos.

Retomada (`POST .../resume`): em `cancelled` e `interrupted` com itens não consultados, numa transação. Itens `cancelled`
voltam a `pending` (sem `error_code`, `error_message` e `attempts`); a análise vira `pending`, com `cancel_requested`,
`error`, `waiting_*`, `finished_at`, `final_stats` e `wait_level` zerados. `started_at` e `created_*` ficam. Itens `done`,
inclusive `unverified`, não são tocados nem consultados de novo: `resume_after` guarda a última posição `done`, e o canário
só considera sequências depois dela. Para exportar parcial: cancelar, exportar e retomar.

Ritmo: a análise usa o portão global (1 em voo, intervalo, pausa publicada), mas NÃO o teto de páginas por hora
(`wait_listing_page`): não consome a cota das importações nem grava `instagram_listing_pages`. O limite vem do executor (3 a
6 s entre perfis mais descansos depois do primeiro sinal de limite; antes dele só o portão, com descanso a cada 100). Falha de gravação: resultado recusado pelo banco vira
`unverified` com `internal_error`; se nada grava, `attempts` limita o item (sem GET novo acima de `MAX_ATTEMPTS + 1`) e o
executor espera em memória.

Relatório (gerado em fluxo, sem memória proporcional ao total; `content-length` no ZIP, entregue em blocos):
`relatorio-perfis-AAAA-MM-DD.{xlsx,csv,html}`. XLSX e CSV: Perfil, Nome, Posts, Posts exato, Views, Reels
somados, Stories, Destaques, Status, Data, Horário, Observação (no XLSX o perfil sai com `@`; no CSV, sem, para não virar
fórmula). HTML: as 8 colunas da tela (`table-layout:fixed` + `<colgroup>`), com nome, Reels somados e observação dentro das
células; cartão Não consultados (Ativas + Privadas + Indisponíveis + Não verificadas + Não consultados = Total) e sem "—".
Gerado sob demanda a partir do banco; não usa
`exports` (sem mídia, TTL nem fila). CSV: UTF-8 com BOM, `;`, CRLF, `Nome`/`Observação` com neutralização de fórmula. HTML:
arquivo único, sem JS nem recurso externo, tudo escapado. XLSX: `app/services/xlsx_writer.py`, em stdlib; aproximado
guarda o número com Posts exato = Não. Status: Ativa, Privada, Restrita, Indisponível, Não verificado, Cancelado (usuário) e Não
consultado (interrompida). Indisponível: Posts, Posts exato, Views, Reels somados, Stories e Destaques = "Não verificado",
com Data e Horário reais. Cancelado e Não consultado: no CSV/XLSX, "Não consultado" por coluna e Data/Horário vazios; no
HTML, uma célula `colspan=4`, a pill de Status e uma célula `colspan=2`, todas com texto.

## 9. Processor (`media_processor`)

```python
from media_processor import (MediaProcessor, ProcessingOptions, ProbeResult, ProcessResult, ToolInfo, ProcessorError,
    FFmpegNotFoundError, InvalidMediaError, ProcessingFailedError, ProcessingCancelledError, sha256_file)
@dataclass(frozen=True) class ProcessingOptions: metadata_cleanup=True; trim_start=0.5; mirror=True; speed=1.05;
    auto_adjust=True; reencode=True;
    zoom=0.0; brightness=0.0; contrast=1.0; saturation=1.0; trim_end=0.0; pitch_shift=0.0; grain=0.0;  # opcionais
    max_duration=0.0; sharpen=False; audio_sync_start=False; cut_end=0.0;   # edição (opcionais)
    @classmethod from_params(cls, params: dict)   # chave desconhecida ou valor fora da faixa -> ValueError
@dataclass class ToolInfo: found: bool; path: str|None; version: str|None; error: str|None
@dataclass class ProbeResult: media_type: "video"|"image"; format_name; duration|None; width|None; height|None; fps|None;
    has_audio: bool; has_video: bool; video_codec|None; audio_codec|None; rotation: int; tags: dict; stream_tags: list[dict]; raw: dict
@dataclass class ProcessResult: output_path: Path; probe: ProbeResult; sha256: str; size: int; command: list[str];
    forced_reencode: bool; elapsed: float
class MediaProcessor:
    def __init__(self, ffmpeg_path: str|None = None, ffprobe_path: str|None = None)   # None -> MEDIA_TOOL_FFMPEG/PATH
    def ffmpeg_info(self) -> ToolInfo; def ffprobe_info(self) -> ToolInfo
    def probe(self, path: Path) -> ProbeResult
    def build_command(self, src: Path, dst: Path, probe: ProbeResult, opts: ProcessingOptions) -> list[str]
    def output_suffix(self, probe: ProbeResult, opts: ProcessingOptions) -> str
    def process(self, src, dst, opts, *, progress: Callable[[float], None]|None = None,
                cancel: threading.Event|None = None, timeout: float|None = None) -> ProcessResult
    def make_thumbnail(self, src: Path, dst: Path, *, width: int = 320) -> Path
    def mux_streams(self, video: Path, audio: Path, dst: Path, *, cancel: threading.Event|None = None,
                    timeout: float|None = 300.0) -> None   # junta vídeo + áudio em MP4 com -c copy (sem recodificar)
def sha256_file(path: Path) -> str
```
subprocess com lista e `shell=False`; nunca tocar `src`; escrever temp e renomear. Vídeo: H.264 libx264 crf ~20,
yuv420p, dimensões pares, SAR 1, AAC se houver áudio, `+faststart`, `-map_metadata -1`, `-map_chapters -1`, bitexact,
`setpts=PTS/speed` + `atempo`, trim com A/V em sincronia, sem áudio não falha. Com `reencode=False` e filtro ativo
força reencode; só cleanup -> stream copy. Imagem: JPEG alta qualidade (PNG fica PNG), hflip, sem EXIF, orientação aplicada.

Ajustes opcionais (com todos neutros o comando do ffmpeg é idêntico ao de um preset sem eles; qualquer um ativo
conta como filtro ativo e entra na duração esperada do progresso):
- `zoom`: recorte central de 1/(1+zoom/100) e reescala lanczos de volta às dimensões que o frame teria sem o zoom
  (depois do `hflip`), sem alterar o SAR que a saída teria sem o zoom; `brightness`/`contrast`/`saturation`: filtro `eq` só com os não neutros; `grain`:
  `noise=alls=<n>:allf=t`. Valem para vídeo e imagem.
- `trim_end`: `trim`/`atrim` com `end = duração - trim_end`, mesmo ponto para vídeo e áudio. **Vídeo curto**: se
  `duração - trim_start - trim_end < 1,0 s`, o corte final é ignorado naquela mídia e o resto do preset é aplicado
  (nunca falha por causa do corte final; o 1,0 s é medido antes da velocidade, saída mínima ≈ 1,0/speed). O erro do `trim_start` (sobra menor que max(0,1 s, 1 frame)) não muda.
- `pitch_shift`: `asetrate=<sr*f>,aresample=<sr>` com `f = 1 + pitch_shift/100`, e `atempo = speed/f` para a
  velocidade final continuar a do preset. Sem áudio, nada. Limitação conhecida: quando o áudio começa depois do
  vídeo, o início do áudio se desloca em até |`pitch_shift`| % do atraso inicial (~28 ms para 1 s com 3 %),
  constante ao longo do arquivo e zero quando áudio e vídeo começam juntos.
- Imagem: `trim_end` e `pitch_shift` são ignorados.
- `max_duration` (0..86400 s, 0 = desligado): duração **alvo da saída**. Corte na linha do tempo de saída: `trim=end=T`
  depois de `setpts`/`fps` (e antes do `setsar=1`) e `atrim=end=T` depois de `atempo`/tom. Só vale quando a duração
  natural da saída `(duração - trim_start - trim_end efetivo) / speed` passa de `T`; senão é ignorado (vídeo menor não
  é prolongado nem cortado). Com corte o plano é sempre `encode` (stream copy só corta em keyframe) e a duração
  esperada do progresso é `min(natural, T)`. A margem de 0,03 s é do backend (seção 8.4), não do processor.
- `cut_end` (0..86400 s, 0 = desligado; precisa ser maior que `trim_start`): recorte manual da edição, trecho
  `[trim_start, cut_end)` da origem. O início é o `trim_start` de sempre (`trim=start=S`/`atrim=start=S` na entrada e
  o mesmo deslocamento constante `setpts=(PTS-S/TB)[/speed]` e `asetpts=PTS-S/TB` nas duas streams, o que mantém a
  sincronia; não usa `PTS-STARTPTS` por stream). O fim vira um alvo de saída `T = (cut_end - trim_start) / speed`
  tratado exatamente como `max_duration` (mesmo `trim=end=T`/`atrim=end=T` depois de `setpts`/`fps`/`atempo`, mesmo
  `-enc_time_base:v filter`, mesma verificação por stream); com os dois ligados vale o menor alvo. Fim no fim do vídeo
  (ou depois) = só o corte inicial. Sem `cut_end` o comando é idêntico ao de antes (presets e edições sem recorte;
  `tests/test_edit_cut.py` compara o hash dos argumentos com os gerados no commit 9648098). Exato por quadro: ficam os
  quadros com PTS em `[S, cut_end)`; o áudio é exato por amostra. Duração desconhecida no probe: o fim não é aplicado
  (como o `max_duration`).
- Com corte, o encoder de vídeo recebe `-enc_time_base:v filter`: sem isso o PTS do vídeo é arredondado para a grade
  1/fps e um vídeo que começa fora da grade (ex.: MPEG-TS com start_time alto) desloca até meio quadro e termina além
  do alvo.
- Verificação (`verify.check_output_duration`, só quando há corte), depois do probe de saída (sem ffprobe extra).
  `commands.expected_spans` calcula, por stream, início e fim esperados na linha do tempo de saída: início do vídeo =
  `(start_time da stream - start_time do container - trim_start) / speed`; início do áudio = a mesma fórmula dividida
  por `speed` **só com `audio_sync_start`**, senão sem a divisão (o `atempo` mantém o PTS inicial) (o deslocamento
  entre as streams da origem é preservado, nada é "puxado" para 0) e fim = `min(T, fim natural daquela stream)` (nenhuma stream é prolongada:
  áudio mais curto que o vídeo termina onde terminava). Com `frame = 1/fps` do vídeo de saída (ou da origem; 0,1 s se
  desconhecido): fim do vídeo a no máximo `frame + 5 ms` do esperado; fim do áudio a no máximo 25 ms; fim do container
  a no máximo `frame + 5 ms` do maior fim esperado; início de cada stream a no máximo 50 ms do esperado (vídeo:
  `max(50 ms, frame + 5 ms)`, porque depois de um corte inicial fora da grade o primeiro quadro começa até um quadro
  depois; só muda algo abaixo de ~22 fps); deslocamento
  vídeo - áudio a no máximo `frame + 25 ms` do esperado. Stream sem duração informada na origem: só o limite superior
  do fim. Violação -> `ProcessingFailedError("duração final fora da tolerância: ...")` e o `.part` é apagado.
- Medido (FFmpeg 9, testsrc2 + seno 48 kHz, T = 15,03): áudio 15,030 em todos os casos; vídeo 15,033 (30 e 60 fps),
  15,040 (25 fps), 15,048 (29,97 fps a 1,0x) e 15,015 (29,97 fps a 1,25x/1,5x).
- Limitação da verificação: ela confere só timestamps e durações do probe de saída. Tolerâncias: início de cada
  stream ±50 ms do esperado e deslocamento vídeo - áudio ±(1 quadro + 25 ms). Um atraso que esteja só no conteúdo do
  áudio (som deslocado dentro de uma stream com timestamps corretos) não é detectável por timestamps.
- `sharpen`: `cas=strength=0.25:planes=1` (só luma) depois do `eq` e antes do `noise`; força reencode. Imagem: ignorado.
- `audio_sync_start` (booleano, a edição sempre envia `true`): com velocidade, acrescenta
  `asetpts=PTS-STARTPTS+STARTPTS/<speed>` depois do `atempo`/tom (e antes do `atrim` do corte). O `atempo` mantém o
  PTS inicial do áudio; sem isto um áudio que começa depois do vídeo fica atrasado em `início × (1 - 1/speed)`
  (medido: ~145 ms com 0,5 s a 1,5x). Desligado (presets) o comando é o de antes. A verificação usa o mesmo modelo:
  início do áudio dividido pela velocidade só com o parâmetro ligado.

## 10. Design (frontend)

Dark grafite, compacto. Tokens:
`--bg #0f1113; --surface #16191c; --surface-2 #1d2125; --surface-3 #252a2f; --border #2a3036; --border-strong #363d44;
--text #e7e9eb; --text-muted #8d949b; --text-dim #646b72; --accent #6b8afd; --accent-strong #5574f0;
--ok #4fae7b; --warn #d6a648; --err #e2574c; --radius 6px`.
Fonte `Inter, "Segoe UI", system-ui, sans-serif`, base 13px. Sem estrelas, glow, gradientes chamativos, bordas laterais
coloridas, etiquetas acima de títulos, cards para tudo, contorno colorido forte no hover. Tabelas e listas compactas.
Animações <= 150ms. Nunca usar o caractere travessão (U+2014) em textos. Rótulos em português do Brasil.
Navegação: Importar (primeira/rota `/`), Biblioteca, Fila, Presets, Configurações.
