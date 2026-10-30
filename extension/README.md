# Media Tool: extensão do Chrome

Central de operações do Media Tool local: Página atual, Importar, Atividade, Biblioteca, Processar, Exportar e
Configurações, no popup e no painel completo (`app.html`). Documentação completa (abas, segurança, API, permissões,
recuperação de conexão, limitações): `docs/EXTENSION.md`.

## Instalar

1. `chrome://extensions` → ligue o **Modo do desenvolvedor**.
2. **Carregar sem compactação** → escolha esta pasta (`extension/`).
3. ID fixo: `lnpealhnkmjaohdlkpapjmdealokkdbf` (vem do campo `key` do manifest, que tem só a chave pública).

## Parear

1. Media Tool → Configurações → Extensão do Chrome → **Gerar código** (8 caracteres, 5 minutos, uso único).
2. Extensão → aba **Configurações** → confira a porta (8765), dê um nome, digite o código e clique em **Parear**.
3. O token fica só em `chrome.storage.local` e persiste entre aberturas do Chrome. Para cortar o acesso, use
   **Revogar** em Configurações do app; **Desconectar** na extensão só apaga o token deste navegador.

## Estrutura

- `popup.html` e `app.html`: a mesma interface (`src/ui/main.js`) em modo popup ou painel completo.
- `src/ui/tabs/*.js`: uma aba por arquivo. `src/ui/dom.js` e `src/ui/thumbs.js`: DOM e miniaturas.
- `src/lib/*.js`: regras puras (cliente da API, importação, atividade, edição, seleção, exportação, conexão),
  cobertas pelos testes.
- `options.html`: redireciona para `app.html?tab=settings`.

## Permissões

- `activeTab`: ler a URL da aba quando você clica no ícone.
- `storage`: guardar porta, nome, token, seleção e preferências (local) e a URL entregue ao painel (sessão).
- `host_permissions` só para `127.0.0.1:8765` e `localhost:8765`. Sem `cookies`, `tabs`, `downloads`, sem acesso ao
  instagram.com. Para outra porta, edite `host_permissions` no `manifest.json` e recarregue a extensão.

## Testes

```
cd extension
node --test
```
