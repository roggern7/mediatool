# Extensão do Chrome

A extensão (pasta `extension/`, Manifest V3, sem bibliotecas) é uma central de operações do Media Tool local: lê a
aba aberta do Instagram, importa publicações e perfis, acompanha a atividade, navega na Biblioteca, processa e exporta
em ZIP. Tudo passa pela API local `/api/ext/*`; quem baixa, processa e exporta é sempre o app. A extensão não lê nada
do instagram.com e não usa cookies do navegador.

A mesma interface (mesmo código, `extension/src/ui/`) aparece em dois lugares:

- **Popup** (`popup.html`, cerca de 420×600): clique no ícone da extensão.
- **Painel completo** (`app.html`, aba própria, layout amplo com as abas na lateral): botão **Abrir painel completo**
  no popup. A URL da aba do Instagram é entregue ao painel por `chrome.storage.session` (só nesta sessão do
  navegador). As Opções da extensão (`options.html`) abrem o painel direto na aba Configurações.

## Instalação

1. Abra `chrome://extensions` e ligue o **Modo do desenvolvedor** (canto superior direito).
2. Clique em **Carregar sem compactação** e escolha a pasta `extension/` do Media Tool. Ao atualizar os arquivos,
   clique em **Recarregar** no cartão da extensão.
3. Fixe o ícone "Media Tool" na barra (ícone de quebra-cabeça → alfinete).

ID da extensão: **`lnpealhnkmjaohdlkpapjmdealokkdbf`**. Ele é fixo porque o `manifest.json` tem o campo `key` (só a
chave PÚBLICA RSA 2048; a privada não fica no repositório). O mesmo ID aparece em qualquer computador que carregue a
pasta. Versão do manifesto: `0.2.0`.

## Pareamento e persistência

1. No Media Tool: **Configurações → Extensão do Chrome → Gerar código**. O código tem 8 caracteres (sem 0/O e 1/I),
   vale 5 minutos e só pode ser usado uma vez. Gerar outro invalida o anterior.
2. Na extensão: aba **Configurações** (sem pareamento, a extensão já abre nela). Confira a porta (padrão 8765), dê um
   nome ao dispositivo, digite o código (com ou sem hífen) e clique em **Parear**.
3. A extensão recebe um token e o guarda em `chrome.storage.local` (nunca `sync`). Ele não aparece na tela, em logs
   nem em URLs; vai só no cabeçalho `Authorization` dos pedidos.

O pareamento persiste: fechar e abrir o Chrome (mesmo perfil) mantém a extensão conectada, sem código novo. Em
Configurações do app aparece a lista de extensões conectadas com **Revogar**: o token revogado recebe 401 na próxima
chamada, a extensão mostra "pareamento perdido" e abre a aba Configurações com o campo do código. **Desconectar** (aba
Configurações da extensão, com confirmação) só apaga o token deste navegador; para cortar o acesso de vez, revogue no
app.

## Abas

### Página atual

Lê a URL da aba ativa no momento do clique (`chrome.tabs.query` com a permissão `activeTab`) e pergunta ao app o que
ela é (`POST /api/ext/page-info`, sem rede):

- Tipo (Reel, Publicação, Perfil), endereço, @ do perfil (quando a URL tem, ou o dono já conhecido de uma publicação
  que está na Biblioteca: `library.source_profile`) e código da publicação. O que não se sabe
  aparece como "não disponível"; nada é inventado.
- **Situação na Biblioteca**: para uma publicação, se já está e quais itens (vídeo, foto ou capa, índice, se tem
  editado); para um perfil, quantos itens dele já existem (vídeos, fotos, capas), com o atalho "Ver na Biblioteca".
- **Analisar publicação** (só quando o usuário clica): `POST /api/ext/post-preview`, 1 leitura da publicação pelo
  portão do app (cache de 10 min no backend). Mostra tipo (Reel, Vídeo, Foto, Carrossel), quantidade de itens,
  vídeos, fotos, capas e o @ do dono.
- **Ações conforme o tipo** (todas viram `POST /api/ext/start` com um link):
  - Reel ou vídeo: Baixar vídeo (`videos`), Baixar capa (`covers`), Baixar tudo (vídeo e capa) (`all`; num Reel é o
    mesmo que "Vídeos e capas", por isso só uma das duas aparece).
  - Foto: Baixar foto(s) (`photos`).
  - Carrossel, ou publicação `/p/` ainda não analisada: Baixar todos os itens (`all`), Só fotos, Só vídeos, Capas dos
    vídeos, Vídeos e capas (`videos_covers`).
  - Qualquer publicação: **Importar o perfil desta publicação** (`POST /api/ext/post-owner`; se o @ está na URL, sem
    consulta) leva à aba Importar com o @ preenchido.
  - Perfil: **Importar @perfil** leva à aba Importar com o @ preenchido.
- As ações usam o **fluxo após o download** salvo na aba Importar (linha "Depois de baixar: …", com atalho para
  alterar). Depois de enviar, a extensão abre a aba Atividade.

### Importar

- **Origem**: um perfil (preenchido pela Página atual) ou vários links e perfis (um por linha). Para a lista colada há
  uma prévia local (mesmas regras de `src/lib/instagramUrl.js`): perfis, publicações, inválidos e repetidos. A prévia
  é só indicativa; o app classifica de novo e a resposta do `start` (`invalid`, `duplicates`, `skipped`, `notes`)
  aparece depois de enviar.
- **Tipo de mídia**: Somente vídeos, Somente fotos, Somente capas, Vídeos e capas, Todas (fotos, vídeos e capas).
- **Quantidade por perfil**: 10, 50, 100, 500, personalizada ou Todas (exige marcar a confirmação explícita). O padrão
  vem de `defaults.limit` de `/api/ext/status`.
- **Período**: qualquer data, últimos 7, 30 ou 90 dias, **ano atual** (convertido em dias de 1º de janeiro até hoje,
  contando hoje; a tela mostra quantos) ou personalizado em dias (1 a 3650).
- **Visualizações**: mínimo e máximo (sem filtro, 1 mil, 10 mil, 50 mil, 100 mil, 500 mil, 1 mi ou personalizado; o
  máximo precisa ser maior ou igual ao mínimo). Desabilitado, com explicação, com Somente fotos e Todas.
- Quantidade, período e visualizações valem só para perfis; quando a lista só tem publicações, eles somem.
- **Depois do download**: Só baixar; Baixar e processar automaticamente (usa a configuração da aba Processar: edição
  com os controles reais ou um preset; vai como `post_processing: "edit"` + `edit`, ou `"preset"` + `preset_id`);
  Baixar e aguardar edição manual (só baixa; edite depois pela aba Processar ou no Media Tool).
- **Exportar ZIP automaticamente ao concluir** (originais, editados ou ambos) → `auto_export: {content}`. Editados
  exigem "Baixar e processar automaticamente".
- **Predefinições de operação** (guardadas só neste navegador): Somente baixar; Baixar e processar; Baixar, processar
  e exportar (ZIP de editados); Baixar tudo sem processamento (tipo Todas); e **Salvar atual como…** (até 20; guardam
  tipo, quantidade, período, visualizações, fluxo e exportação, nunca o perfil ou os links). As últimas escolhas são
  lembradas.
- Pedidos grandes (mais de 1.000 publicações no total, ou mais de 50 entradas) pedem confirmação.
- Cada envio leva um `client_request_id` (UUID). O botão fica desabilitado até a resposta; repetir o mesmo pedido
  depois de um erro de rede reaproveita o mesmo id (o backend devolve a mesma resposta em até 10 min, sem criar de
  novo, inclusive se o app reiniciou nesse meio-tempo: a resposta fica gravada no banco por dispositivo, só com ids e
  a resposta pública). Depois de um envio bem-sucedido, o próximo ganha id novo.

### Atividade

- Lista de `GET /api/ext/activity?limit=20`: importações (lotes e grupos), edições e processamentos, mais recentes
  primeiro, criados pela extensão ou pelo app.
- Estado traduzido: Aguardando, Analisando publicação, Baixando…, Processando, Concluído, Concluído parcialmente,
  Falhou, Interrompido. O estado mostrado é sempre o da API: se a API diz que terminou, a tela não fica em
  "Processando".
- Contadores (os zerados somem, exceto encontradas e baixados): encontradas, analisadas, arquivos, baixados,
  duplicados, fora do período, fora das visualizações, sem contagem de visualizações, falhas, processados; mais
  "processando" e "na fila". Barra de progresso geral (indeterminada enquanto nada foi encontrado).
- **Ver itens** (lotes): `GET /api/ext/batches/{id}/items`, com estado individual (aguardando, baixando vídeo, …,
  concluído, duplicado, falhou) e miniaturas.
- **Ver perfis** (grupos): `GET /api/ext/groups/{id}/children` lista os perfis do grupo (20 por vez, **Carregar mais
  perfis**), cada um com estado, progresso, contadores e aviso próprios; **Ver itens** de um perfil abre os itens do
  lote dele (`GET /api/ext/batches/{id}/items`). Enquanto o grupo está ativo, a lista aberta se atualiza junto.
- **Interromper** (com confirmação): `POST /api/ext/batches/{id}/cancel` ou `/groups/{id}/cancel`.
- **Exportação automática**: status e **Baixar ZIP** quando pronta (abre `download_url` numa aba nova). Pedido com
  perfis e links: um ZIP só para o pedido inteiro; os itens dele mostram a mesma exportação ("do pedido").
- **Aviso do item** (`message`): 1a página vazia (o Instagram não devolveu publicações), "precisa de atenção" com o
  motivo, falha, pausas (do lote, da coleta ou do acesso ao Instagram); aparece numa caixa no item.
- **Abrir no Media Tool** (Fila do lote), e nos lotes concluídos **Exportar** e **Processar** (abre a Biblioteca
  filtrada pelo lote).
- Atualiza a cada 2,5 s enquanto a aba está visível e há algo em andamento (15 s quando tudo terminou); para quando o
  popup fecha ou a aba fica escondida. Ao reabrir, tudo é recuperado da API: nada depende do popup aberto.

### Biblioteca

- `GET /api/ext/library` em páginas de 30 (botão **Carregar mais**, keyset `before_id`), com o total.
- Filtros: tipo (vídeos, fotos), papel (originais, capas), edição (editados, sem edição), perfil e número do lote.
- Miniaturas por `GET /api/ext/media/{id}/thumbnail` com `fetch` + token → blob URL, no máximo 4 ao mesmo tempo; as
  URLs são liberadas (`URL.revokeObjectURL`) quando a lista é trocada.
- Selos: Vídeo, Foto, Capa, Editado, Processando, Falhou. Perfil, data da publicação e data do download.
- Seleção múltipla (até 5.000, guardada em `chrome.storage.local`, sobrevive ao fechar o popup), **Selecionar
  visíveis**, **Selecionar todos do lote** (com o filtro de lote preenchido: `GET /api/ext/library/ids`, que traz tipo e papel
  de cada id) e contador no cabeçalho com o resumo por tipo (vídeos, fotos, capas).
- Ações: **Processar**, **Exportar**, **Abrir no Media Tool**.

### Processar

- `GET /api/ext/edit/options` traz os controles REAIS da página Editar: duração (Original, 15 s, 30 s, 60 s,
  Personalizada até 7200 s, com a nota "limite + 0,03 s"), velocidade de 1,00x a 1,50x (campo, botões − / + de 0,01,
  barra e atalhos de 1x a 1,5x), nitidez sutil e saída sempre MP4. Ou **Usar preset de processamento** (presets do
  app).
- Resumo do que será aplicado e validação antes de enviar.
- Aplica à seleção da Biblioteca, só a vídeos. A tela avisa quantos itens não são vídeo (fotos e capas) e serão
  ignorados; o backend devolve também `skipped`. Edição → `POST /api/ext/edits`; preset → `POST /api/ext/process`.
  Máximo de 500 vídeos por pedido; mais de 20 pede confirmação. O lote criado aparece na Atividade.
- A mesma configuração é a usada por "Baixar e processar automaticamente" na aba Importar.
- **Recortar no Media Tool**: o recorte manual é feito na página Editar do app. O botão abre
  `http://127.0.0.1:<porta>/editar?ids=1,2,3` com os vídeos da seleção (até 500; sem vídeos, só `/editar`); o app
  adiciona só os vídeos à seleção dele, avisa quantos itens ignorou e limpa o parâmetro da URL.

### Exportar

- Fonte: selecionados da Biblioteca, ou um lote ou grupo concluído da Atividade (o botão Exportar de um item da
  Atividade já preenche).
- Conteúdo: originais, editados ou ambos. Capas entram como originais, com `_capa` no nome. **Só capas** filtra a
  seleção pelas capas.
- `POST /api/ext/exports` → acompanha `GET /api/ext/exports/{id}` (barra, itens, tamanho) → **Baixar ZIP** abre
  `download_url` numa aba nova. Sempre UM ZIP por pedido. A última exportação fica guardada e é retomada ao reabrir.

### Configurações

Status da conexão (versão do app, porta e nome), **Testar conexão**, **Desconectar**, código de pareamento, porta e
nome do dispositivo.

## Recuperação de conexão

- **App desligado** (sem resposta ou demora): faixa "Não foi possível falar com o Media Tool" com **Tentar de novo** e
  nova tentativa automática a cada 5 s enquanto a janela está visível. Quando o app volta, a faixa some sozinha e a
  aba atual recarrega. As outras abas ficam desabilitadas enquanto isso; Configurações continua utilizável. A
  Atividade percebe a queda sozinha (ela consulta enquanto está visível); nas outras abas, na próxima ação.
- **Não pareada ou token revogado (401)**: faixa de pareamento perdido e a aba Configurações com o campo do código.
- **Muitos pedidos (429)**: mensagem do app na própria aba, sem derrubar a conexão.
- **Outros erros (4xx/5xx)**: a mensagem do backend (`detail`) aparece na aba, com "Tentar de novo" onde faz sentido.

## Permissões e por quê

| Permissão | Por quê |
| --- | --- |
| `activeTab` | Ler a URL da aba só no momento em que o usuário clica no ícone. Sem `tabs`, nenhuma outra aba é lida. |
| `storage` | `chrome.storage.local`: porta, nome do dispositivo, token, seleção, últimas escolhas, predefinições, configuração do processamento e última exportação. `chrome.storage.session`: a URL entregue ao painel completo. |
| `host_permissions`: `http://127.0.0.1:8765/*`, `http://localhost:8765/*` | Falar com o Media Tool local. Nenhum outro host. |

Não há `cookies`, `tabs`, `downloads`, `scripting`, content scripts nem acesso a `instagram.com`. Páginas do app,
ZIPs e o painel completo abrem com `chrome.tabs.create({url})`, que não exige a permissão `tabs`. `app.html` é uma
página da própria extensão e não precisa de permissão. A CSP das páginas da extensão é
`script-src 'self'; object-src 'none'; base-uri 'none'`.

**Outra porta:** a porta é configurável na aba Configurações, mas `host_permissions` cobre só a 8765. Para usar outra
porta é preciso editar `host_permissions` no `manifest.json` para a mesma porta e recarregar a extensão em
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

Todas sob `/api/ext`, JSON, com o token (exceto `POST /pair`). Detalhes em `docs/CONTRACTS.md` (seção "Extensão do
Chrome").

| Rota | Uso na extensão |
| --- | --- |
| `POST /pair` `{code, device_name}` | Configurações: pareamento → `{token, device_id}` |
| `GET /status` | conexão, versão do app, `defaults`, `features` |
| `POST /page-info` `{url}` | Página atual (sem rede): tipo, código, @, `library` |
| `POST /post-preview` `{url}` | Página atual → Analisar publicação (1 leitura) |
| `POST /post-owner` `{url}` | Importar o perfil desta publicação |
| `POST /start` | Importar e ações da Página atual (`media`, filtros, `post_processing`, `auto_export`, `client_request_id`) |
| `GET /activity?limit=20` | Atividade |
| `GET /batches/{id}/items?before_id&limit` | Atividade → Ver itens |
| `GET /groups/{id}/children?after_position&limit` | Atividade → Ver perfis (grupo) |
| `POST /batches/{id}/cancel`, `POST /groups/{id}/cancel` | Atividade → Interromper |
| `GET /library?before_id&limit&media_type&role&view&batch_id&profile` | Biblioteca |
| `GET /library/ids?batch_id&media_type&role` | Biblioteca → Selecionar todos do lote |
| `GET /media/{id}/thumbnail` | miniaturas (fetch + token → blob URL) |
| `GET /edit/options` | Processar: controles e presets |
| `POST /edits`, `POST /process` | Processar: aplicar à seleção |
| `POST /exports`, `GET /exports/{id}` | Exportar |
| `GET /progress` | rota anterior; a interface nova não usa |

Gerenciamento (só pelo próprio app): `POST /api/settings/extension/pairing-code` (`{id, code, expires_at,
ttl_seconds}`), `GET /api/settings/extension/pairing-code/{id}` (`{status: "pending"|"used"|"expired", device_id}`;
`expired` também para código substituído ou desconhecido; nunca devolve o código), `GET /api/settings/extension/devices`,
`DELETE /api/settings/extension/devices/{id}`. Configurações consulta o status a cada 2 s enquanto o código está na
tela; com `used` troca o código por "Extensão conectada." e atualiza a lista.

## Testes

- Backend: `backend/tests/test_extension_security.py` e `backend/tests/test_extension_api.py`.
- Extensão: `cd extension` e `node --test` (Node 20+, sem dependências; testes em `extension/tests/`): cliente da API
  (rotas, 401, 429, sem conexão e demora, miniatura), corpo do `start` para cada tipo, fluxo e predefinição, período
  "ano atual", visualizações, estados e contadores da atividade, seleção, processamento, exportação e idempotência
  local.
- Teste no Chrome: o popup pode ser aberto como página em
  `chrome-extension://lnpealhnkmjaohdlkpapjmdealokkdbf/popup.html?testUrl=<url>`. O parâmetro `testUrl` só existe
  para testes, só é aceito quando a extensão está instalada sem compactação ("Carregar sem compactação", manifesto
  sem `update_url`; uma instalação pela loja ganha `update_url` e o ignora) e é ignorado quando ausente (aí vale a
  URL da aba ativa). `app.html?tab=<id>` abre o painel numa aba específica (`page`, `import`, `activity`, `library`,
  `process`, `export`, `settings`).

## Solução de problemas

- **"Não foi possível falar com o Media Tool"**: o app não está rodando ou a porta está errada. Inicie o Media Tool;
  a extensão reconecta sozinha em até 5 s. Confira a porta em Configurações (e `host_permissions`, se não for 8765).
- **"Pareamento perdido"**: o token foi revogado no app ou a extensão foi desconectada. Gere um código novo no app e
  pareie de novo.
- **Página atual mostra "Nenhuma página do Instagram"**: clique no ícone com a aba do Instagram ativa (a extensão só
  lê a URL da aba no momento do clique). O painel completo só conhece a página quando é aberto pelo botão do popup.
- **"Sessão do Instagram não configurada"** ao importar: configure o `sessionid` em Configurações do app.
- **"sem miniatura"**: o app não conseguiu gerar a miniatura daquele item; o item continua utilizável.
- **Aplicar desabilitado em Processar**: a seleção não tem vídeos ou a configuração tem erro (veja o resumo em
  vermelho).

## Limitações

- Porta diferente de 8765 exige editar `host_permissions` (ver acima).
- Não há recorte manual na extensão: "Recortar no Media Tool" abre a página Editar já com os vídeos selecionados
  (até 500).
- O cache da prévia de publicação (`post-preview`, 10 min) fica só na memória do app: some num reinício (é só cache;
  custa no máximo uma leitura nova). A idempotência do start, ao contrário, é gravada no banco.
- Predefinições, seleção e últimas escolhas ficam só neste navegador (`chrome.storage.local`).
- A extensão é carregada sem compactação (Modo do desenvolvedor); não há publicação na Chrome Web Store.
- `chrome://` e páginas fora do Instagram aparecem como "Nenhuma página do Instagram".
