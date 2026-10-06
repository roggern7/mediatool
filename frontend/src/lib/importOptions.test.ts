import { describe, expect, it } from "vitest";
import type { ContentType } from "../api/types";
import {
  CONTENT_CHOICES,
  LIMIT_SHORTCUTS,
  PROCESS_AFTER_DOWNLOAD_DEFAULT,
  buildImportGroupRequest,
  buildImportRequest,
  canSubmitImport,
  entriesSummary,
  importHints,
  importSummary,
  parseLimitText,
  planImport,
  resolveLimit,
  startLabel,
} from "./importOptions";

describe("buildImportRequest", () => {
  it("o padrão é download puro, com process e preset_id explícitos", () => {
    expect(PROCESS_AFTER_DOWNLOAD_DEFAULT).toBe(false);
    const body = buildImportRequest({ profile: "perfil", content: "all", limit: 20 });
    expect(body).toEqual({ profile: "perfil", content_type: "all", limit: 20, preset_id: null, process: false });
    expect(Object.keys(body)).toContain("process");
    expect(Object.keys(body)).toContain("preset_id");
  });
  it("download puro ignora o preset escolhido na tela", () => {
    const body = buildImportRequest({
      profile: "perfil",
      content: "reels",
      limit: 10,
      processAfterDownload: false,
      presetId: 7,
    });
    expect(body.process).toBe(false);
    expect(body.preset_id).toBeNull();
  });
  it("com processamento ligado manda process true e o preset", () => {
    const body = buildImportRequest({
      profile: "perfil",
      content: "photos_carousels",
      limit: 50,
      processAfterDownload: true,
      presetId: 7,
    });
    expect(body).toEqual({
      profile: "perfil",
      content_type: "photos_carousels",
      limit: 50,
      preset_id: 7,
      process: true,
    });
  });
  it("Todos manda limit null (não um número grande)", () => {
    const body = buildImportRequest({ profile: "perfil", content: "reels", limit: null });
    expect(body.limit).toBeNull();
    expect(JSON.parse(JSON.stringify(body))).toEqual({
      profile: "perfil",
      content_type: "reels",
      limit: null,
      preset_id: null,
      process: false,
    });
  });
  it("processamento ligado exige preset; download puro não", () => {
    expect(canSubmitImport(false, null)).toBe(true);
    expect(canSubmitImport(true, null)).toBe(false);
    expect(canSubmitImport(true, 3)).toBe(true);
  });
});

describe("buildImportGroupRequest e planImport", () => {
  it("grupo: perfis na ordem, limit por perfil e download puro explícito", () => {
    const profiles = ["a", "b", "c"];
    const body = buildImportGroupRequest({ profiles, content: "reels", limit: 1000 });
    expect(body).toEqual({ profiles: ["a", "b", "c"], content_type: "reels", limit: 1000, preset_id: null, process: false });
    expect(body.profiles).not.toBe(profiles);
  });
  it("grupo com Todos e processamento", () => {
    expect(
      buildImportGroupRequest({
        profiles: ["a", "b"],
        content: "photos_carousels",
        limit: null,
        processAfterDownload: true,
        presetId: 4,
      }),
    ).toEqual({ profiles: ["a", "b"], content_type: "photos_carousels", limit: null, preset_id: 4, process: true });
  });
  it("1 perfil vira lote avulso; 2 ou mais viram grupo; lista vazia não inicia", () => {
    const options = { content: "all" as const, limit: 100 };
    expect(planImport({ ...options, profiles: [] })).toBeNull();
    expect(planImport({ ...options, profiles: ["solo"] })).toEqual({
      kind: "single",
      body: { profile: "solo", content_type: "all", limit: 100, preset_id: null, process: false },
    });
    expect(planImport({ ...options, profiles: ["a", "b"] })).toEqual({
      kind: "group",
      body: { profiles: ["a", "b"], content_type: "all", limit: 100, preset_id: null, process: false },
    });
  });
});

describe("opções da tela", () => {
  it("tipos de mídia oferecidos e atalhos de quantidade", () => {
    expect(CONTENT_CHOICES).toEqual([
      { value: "all", label: "Todos" },
      { value: "reels", label: "Reels" },
      { value: "photos_carousels", label: "Fotos e carrosséis" },
    ]);
    expect([...LIMIT_SHORTCUTS]).toEqual([100, 500, 1000]);
  });
  it("Últimos N aceita inteiro livre de 1 a 1.000.000, com ou sem separador de milhar", () => {
    expect(parseLimitText("1")).toBe(1);
    expect(parseLimitText(" 37 ")).toBe(37);
    expect(parseLimitText("1000")).toBe(1000);
    expect(parseLimitText("1.000")).toBe(1000);
    expect(parseLimitText("1.000.000")).toBe(1_000_000);
    for (const bad of ["", "0", "-5", "1,5", "1.5", "10.00", "abc", "1e3", "1000001", "1 000"]) {
      expect(parseLimitText(bad)).toBeNull();
    }
  });
  it("resolveLimit: Todos é null mesmo com texto inválido; Últimos N exige número", () => {
    expect(resolveLimit("all", "")).toEqual({ ok: true, limit: null });
    expect(resolveLimit("last", "250")).toEqual({ ok: true, limit: 250 });
    expect(resolveLimit("last", "0")).toEqual({ ok: false, error: "Informe um número inteiro de 1 a 1.000.000." });
  });
});

describe("importSummary", () => {
  it("exemplos do pedido", () => {
    expect(importSummary({ profiles: 37, content: "reels", limit: null })).toBe(
      "37 perfis • Todos os Reels • Todas as mídias disponíveis",
    );
    expect(importSummary({ profiles: 37, content: "photos_carousels", limit: 1000 })).toBe(
      "37 perfis • Fotos e carrosséis • Até 1.000 mídias por perfil",
    );
  });
  it("todas as combinações de perfis x tipo x quantidade", () => {
    const type: Record<string, [string, string]> = {
      all: ["Todos os tipos", "Todos os tipos"],
      reels: ["Reels", "Todos os Reels"],
      photos_carousels: ["Fotos e carrosséis", "Todas as fotos e carrosséis"],
    };
    for (const content of ["all", "reels", "photos_carousels"] as ContentType[]) {
      const [withN, withAll] = type[content] as [string, string];
      expect(importSummary({ profiles: 1, content, limit: 100 })).toBe(`1 perfil • ${withN} • Até 100 mídias`);
      expect(importSummary({ profiles: 1, content, limit: null })).toBe(
        `1 perfil • ${withAll} • Todas as mídias disponíveis`,
      );
      expect(importSummary({ profiles: 2, content, limit: 100 })).toBe(
        `2 perfis • ${withN} • Até 100 mídias por perfil`,
      );
      expect(importSummary({ profiles: 2, content, limit: null })).toBe(
        `2 perfis • ${withAll} • Todas as mídias disponíveis`,
      );
    }
  });
  it("singular e separador de milhar", () => {
    expect(importSummary({ profiles: 1, content: "reels", limit: 1 })).toBe("1 perfil • Reels • Até 1 mídia");
    expect(importSummary({ profiles: 1240, content: "all", limit: 1 })).toBe(
      "1.240 perfis • Todos os tipos • Até 1 mídia por perfil",
    );
    expect(importSummary({ profiles: 3, content: "all", limit: 1_000_000 })).toBe(
      "3 perfis • Todos os tipos • Até 1.000.000 mídias por perfil",
    );
  });
});

describe("importHints", () => {
  const ALL =
    "Todos: percorre o perfil até o fim. Em perfis grandes pode levar horas por causa do limite de requisições " +
    "por hora; o lote continua sozinho.";
  it("um perfil, todos os tipos, quantidade N: nenhum aviso", () => {
    expect(importHints({ profiles: 1, content: "all", limit: 100 })).toEqual([]);
  });
  it("Todos avisa que percorre até o fim", () => {
    expect(importHints({ profiles: 1, content: "all", limit: null })).toEqual([ALL]);
    expect(importHints({ profiles: 1, content: "reels", limit: null })).toEqual([ALL]);
  });
  it("tipo filtrado com N avisa que continua procurando", () => {
    expect(importHints({ profiles: 1, content: "reels", limit: 1000 })).toEqual([
      "O app continua procurando até achar 1.000 Reels ou chegar ao fim do perfil.",
    ]);
    expect(importHints({ profiles: 1, content: "reels", limit: 1 })).toEqual([
      "O app continua procurando até achar 1 Reel ou chegar ao fim do perfil.",
    ]);
    expect(importHints({ profiles: 1, content: "photos_carousels", limit: 50 })).toEqual([
      "O app continua procurando até achar 50 fotos e itens de carrossel ou chegar ao fim do perfil.",
    ]);
  });
  it("vários perfis: a quantidade vale para cada um, um de cada vez", () => {
    expect(importHints({ profiles: 2, content: "all", limit: 100 })).toEqual([
      "A quantidade vale para cada perfil; os perfis são listados um de cada vez.",
    ]);
    expect(importHints({ profiles: 37, content: "reels", limit: null })).toEqual([
      ALL,
      "Os perfis são listados um de cada vez.",
    ]);
    expect(importHints({ profiles: 37, content: "photos_carousels", limit: 10 })).toHaveLength(2);
  });
});

describe("contagem de entradas e botão", () => {
  it("entriesSummary mostra só o que existe", () => {
    expect(entriesSummary({ valid: 37, duplicates: 0, invalid: 0 })).toBe("37 perfis válidos");
    expect(entriesSummary({ valid: 1, duplicates: 1, invalid: 1 })).toBe(
      "1 perfil válido • 1 duplicata removida • 1 entrada inválida",
    );
    expect(entriesSummary({ valid: 1200, duplicates: 2, invalid: 3 })).toBe(
      "1.200 perfis válidos • 2 duplicatas removidas • 3 entradas inválidas",
    );
    expect(entriesSummary({ valid: 0, duplicates: 0, invalid: 2 })).toBe("0 perfis válidos • 2 entradas inválidas");
  });
  it("o botão avisa quando há inválidas que ficam de fora", () => {
    expect(startLabel({ process: false, invalid: 0 })).toBe("Baixar originais");
    expect(startLabel({ process: true, invalid: 0 })).toBe("Baixar e processar");
    expect(startLabel({ process: false, invalid: 1 })).toBe("Baixar originais (ignorar 1 inválida)");
    expect(startLabel({ process: true, invalid: 12 })).toBe("Baixar e processar (ignorar 12 inválidas)");
  });
});
