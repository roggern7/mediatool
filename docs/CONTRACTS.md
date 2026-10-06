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
    app\services\        storage, cdn_downloader, diagnostics, system_open, import_service
    app\jobs\            fila persistente, worker, import runner, recovery
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
    def resolve_post(self, shortcode: str) -> list[RemoteMedia]          # renova URLs expiradas
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
listagem, só antes do primeiro item); nunca em 429, checkpoint, restrição, sessão, rede ou timeout. O teste de sessão
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
  (builtin, editáveis, não excluíveis), ambos com os params padrão.
- `media(id, source_platform, source_profile, source_url, source_post_id, source_media_index DEFAULT 0, post_id, media_id_remote,
  media_type, original_filename, original_path /*relativo a storage*/, original_sha256 UNIQUE, file_size, duration, width,
  height, fps, has_audio, mime_type, thumbnail_path, taken_at, created_at, updated_at)`;
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
- `settings(key PK, value json, updated_at)`; `app_state(key PK, value, updated_at)`.
Nenhuma tabela guarda sessão/cookies.

## 7. Estados

Job `status`: `pending | downloading | downloaded | processing | completed | failed | cancelled`.
Ingest download-only (padrão): `pending -> downloading -> completed`. Ingest com `process: true`:
`pending -> downloading -> downloaded -> processing -> completed`. Process: `pending -> processing -> completed`.
`stage`: `queued, waiting_retry, downloading, hashing, probing, thumbnail, processing, finalizing, done, skipped`.
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
| GET | /api/presets | | `Preset[]` |
| POST | /api/presets | `{name, params}` | `Preset` 201 |
| GET/PUT/DELETE | /api/presets/{id} | PUT `{name?, params?}` | `Preset` / 204 (409 builtin) |
| POST | /api/presets/{id}/duplicate | `{name?}` | `Preset` 201 |
| POST | /api/instagram/profile | `{profile}` | `ProfileInfo` (preview); prazo 50 s no backend: 504 `timeout`; 429 `rate_limited`; 403 `action_blocked`/`access_refused`; 502 `contract_changed`/`unsupported_media`/`network_error`; 409 `profile_preview_in_progress`; 409 `instagram_paused` |
| POST | /api/imports | `{profile, content_type, limit: 1..1000000\|null, preset_id?\|null, process?: bool}` (`limit: null` = "Todos", até o fim real do perfil; `content_type`: `all`\|`reels`\|`photos_carousels` e os antigos `posts`\|`videos`\|`images`; `process` padrão `false` = download-only; `preset_id` padrão `null`; `process: true` sem `preset_id` usa o preset padrão) | `Batch` 202; 409 `session_missing`; 409 `instagram_paused`; 409 `profile_already_importing`; 422 `invalid_input` |
| POST | /api/import-groups/preview | `{entries?: string[], text?: string}` | `ProfilesPreview` (sem efeitos, sem Instagram); 422 `too_many_profiles` |
| POST | /api/import-groups | `{profiles: string[], content_type, limit: 1..1000000\|null, preset_id?\|null, process?: bool}`, query `children_limit` | `ImportGroupDetail` 202 (com `skipped`, `duplicates`); 422 `invalid_profiles`\|`no_profiles`\|`too_many_profiles`; 409 `profile_already_importing`\|`session_missing`\|`instagram_paused`; 404 `preset_not_found` |
| GET | /api/import-groups | `limit`<=200, `before_id` | `{items: ImportGroup[], total, next_before_id}` |
| GET | /api/import-groups/{id} | `children_limit`<=500 (100), `after_position` | `ImportGroupDetail`; 404 `group_not_found` |
| POST | /api/import-groups/{id}/cancel | query `children_limit` | `ImportGroupDetail`; 409 `group_finished`; 404 `group_not_found` |
| GET | /api/batches | `limit, offset, source?=instagram_import\|library, group_id?, ungrouped?=true` | `{items: Batch[], total}` (padrão: todos os lotes, inclusive filhos de grupo) |
| GET | /api/batches/{id} | | `Batch` + `jobs: Job[]` |
| POST | /api/batches/{id}/cancel | | `Batch` |
| POST | /api/batches/{id}/retry-failed | | `Batch` |
| GET | /api/media | `view=all\|originals\|processed`, `media_type`, `profile`, `q`, `limit`<=200, `offset` | `{items: Media[], total}` |
| GET | /api/media/{id} | | `Media` |
| GET | /api/media/{id}/thumbnail | | image/jpeg |
| GET | /api/media/{id}/original | | arquivo |
| GET | /api/renditions/{id}/file | | arquivo |
| POST | /api/media/{id}/process | `{preset_id}` | `Job` 202 |
| POST | /api/media/process | `{media_ids, preset_id}` | `Batch` 202 |
| DELETE | /api/renditions/{id} | | 204 |
| DELETE | /api/media/{id} | `?delete_original=true` obrigatório | 204 |
| POST | /api/media/{id}/open | `{target:"file"\|"folder", rendition_id?}` | `{ok}` |
| GET | /api/jobs | `status` csv, `batch_id`, `ids` csv, `limit`, `offset` | `{items, total, counts}` |
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

```ts
PresetParams = { metadata_cleanup: bool, trim_start: number /*0..30*/, mirror: bool, speed: number /*0.5..2*/,
  auto_adjust: bool, reencode: bool }      // chaves extras rejeitadas
Preset = { id, name, is_builtin, params, created_at, updated_at }
ProfileInfo = { username, user_id|null, full_name|null, avatar_url|null, post_count|null, is_private|null, is_verified|null }
Rendition = { id, media_id, preset_id|null, preset_name|null, job_id|null, processed_sha256, file_size, duration|null,
  width|null, height|null, fps|null, has_audio|null, created_at, file_url }
Media = { id, source_platform, source_profile|null, source_url|null, source_post_id|null, source_media_index, media_type,
  original_filename|null, original_sha256, file_size, duration|null, width|null, height|null, fps|null, has_audio|null,
  mime_type|null, taken_at|null, created_at, thumbnail_url|null, original_url, renditions: Rendition[], last_job: Job|null }
Job = { id, batch_id|null, media_id|null, rendition_id|null, kind, preset_id|null, preset_name|null, status, stage|null,
  progress|null, error_code|null, error_message|null, attempts, max_attempts, next_attempt_at|null, is_duplicate,
  duplicate_reason|null, source_platform|null, source_profile|null, source_url|null, source_post_id|null,
  source_media_index|null, media_type|null, thumbnail_url|null /*local se existir, senão null*/, created_at,
  started_at|null, finished_at|null, updated_at, elapsed_seconds|null }
Batch = { id, source, source_profile|null, content_type|null, requested_limit|null, preset_id|null, preset_name|null,
  process, status, found, error_code|null, error_message|null, created_at, updated_at, finished_at|null,
  progress: {found, downloaded, processed, skipped, failed, pending, active}, counts: {<status>: n}, jobs?: Job[],
  listing_state: "pending"|"running"|"waiting_backpressure"|"waiting_rate_limit"|"paused"|"needs_attention"|"done"|
    "cancelled"|null,
  rate_limit_resume_at: string|null /*ISO UTC*/, rate_limit_pages_used: number|null, rate_limit_pages_max: number|null,
  listing_dropped: { unexpected_total: number, reasons: {<motivo>: n}, pending: number },
  listing_end: "profile_end"|"limit"|null,   // por que a listagem terminou (seção 8.1); null enquanto lista/erro/antigo
  group_id: number|null, group_position: number|null /*1-based*/,
  group_state: "waiting"|"active"|"attention"|"completed"|"failed"|"cancelled"|null /*só em filhos de grupo*/ }
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
| Fotos e carrosséis | `photos_carousels` | fotos simples + TODOS os itens dos carrosséis (inclusive vídeo de carrossel); exclui Reels e vídeo avulso que não é Reel |

**Vídeo avulso que não é Reel** (publicação de vídeo legada, sem `product_type` `clips` e fora de carrossel): entra
SÓ em `all` (e no antigo `videos`). Não entra em `reels` nem em `photos_carousels`; nesses filtros é ignorado como
filtro esperado (`content_filter`), nunca como descarte. (Em qual filtro esse caso deve cair está em aberto com o
dono.)

**Método de importação**: "Todos" (`limit: null`), N acima de 5.000 e `photos_carousels` exigem o método web. Com
`instagram_provider = "gallery_dl"` configurado, `POST /api/imports` e `POST /api/import-groups` respondem 422
`not_supported_by_provider` (mensagem em pt-BR) e nada é criado. Em `auto` o pedido é aceito e nunca cai no
gallery-dl: o erro do web fica visível no lote.

Valores antigos ainda aceitos: `posts` (igual a `all`), `videos`, `images`. Carrossel conta por item; se o limite cai
no meio de um carrossel, entram só os primeiros itens dele. O que o filtro ignora NÃO aparece em `listing_dropped`.

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
  profile_end_count: number /*filhos com listing_end = profile_end*/,
  progress: { found, downloaded, processed, skipped, failed, pending, active } /*soma das mídias dos filhos*/,
  listing_dropped_total: number /*soma de listing_dropped definitivo dos filhos; o detalhe fica em cada filho*/ }
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
(ordem id DESC; `next_before_id` `null` no fim). Duas consultas ao banco para a página inteira.

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

## 9. Processor (`media_processor`)

```python
from media_processor import (MediaProcessor, ProcessingOptions, ProbeResult, ProcessResult, ToolInfo, ProcessorError,
    FFmpegNotFoundError, InvalidMediaError, ProcessingFailedError, ProcessingCancelledError, sha256_file)
@dataclass(frozen=True) class ProcessingOptions: metadata_cleanup=True; trim_start=0.5; mirror=True; speed=1.05;
    auto_adjust=True; reencode=True;  @classmethod from_params(cls, params: dict)
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
def sha256_file(path: Path) -> str
```
subprocess com lista e `shell=False`; nunca tocar `src`; escrever temp e renomear. Vídeo: H.264 libx264 crf ~20,
yuv420p, dimensões pares, SAR 1, AAC se houver áudio, `+faststart`, `-map_metadata -1`, `-map_chapters -1`, bitexact,
`setpts=PTS/speed` + `atempo`, trim com A/V em sincronia, sem áudio não falha. Com `reencode=False` e filtro ativo
força reencode; só cleanup -> stream copy. Imagem: JPEG alta qualidade (PNG fica PNG), hflip, sem EXIF, orientação aplicada.

## 10. Design (frontend)

Dark grafite, compacto. Tokens:
`--bg #0f1113; --surface #16191c; --surface-2 #1d2125; --surface-3 #252a2f; --border #2a3036; --border-strong #363d44;
--text #e7e9eb; --text-muted #8d949b; --text-dim #646b72; --accent #6b8afd; --accent-strong #5574f0;
--ok #4fae7b; --warn #d6a648; --err #e2574c; --radius 6px`.
Fonte `Inter, "Segoe UI", system-ui, sans-serif`, base 13px. Sem estrelas, glow, gradientes chamativos, bordas laterais
coloridas, etiquetas acima de títulos, cards para tudo, contorno colorido forte no hover. Tabelas e listas compactas.
Animações <= 150ms. Nunca usar o caractere travessão (U+2014) em textos. Rótulos em português do Brasil.
Navegação: Importar (primeira/rota `/`), Biblioteca, Fila, Presets, Configurações.
