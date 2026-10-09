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

  it("importação por link: preview, criação e links do lote", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ items: [], total: 0 })));
    await api.previewLinks(["https://www.instagram.com/p/AAA/?igsh=1", "x"]);
    await api.createLinksImport({ links: ["https://www.instagram.com/p/AAA/"], preset_id: null, process: false });
    await api.listBatchLinks(9);
    await api.listBatchLinks(9, 200, 400);
    const calls = fetchMock.mock.calls as [string, RequestInit][];
    expect(calls.map((c) => c[0])).toEqual([
      "/api/import-links/preview",
      "/api/import-links",
      "/api/batches/9/links?limit=200&offset=0",
      "/api/batches/9/links?limit=200&offset=400",
    ]);
    expect(calls[0]?.[1].method).toBe("POST");
    expect((calls[0]?.[1].headers as Record<string, string>)["X-Media-Tool-Client"]).toBe("panel");
    expect(JSON.parse(String(calls[0]?.[1].body))).toEqual({
      entries: ["https://www.instagram.com/p/AAA/?igsh=1", "x"],
    });
    expect(calls[1]?.[1].method).toBe("POST");
    expect(JSON.parse(String(calls[1]?.[1].body))).toEqual({
      links: ["https://www.instagram.com/p/AAA/"],
      preset_id: null,
      process: false,
    });
    expect(calls[2]?.[1].method).toBe("GET");
  });

  it("invalid_links chega com a mensagem em pt-BR e o corpo em ApiError.payload", async () => {
    const body = {
      detail: "invalid",
      code: "invalid_links",
      invalid: [{ entry: "x", reason: "Link inválido." }],
      valid: [],
      duplicates: 0,
    };
    fetchMock.mockResolvedValue(jsonResponse(body, 422));
    const err = await api.createLinksImport({ links: ["x"], preset_id: null, process: false }).then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("invalid_links");
    expect((err as ApiError).message).toContain("links inválidos");
    expect((err as ApiError).payload).toEqual(body);
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

describe("cliente: exportar para pasta", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubGlobal("window", globalThis);
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  function call(index = 0): { url: string; init: RequestInit; headers: Record<string, string> } {
    const [url, init] = fetchMock.mock.calls[index] as [string, RequestInit];
    return { url, init, headers: init.headers as Record<string, string> };
  }

  it("pickExportFolder pede o seletor nativo ao backend sem mandar nenhum caminho", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ cancelled: false, token: "tok", path_display: "C:/Saida" }));
    const picked = await api.pickExportFolder();
    const { url, init, headers } = call();
    expect(url).toBe("/api/exports/pick-folder");
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
    expect(headers["X-Media-Tool-Client"]).toBe("panel");
    expect(picked).toEqual({ cancelled: false, token: "tok", path_display: "C:/Saida" });
  });

  it("pickExportFolder devolve o cancelamento do diálogo sem erro", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ cancelled: true, token: null, path_display: null }));
    await expect(api.pickExportFolder()).resolves.toEqual({ cancelled: true, token: null, path_display: null });
  });

  it("createExport manda a fonte e só o token do destino", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 5, status: "pending" }, 202));
    await api.createExport({ source: { kind: "batch", id: 41 }, destination: { token: "tok" } });
    await api.createExport({ source: { kind: "group", id: 7 }, destination: { default: true } });
    await api.createExport({
      source: { kind: "library", selection: { filter: { view: "all", q: "perfil" }, exclude_ids: [3] } },
      destination: { token: "tok2" },
    });
    await api.createExport({
      source: { kind: "library", selection: { media_ids: [1, 2] } },
      destination: { token: "tok3" },
    });
    expect(call(0).url).toBe("/api/exports");
    expect(call(0).init.method).toBe("POST");
    expect(call(0).headers["X-Media-Tool-Client"]).toBe("panel");
    expect(JSON.parse(String(call(0).init.body))).toEqual({
      source: { kind: "batch", id: 41 },
      destination: { token: "tok" },
    });
    expect(JSON.parse(String(call(1).init.body))).toEqual({
      source: { kind: "group", id: 7 },
      destination: { default: true },
    });
    expect(JSON.parse(String(call(2).init.body))).toEqual({
      source: { kind: "library", selection: { filter: { view: "all", q: "perfil" }, exclude_ids: [3] } },
      destination: { token: "tok2" },
    });
    expect(JSON.parse(String(call(3).init.body))).toEqual({
      source: { kind: "library", selection: { media_ids: [1, 2] } },
      destination: { token: "tok3" },
    });
    for (let i = 0; i < 4; i += 1) expect(String(call(i).init.body)).not.toMatch(/path/);
  });

  it("progresso, lista, cancelamento e abrir pasta usam as rotas do contrato", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({ id: 5, items: [], ok: true })));
    await api.getExport(5);
    await api.listExports({ batchId: 41, limit: 1 });
    await api.listExports({ groupId: 7 });
    await api.cancelExport(5);
    await api.openExportFolder(5);
    await api.openLibraryFolder();
    expect(call(0).url).toBe("/api/exports/5");
    expect(call(0).init.method).toBe("GET");
    expect(call(1).url).toBe("/api/exports?batch_id=41&limit=1");
    expect(call(2).url).toBe("/api/exports?group_id=7");
    expect(call(3).url).toBe("/api/exports/5/cancel");
    expect(call(4).url).toBe("/api/exports/5/open");
    expect(call(5).url).toBe("/api/library/open-folder");
    for (const index of [3, 4, 5]) {
      expect(call(index).init.method).toBe("POST");
      expect(call(index).init.body).toBeUndefined(); // nenhuma rota de abrir/cancelar leva caminho
      expect(call(index).headers["X-Media-Tool-Client"]).toBe("panel");
    }
  });

  it("erro do seletor ocupado chega com a mensagem em português", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "x", code: "picker_busy" }, 409));
    const err = await api.pickExportFolder().then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("picker_busy");
    expect((err as ApiError).message).toContain("janela de escolha de pasta");
  });
});

describe("cliente: baixar ZIP", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubGlobal("window", globalThis);
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("createExport pede o ZIP sem token de pasta e sem caminho", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 9, mode: "zip", status: "pending" }, 202));
    const job = await api.createExport({ source: { kind: "batch", id: 41 }, destination: { zip: true } });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/exports");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ source: { kind: "batch", id: 41 }, destination: { zip: true } });
    expect(job.mode).toBe("zip");
  });

  it("sem espaço: erro 507 com a mensagem do backend", async () => {
    const detail = "Sem espaço em disco para preparar o ZIP: são necessários 18,6 GB e há 2,0 GB disponíveis.";
    fetchMock.mockResolvedValue(jsonResponse({ detail, code: "export_no_space" }, 507));
    const err = await api
      .createExport({ source: { kind: "group", id: 7 }, destination: { zip: true } })
      .then(
        () => null,
        (e: unknown) => e,
      );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("export_no_space");
    expect((err as ApiError).message).toBe(detail);
  });
});

describe("cliente: retomada de exportações", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubGlobal("window", globalThis);
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("listExports filtra pelas exportações da Biblioteca", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [] }));
    await api.listExports({ kind: "library", limit: 5 });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/exports?kind=library&limit=5");
  });

  it("409 export_already_running expõe o export_id no payload do erro", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "x", code: "export_already_running", export_id: 31 }, 409));
    const err = await api
      .createExport({ source: { kind: "batch", id: 41 }, destination: { zip: true } })
      .then(
        () => null,
        (e: unknown) => e,
      );
    expect((err as ApiError).code).toBe("export_already_running");
    expect((err as ApiError).payload?.export_id).toBe(31);
  });
});
