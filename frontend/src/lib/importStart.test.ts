import { describe, expect, it } from "vitest";
import type { ProfilesPreview } from "../api/types";
import { resolveLimit } from "./importOptions";
import {
  type StartState,
  WEB_ONLY_MESSAGE,
  canStartImport,
  entriesKey,
  interpretGroupCreated,
  interpretStartError,
  planFromPreview,
  providerBlocker,
  settledPreview,
  startBlocker,
  startSummary,
} from "./importStart";

const preview: ProfilesPreview = { valid: ["a", "b"], invalid: [], duplicates: 0 };
const one: ProfilesPreview = { valid: ["solo"], invalid: [{ entry: "x y", reason: "inválido" }], duplicates: 1 };
const none: ProfilesPreview = { valid: [], invalid: [{ entry: "x y", reason: "inválido" }], duplicates: 0 };

describe("settledPreview", () => {
  const key = entriesKey(["a", "b"]);
  const ok = { currentKey: key, requestedKey: key, typing: false, loading: false, result: { key, data: preview } };
  it("usa a resposta do backend só para exatamente a lista atual, com a conferência assentada", () => {
    expect(entriesKey(["a", "b"])).toBe("a\nb");
    expect(settledPreview(ok)).toBe(preview);
  });
  it("digitando, no debounce, carregando ou com resposta de outra lista: ainda conferindo", () => {
    expect(settledPreview({ ...ok, typing: true })).toBeNull();
    expect(settledPreview({ ...ok, loading: true })).toBeNull();
    expect(settledPreview({ ...ok, requestedKey: "a" })).toBeNull();
    expect(settledPreview({ ...ok, result: { key: "a", data: preview } })).toBeNull();
    expect(settledPreview({ ...ok, result: null })).toBeNull();
    expect(settledPreview({ ...ok, result: undefined })).toBeNull();
  });
  it("lista vazia nunca tem preview (o campo limpo depois de iniciar não reabilita o botão)", () => {
    expect(settledPreview({ ...ok, currentKey: "", requestedKey: "", result: { key: "", data: preview } })).toBeNull();
  });
});

describe("pode iniciar?", () => {
  const base: StartState = {
    sessionOk: true,
    paused: false,
    submitting: false,
    preview,
    limit: resolveLimit("last", "100"),
    process: false,
    presetId: null,
    providerProblem: null,
  };
  it("tudo certo: pode", () => {
    expect(startBlocker(base)).toBeNull();
    expect(canStartImport(base)).toBe(true);
    expect(canStartImport({ ...base, limit: resolveLimit("all", "") })).toBe(true);
    expect(canStartImport({ ...base, preview: one })).toBe(true);
  });
  it("cada condição bloqueia sozinha", () => {
    expect(startBlocker({ ...base, submitting: true })).toBe("submitting");
    expect(startBlocker({ ...base, sessionOk: false })).toBe("no_session");
    expect(startBlocker({ ...base, paused: true })).toBe("paused");
    expect(startBlocker({ ...base, preview: null })).toBe("no_preview");
    expect(startBlocker({ ...base, preview: none })).toBe("no_valid");
    expect(startBlocker({ ...base, limit: resolveLimit("last", "") })).toBe("limit");
    expect(startBlocker({ ...base, limit: resolveLimit("last", "0") })).toBe("limit");
    expect(startBlocker({ ...base, process: true, presetId: null })).toBe("preset");
    expect(startBlocker({ ...base, providerProblem: WEB_ONLY_MESSAGE })).toBe("provider");
    expect(canStartImport({ ...base, preview: null })).toBe(false);
  });
  it("processamento com preset pode", () => {
    expect(canStartImport({ ...base, process: true, presetId: 3 })).toBe(true);
  });
});

describe("providerBlocker", () => {
  it("gallery-dl não aceita Todos nem Publicações do feed", () => {
    expect(providerBlocker("gallery_dl", "all", true)).toBe(WEB_ONLY_MESSAGE);
    expect(providerBlocker("gallery_dl", "photos_carousels", false)).toBe(WEB_ONLY_MESSAGE);
    expect(providerBlocker("gallery_dl", "reels", false)).toBeNull();
    expect(providerBlocker("gallery_dl", "all", false)).toBeNull();
  });
  it("auto, web ou sem a informação: nada muda", () => {
    for (const provider of ["auto", "web", undefined, null] as const) {
      expect(providerBlocker(provider, "photos_carousels", true)).toBeNull();
    }
  });
});

describe("startSummary", () => {
  it("resumo e avisos com o preview assentado", () => {
    expect(startSummary({ preview, limit: resolveLimit("all", ""), content: "reels" })).toEqual({
      kind: "summary",
      text: "2 perfis • Todos os Reels • Todas as mídias disponíveis",
      hints: [
        "Todos: percorre o perfil até o fim. Em perfis grandes pode levar horas por causa do limite de requisições " +
          "por hora; o lote continua sozinho.",
        "Os perfis são listados um de cada vez.",
      ],
    });
  });
  it("quantidade inválida ou vazia: a validação entra no lugar do resumo, nunca 'Todas as mídias'", () => {
    for (const text of ["", "0", "abc"]) {
      const s = startSummary({ preview, limit: resolveLimit("last", text), content: "all" });
      expect(s).toEqual({
        kind: "problem",
        text: "Quantidade por perfil: Informe um número inteiro de 1 a 1.000.000.",
      });
    }
  });
  it("sem preview ou sem perfil válido não há resumo", () => {
    expect(startSummary({ preview: null, limit: resolveLimit("last", "10"), content: "all" })).toBeNull();
    expect(startSummary({ preview: none, limit: resolveLimit("last", "10"), content: "all" })).toBeNull();
  });
});

describe("planFromPreview", () => {
  const options = { content: "reels" as const, limit: resolveLimit("last", "1.000"), processAfterDownload: false, presetId: 9 };
  it("1 perfil válido: POST /api/imports com o username do preview", () => {
    expect(planFromPreview(one, options)).toEqual({
      kind: "single",
      body: { profile: "solo", content_type: "reels", limit: 1000, preset_id: null, process: false },
    });
  });
  it("2 ou mais: POST /api/import-groups com preview.valid, na ordem", () => {
    expect(planFromPreview(preview, { ...options, limit: resolveLimit("all", ""), processAfterDownload: true })).toEqual({
      kind: "group",
      body: { profiles: ["a", "b"], content_type: "reels", limit: null, preset_id: 9, process: true },
    });
  });
  it("sem preview, sem válidos ou com quantidade inválida não monta pedido", () => {
    expect(planFromPreview(null, options)).toBeNull();
    expect(planFromPreview(none, options)).toBeNull();
    expect(planFromPreview(preview, { ...options, limit: resolveLimit("last", "") })).toBeNull();
  });
});

describe("resposta e erro da criação", () => {
  const reason = "profile_already_importing";
  const group = { id: 7, profiles: { total: 2 } };
  it("criado sem pulados", () => {
    expect(interpretGroupCreated({ group, skipped: [] })).toEqual({
      groupId: 7,
      message: "Importação de 2 perfis iniciada.",
      notice: null,
    });
    expect(interpretGroupCreated({ group }).notice).toBeNull();
  });
  it("skipped parcial vira aviso, não erro", () => {
    expect(interpretGroupCreated({ group, skipped: [{ profile: "usuario_dois", reason }] }).notice).toBe(
      "1 perfil foi pulado porque já tem uma importação em andamento: @usuario_dois.",
    );
  });
  it("409 com todos pulados vira aviso na tela, sem toast de erro", () => {
    const r = interpretStartError({
      status: 409,
      code: reason,
      message: "Este perfil já está sendo importado.",
      payload: { code: reason, detail: "x", skipped: [{ profile: "a", reason }, { profile: "b", reason }] },
    });
    expect(r).toEqual({
      notice:
        "Nenhuma importação nova foi criada. 2 perfis foram pulados porque já têm uma importação em andamento: @a, @b.",
      error: null,
      refreshPreview: false,
      reloadRecent: true,
    });
  });
  it("409 de um perfil só (sem lista) continua erro e atualiza os recentes", () => {
    const r = interpretStartError({ status: 409, code: reason, message: "Este perfil já está sendo importado.", payload: { code: reason } });
    expect(r).toEqual({
      notice: null,
      error: "Este perfil já está sendo importado.",
      refreshPreview: false,
      reloadRecent: true,
    });
  });
  it("invalid_profiles refaz o preview", () => {
    const r = interpretStartError({ status: 422, code: "invalid_profiles", message: "Há entradas inválidas.", payload: {} });
    expect(r.refreshPreview).toBe(true);
    expect(r.error).toBe("Há entradas inválidas.");
    expect(r.notice).toBeNull();
  });
  it("413 orienta a dividir a lista", () => {
    const r = interpretStartError({ status: 413, code: "http_413", message: "Arquivo grande demais.", payload: null });
    expect(r.error).toBe(
      "A lista é grande demais para enviar de uma vez. Divida em partes menores ou cole só os nomes de usuário, sem a URL completa.",
    );
    expect(r.refreshPreview).toBe(false);
  });
  it("outros erros e erro desconhecido", () => {
    expect(interpretStartError({ status: 409, code: "session_missing", message: "Sessão do Instagram não configurada." }).error).toBe(
      "Sessão do Instagram não configurada.",
    );
    expect(interpretStartError(null)).toEqual({
      notice: null,
      error: "Erro inesperado.",
      refreshPreview: false,
      reloadRecent: false,
    });
  });
});
