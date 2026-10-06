import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./client";

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

  it("processByFilter envia o filtro", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 1 }, 202));
    await api.processByFilter({ view: "originals", media_type: "image", preset_id: 2 });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/media/process-by-filter");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ view: "originals", media_type: "image", preset_id: 2 });
  });
});
