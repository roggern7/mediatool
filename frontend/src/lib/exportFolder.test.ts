import { describe, expect, it } from "vitest";
import type { ExportJob, LastExport } from "../api/types";
import {
  DEFAULT_FOLDER_BUTTON,
  NOT_EXPORTED_HINT,
  PICKING_TEXT,
  UNFINISHED_BATCH_NOTE,
  UNFINISHED_GROUP_NOTE,
  ZIP_EXPIRED_TEXT,
  ZIP_SPACE_NOTE,
  batchExportOffer,
  buildExportRequest,
  exportActions,
  exportEndedText,
  exportExtrasText,
  exportFraction,
  exportProgressText,
  exportResultText,
  exportRunningText,
  exportSelectedLabel,
  groupExportOffer,
  importExportSource,
  initialLibrarySearch,
  isExportActive,
  libraryPathForProfile,
  librarySelectionSource,
  formatZipSize,
  pickResumeExport,
  runningExportId,
  zipAvailableText,
  zipDownloadUrl,
  zipPreparingText,
  zipReadyText,
  zipSelectedLabel,
} from "./exportFolder";

function job(patch: Partial<ExportJob> = {}): ExportJob {
  return {
    id: 7,
    kind: "batch",
    batch_id: 3,
    group_id: null,
    dest_path: "C:\\Users\\Ana\\Minha pasta",
    status: "running",
    total: 10000,
    processed: 3482,
    copied: 3482,
    skipped_existing: 0,
    renamed: 0,
    missing: 0,
    failed: 0,
    error_code: null,
    error_message: null,
    created_at: "2026-10-06T12:00:00.000Z",
    started_at: "2026-10-06T12:00:01.000Z",
    finished_at: null,
    ...patch,
  };
}

function last(patch: Partial<LastExport> = {}): LastExport {
  return {
    id: 4,
    dest_path: "D:\\Exportadas",
    status: "completed",
    total: 5,
    copied: 5,
    skipped_existing: 0,
    renamed: 0,
    missing: 0,
    failed: 0,
    finished_at: "2026-10-06T12:00:00.000Z",
    ...patch,
  };
}

describe("exportar para pasta: progresso e resultado", () => {
  it("mostra o progresso como 3.482 / 10.000", () => {
    expect(exportProgressText(job())).toBe("3.482 / 10.000");
    expect(exportProgressText(job({ processed: 0 }))).toBe("0 / 10.000");
    expect(exportProgressText(job({ processed: 12000 }))).toBe("10.000 / 10.000");
    expect(exportFraction(job())).toBeCloseTo(0.3482);
    expect(exportFraction(job({ total: 0, processed: 0 }))).toBe(0);
    expect(exportFraction(job({ processed: 20000 }))).toBe(1);
  });

  it("diferencia fila de cópia em andamento", () => {
    expect(exportRunningText(job({ status: "pending" }))).toBe("Na fila para copiar para C:\\Users\\Ana\\Minha pasta");
    expect(exportRunningText(job())).toBe("Copiando para C:\\Users\\Ana\\Minha pasta");
    expect(isExportActive(job())).toBe(true);
    expect(isExportActive(job({ status: "pending" }))).toBe(true);
    for (const status of ["completed", "cancelled", "failed"] as const) {
      expect(isExportActive(job({ status }))).toBe(false);
    }
    expect(isExportActive(null)).toBe(false);
  });

  it("resume o resultado com número em pt-BR e a pasta", () => {
    expect(exportResultText(job({ status: "completed", copied: 1248 }))).toBe(
      "1.248 mídias exportadas para C:\\Users\\Ana\\Minha pasta",
    );
    expect(exportResultText(job({ copied: 1 }))).toBe("1 mídia exportada para C:\\Users\\Ana\\Minha pasta");
    expect(exportResultText(job({ copied: 0, skipped_existing: 12 }))).toBe(
      "Nenhuma mídia nova para copiar: a pasta C:\\Users\\Ana\\Minha pasta já está completa",
    );
    expect(exportResultText(job({ copied: 0 }))).toBe("0 mídias exportadas para C:\\Users\\Ana\\Minha pasta");
  });

  it("lista só os complementos que aconteceram", () => {
    expect(exportExtrasText(job())).toBe("");
    expect(exportExtrasText(job({ skipped_existing: 12, renamed: 3, missing: 1 }))).toBe(
      "12 já estavam na pasta · 3 renomeadas · 1 original ausente",
    );
    expect(exportExtrasText(job({ skipped_existing: 1, renamed: 1, missing: 2, failed: 1 }))).toBe(
      "1 já estava na pasta · 1 renomeada · 2 originais ausentes · 1 não copiada",
    );
    expect(exportExtrasText(job({ failed: 1500 }))).toBe("1.500 não copiadas");
  });

  it("cancelada ou com falha: mensagem clara e a Biblioteca intacta", () => {
    expect(exportEndedText(job({ status: "completed" }))).toBeNull();
    expect(exportEndedText(job())).toBeNull();
    expect(exportEndedText(job({ status: "cancelled", copied: 1200 }))).toBe(
      "Exportação cancelada. 1.200 arquivos já copiados continuam na pasta. A Biblioteca não foi alterada.",
    );
    expect(exportEndedText(job({ status: "cancelled", copied: 1 }))).toBe(
      "Exportação cancelada. 1 arquivo já copiado continua na pasta. A Biblioteca não foi alterada.",
    );
    expect(exportEndedText(job({ status: "cancelled", copied: 0 }))).toBe(
      "Exportação cancelada. Nenhum arquivo foi copiado. A Biblioteca não foi alterada.",
    );
    expect(
      exportEndedText(job({ status: "failed", error_code: "export_dest_missing", error_message: "A pasta sumiu." })),
    ).toBe("A pasta sumiu. A Biblioteca não foi alterada.");
    const already = "O disco de destino ficou sem espaço. A Biblioteca não foi alterada.";
    expect(exportEndedText(job({ status: "failed", error_message: already }))).toBe(already);
    expect(exportEndedText(job({ status: "failed", error_message: null }))).toBe(
      "A exportação não terminou. A Biblioteca não foi alterada.",
    );
  });
});

describe("exportar para pasta: estado dos botões", () => {
  it("importação ainda em andamento não oferece exportar", () => {
    const actions = exportActions({ finished: false, lastExport: null, current: null, busy: false });
    expect(actions).toEqual({ showExport: false, canExport: false, canOpen: false, openId: null, openHint: null });
  });

  it("importação terminada e nunca exportada: exportar liberado e abrir pasta com orientação", () => {
    const actions = exportActions({ finished: true, lastExport: undefined, current: null, busy: false });
    expect(actions.showExport).toBe(true);
    expect(actions.canExport).toBe(true);
    expect(actions.canOpen).toBe(false);
    expect(actions.openHint).toBe(NOT_EXPORTED_HINT);
    expect(NOT_EXPORTED_HINT).toContain("Exportar para pasta");
  });

  it("com exportação concluída o Abrir pasta aponta para ela", () => {
    const actions = exportActions({ finished: true, lastExport: last(), current: null, busy: false });
    expect(actions.canOpen).toBe(true);
    expect(actions.openId).toBe(4);
    expect(actions.openHint).toBeNull();
    expect(actions.canExport).toBe(true);
  });

  it("seletor aberto ou cópia em andamento bloqueiam um novo pedido", () => {
    expect(exportActions({ finished: true, lastExport: null, current: null, busy: true }).canExport).toBe(false);
    const running = exportActions({ finished: true, lastExport: last(), current: job(), busy: false });
    expect(running.canExport).toBe(false);
    expect(running.openId).toBe(4); // a pasta da exportação anterior continua acessível
    const pending = exportActions({ finished: true, lastExport: null, current: job({ status: "pending" }), busy: false });
    expect(pending.canExport).toBe(false);
    expect(pending.canOpen).toBe(false);
  });

  it("a exportação concluída mais recente vence", () => {
    const fresh = job({ id: 9, status: "completed" });
    expect(exportActions({ finished: true, lastExport: last(), current: fresh, busy: false }).openId).toBe(9);
    expect(exportActions({ finished: true, lastExport: last({ id: 12 }), current: fresh, busy: false }).openId).toBe(12);
    expect(exportActions({ finished: true, lastExport: null, current: fresh, busy: false }).openId).toBe(9);
    const failed = job({ id: 9, status: "failed" });
    expect(exportActions({ finished: true, lastExport: last(), current: failed, busy: false }).openId).toBe(4);
    expect(exportActions({ finished: true, lastExport: null, current: failed, busy: false }).canExport).toBe(true);
  });
});

describe("exportar para pasta: pedido a partir da seleção", () => {
  it("lote e grupo viram a fonte da exportação", () => {
    expect(importExportSource("batch", 41)).toEqual({ kind: "batch", id: 41 });
    expect(importExportSource("group", 7)).toEqual({ kind: "group", id: 7 });
  });

  it("ids selecionados na página vão como media_ids", () => {
    const source = librarySelectionSource({
      allByFilter: false,
      view: "all",
      typeFilter: "all",
      q: "",
      selectedIds: [5, 9, 11],
    });
    expect(source).toEqual({ kind: "library", selection: { media_ids: [5, 9, 11] } });
  });

  it("todos os resultados do filtro vão como filtro, sem ids", () => {
    const source = librarySelectionSource({
      allByFilter: true,
      view: "originals",
      typeFilter: "video",
      q: "perfil",
      selectedIds: [1, 2, 3],
    });
    expect(source).toEqual({
      kind: "library",
      selection: { filter: { view: "originals", media_type: "video", q: "perfil" } },
    });
    expect(JSON.stringify(source)).not.toContain("media_ids");
    const plain = librarySelectionSource({ allByFilter: true, view: "all", typeFilter: "all", q: "", selectedIds: [] });
    expect(plain).toEqual({ kind: "library", selection: { filter: { view: "all" } } });
  });

  it("exclusões acompanham a seleção por filtro", () => {
    const source = librarySelectionSource({
      allByFilter: true,
      view: "all",
      typeFilter: "image",
      q: "",
      selectedIds: [],
      excludedIds: [4, 8],
    });
    expect(source).toEqual({
      kind: "library",
      selection: { filter: { view: "all", media_type: "image" }, exclude_ids: [4, 8] },
    });
  });

  it("nada selecionado não gera pedido, e o pedido nunca leva caminho", () => {
    expect(
      librarySelectionSource({ allByFilter: false, view: "all", typeFilter: "all", q: "", selectedIds: [] }),
    ).toBeNull();
    const text = JSON.stringify(
      librarySelectionSource({ allByFilter: false, view: "all", typeFilter: "all", q: "x", selectedIds: [1] }),
    );
    expect(text).not.toMatch(/path|dest|folder/);
  });

  it("rótulo acessível do botão da Biblioteca", () => {
    expect(exportSelectedLabel(1)).toBe("Exportar 1 selecionado para uma pasta");
    expect(exportSelectedLabel(5000)).toBe("Exportar 5.000 selecionados para uma pasta");
  });
});

describe("ver na Biblioteca", () => {
  it("abre a Biblioteca filtrada pelo perfil do lote; sem perfil, só navega", () => {
    expect(libraryPathForProfile("perfil.um")).toBe("/biblioteca?perfil=perfil.um");
    expect(libraryPathForProfile("@Perfil_Dois")).toBe("/biblioteca?perfil=Perfil_Dois");
    expect(libraryPathForProfile(null)).toBe("/biblioteca");
    expect(libraryPathForProfile("  ")).toBe("/biblioteca");
  });

  it("a Biblioteca lê a busca inicial da URL", () => {
    expect(initialLibrarySearch("?perfil=perfil.um")).toBe("perfil.um");
    expect(initialLibrarySearch("?perfil=%40outro")).toBe("outro");
    expect(initialLibrarySearch("")).toBe("");
    expect(initialLibrarySearch("?lote=3")).toBe("");
    expect(initialLibrarySearch(`?perfil=${"a".repeat(200)}`)).toHaveLength(64);
  });

  it("texto do seletor aberto", () => {
    expect(PICKING_TEXT).toBe("Escolha a pasta na janela do Windows…");
  });
});

describe("exportar para pasta: quando a importação oferece a exportação", () => {
  const progress = { found: 10, downloaded: 0, processed: 0, skipped: 0, failed: 0, pending: 0, active: 0 };
  const profiles = { total: 4, waiting: 0, active: 0, completed: 4, attention: 0, failed: 0, cancelled: 0 };

  it("lote encerrado sempre oferece, sem aviso", () => {
    for (const status of ["completed", "failed", "cancelled"] as const) {
      expect(batchExportOffer({ status, listing_state: "done", progress })).toEqual({ offer: true, note: null });
    }
  });

  it("lote em andamento não oferece", () => {
    const busy = { ...progress, downloaded: 5 };
    expect(batchExportOffer({ status: "running", listing_state: "done", progress: busy }).offer).toBe(false);
    expect(batchExportOffer({ status: "listing", listing_state: "running", progress: busy }).offer).toBe(false);
    expect(batchExportOffer({ status: "listing", listing_state: "waiting_rate_limit", progress: busy }).offer).toBe(
      false,
    );
    expect(batchExportOffer({ status: "listing", listing_state: "paused", progress: busy }).offer).toBe(false);
  });

  it("lote parado em atenção com itens já baixados oferece, avisando que não terminou", () => {
    const attention = { status: "listing", listing_state: "needs_attention" } as const;
    expect(batchExportOffer({ ...attention, progress: { ...progress, downloaded: 240 } })).toEqual({
      offer: true,
      note: UNFINISHED_BATCH_NOTE,
    });
    expect(batchExportOffer({ ...attention, progress: { ...progress, skipped: 3 } }).offer).toBe(true);
    expect(batchExportOffer({ ...attention, progress })).toEqual({ offer: false, note: null });
    expect(UNFINISHED_BATCH_NOTE).toContain("não terminou");
  });

  it("grupo concluído, inclusive com pendências, e grupo cancelado oferecem sem aviso", () => {
    expect(groupExportOffer({ profiles })).toEqual({ offer: true, note: null });
    expect(groupExportOffer({ profiles: { ...profiles, completed: 2, failed: 2 } })).toEqual({
      offer: true,
      note: null,
    });
    expect(groupExportOffer({ profiles: { ...profiles, completed: 0, cancelled: 4 } }).offer).toBe(true);
  });

  it("grupo parado em atenção, sem nenhum perfil ativo, oferece com aviso", () => {
    expect(groupExportOffer({ profiles: { ...profiles, completed: 2, attention: 1, failed: 1 } })).toEqual({
      offer: true,
      note: UNFINISHED_GROUP_NOTE,
    });
  });

  it("grupo com perfil na fila ou trabalhando não oferece", () => {
    expect(groupExportOffer({ profiles: { ...profiles, completed: 3, active: 1 } }).offer).toBe(false);
    expect(groupExportOffer({ profiles: { ...profiles, completed: 2, waiting: 1, attention: 1 } }).offer).toBe(false);
    expect(groupExportOffer({ profiles: { ...profiles, total: 0, completed: 0 } }).offer).toBe(false);
  });

  it("a oferta entra no estado dos botões", () => {
    const offer = batchExportOffer({
      status: "listing",
      listing_state: "needs_attention",
      progress: { ...progress, downloaded: 2 },
    });
    const actions = exportActions({ finished: offer.offer, lastExport: null, current: null, busy: false });
    expect(actions.showExport).toBe(true);
    expect(actions.canExport).toBe(true);
    expect(DEFAULT_FOLDER_BUTTON).toBe("Exportar para a pasta padrão em Documentos");
  });
});

describe("baixar ZIP", () => {
  const zip = (patch: Partial<ExportJob> = {}) =>
    job({ mode: "zip", dest_path: "importacao-2026-10-06-1143.zip", zip_name: "importacao-2026-10-06-1143.zip", ...patch });

  it("progresso da preparação", () => {
    expect(zipPreparingText(zip())).toBe("Preparando ZIP • 3.482 / 10.000 mídias");
    expect(zipPreparingText(zip({ status: "pending", processed: 0 }))).toBe("Preparando ZIP • 0 / 10.000 mídias");
  });

  it("ZIP pronto com quantidade e tamanho em pt-BR", () => {
    const ready = zip({ status: "completed", copied: 10000, zip_bytes: 18.4 * 1024 ** 3 });
    expect(zipReadyText(ready)).toBe("ZIP pronto • 10.000 mídias • 18,4 GB");
    expect(zipReadyText(zip({ copied: 1, zip_bytes: 2048 }))).toBe("ZIP pronto • 1 mídia • 2 KB");
  });

  it("tamanho em B, KB, MB e GB com vírgula", () => {
    expect(formatZipSize(512)).toBe("512 B");
    expect(formatZipSize(340 * 1024)).toBe("340 KB");
    expect(formatZipSize(12.5 * 1024 ** 2)).toBe("12,5 MB");
    expect(formatZipSize(18.4 * 1024 ** 3)).toBe("18,4 GB");
    expect(formatZipSize(1536 * 1024 ** 3)).toBe("1.536,0 GB");
    expect(formatZipSize(null)).toBe("-");
    expect(formatZipSize(-1)).toBe("-");
  });

  it("validade do ZIP em hora local", () => {
    const now = new Date(2026, 9, 6, 9, 0).getTime();
    expect(zipAvailableText({ zip_expires_at: new Date(2026, 9, 6, 14, 5).toISOString() }, now)).toBe(
      "disponível até 14:05",
    );
    // Outro dia: mostra a data também (o ZIP pronto vale 24 h).
    expect(zipAvailableText({ zip_expires_at: new Date(2026, 9, 7, 8, 30).toISOString() }, now)).toBe(
      "disponível até 07/10 08:30",
    );
    expect(zipAvailableText({ zip_expires_at: null })).toBeNull();
    expect(zipAvailableText({ zip_expires_at: "lixo" })).toBeNull();
  });

  it("o download só é oferecido com o ZIP pronto e com link", () => {
    const url = "/api/exports/7/download?token=abc";
    expect(zipDownloadUrl(zip({ status: "completed", zip_ready: true, download_url: url }))).toBe(url);
    expect(zipDownloadUrl(zip({ status: "running", zip_ready: false, download_url: null }))).toBeNull();
    expect(zipDownloadUrl(zip({ status: "expired", zip_ready: false, download_url: null }))).toBeNull();
    expect(zipDownloadUrl(zip({ status: "completed", zip_ready: true, download_url: null }))).toBeNull();
    expect(zipDownloadUrl(job({ status: "completed" }))).toBeNull(); // exportação para pasta
    expect(zipDownloadUrl(null)).toBeNull();
  });

  it("cancelado, falho e expirado têm mensagens próprias", () => {
    expect(exportEndedText(zip({ status: "cancelled", copied: 40 }))).toBe(
      "Preparação do ZIP cancelada. Nenhum arquivo foi baixado. A Biblioteca não foi alterada.",
    );
    expect(exportEndedText(zip({ status: "expired" }))).toBe(ZIP_EXPIRED_TEXT);
    expect(exportEndedText(zip({ status: "failed", error_message: null }))).toBe(
      "O ZIP não pôde ser preparado. A Biblioteca não foi alterada.",
    );
    const noSpace = "O disco ficou sem espaço enquanto o ZIP era preparado. A Biblioteca não foi alterada.";
    expect(exportEndedText(zip({ status: "failed", error_message: noSpace }))).toBe(noSpace);
    expect(ZIP_SPACE_NOTE).toContain("Downloads");
  });

  it("monta o pedido: ZIP, token do seletor ou pasta padrão, nunca um caminho", () => {
    const source = { kind: "group", id: 7 } as const;
    expect(buildExportRequest(source, { kind: "zip" })).toEqual({ source, destination: { zip: true } });
    expect(buildExportRequest(source, { kind: "token", token: "tok" })).toEqual({
      source,
      destination: { token: "tok" },
    });
    expect(buildExportRequest(source, { kind: "default" })).toEqual({ source, destination: { default: true } });
    const all = librarySelectionSource({ allByFilter: true, view: "all", typeFilter: "all", q: "", selectedIds: [] });
    expect(all && buildExportRequest(all, { kind: "zip" })).toEqual({
      source: { kind: "library", selection: { filter: { view: "all" } } },
      destination: { zip: true },
    });
  });

  it("ZIP em preparação bloqueia um novo pedido; rótulos acessíveis", () => {
    const actions = exportActions({ finished: true, lastExport: null, current: zip(), busy: false });
    expect(actions.canExport).toBe(false);
    // ZIP concluído não vira "pasta para abrir".
    const done = exportActions({
      finished: true,
      lastExport: null,
      current: zip({ status: "completed", zip_ready: true }),
      busy: false,
    });
    expect(done.canExport).toBe(true);
    expect(done.canOpen).toBe(false);
    expect(zipSelectedLabel(1)).toBe("Baixar 1 selecionado em um ZIP");
    expect(zipSelectedLabel(5000)).toBe("Baixar 5.000 selecionados em um ZIP");
  });
});

describe("retomada ao abrir a tela", () => {
  const now = new Date("2026-10-06T12:00:00.000Z").getTime();
  const url = "/api/exports/9/download?token=abc";
  const readyZip = (patch: Partial<ExportJob> = {}) =>
    job({
      id: 9,
      mode: "zip",
      status: "completed",
      zip_ready: true,
      download_url: url,
      zip_expires_at: "2026-10-07T11:00:00.000Z",
      ...patch,
    });

  it("exportação em andamento é acompanhada (pasta ou ZIP), mesmo havendo um ZIP pronto mais novo", () => {
    expect(pickResumeExport([job({ id: 5, status: "running" })], now)).toEqual({
      job: job({ id: 5, status: "running" }),
      action: "follow",
    });
    const decision = pickResumeExport([readyZip({ id: 12 }), job({ id: 11, mode: "zip", status: "pending" })], now);
    expect(decision?.action).toBe("follow");
    expect(decision?.job.id).toBe(11);
  });

  it("ZIP pronto e ainda válido é oferecido, sem ser tratado como em andamento", () => {
    const decision = pickResumeExport([job({ id: 10, status: "failed" }), readyZip()], now);
    expect(decision).toEqual({ job: readyZip(), action: "offer" });
  });

  it("expirado, cancelado, falho ou pasta já concluída: nada", () => {
    expect(pickResumeExport([], now)).toBeNull();
    expect(pickResumeExport([readyZip({ zip_expires_at: "2026-10-06T11:59:00.000Z" })], now)).toBeNull();
    expect(pickResumeExport([readyZip({ status: "expired", zip_ready: false, download_url: null })], now)).toBeNull();
    expect(pickResumeExport([job({ status: "cancelled" }), job({ status: "failed", mode: "zip" })], now)).toBeNull();
    expect(pickResumeExport([job({ status: "completed" })], now)).toBeNull();
  });

  it("409 export_already_running traz a exportação para acompanhar", () => {
    expect(runningExportId("export_already_running", { export_id: 31, code: "export_already_running" })).toBe(31);
    expect(runningExportId("export_already_running", {})).toBeNull();
    expect(runningExportId("export_already_running", { export_id: "31" })).toBeNull();
    expect(runningExportId("export_queue_full", { export_id: 31 })).toBeNull();
    expect(runningExportId("export_already_running", null)).toBeNull();
  });
});
