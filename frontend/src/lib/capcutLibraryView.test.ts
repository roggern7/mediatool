import { describe, expect, it, vi } from "vitest";
import type { CapCutLibraryItem, CapCutLibraryQuery, MediaDeleteRequest } from "../api/types";
import { EMPTY_SELECTION, selectAllFiltered } from "./capcutBatch";
import {
  CLEAR_CONFIRM_TEXT,
  DELETE_CHUNK,
  type LibraryViewApi,
  chunk,
  clearLibrary,
  clearSummary,
  clearTone,
  collectVisible,
  viewHasHidden,
} from "./capcutLibraryView";

/** Biblioteca falsa: ids DESC, keyset por before_id, `open` nos ids pedidos. */
function fakeApi(ids: number[], open: number[] = [], pageSize?: number) {
  const queries: CapCutLibraryQuery[] = [];
  const deletes: number[][] = [];
  const clears: (number[] | undefined)[] = [];
  const order: string[] = [];
  const sorted = [...ids].sort((a, b) => b - a);
  const deps: LibraryViewApi = {
    capcutLibrary: vi.fn((q: CapCutLibraryQuery) => {
      queries.push(q);
      order.push("list");
      const limit = pageSize ?? q.limit ?? 50;
      const rest = sorted.filter((id) => q.before_id === undefined || id < q.before_id);
      const page = rest.slice(0, limit);
      const items = page.map((id) => ({ id, capcut: { done: 0, last_output_name: null, open: open.includes(id) } }));
      return Promise.resolve({
        items: items as unknown as CapCutLibraryItem[],
        total: sorted.length,
        next_before_id: rest.length > limit ? (page[page.length - 1] ?? null) : null,
      });
    }),
    clearCapcutLibrary: vi.fn((mediaIds?: number[]) => {
      clears.push(mediaIds);
      order.push("clear");
      return Promise.resolve({ cleared: mediaIds === undefined, hidden_count: mediaIds?.length ?? 0 });
    }),
    deleteMediaBulk: vi.fn((body: MediaDeleteRequest) => {
      const part = "media_ids" in body ? body.media_ids : [];
      deletes.push(part);
      order.push("delete");
      return Promise.resolve({ deleted: part.length, deleted_ids: part, skipped: [], failed: [] });
    }),
  };
  return { deps, queries, deletes, clears, order };
}

describe("Edição CapCut: limpar a lista do seletor", () => {
  it("texto da confirmação e visão com algo escondido", () => {
    expect(CLEAR_CONFIRM_TEXT).toBe(
      "Os vídeos atuais saem desta lista. Nada é apagado; novos downloads e envios continuam aparecendo.",
    );
    expect(viewHasHidden(undefined)).toBe(false);
    expect(viewHasHidden({ cleared: false, hidden_count: 0 })).toBe(false);
    expect(viewHasHidden({ cleared: true, hidden_count: 0 })).toBe(true);
    expect(viewHasHidden({ cleared: false, hidden_count: 2 })).toBe(true);
  });

  it("blocos respeitam o tamanho e a ordem", () => {
    expect(chunk([1, 2, 3, 4, 5], 2)).toEqual([[1, 2], [3, 4], [5]]);
    expect(chunk([], 500)).toEqual([]);
  });

  it("collectVisible pagina pelo keyset até o fim, com o filtro", async () => {
    const { deps, queries } = fakeApi([1, 2, 3, 4, 5], [4], 2);
    const all = await collectVisible(deps, { q: "gato" });
    expect(all).toEqual([
      { id: 5, open: false },
      { id: 4, open: true },
      { id: 3, open: false },
      { id: 2, open: false },
      { id: 1, open: false },
    ]);
    expect(queries.map((q) => q.before_id)).toEqual([undefined, 4, 2]);
    expect(queries.every((q) => q.q === "gato" && q.limit === 200)).toBe(true);
  });

  it("limpar tudo sem apagar: só a rota de limpar, sem ler a lista", async () => {
    const { deps, order, clears } = fakeApi([1, 2]);
    const outcome = await clearLibrary(deps, { kind: "all" }, false);
    expect(order).toEqual(["clear"]);
    expect(clears).toEqual([undefined]);
    expect(clearSummary(outcome)).toBe("A lista foi limpa.");
    expect(clearTone(outcome)).toBe("ok");
  });

  it("limpar tudo e apagar: lê os visíveis ANTES, limpa e apaga em blocos de 500, pulando job aberto", async () => {
    const ids = Array.from({ length: 1203 }, (_, i) => i + 1);
    const { deps, order, deletes } = fakeApi(ids, [7, 1000]);
    const outcome = await clearLibrary(deps, { kind: "all" }, true);
    expect(order.indexOf("clear")).toBeGreaterThan(order.lastIndexOf("list"));
    expect(order.indexOf("delete")).toBeGreaterThan(order.indexOf("clear"));
    expect(deletes.map((d) => d.length)).toEqual([DELETE_CHUNK, DELETE_CHUNK, 201]);
    const flat = deletes.flat();
    expect(flat).not.toContain(7);
    expect(flat).not.toContain(1000);
    expect(new Set(flat).size).toBe(1201);
    expect(outcome).toMatchObject({ removed: 1203, deleted: 1201, skippedOpen: 2, skipped: 0, failed: 0 });
    expect(clearSummary(outcome)).toBe(
      "1.203 vídeos saíram da lista. 1.201 arquivos apagados da biblioteca. 2 vídeos com job do CapCut aberto não foram apagados.",
    );
    expect(clearTone(outcome)).toBe("warn");
  });

  it("tirar selecionados sem apagar: manda os ids, sem ler a lista", async () => {
    const { deps, order, clears } = fakeApi([1, 2, 3]);
    const outcome = await clearLibrary(deps, { kind: "selection", selection: { ids: new Set([3, 1]), all: null } }, false);
    expect(order).toEqual(["clear"]);
    expect(clears).toEqual([[1, 3]]);
    expect(clearSummary(outcome)).toBe("2 vídeos saíram da lista.");
  });

  it("tirar selecionados e apagar: pula os com job aberto e soma o resultado do servidor", async () => {
    const { deps, deletes } = fakeApi([1, 2, 3], [2]);
    deps.deleteMediaBulk = vi.fn((body: MediaDeleteRequest) => {
      const part = "media_ids" in body ? body.media_ids : [];
      deletes.push(part);
      return Promise.resolve({
        deleted: 1,
        deleted_ids: [1],
        skipped: [{ id: 3, code: "media_busy", message: "ocupado" }],
        failed: [],
      });
    });
    const outcome = await clearLibrary(deps, { kind: "selection", selection: { ids: new Set([1, 2, 3]), all: null } }, true);
    expect(deletes).toEqual([[1, 3]]);
    expect(outcome).toMatchObject({ removed: 3, deleted: 1, skippedOpen: 1, skipped: 1, failed: 0 });
    expect(clearSummary(outcome)).toBe(
      "3 vídeos saíram da lista. 1 arquivo apagado da biblioteca. 1 vídeo com job do CapCut aberto não foi apagado. " +
        "1 vídeo foi pulado (em uso, numa exportação ou já apagado).",
    );
  });

  it("todos os filtrados: resolve os ids pelo filtro antes de tirar da lista", async () => {
    const { deps, clears, queries } = fakeApi([4, 5, 6]);
    const selection = selectAllFiltered({ profile: "gato" }, 3);
    await clearLibrary(deps, { kind: "selection", selection }, false);
    expect(queries[0]?.profile).toBe("gato");
    expect(clears).toEqual([[6, 5, 4]]);
  });

  it("seleção vazia é erro e nada muda", async () => {
    const { deps, order } = fakeApi([1]);
    await expect(clearLibrary(deps, { kind: "selection", selection: EMPTY_SELECTION }, false)).rejects.toThrow();
    expect(order).toEqual([]);
  });

  it("falha ao ler a lista antes de limpar: nada muda", async () => {
    const { deps, order } = fakeApi([1]);
    deps.capcutLibrary = vi.fn(() => Promise.reject(new Error("fora do ar")));
    await expect(clearLibrary(deps, { kind: "all" }, true)).rejects.toThrow("fora do ar");
    expect(order).toEqual([]);
  });

  it("falha no meio da exclusão: a lista já foi limpa e o resumo avisa", async () => {
    const ids = Array.from({ length: 600 }, (_, i) => i + 1);
    const { deps } = fakeApi(ids);
    let calls = 0;
    deps.deleteMediaBulk = vi.fn((body: MediaDeleteRequest) => {
      calls += 1;
      if (calls === 2) return Promise.reject(new Error("Backend desconectado."));
      const part = "media_ids" in body ? body.media_ids : [];
      return Promise.resolve({ deleted: part.length, deleted_ids: part, skipped: [], failed: [] });
    });
    const outcome = await clearLibrary(deps, { kind: "all" }, true);
    expect(outcome.deleted).toBe(500);
    expect(outcome.deleteError).toBe("Backend desconectado.");
    expect(clearTone(outcome)).toBe("err");
    expect(clearSummary(outcome)).toContain("A exclusão parou no meio: Backend desconectado.");
  });
});
