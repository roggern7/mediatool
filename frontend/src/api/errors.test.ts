import { describe, expect, it } from "vitest";
import { describeErrorCode, errorMessageFor } from "./errors";

describe("errorMessageFor", () => {
  it("prefere a mensagem do backend para códigos do Instagram", () => {
    expect(errorMessageFor("session_invalid", 400, "Cookie recusado pelo Instagram (HTTP 401).")).toBe(
      "Cookie recusado pelo Instagram (HTTP 401).",
    );
    expect(errorMessageFor("instagram_paused", 409, "Instagram em pausa até 14:30.")).toBe(
      "Instagram em pausa até 14:30.",
    );
    expect(errorMessageFor("access_refused", 403, "Recusado.")).toBe("Recusado.");
  });
  it("usa o texto fixo como fallback", () => {
    expect(errorMessageFor("action_blocked", 409, null)).toBe("O Instagram respondeu com um aviso de restrição a esta chamada.");
    expect(errorMessageFor("access_refused", 409, "  ")).toBe("O Instagram recusou a chamada usada pelo Media Tool.");
    expect(errorMessageFor("instagram_paused", 409, null)).toBe("Instagram em pausa.");
  });
  it("request_auth_failed não diz que a sessão é inválida", () => {
    expect(describeErrorCode("request_auth_failed", null)).toBe(
      "O Instagram recusou um acesso desta vez. O Media Tool verifica de novo sozinho em alguns minutos; a sessão salva continua guardada.",
    );
    expect(describeErrorCode("request_auth_failed", "Mensagem do backend.")).toBe("Mensagem do backend.");
    expect(describeErrorCode("session_invalid", null)).toBe(
      "A sessão precisa ser verificada. Teste a sessão ou salve a sessão de novo em Configurações.",
    );
  });
  it("limit_all_disabled não existe mais: cai no detalhe do backend", () => {
    expect(errorMessageFor("limit_all_disabled", 422, "Unprocessable")).toBe("Unprocessable");
  });
  it("erros da importação de vários perfis têm texto fixo em português", () => {
    expect(errorMessageFor("invalid_profiles", 422, "x")).toBe(
      "Há entradas inválidas na lista de perfis. Corrija ou remova as entradas indicadas e tente de novo.",
    );
    expect(errorMessageFor("no_profiles", 422, "x")).toBe("Informe ao menos um perfil.");
    expect(errorMessageFor("too_many_profiles", 422, "x")).toBe(
      "Perfis demais de uma vez. O máximo é 10.000 por importação.",
    );
    expect(errorMessageFor("group_finished", 409, "x")).toBe("Esta importação já terminou; não há nada para cancelar.");
    expect(errorMessageFor("group_not_found", 404, "x")).toBe("Importação não encontrada.");
  });
  it("413 payload_too_large orienta a dividir a lista", () => {
    expect(errorMessageFor("payload_too_large", 413, "Corpo da requisição maior que o limite de 1 MB.")).toBe(
      "A lista é grande demais para enviar de uma vez. Divida em partes menores ou cole só os nomes de usuário, sem a URL completa.",
    );
  });
  it("not_supported_by_provider orienta a trocar o método de importação", () => {
    expect(errorMessageFor("not_supported_by_provider", 422, "x")).toBe(
      "“Todos” e “Publicações do feed” exigem o método de importação web. Troque o método em Configurações > Instagram.",
    );
  });
  it("contract_changed prioriza a mensagem do backend e tem fallback fixo", () => {
    expect(errorMessageFor("contract_changed", 502, "Formato de resposta inesperado.")).toBe(
      "Formato de resposta inesperado.",
    );
    expect(errorMessageFor("contract_changed", 502, null)).toBe(
      "O Instagram mudou a forma de carregar publicações; é preciso atualizar o Media Tool.",
    );
    expect(describeErrorCode("contract_changed", "Formato de resposta inesperado.")).toBe(
      "Formato de resposta inesperado.",
    );
    expect(describeErrorCode("contract_changed", " ")).toBe(
      "O Instagram mudou a forma de carregar publicações; é preciso atualizar o Media Tool.",
    );
  });
  it("códigos locais mantêm o texto fixo", () => {
    expect(errorMessageFor("backend_offline", 0, "x")).toBe("Não foi possível falar com o backend local.");
  });
  it("código desconhecido usa detail ou status", () => {
    expect(errorMessageFor("algo", 409, "Detalhe.")).toBe("Detalhe.");
    expect(errorMessageFor(null, 404, null)).toBe("Item não encontrado.");
  });
});

describe("describeErrorCode", () => {
  it("prefere a mensagem gravada para códigos do Instagram", () => {
    expect(describeErrorCode("rate_limited", "Limite atingido; nova tentativa às 10:00.")).toBe(
      "Limite atingido; nova tentativa às 10:00.",
    );
    expect(describeErrorCode("rate_limited", null)).toMatch(/limitou/);
  });
  it("demais casos", () => {
    expect(describeErrorCode("interrupted", "x")).toBe("Interrompido porque o backend foi reiniciado.");
    expect(describeErrorCode("novo", null)).toBe("Erro (novo).");
    expect(describeErrorCode(null, null)).toBe("Erro desconhecido.");
  });
});

describe("perfil já em importação", () => {
  it("mensagem clara para 409 profile_already_importing", () => {
    expect(errorMessageFor("profile_already_importing", 409, "conflict")).toContain("já está sendo importado");
  });
});

describe("exportar para pasta", () => {
  it("cada código novo tem mensagem própria em português", () => {
    const codes: [string, number, RegExp][] = [
      ["picker_busy", 409, /janela de escolha de pasta/],
      ["picker_unavailable", 501, /Não foi possível abrir a janela de escolha de pasta.*pasta padrão/],
      ["export_token_invalid", 409, /Escolha a pasta de novo/],
      ["export_dest_invalid", 422, /Pasta de destino inválida/],
      ["export_dest_missing", 409, /não existe mais/],
      ["export_dest_not_writable", 409, /Sem permissão para gravar/],
      ["export_dest_drive_root", 422, /raiz de uma unidade/],
      ["export_empty", 409, /Não há mídias para exportar/],
      ["export_dest_too_long", 422, /longo demais/],
      ["export_already_running", 409, /Já existe uma exportação em andamento/],
      ["export_queue_full", 409, /exportações demais na fila/],
      ["export_zip_not_ready", 409, /ainda não está pronto/],
      ["export_read_failed", 500, /não pôde ser lido/],
      ["export_zip_expired", 410, /já foi removido/],
      ["export_not_folder", 409, /é um ZIP/],
      ["export_not_found", 404, /Exportação não encontrada/],
      ["export_finished", 409, /já terminou/],
      ["export_disk_full", 507, /sem espaço/],
      ["export_write_failed", 500, /falhas seguidas/],
    ];
    for (const [code, status, pattern] of codes) {
      expect(errorMessageFor(code, status, "detalhe técnico")).toMatch(pattern);
      expect(describeErrorCode(code, null)).toMatch(pattern);
    }
  });
  it("sem espaço para o ZIP mostra os números do backend, com fallback", () => {
    const detail = "Sem espaço em disco para preparar o ZIP: são necessários 18,6 GB e há 2,0 GB disponíveis.";
    expect(errorMessageFor("export_no_space", 507, detail)).toBe(detail);
    expect(errorMessageFor("export_no_space", 507, null)).toMatch(/Sem espaço em disco para preparar o ZIP/);
  });
  it("pasta recusada e pasta que sumiu usam o texto preciso do backend, com fallback", () => {
    const parent = "Esta pasta contém as pastas internas do Media Tool. Escolha ou crie uma subpasta separada.";
    expect(errorMessageFor("export_dest_forbidden", 422, parent)).toBe(parent);
    expect(errorMessageFor("export_dest_forbidden", 422, null)).toMatch(/usada pelo Media Tool ou pelo Windows/);
    expect(errorMessageFor("export_folder_missing", 409, null)).toMatch(/Exporte de novo/);
  });
});
