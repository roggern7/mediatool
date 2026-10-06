import { describe, expect, it } from "vitest";
import { batchLabel, batchTitle, batchToneOf, isDownloadOnly, jobKindLabel, showDuplicate } from "./labels";

const base = { source: "instagram_import" as const, source_profile: "perfil", progress: { failed: 0 } };

describe("rótulos de lote", () => {
  it("concluído com erros usa aviso", () => {
    const b = { ...base, status: "completed" as const, progress: { failed: 2 } };
    expect(batchLabel(b)).toBe("Concluído com erros");
    expect(batchToneOf(b)).toBe("warn");
  });
  it("concluído sem erros usa ok", () => {
    const b = { ...base, status: "completed" as const };
    expect(batchLabel(b)).toBe("Concluído");
    expect(batchToneOf(b)).toBe("ok");
  });
  it("título muda quando termina", () => {
    expect(batchTitle({ ...base, status: "running" })).toBe("Importando @perfil");
    expect(batchTitle({ ...base, status: "listing" })).toBe("Importando @perfil");
    expect(batchTitle({ ...base, status: "cancelled" })).toBe("Importação de @perfil");
  });
});

describe("lote somente de download", () => {
  it("só importação com process false", () => {
    expect(isDownloadOnly({ source: "instagram_import", process: false })).toBe(true);
    expect(isDownloadOnly({ source: "instagram_import", process: true })).toBe(false);
    expect(isDownloadOnly({ source: "library", process: false })).toBe(false);
    expect(isDownloadOnly({ source: "instagram_import" })).toBe(false);
  });
});

describe("rótulos de job", () => {
  it("tipo", () => {
    expect(jobKindLabel("process")).toBe("Processamento");
    expect(jobKindLabel("ingest")).toBe("Download");
  });
  it("duplicado só em download", () => {
    expect(showDuplicate({ kind: "ingest", is_duplicate: true })).toBe(true);
    expect(showDuplicate({ kind: "process", is_duplicate: true })).toBe(false);
    expect(showDuplicate({ kind: "ingest", is_duplicate: false })).toBe(false);
  });
  it("lote ativo parado mostra atenção ou pausa", () => {
    const b = { ...base, status: "listing" as const, listing_state: "needs_attention" };
    expect(batchLabel(b)).toBe("Precisa de atenção");
    expect(batchToneOf(b)).toBe("warn");
    const p = { ...base, status: "running" as const, paused_reason: "content_unavailable" };
    expect(batchLabel(p)).toBe("Pausado");
    expect(batchToneOf({ ...base, status: "cancelled" as const, paused_reason: "x" })).toBe("muted");
  });
  it("espera pelo limite de requisições tem rótulo próprio e o tom normal da coleta", () => {
    const w = { ...base, status: "listing" as const, listing_state: "waiting_rate_limit" };
    expect(batchLabel(w)).toBe("Aguardando limite de requisições");
    expect(batchToneOf(w)).toBe(batchToneOf({ ...base, status: "listing" as const, listing_state: "running" }));
    expect(batchToneOf(w)).toBe("accent");
    expect(batchLabel({ ...w, listing_state: "running" })).toBe("Coletando lista");
    expect(batchLabel({ ...w, status: "cancelled" as const })).toBe("Cancelado");
    expect(batchLabel({ ...w, paused_reason: "content_unavailable" })).toBe("Pausado");
  });
});
