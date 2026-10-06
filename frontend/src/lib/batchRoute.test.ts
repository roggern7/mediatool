import { describe, expect, it } from "vitest";
import { batchIdFromSearch, queueForBatch } from "./batchRoute";

describe("filtro da Fila por lote", () => {
  it("monta o endereço da Fila do lote", () => {
    expect(queueForBatch(42)).toBe("/fila?lote=42");
    expect(batchIdFromSearch(queueForBatch(42).slice("/fila".length))).toBe(42);
  });

  it("aceita só ids inteiros positivos", () => {
    expect(batchIdFromSearch("?lote=7")).toBe(7);
    expect(batchIdFromSearch("")).toBeNull();
    expect(batchIdFromSearch("?lote=")).toBeNull();
    expect(batchIdFromSearch("?lote=0")).toBeNull();
    expect(batchIdFromSearch("?lote=-3")).toBeNull();
    expect(batchIdFromSearch("?lote=1.5")).toBeNull();
    expect(batchIdFromSearch("?lote=abc")).toBeNull();
    expect(batchIdFromSearch("?lote=99999999999999999999")).toBeNull();
  });
});
