import assert from "node:assert/strict";
import { test } from "node:test";

import {
  autoExportText, childRefreshLimit, childrenCountText, counterList, filtersText, isActive, itemMessage,
  mergeChildPage, progressFraction, shouldKeepPolling, stateLabel, stateTone,
} from "../src/lib/activityModel.js";
import { ExtApiError } from "../src/lib/api.js";
import { describeError, isBlocking } from "../src/lib/connection.js";
import {
  DEFAULT_PROCESS, buildProcessRequest, durationOptions, editPagePath, formatSpeed, parseSpeedInput, processSummary,
  snapSpeed,
  splitForEdit, stepSpeed, toStoredProcess, validateProcess,
} from "../src/lib/editModel.js";
import { buildExportBody, exportFinished, exportStatusText, formatBytes } from "../src/lib/exportModel.js";
import { FormError } from "../src/lib/importModel.js";
import { Selection, selectionText } from "../src/lib/selection.js";

/* ---------- atividade ---------- */

test("estados traduzidos", () => {
  assert.deepEqual(["waiting", "analyzing", "downloading", "processing", "done", "partial", "failed", "cancelled"]
    .map(stateLabel), ["Aguardando", "Analisando publicação", "Baixando…", "Processando", "Concluído",
    "Concluído parcialmente", "Falhou", "Interrompido"]);
  assert.equal(stateLabel("x"), "Estado desconhecido");
  assert.equal(stateTone("downloading"), "active");
  assert.equal(stateTone("partial"), "warn");
});

test("contadores: zerados somem, exceto encontradas e baixados", () => {
  const list = counterList({ found: 10, analyzed: 0, downloaded: 4, duplicates: 2, filtered_views: 3, no_count: 1,
    failed: 0, processed: 2 });
  assert.deepEqual(list.map((c) => `${c.value} ${c.label}`), ["10 encontradas", "4 baixados", "2 duplicados",
    "3 fora das visualizações", "1 sem contagem de visualizações", "2 processados"]);
});

test("progresso geral e fim nunca preso em processando", () => {
  assert.equal(progressFraction({ state: "downloading", progress: { found: 0 } }), null);
  assert.equal(progressFraction({ state: "downloading", progress: { files: 10, downloaded: 4, duplicates: 1 } }), 0.5);
  // A API diz que terminou: barra cheia e sem polling, mesmo com contadores incompletos.
  const done = { state: "done", progress: { files: 10, downloaded: 2 } };
  assert.equal(progressFraction(done), 1);
  assert.equal(isActive(done), false);
  assert.equal(shouldKeepPolling([done]), false);
  assert.equal(shouldKeepPolling([{ state: "processing" }]), true);
  assert.equal(shouldKeepPolling([{ state: "done", auto_export: { status: "running" } }]), true);
  assert.equal(shouldKeepPolling([{ state: "done", auto_export: { status: "failed" } }]), false);
  assert.equal(shouldKeepPolling([{ state: "done", auto_export: { status: "completed", download_url: "/x" } }]), false);
});

test("exportação automática e filtros do lote", () => {
  assert.equal(autoExportText(null), null);
  assert.equal(autoExportText({ content: "edited", status: "running" }), "Exportação automática (editados): gerando ZIP");
  assert.equal(autoExportText({ content: "both", status: "completed", download_url: "/d" }),
    "Exportação automática (originais e editados): ZIP pronto");
  assert.equal(autoExportText({ content: "originals", status: "waiting", request_id: 3 }),
    "Exportação automática do pedido (originais): aguardando o fim de todo o pedido");
  assert.equal(filtersText({ limit: 50, posted_within_days: 30, min_views: 10000, max_views: 1500000 }),
    "até 50 · últimos 30 dias · mín. 10 mil · máx. 1,5 mi");
});

test("atividade: aviso do item (1a página vazia, atenção, pausa)", () => {
  assert.equal(itemMessage({ state: "done", message: null }), null);
  assert.equal(itemMessage({ state: "done" }), null);
  assert.deepEqual(itemMessage({ state: "done", message: "O Instagram não devolveu nenhuma publicação." }),
    { text: "O Instagram não devolveu nenhuma publicação.", tone: "warn" });
  assert.deepEqual(itemMessage({ state: "failed", message: "Perfil não encontrado." }),
    { text: "Perfil não encontrado.", tone: "err" });
  assert.equal(itemMessage({ state: "waiting", message: "O acesso automático ao Instagram está pausado." }).tone,
    "warn");
});

/* ---------- conexão ---------- */

test("grupo com filhos: páginas de perfis, atualização e contagem", () => {
  const child = (id, state = "downloading") => ({ id, kind: "batch", title: `@p${id}`, state, progress: {} });
  let entry = mergeChildPage({}, { items: [child(1), child(2)], total: 3, next_after_position: 2 }, false);
  assert.deepEqual(entry.children.map((c) => c.id), [1, 2]);
  assert.equal(entry.nextAfter, 2);
  assert.equal(childrenCountText(entry), "2 de 3 perfis");
  assert.equal(childRefreshLimit(entry), 20);
  // "Carregar mais": acrescenta sem repetir e o cursor chega ao fim.
  entry = mergeChildPage(entry, { items: [child(2), child(3, "failed")], total: 3, next_after_position: null }, true);
  assert.deepEqual(entry.children.map((c) => c.id), [1, 2, 3]);
  assert.equal(entry.nextAfter, null);
  assert.equal(childrenCountText(entry), "3 de 3 perfis");
  // Atualização do começo: troca os dados dos já carregados.
  entry = mergeChildPage(entry, { items: [child(1, "done"), child(2, "done"), child(3, "failed")], total: 3,
    next_after_position: null }, false);
  assert.deepEqual(entry.children.map((c) => c.state), ["done", "done", "failed"]);
  // Lista aberta maior que a página de atualização (100): mantém os seguintes e o cursor deles.
  const big = { children: Array.from({ length: 120 }, (_, i) => child(i + 1)), nextAfter: 120, total: 300 };
  assert.equal(childRefreshLimit(big), 100);
  const refreshed = mergeChildPage(big, { items: [child(1, "done")], total: 300, next_after_position: 1 }, false);
  assert.equal(refreshed.children.length, 120);
  assert.equal(refreshed.children[0].state, "done");
  assert.equal(refreshed.nextAfter, 120);
  assert.equal(childrenCountText({ children: [child(1)], total: 1 }), "1 de 1 perfil");
});

test("recuperação de erros: 0 (offline), 401, 429, 4xx", () => {
  const offline = describeError(new ExtApiError("offline", "sem app"));
  assert.equal(offline.state, "offline");
  assert.equal(offline.retryMs, 5000);
  assert.equal(isBlocking("offline"), true);
  assert.equal(describeError(new ExtApiError("timeout", "lento")).state, "offline");
  assert.equal(describeError(new ExtApiError("unauthorized", "x", { status: 401, code: "extension_unauthorized" }))
    .state, "revoked");
  assert.equal(describeError(new ExtApiError("unauthorized", "x", { status: 401 })).state, "unpaired");
  const rate = describeError(new ExtApiError("rate_limited", "calma", { status: 429, retryAfter: 30 }));
  assert.deepEqual([rate.state, rate.retryMs], ["rate_limited", 30000]);
  assert.equal(isBlocking("rate_limited"), false);
  assert.equal(describeError(new ExtApiError("rate_limited", "calma", { status: 429 })).retryMs, 10000);
  assert.equal(describeError(new ExtApiError("http", "recusado", { status: 422 })).state, "error");
  assert.equal(describeError(new FormError("campo")).message, "campo");
});

/* ---------- processar ---------- */

test("velocidade: faixa, passo 0,01, campo e botões", () => {
  assert.equal(snapSpeed(1.234), 1.23);
  assert.equal(snapSpeed(2), 1.5);
  assert.equal(snapSpeed(0.5), 1);
  assert.equal(parseSpeedInput("1,07"), 1.07);
  assert.equal(parseSpeedInput("1.25x"), 1.25);
  assert.equal(parseSpeedInput("1,6"), null);
  assert.equal(parseSpeedInput("abc"), null);
  assert.equal(stepSpeed(1.1, 1), 1.11);
  assert.equal(stepSpeed(1, -1), 1);
  assert.equal(stepSpeed(1.5, 1), 1.5);
  assert.equal(formatSpeed(1.1), "1,10x");
});

test("duração e validação como na página Editar", () => {
  assert.deepEqual(durationOptions(null).map((o) => o.value), ["original", "15", "30", "60", "custom"]);
  assert.equal(validateProcess({ ...DEFAULT_PROCESS, durationMode: "custom", customText: "" }),
    "Informe a duração personalizada em segundos.");
  assert.match(validateProcess({ ...DEFAULT_PROCESS, durationMode: "custom", customText: "9000" }), /7200/);
  assert.equal(validateProcess({ ...DEFAULT_PROCESS, durationMode: "custom", customText: "12,5" }), null);
  assert.equal(validateProcess({ ...DEFAULT_PROCESS, mode: "preset" }), "Escolha um preset de processamento.");
  assert.deepEqual(toStoredProcess({ ...DEFAULT_PROCESS, durationMode: "custom", customText: "12,5", speed: 1.2,
    sharpen: true }), { mode: "edit", edit: { duration_mode: "custom", custom_seconds: 12.5, speed: 1.2,
    sharpen: true, output_type: "mp4" } });
  assert.deepEqual(toStoredProcess({ ...DEFAULT_PROCESS, mode: "preset", presetId: "3" }), { mode: "preset",
    presetId: 3 });
  assert.equal(toStoredProcess({ ...DEFAULT_PROCESS, mode: "preset" }), null);
  assert.equal(processSummary({ ...DEFAULT_PROCESS, durationMode: "30", speed: 1.1, sharpen: true }),
    "corte em 30 s (+0,03 s) · velocidade 1,10x · nitidez sutil · saída MP4");
});

test("aplicar à seleção: só vídeos, avisa os ignorados", () => {
  const sel = new Selection([[1, { media_type: "video", role: "original" }], [2, { media_type: "image", role: "original" }],
    [3, { media_type: "image", role: "cover" }], [4, null]]);
  const { videos, ignored } = splitForEdit(sel.entries());
  assert.deepEqual(videos, [1, 4]);
  assert.equal(ignored, 2);
  assert.deepEqual(buildProcessRequest({ ...DEFAULT_PROCESS, speed: 1.2 }, videos), { route: "edits",
    body: { media_ids: [1, 4], settings: { duration_mode: "original", custom_seconds: null, speed: 1.2,
      sharpen: false, output_type: "mp4" } } });
  assert.deepEqual(buildProcessRequest({ ...DEFAULT_PROCESS, mode: "preset", presetId: 2 }, [1]), { route: "process",
    body: { media_ids: [1], preset_id: 2 } });
  assert.throws(() => buildProcessRequest(DEFAULT_PROCESS, []), /Nenhum/);
  assert.throws(() => buildProcessRequest(DEFAULT_PROCESS, Array.from({ length: 501 }, (_, i) => i + 1)), /500/);
});

test("recortar no Media Tool: /editar?ids= com os vídeos da seleção (até 500)", () => {
  const sel = new Selection([[9, { media_type: "video", role: "original" }], [2, { media_type: "image", role: "original" }],
    [3, { media_type: "image", role: "cover" }], [4, { media_type: "video", role: "original" }]]);
  assert.equal(editPagePath(splitForEdit(sel.entries()).videos), "/editar?ids=9,4");
  assert.equal(editPagePath([]), "/editar");
  assert.equal(editPagePath([0, -1, "x", 1.5, 7, 7]), "/editar?ids=7");
  const many = editPagePath(Array.from({ length: 600 }, (_, i) => i + 1));
  assert.equal(many.split("=")[1].split(",").length, 500);
});

/* ---------- seleção ---------- */

test("seleção: alternar, lote inteiro, contagens e persistência", () => {
  const sel = new Selection();
  assert.equal(sel.toggle({ id: 5, media_type: "video", role: "original" }), true);
  assert.equal(sel.toggle({ id: 6, media_type: "image", role: "cover" }), true);
  assert.equal(sel.toggle({ id: 5 }), false);
  assert.equal(sel.addIds([6, 7, 8, "x", 0]), 2);
  assert.deepEqual(sel.counts(), { total: 3, videos: 0, photos: 0, covers: 1, unknown: 2 });
  assert.deepEqual(sel.coverIds(), [6]);
  assert.equal(selectionText(sel), "3 selecionados (1 capa, 2 de tipo não identificado)");
  const again = new Selection(JSON.parse(JSON.stringify(sel)));
  assert.deepEqual(again.ids(), [6, 7, 8]);
  sel.clear();
  assert.equal(selectionText(sel), "Nenhum item selecionado");
});

test("seleção: \"selecionar todos do lote\" com tipo e papel (GET /api/ext/library/ids → items)", () => {
  const sel = new Selection();
  const items = [{ id: 9, media_type: "video", role: "original" }, { id: 8, media_type: "image", role: "cover" },
    { id: 7, media_type: "image", role: "original" }];
  assert.equal(sel.addItems(items), 3);
  assert.deepEqual(sel.counts(), { total: 3, videos: 1, photos: 1, covers: 1, unknown: 0 });
  assert.equal(selectionText(sel), "3 selecionados (1 vídeo, 1 foto, 1 capa)");
});

/* ---------- exportação ---------- */

test("exportação: um ZIP por pedido, fontes e só capas", () => {
  assert.deepEqual(buildExportBody({ kind: "library", mediaIds: [1, 2] }, "both"),
    { source: { kind: "library", media_ids: [1, 2] }, content: "both" });
  assert.deepEqual(buildExportBody({ kind: "library", mediaIds: [1, 2], coversOnly: true, coverIds: [2] }, "originals"),
    { source: { kind: "library", media_ids: [2] }, content: "originals" });
  assert.throws(() => buildExportBody({ kind: "library", mediaIds: [1], coversOnly: true, coverIds: [] }, "originals"),
    /capa/);
  assert.deepEqual(buildExportBody({ kind: "group", id: "9" }, "edited"),
    { source: { kind: "group", id: 9 }, content: "edited" });
  assert.throws(() => buildExportBody({ kind: "batch", id: "" }, "edited"), /lote/);
  assert.throws(() => buildExportBody({ kind: "library", mediaIds: [] }, "edited"), /Selecione/);
  assert.throws(() => buildExportBody({ kind: "library", mediaIds: [1] }, "zip"), /conteúdo/);
});

test("status da exportação", () => {
  assert.equal(exportStatusText({ status: "running", processed: 3, total: 10 }), "Gerando ZIP… 3 de 10");
  assert.equal(exportStatusText({ status: "completed", zip_ready: true }), "ZIP pronto");
  assert.equal(exportStatusText({ status: "failed", error_message: "disco cheio" }), "Falhou: disco cheio");
  assert.equal(exportFinished({ status: "running" }), false);
  assert.equal(exportFinished({ status: "failed" }), true);
  assert.equal(exportFinished({ zip_ready: true }), true);
  assert.equal(formatBytes(5 * 1024 * 1024), "5,0 MB");
});

test("opções reais do backend: duração como {value, label}", () => {
  const opts = durationOptions({ edit: { duration_modes: [{ value: "original", label: "Original" },
    { value: "custom", label: "Personalizada" }] } });
  assert.deepEqual(opts, [{ value: "original", label: "Original" }, { value: "custom", label: "Personalizada" }]);
  assert.equal(shouldKeepPolling([{ state: "done", auto_export: { status: "expired" } }]), false);
  assert.equal(autoExportText({ content: "originals", status: "failed", error_message: "sem espaço" }),
    "Exportação automática (originais): falhou (sem espaço)");
});
