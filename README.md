# Media Tool

Ferramenta local (Windows) para importar mídias de perfis do Instagram usando a **sua própria sessão**, guardar os
originais, processar em lote com FFmpeg e organizar tudo numa biblioteca. Roda inteiramente no seu computador:
backend FastAPI em `127.0.0.1`, painel web React servido pelo próprio backend, banco SQLite e arquivos em pastas
locais. Nada é enviado para servidores de terceiros além das requisições ao próprio Instagram.

## Sumário

- [Requisitos](#requisitos)
- [Primeiro uso](#primeiro-uso)
- [Arquitetura](#arquitetura)
- [Estrutura de pastas](#estrutura-de-pastas)
- [Setup](#setup)
- [Iniciar e parar](#iniciar-e-parar)
- [Configurar a sessão do Instagram](#configurar-a-sessão-do-instagram)
- [Prova de conceito](#prova-de-conceito)
- [Importar um perfil](#importar-um-perfil)
- [Presets](#presets)
- [Processamento em lote](#processamento-em-lote)
- [Lotes grandes](#lotes-grandes)
- [Fila e recuperação](#fila-e-recuperação)
- [Biblioteca](#biblioteca)
- [Baixar ZIP](#baixar-zip)
- [Exportar para pasta](#exportar-para-pasta)
- [Diagnóstico](#diagnóstico)
- [Segurança e privacidade](#segurança-e-privacidade)
- [gallery-dl](#gallery-dl)
- [Extensão Chromium](#extensão-chromium)
- [Troubleshooting](#troubleshooting)
- [Testes](#testes)

## Requisitos

| Item | Versão | Observação |
|---|---|---|
| Windows | 10 ou 11 | A sessão é protegida com DPAPI do Windows. |
| PowerShell | Windows PowerShell 5.1 ou PowerShell 7 | Os scripts funcionam nos dois. |
| Git | qualquer versão recente | Para clonar o repositório. |
| Python | 3.13 (mínimo 3.11) | Usado num ambiente virtual `.venv` do projeto. |
| Node.js / npm | Node 24 / npm 11 | Só para instalar e gerar o build do painel. |
| FFmpeg / ffprobe | qualquer build recente | Opcional: só para processar mídias, gerar miniaturas e metadados. Baixar originais não precisa. `winget install Gyan.FFmpeg` (o setup só avisa, não instala). |

Nada é instalado globalmente pelos scripts: dependências Python vão para `.venv` e as do painel para
`frontend\node_modules`.

## Primeiro uso

Passo a passo numa máquina nova. Clone o repositório numa pasta à sua escolha e abra o PowerShell nela:

```powershell
git clone https://github.com/roggern7/mediatool.git media-tool
cd media-tool
```

Banco, sessão do Instagram, mídias e logs são criados localmente no primeiro uso e nunca são versionados (veja
[Segurança e privacidade](#segurança-e-privacidade)).

1. **Setup** (uma vez):

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
   ```

   Confira o resumo no final. O aviso de FFmpeg ausente só importa se você for processar mídias: nesse caso rode
   `winget install Gyan.FFmpeg`, abra um novo terminal e repita o setup.

2. **Iniciar:**

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1 -Open
   ```

3. **Abrir o painel:** http://127.0.0.1:8765/ (o `-Open` já abre no navegador padrão).

4. **Obter o `sessionid` no seu próprio navegador:** entre em https://www.instagram.com com a sua conta, abra o
   DevTools (F12) > **Application** (Aplicativo) > **Cookies** > `https://www.instagram.com` e copie o valor da linha
   `sessionid`. Esse valor dá acesso à sua conta: **nunca compartilhe** (nem em chats, e-mails, issues ou prompts).

5. **Configurar a sessão** (escolha um):
   - no painel: **Configurações** > seção **Instagram** > **Configurar sessão** > cole no campo `sessionid` >
     **Salvar sessão**;
   - ou pelo script, com entrada oculta: `powershell -ExecutionPolicy Bypass -File .\scripts\set-instagram-session.ps1`.

6. **Testar a sessão:** em **Configurações** > **Instagram**, clique em **Testar sessão** e confira o resultado.

7. **Prova de conceito com 1 mídia** (opcional, recomendado na primeira vez):

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\poc-instagram.ps1 -Username <perfil> -Content reels
   ```

   Ela testa a sessão, lê o perfil, lista no máximo 1 mídia, baixa só esse arquivo para `storage\temp\poc-<data>` e
   valida o arquivo. Imprime um relatório sem segredos. Essa prova usa o gallery-dl (legado); a do provider web, que
   é o padrão, está em [Prova de conceito](#prova-de-conceito) e exige o app fechado.

8. **Primeira importação:** na tela **Importar**, informe o perfil em **Perfis**, deixe **Quantidade por perfil** em
   **Últimos N** com **10** e clique em
   **Baixar originais**. Só os originais são baixados, sem processamento. Acompanhe em **Importações recentes** ou na tela **Fila**; os itens aparecem na **Biblioteca**.

9. **Baixar e processar (opcional):** repita ligando **Processar após baixar (opcional)**, com **Preset do
   processamento** = **Anti-duplicidade**, e clique em **Baixar e processar**. Os processados aparecem na Biblioteca (aba **Processados**).

10. **Parar** quando terminar:

    ```powershell
    powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
    ```

## Arquitetura

```
  Instagram (web)
        |
        |  cookies da SUA sessão (sessionid), enviados só ao Instagram
        v
  +-----------------------------+      data\secrets\instagram-session.bin
  | Sessão local                | <--  (JSON cifrado com DPAPI, usuário atual)
  | InstagramSessionProvider    |
  +-----------------------------+
        |  InstagramSession (opaca, nunca logada)
        v
  +-----------------------------+      web (padrão): InstagramWebDownloader, no próprio processo
  | CompositeInstagramDownloader| ---> (página inicial + consulta GraphQL com doc_id; tokens só em memória)
  |  (adapter InstagramDownloader)     gallery_dl ou fallback de auto: processo filho com gallery-dl
  +-----------------------------+      (pedido pelo stdin, resultados em JSON lines)
        |  RemoteMedia (URL de CDN) -> download seguro, sem cookies
        v
  storage\temp  --SHA-256 / dedup-->  storage\originals
        |
        v
  +-----------------------------+
  | Fila persistente (SQLite)   |  jobs ingest / process, retry controlado, recovery
  +-----------------------------+
        |
        v
  +-----------------------------+
  | MediaProcessor (FFmpeg)     |  preset: limpeza de metadados, corte, espelho, velocidade...
  +-----------------------------+
        |
        v
  storage\processed  +  storage\thumbs
        |
        v
  Biblioteca (painel em http://127.0.0.1:8765/)
```

Componentes:

- **Backend** (`backend\app`, FastAPI): API REST em `/api`, fila e worker, banco SQLite, serve o painel em `/`.
- **Adapter do Instagram** (`backend\app\integrations\instagram`): implementa a interface `InstagramDownloader`
  com dois providers. O **Instagram Web** (`web\`, padrão) lista como o próprio site: abre a página inicial e
  consulta as publicações pela API GraphQL com `doc_id`, tudo em memória no backend
  ([contrato](docs/INSTAGRAM_WEB_CONTRACT.md)). O **gallery-dl** (legado) roda num processo filho que recebe o
  pedido (incluindo os cookies) pelo stdin. O `CompositeInstagramDownloader` escolhe entre eles pelo **Método de
  importação**.
- **Processor** (`processor\media_processor`): wrapper de FFmpeg/ffprobe (probe, processamento, miniaturas, SHA-256).
- **Frontend** (`frontend`, React + Vite + TypeScript): painel com as telas Importar, Biblioteca, Fila, Presets e
  Configurações.

Detalhes técnicos (módulos, estados da fila, deduplicação, segurança): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Contratos entre módulos: [docs/CONTRACTS.md](docs/CONTRACTS.md).

## Estrutura de pastas

```
media-tool\              (a pasta onde você clonou o repositório)
  .venv\                  ambiente Python do projeto (criado pelo setup)
  .env                    configuração local opcional (não versionado; modelo em .env.example)
  backend\                FastAPI (pacote app), migrations em app\db\migrations, tools\ e testes em backend\tests
  processor\              pacote media_processor (FFmpeg) e testes
  frontend\               painel React; build em frontend\dist
  gallery-dl\             opcional: cópia local do gallery-dl upstream (não versionada aqui, não modificada)
  scripts\
    setup.ps1             prepara o ambiente
    start.ps1             inicia o backend (e opcionalmente o Vite)
    stop.ps1              para tudo
    set-instagram-session.ps1   salva/consulta/remove a sessão do Instagram
    poc-instagram.ps1     prova de conceito do adapter (1 mídia)
    lib\common.ps1        funções comuns dos scripts
  data\
    media-tool.db         banco SQLite (criado no primeiro start)
    secrets\              sessão cifrada (nunca versionada)
    .runtime\             PID files dos processos iniciados pelo start.ps1
  storage\
    originals\            originais baixados (por ano\mês\hash)
    processed\            arquivos processados
    temp\                 temporários de download/processamento
    thumbs\               miniaturas
  logs\                   logs do backend e do frontend dev
  docs\                   CONTRACTS.md, ARCHITECTURE.md
```

## Setup

Abra o PowerShell na pasta do projeto e rode:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

O setup:

1. cria as pastas `data`, `data\secrets`, `logs` e `storage\{originals,processed,temp,thumbs}`;
2. verifica o Python 3.11+; cria o `.venv` se não existir (se existir, reusa);
3. instala `backend\requirements.txt` e o `processor` (modo editable) no `.venv`;
4. instala o gallery-dl (método legado de importação) só se ainda não estiver no `.venv`: da cópia local
   `.\gallery-dl` em modo editable, se ela existir; senão, a versão fixada `gallery-dl==1.32.14` do PyPI;
5. verifica Node/npm, roda `npm ci` (ou `npm install` sem lockfile) e `npm run build` em `frontend`;
6. verifica FFmpeg e ffprobe (se faltar, mostra o comando sugerido `winget install Gyan.FFmpeg`);
7. imprime um resumo e os próximos passos.

Parâmetros: `-SkipPython` (pula venv e pacotes) e `-SkipFrontend` (pula Node, npm e build).
O banco é criado e migrado automaticamente pelo backend no primeiro start.

Configuração opcional: copie `.env.example` para `.env` e ajuste (FFmpeg fora do PATH, concorrência, pastas).

## Iniciar e parar

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1 -Open
powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
```

`start.ps1` valida o ambiente (`.venv`, pacotes `fastapi`, `uvicorn`, `app`, `media_processor`, `gallery_dl`,
`frontend\dist`), verifica a porta, inicia o backend em segundo plano e espera `/api/health` responder (até 30 s).
Log da aplicação: `logs\media-tool.log` (rotativo, 5 MB x 5). Em segundo plano ele não é repetido no console; o
access log do uvicorn fica desligado (`--no-access-log`) e `logs\backend.out.log`/`logs\backend.err.log` guardam só
mensagens do servidor (início e erros de inicialização). PID: `data\.runtime\backend.pid`.
Se o Media Tool já estiver rodando na porta, o script só mostra as URLs e sai com sucesso.

| Opção | Efeito |
|---|---|
| `-Port <n>` | Porta do backend (padrão 8765). |
| `-Open` | Abre o painel no navegador padrão (com `-Dev`, abre o Vite). |
| `-Dev` | Também inicia o Vite (`npm run dev`) em `http://127.0.0.1:5173`, com PID em `data\.runtime\frontend.pid` e logs em `logs\frontend.*.log`. O backend iniciado com `-Dev` recebe `MEDIA_TOOL_DEV=1` (só no processo dele, nada global ou persistente), o que libera a origem do Vite (`127.0.0.1:5173` / `localhost:5173`). |
| `-Foreground` | Roda o backend no console atual, com os logs na tela; Ctrl+C para parar. |
| `-NoBuild` | Se `frontend\dist` não existir, não gera o build (só avisa; a API funciona). |

`stop.ps1` encerra os processos dos PID files (com os processos filhos), confirmando antes pela linha de comando
que são do Media Tool. Se não houver PID file, procura quem escuta na porta 8765 (ou `-Port`) e só encerra se for
`uvicorn app.main:app`. Rodar o `stop.ps1` sem nada em execução não é erro.

Ambiente dos processos iniciados pelo `start.ps1`: o backend recebe `MEDIA_TOOL_HOST=127.0.0.1` e
`MEDIA_TOOL_PORT=<porta>` (sobrepõem o `.env`); sem `-Dev`, `MEDIA_TOOL_DEV` é removido do ambiente dele. Variáveis
cujo nome começa com `INSTAGRAM_` não são repassadas aos filhos, exceto `INSTAGRAM_SESSIONID` para o backend (fallback
documentado da sessão). O Vite não recebe nenhuma variável `INSTAGRAM_*`.

Se o backend já estava rodando sem `-Dev` e você roda `start.ps1 -Dev`, o Vite sobe mas o backend continua recusando
alterações vindas dele (`origin_not_allowed`): rode `stop.ps1` e depois `start.ps1 -Dev`.

URLs:

- Painel: http://127.0.0.1:8765/
- API docs (Swagger): http://127.0.0.1:8765/docs
- Health: http://127.0.0.1:8765/api/health
- Vite (só com `-Dev`): http://127.0.0.1:5173/ (o proxy de `/api` aponta para a porta do backend, lida de
  `MEDIA_TOOL_PORT`, que o `start.ps1` repassa ao Vite)

Para rodar o backend manualmente, sem script:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8765
```

## Configurar a sessão do Instagram

O Media Tool acessa o Instagram como **você**, usando o cookie `sessionid` de uma sessão em que você já fez login
no navegador. Esse cookie é uma credencial: quem o tiver acessa a sua conta.

**Onde fica:** `data\secrets\instagram-session.bin`, um JSON cifrado com a DPAPI do Windows no escopo do usuário
atual (gravado de forma atômica). Só a sua conta do Windows, nesta máquina, consegue decifrar. Além disso, a ACL do
arquivo é restringida (via `icacls`, sem herança) para dar acesso só ao usuário atual. O arquivo nunca é versionado,
o valor nunca vai para o banco, logs, respostas da API ou linha de comando de processos.

**Como obter o `sessionid` no seu navegador** (Chrome, Edge ou outro Chromium):

1. Entre em https://www.instagram.com com a sua conta.
2. Abra o DevTools (F12) e vá em **Application** (Aplicativo) > **Cookies** > `https://www.instagram.com`.
3. Copie o valor da linha `sessionid`.
4. Cole somente no Media Tool (passos abaixo). **Não compartilhe esse valor com ninguém**, não cole em chats,
   e-mails, issues ou prompts. Opcionalmente o painel aceita também `csrftoken`, `ds_user_id`, `mid` e `ig_did`.

**Como salvar** (escolha um):

- Painel: **Configurações** > seção **Instagram** > **Configurar sessão** (ou **Atualizar sessão**, se já houver uma)
  > colar no campo `sessionid` > **Salvar sessão**; depois clique em **Testar sessão**. **Remover sessão** apaga o
  arquivo protegido. O valor salvo nunca é exibido de volta.
- Script (entrada oculta, o valor vai ao backend só pelo stdin):

  ```powershell
  powershell -ExecutionPolicy Bypass -File .\scripts\set-instagram-session.ps1          # salvar
  powershell -ExecutionPolicy Bypass -File .\scripts\set-instagram-session.ps1 -Status  # ver se está configurada
  powershell -ExecutionPolicy Bypass -File .\scripts\set-instagram-session.ps1 -Clear   # remover
  ```

Precedência: o arquivo protegido; se ele não existir, a variável `INSTAGRAM_SESSIONID` (ambiente ou `.env`). A
variável é só um fallback e fica em texto puro; prefira o arquivo.

Quando a sessão expira (logout, troca de senha, checkpoint), pegue um novo `sessionid` e salve de novo.

### Configurações > Instagram: teste de sessão e método de importação

**Testar sessão** usa o método de importação em uso. Com o Instagram Web (padrão) e com Automático, o teste não
baixa nada: abre a página inicial do Instagram com a sua sessão e pede 1 publicação
(count=1) do perfil público `instagram`. O cartão mostra as etapas, cada uma como "ok", "falhou" ou "não testado":

| Etapa no painel | Campo em `last_test` | O que significa |
|---|---|---|
| Sessão salva | `session_present` | Há um `sessionid` salvo. |
| Reconhecida pelo Instagram | `session_recognized` | A resposta da consulta identificou a mesma conta da sessão salva (`ds_user_id`). Vazio = não confirmado; "não" = outra conta ou sessão recusada. A página inicial não conta: ela pode vir sem usuário mesmo logado. |
| Acesso web preparado | `bootstrap` | A página inicial abriu e trouxe o que o Media Tool precisa para consultar. |
| Consulta de publicações | `graphql` | A consulta de 1 publicação do perfil `instagram` funcionou. |
| Listagem de perfil | `listing_api` | Resumo da listagem: funcionando, recusada, limitada ou desconhecida. |

A sessão só é dada como **válida** quando a consulta de publicações funciona. Com gallery-dl (legado), o teste é o
antigo (uma requisição ao endpoint `feed/user`) e as etapas acima não aparecem.

**Método de importação** fica em **Configurações** > **Processamento** (salve com **Salvar**):

- **Instagram Web**: o padrão. Usa só o provider moderno, sem fallback.
- **Automático**: usa o Instagram Web e, só quando ele responde `contract_changed` (o Instagram mudou) ou
  `unsupported_media` (tipo de mídia que o app não baixa), tenta uma vez pelo gallery-dl naquela operação. Nunca cai
  para o gallery-dl em limite de taxa, checkpoint, restrição, sessão inválida, rede ou timeout.
- **gallery-dl (legado)**: usa só o gallery-dl.

Sem escolha salva vale `MEDIA_TOOL_INSTAGRAM_PROVIDER` (`web`, `auto` ou `gallery_dl`, no ambiente ou no `.env`); sem
ela, `web`.

## Prova de conceito

### Provider web (padrão)

`scripts\poc-instagram-web.ps1` valida o provider Instagram Web sobre o app real, com **uma única mídia**:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\poc-instagram-web.ps1 -Username <perfil>
powershell -ExecutionPolicy Bypass -File .\scripts\poc-instagram-web.ps1 -Username <perfil> -Process
```

- `-Username` é obrigatório; `-Content` aceita `reels`, `posts`, `videos`, `images` ou `all` (padrão `all`).
- Cria **um** lote com limite 1, lista pelo provider web (página inicial + consulta de publicações), baixa a mídia
  para `storage\originals` e a registra no banco real em `data\` (ela aparece na Biblioteca).
- Com `-Process`, processa essa mídia com o preset **Anti-duplicidade** e confere que o original não mudou (SHA-256).
- **Para no primeiro erro**, sem nova tentativa (`rate_limited`, `checkpoint_required`, `action_blocked`,
  `contract_changed`, `session_invalid`...).
- **Não roda com o app aberto**: como usa a mesma fila, ela recusa (`app_running`) se o backend estiver respondendo
  na porta; feche com `scripts\stop.ps1` antes. Também recusa se houver lote ou job em andamento (`queue_busy`).
- Imprime um relatório JSON sem segredos (código, mensagem, etapa e `detail`). Saída 0 = ok, 1 = falha, 2 =
  pré-condição (app aberto, fila ocupada, preset ausente).

### gallery-dl (legado)

`scripts\poc-instagram.ps1` valida o adapter gallery-dl de ponta a ponta com uma única mídia, sem passar pela fila:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\poc-instagram.ps1 -Username <perfil> -Content reels
```

`-Username` é obrigatório; `-Content` aceita `reels` (padrão), `posts`, `videos`, `images` ou `all`. A prova usa a
sessão configurada, testa a sessão, lê o perfil, lista no máximo 1 mídia, baixa só esse arquivo para
`storage\temp\poc-<timestamp>` e valida (tamanho, magic bytes, ffprobe). O relatório é JSON, sem segredos.

## Importar um perfil

Na tela **Importar** (rota `/`):

1. **Perfis:** campo de várias linhas, um perfil por linha: `usuario`, `@usuario` ou a URL
   `https://www.instagram.com/usuario/`. Linhas vazias são ignoradas e repetições são removidas. A lista é conferida
   pelo backend enquanto você digita (`POST /api/import-groups/preview`, sem falar com o Instagram): a tela mostra
   quantos perfis são válidos, quantas duplicatas saíram e quais entradas são inválidas, com o motivo. As inválidas
   ficam de fora e o botão de início avisa isso. Com um único perfil na lista, **Consultar perfil** mostra a prévia
   (nome, avatar, quantidade de posts, privado/verificado). Sem sessão configurada a tela mostra
   **Configurar em Configurações**.
2. **Tipo de mídia:** **Todos**, **Reels** ou **Publicações do feed** (`all`, `reels`, `photos_carousels`).
   **Reels** traz só publicações classificadas como Reel. **Publicações do feed** traz todo o resto: fotos simples,
   vídeos avulsos antigos que não são Reels e todos os itens dos carrosséis (fotos e vídeos).
3. **Quantidade por perfil:** **Últimos N** (N inteiro livre de 1 a 1.000.000, com atalhos 100, 500 e 1.000) ou
   **Todos** (`limit` nulo), que percorre o perfil até o fim real da paginação; em perfis grandes isso pode levar
   horas por causa do limite de requisições por hora, e o lote continua sozinho. A quantidade conta mídias do tipo
   escolhido, não posts (um carrossel pode ter várias; "100 + Reels" são 100 Reels): o app continua procurando até
   achar N ou chegar ao fim do perfil.
4. **Processamento:** **Processar após baixar (opcional)** vem desligado. Ligado, mostra o **Preset do
   processamento** (padrão: o preset padrão de Configurações, ou Anti-duplicidade).
5. Antes de iniciar, a tela mostra um resumo em uma linha, por exemplo
   `37 perfis • Todos os Reels • Todas as mídias disponíveis`. Clique em **Baixar originais** (só baixa os
   originais). Com o processamento ligado, o botão vira **Baixar e processar**.

Com **um perfil** é criado um lote avulso (`POST /api/imports`): primeiro a listagem do perfil (`listing`), depois
um job por mídia (`running`). O painel do lote, logo abaixo do formulário, mostra o progresso real com **Cancelar**,
**Tentar falhas novamente** e **Abrir Biblioteca**.

Com **dois ou mais perfis** é criada uma importação em grupo (`POST /api/import-groups`, contrato em
`docs/CONTRACTS.md`, seção 8.1): um lote por perfil, listados um de cada vez; a quantidade vale para cada perfil.
Perfis que já têm importação em andamento são pulados e a tela diz quais. O painel **Importação • N perfis** mostra
o resumo (concluídos, ativos, aguardando, com atenção, mídias baixadas, descartes) e a lista de perfis, 100 por
página, com o estado de cada um (aguardando, listando, baixando, aguardando downloads, aguardando limite de
requisições com o horário de retomada, pausado, atenção, concluído, falhou, cancelado) e a quantidade
(`830 / 2.000`, `1.240 mídias`, `perfil chegou ao fim: 37 de 100`). **Cancelar importação** cancela todos os perfis
ainda abertos; clicar em um perfil abre o lote dele (cancelar, retomar, **Ver na Fila**).

A tabela **Importações recentes** lista as importações anteriores: cada grupo aparece em uma linha só, junto com
os lotes de um perfil. Só uma listagem roda por vez. Mídias já baixadas antes não são baixadas de novo (ver
deduplicação na seção Fila).

### Importar por link

No modo **Por link**, cole um ou vários links de publicações, um por linha (`https://www.instagram.com/p/...`,
`/reel/...` ou `/reels/...`; até 500 por vez). O app baixa todas as mídias de cada publicação: foto, Reel, vídeo ou o
carrossel inteiro. Os links colados formam um lote só (`POST /api/import-links`, contrato em `docs/CONTRACTS.md`,
seção 8.3), consultado um link por vez. Link repetido é removido; link que falha (publicação apagada, perfil privado)
fica marcado com o motivo e os outros seguem. O lote aparece em **Importações recentes** e aceita **Cancelar**,
**Tentar falhas novamente**, **Baixar ZIP** e **Exportar para pasta** como os demais.

Cada link é aberto como a página pública da publicação, sem usar a sessão salva. Ressalva: isso foi verificado no
Instagram real só para um Reel público (2026-10-10); foto, carrossel e publicação restrita ainda dependem de uma
prova autorizada pelo dono (`scripts\poc-instagram-post.ps1`), e uma publicação que exige login não é importada.
Como proteção, se 3 links seguidos
falharem o lote para e pede atenção (`links_failing`) em vez de gastar uma consulta por link; **Retomar** continua
pelos próximos.

## Presets

Dois presets internos vêm prontos (criados pela migration `0001`): **Padrão** e **Anti-duplicidade**. Ambos podem
ter os parâmetros editados e ser duplicados, mas não podem ser renomeados nem excluídos. Os dois começam com os
mesmos valores:

| Parâmetro | Valor | Significado |
|---|---|---|
| `metadata_cleanup` | `true` | Remove metadados e capítulos (`-map_metadata -1`, `-map_chapters -1`, bitexact). |
| `trim_start` | `0.5` | Corta 0,5 s do início (0 a 30), mantendo áudio e vídeo sincronizados. |
| `mirror` | `true` | Espelha horizontalmente (hflip). |
| `speed` | `1.05` | Velocidade 1,05x (0,5 a 2), vídeo e áudio. |
| `auto_adjust` | `true` | Corrige pixels não quadrados, dimensões ímpares, rotação e timestamps quando necessário. |
| `reencode` | `true` | Reencoda (H.264 libx264, yuv420p, AAC, `+faststart`). |

Com `reencode=false` e algum filtro ativo, o reencode é forçado; só a limpeza de metadados usa cópia de streams.
Imagens viram JPEG de alta qualidade (PNG continua PNG), espelhadas, sem EXIF e com a orientação aplicada.
Presets próprios: tela **Presets** (**Novo preset**, **Editar**, **Duplicar**, **Excluir**). No editor os campos são
Nome, Limpar metadados, Cortar início, Espelhar, Velocidade, Ajustes automáticos e Reencode.

## Processamento em lote

O fluxo principal e padrão do Media Tool é **só baixar**: cada mídia termina em `completed` assim que o original
está salvo, conferido (SHA-256) e registrado, sem passar pelo FFmpeg. O processamento com preset é opcional e
separado: nada é processado automaticamente depois do download. O download puro não usa FFmpeg nem ffprobe (nem
quando estão instalados): valida o arquivo pela assinatura, calcula o SHA-256 e registra, então as mídias ficam sem
miniatura e sem dados técnicos. A análise (metadados técnicos e miniatura) acontece no processamento opcional. Na API, `POST /api/imports` aceita `process`
(padrão `false`) e `preset_id` (padrão `null`) como opcionais.

- Na importação: ligue **Processar após baixar (opcional)**, escolha o **Preset do processamento** e clique em
  **Baixar e processar**; cada mídia baixada
  é processada em seguida.
- Na biblioteca: selecione várias mídias e use **Processar com preset**; é criado um lote só de processamento. Para
  uma mídia só, use **Processar** (ou **Reprocessar**, se ela já tiver processados).
- Se a mídia já tem um processado do mesmo preset com os mesmos parâmetros, ela não é reprocessada (o job fica
  concluído com etapa `skipped`).
- O processamento nunca altera o original; o resultado vai para `storage\processed`.

## Lotes grandes

O Media Tool foi preparado para lotes de 1.000 a 10.000 mídias. Por enquanto a tela de importação oferece só 10, 20,
50 ou 100; a API já aceita 250, 500, 1000, 2000, 5000 e 10000. Como ele se comporta em lotes longos:

- **A lista é coletada aos poucos e pode ser retomada.** O Media Tool guarda o ponto onde parou depois de cada
  página. Se o app fechar, o Windows reiniciar ou a rede cair, a coleta continua dali, sem baixar de novo o que já
  terminou.
- **Sem acumular downloads.** A coleta espera quando há cerca de 100 mídias aguardando download no lote (200 no app
  todo) e volta quando a fila diminui. Assim os links de download são usados poucos minutos depois de coletados.
- **A coleta só termina na quantidade pedida ou no fim real do perfil.** Páginas seguidas sem nada do tipo pedido não
  encerram mais a importação: ela continua até achar a quantidade ou o Instagram dizer que o perfil acabou. Se o
  perfil acabar antes, a importação conclui com o que encontrou e avisa. Se a paginação parar de um jeito estranho
  (página vazia ou sem continuação antes do fim), a importação NÃO é dada como concluída: fica aguardando atenção,
  com o ponto de retomada guardado.
- **Ritmo cuidadoso com o Instagram.** Uma coleta por vez no app inteiro, uma requisição ao Instagram por vez e 2 a
  4 s entre páginas (6 a 12 s durante 1 hora depois de um aviso de limite; no máximo 600 páginas por hora, configurável por `INSTAGRAM_PAGE_RATE_LIMIT`).
  Baixar os arquivos e processar não aumentam as chamadas ao Instagram.
- **Link de download vencido.** O Media Tool renova a página daquela mídia uma única vez e tenta de novo; se não der,
  a mídia fica com erro "Conteúdo indisponível". Mídias da mesma página aproveitam a mesma renovação.
- **Pausas que se resolvem sozinhas.** Limite de requisições, recusa pontual de acesso e avisos de restrição pausam
  o Instagram, e o Media Tool verifica de novo sozinho em intervalos crescentes (por exemplo 15 min, 30 min, 1 h).
  Verificação de segurança (checkpoint) ou sessão inválida mostram "Ação necessária". A sessão salva nunca é apagada
  automaticamente.
- **Falhas de rede na coleta** pausam só aquele lote, que tenta de novo sozinho (1 min, 5 min, 15 min, 1 h).
- **"Precisa de atenção".** Se o Instagram recusar a coleta no meio do lote, a importação para, guarda o ponto onde
  estava e mostra o motivo. Depois de resolver, use **Retomar importação** no painel do lote (Importar ou Fila).
- **Pausa do lote por falhas seguidas.** Se muitas mídias seguidas falham pelo mesmo motivo, o lote pausa sozinho e
  o painel mostra o motivo e o horário previsto para retomar.
- **Um perfil por vez.** Começar outra importação de um perfil que já está sendo importado mostra "Este perfil já
  está sendo importado".
- **Espaço em disco.** Antes de cada download e de cada trecho da coleta o Media Tool confere o espaço livre (mínimo
  de 5 GB mais uma folga para os downloads em andamento; a folga para os arquivos processados só entra enquanto
  houver processamento na fila). Faltando espaço, os downloads param sem gastar
  tentativas, uma faixa aparece no topo do painel e tudo continua sozinho quando houver espaço (ele confere a cada
  minuto). O espaço livre aparece em **Configurações** > **Processamento**.
- **Downloads e processamentos separados.** Em **Configurações** > **Processamento**: **Downloads simultâneos** (1 a
  4, padrão 2) e **Processamentos simultâneos** (1 a 2, padrão 1; usa mais o computador).
- **Computador acordado.** Enquanto houver lote ativo, o Windows não entra em suspensão (a tela ainda pode desligar).
- **Processar por filtro** não cria trabalho repetido: ignora mídias que já estão sendo processadas ou que já têm o
  mesmo preset aplicado.
- **Logs enxutos.** Uma linha por mídia concluída ou com erro em `logs\media-tool.log`; as etapas de cada mídia só
  aparecem com nível DEBUG.

## Fila e recuperação

A tela **Fila** mostra os jobs com filtros **Todos**, **Pendentes**, **Processando**, **Concluídos** e **Erros**, e
as ações **Cancelar** e **Tentar novamente** por job. O progresso de cada lote (`found`, `downloaded`, `processed`,
`skipped`, `failed`, `pending`, `active`) aparece em **Importações recentes**, na tela Importar.

- Estados do job: `pending`, `downloading`, `downloaded`, `processing`, `completed`, `failed`, `cancelled`.
- Concorrência: **Configurações** > **Processamento** > **Downloads simultâneos** (1 a 4; padrão 2) e
  **Processamentos simultâneos** (1 a 2; padrão 1). Ver [Lotes grandes](#lotes-grandes).
- Cada job é reivindicado de forma atômica com um "lease" (coluna `claimed_by`, migration `0002`), então um job em
  execução nunca é pego duas vezes.
- Retry controlado: erros marcados como temporários (por exemplo rede, limite de taxa, falha transitória de download)
  voltam para `pending` com espera de 30 s na primeira falha e 2 min na segunda; na terceira tentativa
  (`max_attempts` = 3) o job fica `failed`. Nunca há retry infinito.
- Pausa do Instagram: todo o tráfego automático ao Instagram fica pausado em quatro casos. `rate_limited` (HTTP 429)
  pausa por 10 minutos. `action_blocked` (resposta com campos explícitos de restrição, resultado `restricted`) aplica
  uma pausa de proteção do Media Tool de 1 hora e não é repetido (o job ou lote falha com mensagem clara).
  `checkpoint_required` (o Instagram pediu uma verificação de segurança) pausa por 24 horas: conclua a verificação
  no navegador e libere a pausa em Configurações. `session_invalid` com evidência explícita (`require_login`, HTTP
  401, `sessionid` apagado) pausa até você salvar a sessão ou atualizar os cookies em Configurações (a pausa é
  liberada sozinha nesse momento), com teto de 24 horas. Nenhum desses casos tem retry automático.
  `access_refused` (redirecionamento para a página inicial) **não** pausa: o job ou lote falha sem retry. Uma pausa
  nova nunca encurta a vigente. A pausa (fim e motivo) fica salva no banco e continua valendo após reiniciar o
  backend; uma pausa antiga com motivo `access_refused` é descartada ao iniciar. Todas podem ser liberadas
  manualmente.
  Durante a pausa: downloads de jobs não começam, novas importações e a prévia de perfil respondem
  `instagram_paused` (com o horário de fim e o motivo), e lotes já criados em `listing` esperam a pausa acabar para
  listar. Continuam funcionando: processamento com FFmpeg, biblioteca e o botão **Testar sessão** (uma requisição
  manual; a resposta mostra a pausa vigente). Para liberar antes do prazo, use **Liberar pausa** em Configurações
  (`DELETE /api/settings/instagram/pause`).
- Deduplicação: antes de baixar, se a mesma mídia (post e índice) já existe, não baixa de novo
  (`duplicate_reason=source_id`); depois de baixar, se o SHA-256 for igual a um original existente, o arquivo novo é
  descartado e o job é ligado ao existente (`duplicate_reason=sha256`).
- Recuperação no start: jobs interrompidos voltam para a fila (`downloading` volta a `pending`, `processing` volta a
  `downloaded`), com a tentativa contada e `error_code=interrupted`; se já esgotaram as tentativas, ficam `failed`.
  Listagens interrompidas são retomadas sem duplicar jobs e temporários órfãos em `storage\temp` são removidos.
- Jobs travados: se um job em execução fica mais de 10 minutos sem sinal de vida (heartbeat), a manutenção do
  worker o devolve à fila.

## Biblioteca

A tela **Biblioteca** lista as mídias com miniatura local, abas **Todos**, **Originais** e **Processados**, filtro
de tipo (**Todos**, **Vídeos**, **Imagens**), busca **Buscar por @perfil** e detalhes de origem (perfil, post, data).
Para cada mídia: ver o original e os processados, **Abrir arquivo**, **Abrir pasta** (Explorer), **Processar** /
**Reprocessar**, **Excluir processado** e **Excluir item** (a confirmação, **Excluir item e original**, apaga também
o arquivo original). Só arquivos de mídia dentro do storage podem ser abertos pelo painel. O botão **Abrir pasta da
Biblioteca** abre a pasta dos originais no Explorer e **Exportar selecionados** copia a seleção para uma pasta sua
(ver [Exportar para pasta](#exportar-para-pasta)).

Arquivos: originais em `storage\originals\<ano>\<mês>\<hash[:2]>\<hash>.<ext>`, processados em
`storage\processed\<ano>\<mês>\`, miniaturas em `storage\thumbs`. Os nomes internos nunca vêm da origem e nada é
sobrescrito.

## Baixar ZIP

A forma principal de levar as mídias para fora do Media Tool. Ficam três caminhos: **Baixar ZIP**, **Exportar para
pasta** e **Abrir pasta da Biblioteca**. O armazenamento interno não muda e continua sendo a fonte da verdade.

- **Importar**: na importação concluída (um perfil ou vários), **Baixar ZIP** gera um arquivo
  `importacao-AAAA-MM-DD-HHMM.zip` com todas as mídias daquela importação, inclusive as que já estavam na Biblioteca.
- **Biblioteca**: com mídias selecionadas, ou com **Selecionar todos os N resultados**, **Baixar ZIP** gera
  `biblioteca-AAAA-MM-DD-HHMM.zip` com a seleção. Não há limite de quantidade: 100, 10.000 ou mais, só o disco limita.
- O ZIP é **plano**: todos os arquivos na raiz, sem as pastas internas de data/hash e sem pasta por perfil, com os
  mesmos nomes da exportação para pasta. Dois arquivos nunca ficam com o mesmo nome (o segundo ganha ` (2)`).
- Enquanto prepara, o painel mostra `Preparando ZIP • 3.482 / 10.000 mídias` e o botão **Cancelar**. Ao terminar:
  `ZIP pronto • 10.000 mídias • 18,4 GB` e o download começa sozinho pelo navegador. **Baixar novamente** fica
  disponível enquanto o ZIP existir (`disponível até HH:MM`).
- **Espaço**: o ZIP é montado primeiro num arquivo temporário do Media Tool (`data\exports-tmp`), do tamanho da soma
  das mídias. Antes de aceitar o pedido e de novo ao começar a preparar, o Media Tool confere se há espaço,
  contando os ZIPs que já estão na fila e mantendo livre o mínimo que os downloads exigem; se não houver, avisa
  quanto precisa e quanto há. Essa conferência é feita no início: se o disco encher por outro motivo durante a
  preparação, o ZIP falha com aviso de disco cheio e o arquivo parcial é apagado. O navegador vai precisar do mesmo espaço de novo na
  pasta de Downloads, e isso o Media Tool não consegue conferir. Para acervos muito grandes, **Exportar para pasta**
  copia direto para o destino, sem o arquivo intermediário.
- **Limpeza automática**: o ZIP pronto fica guardado por 24 horas; depois que um download termina por inteiro, por
  no máximo mais 1 hora (um download em andamento nunca é interrompido pela limpeza, e um download interrompido não conta). Cancelar ou falhar apaga o
  arquivo parcial na hora. Se o disco apertar para os downloads, ZIPs que já foram baixados saem antes do prazo.
  O Media Tool só apaga os arquivos que ele mesmo criou em `data\exports-tmp`; os originais nunca são tocados.
- **Saiu da tela?** A preparação continua. Ao voltar para a importação ou para a Biblioteca, o progresso reaparece;
  se o ZIP já ficou pronto, aparece o link **Baixar novamente** com a validade.

## Exportar para pasta

As mídias ficam guardadas em `storage\originals`, em subpastas internas de data e hash. Essa organização serve ao
Media Tool (deduplicação, histórico, processamento) e não muda. Para usar os arquivos fora dele existe a
**exportação**: uma CÓPIA das mídias, com nomes legíveis, direto numa pasta que você escolhe, sem subpastas.

- **Importar**: quando uma importação termina (um perfil ou vários), o painel dela mostra **Ver na Biblioteca**,
  **Exportar para pasta**, **Abrir pasta** e a opção discreta **Usar pasta padrão**.
  - **Exportar para pasta** abre a janela "Selecionar pasta" do próprio Windows (aparece a mensagem *Escolha a pasta
    na janela do Windows…*). Depois de escolher, a cópia roda em segundo plano com progresso (`3.482 / 10.000`) e
    botão **Cancelar exportação**. No fim: `1.248 mídias exportadas para C:\...\Minha pasta` e **Abrir pasta**.
  - **Usar pasta padrão** dispensa a janela: o Media Tool cria `Documentos\Media Tool\Exports\Importacao-AAAA-MM-DD-HHMM`
    e exporta para lá.
  - **Abrir pasta** abre no Explorer a pasta da última exportação concluída daquela importação; se ela ainda não foi
    exportada, o painel orienta a usar **Exportar para pasta**.
  - A exportação é o resultado COMPLETO da importação: entram também as mídias que já estavam na Biblioteca e foram
    reconhecidas como duplicadas. Com vários perfis, tudo vai junto para a mesma pasta, cada mídia uma vez.
- **Biblioteca**: **Abrir pasta da Biblioteca** abre no Explorer a pasta física dos originais. Com mídias
  selecionadas, **Exportar selecionados** copia a seleção para a pasta escolhida; com **Selecionar todos os N
  resultados**, exporta todos os resultados do filtro (50, 500 ou dezenas de milhares), sem limite de quantidade.

O que esperar:

- **É cópia.** O original nunca é movido nem apagado, e não há atalho/link entre a pasta exportada e a Biblioteca.
  Você pode mover, renomear ou apagar a pasta exportada (ou copiá-la para outro computador) sem afetar nada.
- **São os originais**, não as versões processadas.
- **Nomes**: `perfil_AAAA-MM-DD_codigo-do-post.ext` (item de carrossel ganha `_1`, `_2`...), com a extensão do
  original. Caracteres que o Windows não aceita são trocados por `_`.
- **Data no nome**: é a data da publicação no SEU fuso, não em UTC: uma publicação feita às 22h no Brasil sai com
  a data daquele dia. O fuso é resolvido nesta ordem: (1) a variável opcional `MEDIA_TOOL_TIMEZONE` (nome IANA,
  ex.: `America/Sao_Paulo`, no `.env`; exige o pacote `tzdata`, instalado pelo `setup.ps1`); (2) o fuso do
  Windows, com as regras de horário de verão do ano de cada publicação (datas antigas saem certas, inclusive o
  horário de verão do Brasil até 2019); (3) UTC, se nada puder ser determinado. Valor inválido na variável gera um
  aviso no log e vale o fuso do Windows. Dentro do ZIP, a data de modificação de cada arquivo é a hora da
  publicação nesse mesmo fuso; na exportação para pasta, a data de modificação do arquivo copiado também é o
  horário da publicação (sem data de publicação, a do original), o que permite ordenar por data no Explorer. Nada
  é alterado na Biblioteca: é só o que é exportado. Mudar `MEDIA_TOOL_TIMEZONE` ou o fuso do Windows só vale
  depois de REINICIAR o Media Tool. Pelo fuso do Windows, o horário de verão antigo vale a partir do primeiro ano
  com regra registrada no sistema (2004 no Windows atual; antes disso a data pode divergir em publicações perto da
  meia-noite); com `MEDIA_TOOL_TIMEZONE` + `tzdata` vale para qualquer data.
- **O que muda nos nomes**: exportações feitas antes desta regra usavam a data em UTC, então uma mídia publicada à
  noite pode ter saído com o dia seguinte (ex.: antes `perfil_2026-03-05_ABC.jpg`, agora
  `perfil_2026-03-04_ABC.jpg`). O mesmo vale se você trocar o fuso. Como "já está na pasta" é decidido pelo nome,
  reexportar para uma pasta antiga pode gravar essas mídias de novo com o nome novo, ao lado das antigas (nada é
  sobrescrito). Para evitar repetição, exporte para uma pasta nova.
- **Nada é sobrescrito.** Se a pasta já tem um arquivo com o mesmo nome: sendo o mesmo conteúdo, ele não é copiado de
  novo ("já estavam na pasta"); sendo outro arquivo, a mídia é gravada com ` (2)`, ` (3)`... ("renomeadas"). Exportar
  de novo para a mesma pasta só completa o que faltava.
- **Cancelar** mantém os arquivos já copiados e remove o arquivo temporário que estava pela metade. Se o Media Tool
  for fechado no meio, a exportação aparece como interrompida; exporte de novo para a mesma pasta para completar.
- **Arquivos temporários**: durante a cópia cada mídia é gravada primeiro num arquivo `.nome.N.xxxxxxxx.mtpart`
  (começa com ponto, termina em `.mtpart`) e só depois recebe o nome final. O Media Tool só apaga os temporários
  que ele mesmo criou naquela execução e nunca apaga outro arquivo da sua pasta, mesmo com nome parecido. Se o
  computador ou o app for desligado no meio, um `.mtpart` pode sobrar: pode apagar à mão.
- **Uma exportação por vez**: as outras esperam na fila (até 20). Downloads e processamento continuam normalmente.
  A mesma importação não pode ter duas exportações abertas ao mesmo tempo (nem a Biblioteca).
- A pasta padrão criada em Documentos é removida sozinha se a exportação for cancelada ou falhar sem copiar nada.
- **Pastas recusadas**: a raiz de uma unidade (`C:\`, escolha uma subpasta), as pastas do próprio Media Tool (storage,
  data, logs, a pasta do projeto, ou uma pasta que as contenha) e as pastas do Windows (`Windows`, `Program Files`,
  `ProgramData`). Pen drive, HD externo e pasta de rede são aceitos.
- Disco de destino cheio: a exportação para com um aviso claro; libere espaço e exporte de novo para a mesma pasta.

- **Importações antigas continuam completas.** A limpeza automática da fila (jobs encerrados há mais de 30 dias) não
  reduz a exportação: antes de apagar um job antigo, o Media Tool guarda a ligação entre a importação e a mídia
  (tabela `batch_media`). Exportar uma importação meses depois traz as mesmas mídias. Só sai da lista a mídia que
  você excluiu da Biblioteca.
- **Importação que não terminou**: um perfil parado em "Precisa de atenção" com mídias já baixadas, ou uma
  importação de vários perfis sem nenhum perfil em andamento, também mostram **Exportar para pasta**, com o aviso de
  que a exportação leva só o que já foi baixado.
- **Se a janela de escolha de pasta não abrir**: o Media Tool tenta dois métodos do Windows. Se nenhum funcionar no
  seu computador, aparece um aviso com o botão **Exportar para a pasta padrão em Documentos** (na Biblioteca a pasta
  criada é `Documentos\Media Tool\Exports\Biblioteca-AAAA-MM-DD-HHMM`).

## Diagnóstico

`GET /api/diagnostics` (também exibido no painel em **Configurações** > **Sistema e diagnóstico**) verifica Python,
SQLite e versão do schema,
FFmpeg e ffprobe, gallery-dl, pastas de storage (existem e graváveis), worker, sessão do Instagram configurada (sem
mostrar o valor) e host/porta do backend. Cada item tem status `ok`, `warn` ou `error`.

Logs: `logs\media-tool.log` (aplicação; uma linha por job concluído ou com erro, etapas só em DEBUG) e
`logs\backend.out.log`/`logs\backend.err.log` (servidor, quando iniciado pelo `start.ps1`). Os logs passam por um
filtro que remove valores de sessão e cookies.

## Segurança e privacidade

- O backend escuta só em `127.0.0.1`; o header Host aceito é `127.0.0.1:<porta>` ou `localhost:<porta>` (proteção
  contra DNS rebinding).
- Mutações na API (POST, PUT, PATCH, DELETE) exigem o header `X-Media-Tool-Client` não vazio e, se houver `Origin`,
  ele precisa ser o do próprio backend (`http://127.0.0.1:<porta>` ou `http://localhost:<porta>`); senão a resposta é
  403 (`origin_not_allowed` / `client_header_required`).
- A origem do Vite (`http://127.0.0.1:5173`, `http://localhost:5173`) e o CORS para ela só são liberados com
  `MEDIA_TOOL_DEV=1`, que o `start.ps1 -Dev` define apenas no processo do backend. Sem `-Dev`, não há CORS.
- Corpo de requisição limitado a 1 MB (413 `payload_too_large`).
- Content-Security-Policy restritiva no painel e na API (`default-src 'self'`, `script-src 'self'`,
  `object-src 'none'`, `frame-ancestors 'none'`, imagens só locais ou da CDN do Instagram), além de
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` e `X-Frame-Options: DENY`. As páginas `/docs` e
  `/openapi.json` ficam sem CSP porque o Swagger carrega scripts externos.
- Caminhos do FFmpeg/ffprobe configurados no painel são validados: caminho absoluto, em disco local (sem UNC nem
  unidade de rede), arquivo existente chamado `ffmpeg.exe` / `ffprobe.exe` e que responde a `-version` como a
  ferramenta certa.
- A sessão do Instagram fica cifrada com DPAPI em `data\secrets\instagram-session.bin`, com ACL restrita ao usuário
  atual; nunca vai para SQLite, logs, respostas da API, argumentos de processo ou arquivos em texto puro. O gallery-dl
  recebe os cookies pelo stdin de um processo filho. Os scripts não repassam variáveis `INSTAGRAM_*` ao Vite.
- Os arquivos de mídia são baixados das URLs de CDN do Instagram sem cookies, só por HTTPS, apenas para hosts
  `*.cdninstagram.com` e `*.fbcdn.net`, com bloqueio de IPs privados, limite de tamanho e verificação do tipo real
  do arquivo.
- O FFmpeg é chamado sem shell, com lista de argumentos; o original nunca é modificado.
- **Exportar para pasta, como o destino é autorizado:** a API não tem nenhum campo de caminho. A pasta de destino só
  existe se você a escolheu na janela "Selecionar pasta" do Windows, aberta pelo próprio backend nesta máquina (um
  processo filho curto, sem shell), ou se é a pasta padrão que o backend cria em `Documentos\Media Tool\Exports`. A
  pasta escolhida fica guardada no backend, ligada a um código aleatório de uso único que vale 10 minutos; o painel
  só devolve esse código. Antes de copiar, o backend confere de novo a pasta: precisa existir, ser gravável, não ser
  a raiz de uma unidade e estar fora das pastas do Media Tool (storage, data, logs, projeto) e do Windows
  (`Windows`, `Program Files`, `ProgramData`), inclusive por atalho, junção ou caminho alternativo para a mesma pasta (rede apontando para
  este computador, `\\?\`): a comparação é pela identidade da pasta no disco, não só pelo texto do caminho. "Abrir pasta" só abre a pasta
  registrada de uma exportação (ou a pasta dos originais), nunca um caminho informado pela página. Como todas essas
  ações são mutações, valem as proteções acima: uma página de outro site não consegue abrir o seletor, exportar nem
  abrir pastas.
- `.gitignore` exclui `.env`, toda a pasta `data` (banco, backups e `data\secrets`), o conteúdo de `storage`, logs,
  arquivos de cookies e artefatos de prova de conceito. O repositório só tem código, migrations, testes, scripts e
  documentação.
- Use o Media Tool só com conteúdo que você tem direito de baixar e de acordo com os termos do Instagram. Respeite
  os intervalos de requisição: importações muito grandes ou repetidas podem levar a limite de taxa ou checkpoint na
  sua conta.

## gallery-dl

O [gallery-dl](https://github.com/mikf/gallery-dl) upstream, versão 1.32.14, é o método **legado** de importação
(o padrão é o provider web). Ele **não faz parte deste repositório** e **não é modificado**: toda a integração fica
no adapter (`backend\app\integrations\instagram\gallerydl.py` e `gallerydl_runner.py`).

Numa máquina nova o setup instala `gallery-dl==1.32.14` do PyPI no `.venv`. Se preferir trabalhar com o código
upstream, clone-o antes do setup na pasta `gallery-dl\` (ignorada pelo git do Media Tool) e o setup o instala em
modo editable:

```powershell
git clone --branch v1.32.14 https://github.com/mikf/gallery-dl.git gallery-dl
```

Para atualizar, atualize a cópia upstream (por exemplo, `git pull` dentro de `gallery-dl\`) ou a versão fixada em
`scripts\setup.ps1`, e rode os testes do adapter.

## Extensão Chromium

Fora da V1. Não existe nenhuma implementação de extensão neste projeto; a importação é feita pelo painel a partir do
perfil, com a sessão configurada localmente.

## Troubleshooting

**FFmpeg ausente.** O setup e o diagnóstico mostram `ffmpeg não encontrado`. Instale com
`winget install Gyan.FFmpeg`, abra um novo terminal e rode o setup de novo. Se o FFmpeg estiver fora do PATH,
defina `MEDIA_TOOL_FFMPEG` e `MEDIA_TOOL_FFPROBE` no `.env` com os caminhos completos, ou preencha
**Caminho do FFmpeg** e **Caminho do ffprobe** em **Configurações** > **Processamento** e clique em **Salvar**.

**Porta ocupada.** Se a porta 8765 estiver em uso por outro programa, o `start.ps1` informa o processo e não o
encerra. Feche o programa ou use `start.ps1 -Port 8780`. Se for um Media Tool antigo, rode `stop.ps1`. Para ver quem
usa a porta: `Get-NetTCPConnection -LocalPort 8765 -State Listen`.

**Backend não sobe.** O `start.ps1` mostra as últimas linhas de `logs\backend.err.log`. Confira se o setup terminou
sem erros e tente `start.ps1 -Foreground` para ver a saída completa no console.

**Sessão inválida, expirada ou checkpoint.** Use **Testar sessão** em Configurações. `session_invalid` ou
`session_expired`: obtenha um novo `sessionid` e salve de novo. `checkpoint_required`: abra o Instagram no navegador,
conclua a verificação pedida e depois salve um `sessionid` novo. `session_missing`: nenhuma sessão configurada.
Com `checkpoint_required` o tráfego automático ao Instagram fica pausado por 24 horas (libere a pausa em
Configurações depois de concluir a verificação). Com `session_invalid` a pausa dura até você salvar a sessão ou os
cookies de novo (limite de 24 horas).

**Limite de taxa (`rate_limited`).** O Instagram limitou as requisições. Os jobs voltam para a fila com espera
crescente e todo o tráfego automático ao Instagram fica pausado por 10 minutos (a pausa sobrevive a um reinício do
backend; importações e prévia respondem `instagram_paused` até lá). Espere, prefira quantidades menores e evite
repetir importações grandes em sequência.

**Sessão reconhecida; listagem recusada (`api_refused` / `access_refused`).** O Instagram reconheceu a sessão (não
apagou o `sessionid`), mas recusou a chamada de listagem usada pelo Media Tool (gallery-dl, endpoint `feed/user`),
redirecionando para a página inicial. O Instagram Web atual carrega perfis por outra API (GraphQL), então isso não
indica sessão inválida nem restrição da conta. Isso vale para o método gallery-dl (legado); o método padrão,
Instagram Web, usa a mesma API do navegador. Não há pausa
automática. O cartão do teste mostra os dois fatos separados: "Sessão: reconhecida/não reconhecida/desconhecida" e
"Listagem: funcionando/recusada/limitada/desconhecida". Se o Set-Cookie apagar o `sessionid`, o resultado é
`invalid`: salve um `sessionid` novo.

**Aviso de restrição (`restricted` / `action_blocked`).** O Instagram respondeu a esta chamada com um aviso de
restrição (campos `feedback_*`/`restriction_enrollment_data`). Isso não prova que a conta inteira esteja restrita.
Como proteção, o Media Tool pausa o tráfego automático ao Instagram por 1 hora; a pausa pode ser liberada em
Configurações. Detalhes técnicos em `docs/instagram-gallerydl-notes.md`.

**O Instagram mudou (`contract_changed`).** A resposta do Instagram não tem mais o formato que o Media Tool
conhece: o Instagram mudou e é preciso atualizar o Media Tool. Não é problema da sua sessão nem da conta, não há
pausa e repetir não resolve. O teste de sessão mostra `instagram_error` com código `contract_changed`; o `detail`
(no cartão do teste, no relatório da prova de conceito ou nos logs) diz qual parte mudou. Se a causa for o
identificador da consulta (`doc_id`; `detail` como `persisted_query_error` ou `missing_root_field`), dá para trocar
sem nova versão, com o `doc_id` atual copiado do DevTools do navegador (aba Network, `POST /graphql/query` da
consulta `PolarisProfilePostsQuery`; copie só o `doc_id`, nunca cookies ou tokens):

- no `.env` (vale depois de reiniciar o backend): `MEDIA_TOOL_IG_DOC_POLARISPROFILEPOSTSQUERY=<doc_id>`;
- ou pela API (vale na próxima operação e tem precedência sobre o `.env`):

  ```powershell
  Invoke-RestMethod -Method Put -Uri http://127.0.0.1:8765/api/settings -ContentType "application/json" `
    -Headers @{ "X-Media-Tool-Client" = "panel" } `
    -Body '{"instagram_web_doc_ids": {"PolarisProfilePostsQuery": "<doc_id>"}}'
  ```

  Para voltar ao valor embutido, envie `{"instagram_web_doc_ids": null}`. Detalhes e outros casos em
  [docs/INSTAGRAM_WEB_CONTRACT.md](docs/INSTAGRAM_WEB_CONTRACT.md#como-atualizar-quando-o-instagram-mudar). Em
  **Automático**, `contract_changed` faz a operação tentar uma vez pelo gallery-dl.

**Painel do Vite não salva nada (`origin_not_allowed`).** O backend foi iniciado sem `-Dev`. Rode `stop.ps1` e
depois `start.ps1 -Dev`.

**Perfil privado (`profile_private`).** Só é possível importar perfis privados que a sua conta segue. Perfil
inexistente aparece como `profile_not_found`.

**Build do frontend.** Se `frontend\dist` não existir, o painel em `/` não abre (a API continua funcionando). Rode
`setup.ps1` (ou, em `frontend`, `npm ci` e `npm run build`). Erros de tipos no build aparecem na saída do
`tsc`. Para trabalhar no painel com recarga automática use `start.ps1 -Dev`.

**Script bloqueado pela política de execução.** Use `powershell -ExecutionPolicy Bypass -File .\scripts\<script>.ps1`
(vale só para aquela execução; nenhuma configuração do sistema é alterada).

## Testes

Rode a partir da raiz do projeto:

```powershell
# Backend (API, banco, fila, adapter, segurança)
cd backend
..\.venv\Scripts\python.exe -m pytest -q
..\.venv\Scripts\python.exe -m pytest -q -m "not slow"   # pula os lentos (uvicorn e FFmpeg reais)
cd ..

# Processor (FFmpeg)
cd processor
..\.venv\Scripts\python.exe -m pytest -q
cd ..

# Frontend: typecheck, lint, testes (vitest) e build
cd frontend
npm run typecheck
npm run lint
npm test
npm run build
cd ..
```

Os testes usam valores falsos de sessão (por exemplo `FAKE-SESSION-0000`) e não acessam o Instagram.
