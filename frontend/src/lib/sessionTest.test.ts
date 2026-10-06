import { describe, expect, it } from "vitest";
import type { SessionTest } from "../api/types";
import { describeSessionTest, formatTestDuration, sessionResult } from "./sessionTest";

const base: SessionTest = { ok: false, code: "x", message: "", username: null, tested_at: "2026-10-03T12:00:00Z" };

describe("describeSessionTest", () => {
  it("válida com usuário", () => {
    const v = describeSessionTest({ ...base, ok: true, result: "valid", code: "ok", username: "conta" });
    expect(v.label).toBe("Válida (@conta)");
    expect(v.tone).toBe("ok");
  });
  it("válida com conta confirmada fica verde", () => {
    const v = describeSessionTest({
      ...base,
      ok: true,
      result: "valid",
      code: "ok",
      username: "conta",
      session_present: true,
      session_recognized: true,
      bootstrap: "ok",
      graphql: "ok",
      listing_api: "ok",
    });
    expect(v.label).toBe("Válida (@conta)");
    expect(v.tone).toBe("ok");
  });
  it("válida sem conta confirmada fica em aviso", () => {
    const v = describeSessionTest({
      ...base,
      ok: true,
      result: "valid",
      code: "ok",
      username: null,
      message: "A consulta de publicações funcionou, mas o Instagram não confirmou a conta da sessão.",
      session_present: true,
      session_recognized: null,
      bootstrap: "ok",
      graphql: "ok",
      listing_api: null,
    });
    expect(v.label).toBe("Funcionando, conta não confirmada");
    expect(v.tone).toBe("warn");
  });
  it("válida com outra conta fica em aviso", () => {
    const v = describeSessionTest({
      ...base,
      ok: true,
      result: "valid",
      code: "ok",
      username: "outra",
      message: "A sessão salva pertence a outra conta.",
      session_present: true,
      session_recognized: false,
      bootstrap: "ok",
      graphql: "ok",
      listing_api: null,
    });
    expect(v.label).toBe("Funcionando com outra conta");
    expect(v.tone).toBe("warn");
  });
  it("tons por resultado", () => {
    expect(describeSessionTest({ ...base, result: "rate_limited" }).tone).toBe("warn");
    expect(describeSessionTest({ ...base, result: "timeout" }).tone).toBe("warn");
    expect(describeSessionTest({ ...base, result: "network_error" }).tone).toBe("warn");
    expect(describeSessionTest({ ...base, result: "invalid" }).tone).toBe("err");
    expect(describeSessionTest({ ...base, result: "expired" }).tone).toBe("err");
    expect(describeSessionTest({ ...base, result: "checkpoint_required" }).tone).toBe("err");
    expect(describeSessionTest({ ...base, result: "instagram_error" }).tone).toBe("err");
  });
  it("rótulos pt-BR", () => {
    expect(describeSessionTest({ ...base, result: "checkpoint_required" }).label).toBe(
      "Verificação de segurança necessária",
    );
    expect(describeSessionTest({ ...base, result: "checkpoint_required" }).hint).toMatch(/navegador/);
    expect(describeSessionTest({ ...base, result: "rate_limited" }).label).toBe("Limite de requisições");
    expect(describeSessionTest({ ...base, result: "timeout" }).label).toBe("Tempo esgotado");
    expect(describeSessionTest({ ...base, result: "session_missing" }).label).toBe("Sessão não configurada");
  });
  it("restricted", () => {
    const v = describeSessionTest({ ...base, result: "restricted", code: "action_blocked" });
    expect(v.label).toBe("Aviso de restrição do Instagram");
    expect(v.tone).toBe("err");
    expect(v.hint).toMatch(/1 hora/);
    expect(v.hint).toMatch(/Configurações/);
    expect(sessionResult({ ...base, code: "action_blocked" })).toBe("restricted");
  });
  it("api_refused: sessão reconhecida, listagem recusada", () => {
    const v = describeSessionTest({
      ...base,
      result: "api_refused",
      code: "access_refused",
      session_recognized: true,
      listing_api: "refused",
    });
    expect(v.label).toBe("Sessão reconhecida; listagem recusada");
    expect(v.tone).toBe("warn");
    expect(v.session).toBe("Sessão: reconhecida");
    expect(v.listing).toBe("Listagem: recusada");
    expect(sessionResult({ ...base, code: "access_refused" })).toBe("api_refused");
  });
  it("linhas de sessão e listagem", () => {
    expect(describeSessionTest({ ...base, session_recognized: false }).session).toBe("Sessão: não reconhecida");
    expect(describeSessionTest({ ...base, session_recognized: null }).session).toBe("Sessão: desconhecida");
    expect(describeSessionTest(base).session).toBe("Sessão: desconhecida");
    expect(describeSessionTest({ ...base, listing_api: "ok" }).listing).toBe("Listagem: funcionando");
    expect(describeSessionTest({ ...base, listing_api: "rate_limited" }).listing).toBe("Listagem: limitada");
    expect(describeSessionTest({ ...base, listing_api: "unknown" }).listing).toBe("Listagem: desconhecida");
    expect(describeSessionTest(base).listing).toBe("Listagem: desconhecida");
  });
  it("sem result usa code", () => {
    expect(sessionResult({ ...base, code: "session_expired" })).toBe("expired");
    expect(sessionResult({ ...base, code: "checkpoint_required" })).toBe("checkpoint_required");
    expect(sessionResult({ ...base, ok: true, code: "ok" })).toBe("valid");
    expect(sessionResult({ ...base, code: "algo_novo" })).toBe("instagram_error");
  });
});

describe("formatTestDuration", () => {
  it("formata", () => {
    expect(formatTestDuration(2300)).toBe("em 2,3 s");
    expect(formatTestDuration(850)).toBe("em 850 ms");
    expect(formatTestDuration(null)).toBeNull();
  });
});
