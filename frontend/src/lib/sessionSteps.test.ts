import { describe, expect, it } from "vitest";
import type { SessionTest } from "../api/types";
import { STEP_STATE_LABEL, sessionSteps, stepState } from "./sessionSteps";

const base: SessionTest = { ok: false, code: "x", message: "", username: null, tested_at: "2026-10-03T12:00:00Z" };

describe("sessionSteps", () => {
  it("normaliza estados", () => {
    expect(stepState(true)).toBe("ok");
    expect(stepState("ok")).toBe("ok");
    expect(stepState(false)).toBe("failed");
    expect(stepState("failed")).toBe("failed");
    expect(stepState("refused")).toBe("failed");
    expect(stepState("rate_limited")).toBe("failed");
    expect(stepState("unknown")).toBe("untested");
    expect(stepState(null)).toBe("untested");
    expect(stepState(undefined)).toBe("untested");
  });
  it("lista etapas sem jargão", () => {
    const steps = sessionSteps({
      ...base,
      session_present: true,
      session_recognized: true,
      bootstrap: "ok",
      graphql: "failed",
      listing_api: "unknown",
    });
    expect(steps.map((s) => `${s.label}:${s.state}`)).toEqual([
      "Sessão salva:ok",
      "Reconhecida pelo Instagram:ok",
      "Acesso web preparado:ok",
      "Consulta de publicações:failed",
      "Listagem de perfil:untested",
    ]);
  });
  it("etapa de reconhecimento conforme a conta", () => {
    const web: SessionTest = {
      ...base,
      ok: true,
      result: "valid",
      code: "ok",
      session_present: true,
      bootstrap: "ok",
      graphql: "ok",
      listing_api: null,
    };
    const recognized = (t: SessionTest) => sessionSteps(t).find((s) => s.label === "Reconhecida pelo Instagram")?.state;
    expect(recognized({ ...web, session_recognized: true })).toBe("ok");
    expect(recognized({ ...web, session_recognized: false })).toBe("other_account");
    expect(recognized({ ...web, session_recognized: null })).toBe("unconfirmed");
    expect(recognized({ ...web, ok: false, result: "invalid", code: "session_invalid", session_recognized: false })).toBe(
      "failed",
    );
    expect(recognized({ ...web, ok: false, result: "expired", code: "session_expired", session_recognized: false })).toBe(
      "failed",
    );
    expect(recognized({ ...web, session_recognized: null, graphql: null })).toBe("untested");
    expect(STEP_STATE_LABEL.other_account).toBe("outra conta");
    expect(STEP_STATE_LABEL.unconfirmed).toBe("não confirmado");
  });
  it("compatível sem campos", () => {
    expect(sessionSteps(base)).toEqual([]);
  });
  it("registro antigo/gallery-dl com null nas etapas web não lista etapas", () => {
    expect(
      sessionSteps({
        ...base,
        session_present: null,
        bootstrap: null,
        graphql: null,
        session_recognized: true,
        listing_api: "ok",
      }),
    ).toEqual([]);
    expect(
      sessionSteps({
        ...base,
        session_present: null,
        bootstrap: null,
        graphql: null,
        session_recognized: null,
        listing_api: null,
      }),
    ).toEqual([]);
  });
  it("lista etapas quando ao menos uma etapa web vem preenchida", () => {
    const steps = sessionSteps({ ...base, session_present: true, bootstrap: null, graphql: null });
    expect(steps).toHaveLength(5);
    expect(steps[0]).toEqual({ label: "Sessão salva", state: "ok" });
  });
});
