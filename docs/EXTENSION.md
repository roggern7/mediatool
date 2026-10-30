# Extensão do Chrome

A extensão (pasta `extension/`, Manifest V3, sem bibliotecas) envia a aba aberta do Instagram para o Media Tool
local: uma publicação/Reel (vídeos, só a capa ou tudo) ou um perfil (com mídia, quantidade, período e visualizações).
Quem baixa é sempre o app, pelo mesmo caminho da tela Importar; a extensão não lê nada do instagram.com e não usa
cookies do navegador.

## Instalação

1. Abra `chrome://extensions` e ligue o **Modo do desenvolvedor** (canto superior direito).
2. Clique em **Carregar sem compactação** e escolha a pasta `extension/` do Media Tool.
3. Fixe o ícone "Media Tool" na barra (ícone de quebra-cabeça → alfinete).

ID da extensão: **`lnpealhnkmjaohdlkpapjmdealokkdbf`**. Ele é fixo porque o `manifest.json` tem o campo `key` (só a
chave PÚBLICA RSA 2048; a privada não fica no repositório). O mesmo ID aparece em qualquer computador que carregue a
pasta.

## Pareamento

1. No Media Tool: **Configurações → Extensão do Chrome → Gerar código**. O código tem 8 caracteres (sem 0/O e 1/I),
   vale 5 minutos e só pode ser usado uma vez. Gerar outro invalida o anterior.
2. Na extensão: **Opções** (clique direito no ícone → Opções, ou o link "Opções" do popup). Confira a porta (padrão
   8765), dê um nome ao dispositivo, digite o código (com ou sem hífen) e clique em **Parear**.
3. A extensão recebe um token e o guarda em `chrome.storage.local` (nunca `sync`). Ele não é mostrado em lugar
   nenhum.

Em Configurações aparece a lista de extensões conectadas (nome, conectada em, último uso) com **Revogar**: o token
revogado recebe 401 na próxima chamada e o popup pede um pareamento novo. **Desconectar** nas Opções da extensão só
apaga o token deste navegador; para cortar o acesso de vez, revogue no app.

## Uso (popup)

O popup lê a URL da aba ativa no momento do clique (permissão `activeTab`) e pergunta ao app o que ela é
(`/api/ext/page-info`, sem rede):

- **Publicação / Reel** (`/p/`, `/reel/`, `/reels/`, `/tv/`, também com `/<usuario>/` na frente): "Baixar vídeo(s)",
  "Baixar só a capa", "Baixar tudo da publicação" (fotos e carrossel) e "Coletar perfil desta publicação" (o app
  descobre o @ do dono; se o @ já está na URL, sem consulta).
- **Perfil** (`/<usuario>/`, `/<usuario>/reels/`, `/<usuario>/tagged/`): Mídia (Somente vídeos / Todas / Somente
  capas), Quantidade (10, 50, 100, 500, personalizada, Todas, esta com confirmação; padrão 50, o mesmo "Últimas 50"
  da tela Importar, vindo de `defaults` de `/api/ext/status`), Período (qualquer data, 7, 30,
  60, 90 dias ou personalizado) e Visualizações mínimo/máximo (1 mil, 10 mil, 50 mil, 100 mil, 500 mil, 1 mi ou
  personalizado; desligado com Mídia "Todas", porque o filtro só vale para vídeos e capas).
- **Pós-processamento**: o popup só oferece "Só baixar". Editar após baixar e presets ficam no app ("Mais opções no
  Media Tool" abre a tela Importar).
- **Andamento**: depois de iniciar, o popup consulta `/api/ext/progress` a cada 2 s enquanto está aberto e mostra
  encontradas, baixadas, já existentes, filtradas e falhas. "Abrir no Media Tool" abre a Fila do lote (ou a tela
  Importar, para grupos) numa aba nova (`chrome.tabs.create`, que não exige a permissão `tabs`).
- **Estados**: app desligado ("Abra o Media Tool"), não pareada (botão para as Opções), token revogado (pede novo
  pareamento), Instagram em pausa ou sem sessão (mensagem do próprio app) e erros 4xx com a mensagem do backend.

## Permissões e por quê

| Permissão | Por quê |
| --- | --- |
| `activeTab` | Ler a URL da aba só no momento em que o usuário clica no ícone. Sem `tabs`, nenhuma outra aba é lida. |
| `storage` | Guardar porta, nome do dispositivo e o token em `chrome.storage.local`. |
| `host_permissions`: `http://127.0.0.1:8765/*`, `http://localhost:8765/*` | Falar com o Media Tool local. Nenhum outro host. |

Não há `cookies`, `tabs`, `scripting`, content scripts nem acesso a `instagram.com`. A CSP das páginas da extensão é
`script-src 'self'; object-src 'none'; base-uri 'none'`.

**Outra porta:** a porta é configurável nas Opções, mas `host_permissions` cobre só a 8765. Para usar outra porta é
preciso editar `host_permissions` no `manifest.json` para a mesma porta e recarregar a extensão em
`chrome://extensions` (o ID não muda, porque vem do `key`).

## Segurança (backend)

- **Host** continua só local (`127.0.0.1:<porta>`/`localhost:<porta>`).
- **Origem `chrome-extension://<id>`**: só a NOSSA extensão, de ID fixo `lnpealhnkmjaohdlkpapjmdealokkdbf` (derivado
  da `key` do manifesto), mais IDs extras opcionais na variável `MEDIA_TOOL_EXTENSION_IDS` (separados por vírgula;
  IDs inválidos são ignorados). Outra extensão recebe 403 também em `/api/ext/*`. A nossa só é aceita em
  `/api/ext/*`. Em qualquer outra rota,
  inclusive as de gerenciamento da extensão em Configurações, recebe 403 `origin_not_allowed` em qualquer método. Em
  `/api/ext/*`, outras origens que não sejam do próprio app também recebem 403.
- **CORS** para a origem da extensão só em `/api/ext/*`: preflight `OPTIONS` respondido com 204, métodos `GET, POST`,
  cabeçalhos `Authorization, Content-Type, X-Media-Tool-Client, X-Media-Tool-Token` e
  `Access-Control-Allow-Private-Network: true` quando o navegador pede. Sem credenciais.
- **Token** obrigatório em todas as rotas `/api/ext/*` exceto `POST /api/ext/pair`: `Authorization: Bearer <token>`
  ou `X-Media-Tool-Token`. Inválido, ausente ou revogado: 401 `extension_unauthorized`. Mutações continuam exigindo
  `X-Media-Tool-Client`.
- **Armazenamento** (tabela `app_state`, sem migração): `extension_pairing` guarda só `sha256` do código, validade,
  tentativas e bloqueio; `extension_devices` guarda `id`, nome, `sha256(token)`, criado em e último uso (gravado no
  máximo uma vez por minuto). Comparações com `hmac.compare_digest`, sempre contra todos os dispositivos.
- **Pareamento**: tentativa sem nenhum código ativo (nenhum gerado, expirado ou já usado) responde 400
  `pairing_code_expired` e NÃO conta para o bloqueio. Com código ativo, código errado conta tentativa; na 5ª o pareamento fica bloqueado por 5 minutos (429
  `pairing_locked`) e o código vigente deixa de valer. Gerar um código novo no app libera o bloqueio. Código expirado
  ou já usado: 400 `pairing_code_expired`; errado: 400 `pairing_code_invalid`.
- **Token** de 32 bytes aleatórios em base64url (`secrets.token_urlsafe(32)`). Aparece só na resposta do pareamento
  (com `Cache-Control: no-store`); nunca em log, erro, outra resposta ou na UI.

## API

Todas sob `/api/ext`, JSON. Detalhes em `docs/CONTRACTS.md` (seção "Extensão do Chrome").

| Rota | Autorização | Resposta |
| --- | --- | --- |
| `POST /pair` `{code, device_name}` | código de pareamento | `{token, device_id}` |
| `GET /status` | token | `{ok, app: {version}, instagram: {session_configured, paused, pause_reason, paused_until, message}, defaults}` |
| `POST /page-info` `{url}` | token | `{kind: "post"\|"reel"\|"profile"\|"other", shortcode?, username?, canonical_url?}` |
| `POST /post-owner` `{url}` | token | `{username, shortcode}` (uma consulta pelo portão do app; erros como a prévia de perfil) |
| `POST /start` | token | mesmo corpo e resposta de `POST /api/imports/start`, mais `open_url` |
| `GET /progress?batch_ids=1,2&group_id=3` | token | `{batches: [...], group, finished, missing, open_url, library_url}` |

Gerenciamento (só pelo próprio app): `POST /api/settings/extension/pairing-code` (`{id, code, expires_at,
ttl_seconds}`), `GET /api/settings/extension/pairing-code/{id}` (`{status: "pending"|"used"|"expired", device_id}`;
`expired` também para código substituído ou desconhecido; nunca devolve o código), `GET /api/settings/extension/devices`,
`DELETE /api/settings/extension/devices/{id}`. Configurações consulta o status a cada 2 s enquanto o código está na
tela; com `used` troca o código por "Extensão conectada." e atualiza a lista.

## Testes

- Backend: `backend/tests/test_extension_security.py` e `backend/tests/test_extension_api.py`.
- Extensão: `cd extension` e `node --test` (Node 20+, sem dependências; testes em `extension/tests/`).
- Teste manual/automatizado no Chrome: abra o popup como página com
  `chrome-extension://lnpealhnkmjaohdlkpapjmdealokkdbf/popup.html?testUrl=<url>`. O parâmetro `testUrl` só existe
  para testes, só é aceito quando a extensão está instalada sem compactação ("Carregar sem compactação", manifesto
  sem `update_url`; uma instalação pela loja ganha `update_url` e o ignora) e é ignorado quando ausente (aí vale a
  URL da aba ativa).

## Limitações

- O andamento só é atualizado enquanto o popup está aberto; fechar o popup não interrompe a importação, que segue no
  app (Fila/Importar).
- Porta diferente de 8765 exige editar `host_permissions` (ver acima).
- "Editar após baixar" e presets não estão no popup.
- A extensão é carregada sem compactação (Modo do desenvolvedor); não há publicação na Chrome Web Store.
- `chrome://` e páginas fora do Instagram aparecem como "página não suportada".
