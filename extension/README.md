# Media Tool: extensão do Chrome

Envia a publicação, o Reel ou o perfil do Instagram aberto na aba para o Media Tool local. Documentação completa
(segurança, API, permissões, limitações): `docs/EXTENSION.md`.

## Instalar

1. `chrome://extensions` → ligue o **Modo do desenvolvedor**.
2. **Carregar sem compactação** → escolha esta pasta (`extension/`).
3. ID fixo: `lnpealhnkmjaohdlkpapjmdealokkdbf` (vem do campo `key` do manifest, que tem só a chave pública).

## Parear

1. Media Tool → Configurações → Extensão do Chrome → **Gerar código** (8 caracteres, 5 minutos, uso único).
2. Extensão → **Opções** → confira a porta (8765), dê um nome, digite o código e clique em **Parear**.
3. O token fica só em `chrome.storage.local`. Para cortar o acesso, use **Revogar** em Configurações do app;
   **Desconectar** nas Opções só apaga o token deste navegador.

## Permissões

- `activeTab`: ler a URL da aba quando você clica no ícone.
- `storage`: guardar porta, nome e token (local).
- `host_permissions` só para `127.0.0.1:8765` e `localhost:8765`. Sem `cookies`, sem `tabs`, sem acesso ao
  instagram.com. Para outra porta, edite `host_permissions` no `manifest.json` e recarregue a extensão.

## Testes

```
cd extension
node --test
```
