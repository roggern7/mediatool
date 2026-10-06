import { describe, expect, it } from "vitest";
import { buildCookiePatch, invalidCookieKeys, savedKeysLabel } from "./sessionCookies";

const empty = { csrftoken: "", ds_user_id: "", mid: "", ig_did: "" };

describe("savedKeysLabel", () => {
  it("lista chaves salvas ou Nenhum", () => {
    expect(savedKeysLabel(["csrftoken", "ds_user_id"])).toBe("Salvos: csrftoken, ds_user_id");
    expect(savedKeysLabel([])).toBe("Nenhum");
    expect(savedKeysLabel(undefined)).toBe("Nenhum");
    expect(savedKeysLabel(["rur"])).toBe("Nenhum");
  });
});

describe("buildCookiePatch", () => {
  it("envia só o que mudou", () => {
    expect(buildCookiePatch({ ...empty, csrftoken: " abc " }, new Set())).toEqual({ csrftoken: "abc" });
  });
  it("remoção envia string vazia", () => {
    expect(buildCookiePatch(empty, new Set(["mid"]))).toEqual({ mid: "" });
  });
  it("valor novo vence a remoção", () => {
    expect(buildCookiePatch({ ...empty, mid: "x1" }, new Set(["mid"]))).toEqual({ mid: "x1" });
  });
  it("nada preenchido gera corpo vazio", () => {
    expect(buildCookiePatch(empty, new Set())).toEqual({});
  });
});

describe("invalidCookieKeys", () => {
  it("rejeita caracteres fora do padrão", () => {
    expect(invalidCookieKeys({ ...empty, csrftoken: "a b", ds_user_id: "123" })).toEqual(["csrftoken"]);
    expect(invalidCookieKeys({ ...empty, mid: "a%3A_b-c.d" })).toEqual([]);
  });
});
