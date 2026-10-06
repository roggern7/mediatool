import { describe, expect, it } from "vitest";
import { MAX_BULK_PROCESS, canOfferSelectAll, processByFilterBody, withinBulkCap } from "./filterSelection";

describe("seleção por filtro", () => {
  it("corpo usa o mesmo filtro da listagem", () => {
    expect(processByFilterBody({ view: "all", typeFilter: "all", q: "" }, 3)).toEqual({ view: "all", preset_id: 3 });
    expect(processByFilterBody({ view: "originals", typeFilter: "video", q: "perfil" }, 4)).toEqual({
      view: "originals",
      media_type: "video",
      q: "perfil",
      preset_id: 4,
    });
  });

  it("oferece todos os resultados só com a página inteira marcada e mais itens fora dela", () => {
    expect(canOfferSelectAll(50, 50, 1_200)).toBe(true);
    expect(canOfferSelectAll(50, 49, 1_200)).toBe(false);
    expect(canOfferSelectAll(50, 50, 50)).toBe(false);
    expect(canOfferSelectAll(0, 0, 10)).toBe(false);
  });

  it("respeita o teto de 10.000", () => {
    expect(MAX_BULK_PROCESS).toBe(10_000);
    expect(withinBulkCap(10_000)).toBe(true);
    expect(withinBulkCap(10_001)).toBe(false);
    expect(withinBulkCap(0)).toBe(false);
  });
});
