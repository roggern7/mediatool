import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

describe("cliente: keyset, resumo do lote e processar por filtro", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubGlobal("window", globalThis);
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("listJobs manda batch_id e before_id", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [], total: 0, counts: {}, next_before_id: null }));
    const page = await api.listJobs({ batch_id: 5, limit: 100, before_id: 77 });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/jobs?batch_id=5&limit=100&before_id=77");
    expect(page.next_before_id).toBeNull();
  });

  it("listMedia manda before_id e aceita resposta antiga sem next_before_id", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [], total: 3 }));
    const page = await api.listMedia({ view: "all", limit: 50, before_id: 10 });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/media?view=all&limit=50&before_id=10");
    expect(page.next_before_id).toBeUndefined();
  });

  it("getBatch pede só o resumo por padrão", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 9 }));
    await api.getBatch(9);
    await api.getBatch(9, true);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/batches/9");
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/batches/9?include_jobs=true");
  });

  it("createImport manda process e preset_id explícitos no download puro", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 3 }, 202));
    await api.createImport({ profile: "perfil", content_type: "all", limit: 10, preset_id: null, process: false });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/imports");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      profile: "perfil",
      content_type: "all",
      limit: 10,
      preset_id: null,
      process: false,
    });
  });

  it("createImport manda limit null para Todos e o content_type novo", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 3 }, 202));
    await api.createImport({
      profile: "perfil",
      content_type: "photos_carousels",
      limit: null,
      preset_id: null,
      process: false,
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/imports");
    expect(String(init.body)).toContain('"limit":null');
    expect(JSON.parse(String(init.body))).toEqual({
      profile: "perfil",
      content_type: "photos_carousels",
      limit: null,
      preset_id: null,
      process: false,
    });
  });

  it("previewProfiles manda as entradas (já aparadas) para a pré-validação", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ valid: ["a"], invalid: [], duplicates: 1 }));
    const preview = await api.previewProfiles(["a", "@A"]);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/import-groups/preview");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["X-Media-Tool-Client"]).toBe("panel");
    expect(JSON.parse(String(init.body))).toEqual({ entries: ["a", "@A"] });
    expect(preview).toEqual({ valid: ["a"], invalid: [], duplicates: 1 });
  });

  it("createImportGroup manda o corpo exato do contrato", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ group: { id: 7 }, children: [], next_after_position: null }, 202));
    await api.createImportGroup({
      profiles: ["usuario_um", "usuario.tres"],
      content_type: "reels",
      limit: 1000,
      preset_id: null,
      process: false,
    });
    await api.createImportGroup({ profiles: ["a", "b"], content_type: "all", limit: null, preset_id: 4, process: true });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/import-groups");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      profiles: ["usuario_um", "usuario.tres"],
      content_type: "reels",
      limit: 1000,
      preset_id: null,
      process: false,
    });
    const second = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(String(second[1].body))).toEqual({
      profiles: ["a", "b"],
      content_type: "all",
      limit: null,
      preset_id: 4,
      process: true,
    });
  });

  it("grupos: lista, detalhe paginado por after_position e cancelamento", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ items: [], total: 0, next_before_id: null })));
    await api.listImportGroups(8);
    await api.listImportGroups(20, 3);
    await api.getImportGroup(7);
    await api.getImportGroup(7, { childrenLimit: 100, afterPosition: null });
    await api.getImportGroup(7, { childrenLimit: 100, afterPosition: 100 });
    await api.cancelImportGroup(7);
    const urls = fetchMock.mock.calls.map((c) => c[0] as string);
    expect(urls).toEqual([
      "/api/import-groups?limit=8",
      "/api/import-groups?limit=20&before_id=3",
      "/api/import-groups/7",
      "/api/import-groups/7?children_limit=100",
      "/api/import-groups/7?children_limit=100&after_position=100",
      "/api/import-groups/7/cancel",
    ]);
    const cancel = fetchMock.mock.calls[5]?.[1] as RequestInit;
    expect(cancel.method).toBe("POST");
    expect(cancel.body).toBeUndefined();
  });

  it("listBatches: ungrouped e group_id só quando pedidos", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ items: [], total: 0 })));
    await api.listBatches(8, 0, "instagram_import");
    await api.listBatches(8, 0, "instagram_import", { ungrouped: true });
    await api.listBatches(100, 200, undefined, { groupId: 7 });
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
      "/api/batches?limit=8&offset=0&source=instagram_import",
      "/api/batches?limit=8&offset=0&source=instagram_import&ungrouped=true",
      "/api/batches?limit=100&offset=200&group_id=7",
    ]);
  });

  it("erro com campos extras (skipped, invalid) chega em ApiError.payload", async () => {
    const body = {
      detail: "Todos os perfis da lista já têm uma importação em andamento.",
      code: "profile_already_importing",
      skipped: [{ profile: "a", reason: "profile_already_importing" }],
    };
    fetchMock.mockResolvedValue(jsonResponse(body, 409));
    const err = await api
      .createImportGroup({ profiles: ["a", "b"], content_type: "all", limit: 10, preset_id: null, process: false })
      .then(
        () => null,
        (e: unknown) => e,
      );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("profile_already_importing");
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).payload).toEqual(body);
  });

  it("processByFilter envia o filtro", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 1 }, 202));
    await api.processByFilter({ view: "originals", media_type: "image", preset_id: 2 });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/media/process-by-filter");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ view: "originals", media_type: "image", preset_id: 2 });
  });
});
