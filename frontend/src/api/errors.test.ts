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
  it("limit_all_disabled explica que Todos está indisponível", () => {
    expect(errorMessageFor("limit_all_disabled", 422, "Unprocessable")).toBe(
      "A opção Todos está indisponível por enquanto. Escolha 10, 20, 50 ou 100.",
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
