import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BUILTIN_PRESETS, DEFAULT_EDIT, DEFAULT_FORM, FLOW_OPTIONS, FormError, NO_CHANGES_TEXT, RequestIdKeeper, applyPreset,
  buildPostBody, buildStartBody, classifyEntries, daysSinceYearStart, describeStartBody, editProblem, editSummary,
  flowProblem, flowSummary, makeUserPreset, migrateFlowForm, migrateUserPreset, needsConfirmation, periodDays,
  postActions, viewsLabel, viewsRange,
} from "../src/lib/importModel.js";

// "Editar após baixar" com corte em 30 s, 1,10x e nitidez (formulário) e o `edit` que vai no corpo do start.
const EDIT_FORM = { durationMode: "30", customText: "", speed: 1.1, sharpen: true };
const EDIT_SETTINGS = { duration_mode: "30", custom_seconds: null, speed: 1.1, sharpen: true, output_type: "mp4" };
const form = (over = {}) => ({ ...DEFAULT_FORM, profile: "@Fulano.Silva", ...over });
const editing = (edit = EDIT_FORM, over = {}) => form({ flow: "edit", edit: { ...DEFAULT_EDIT, ...edit }, ...over });

test("perfil: padrão só baixar, 50, sem filtros", () => {
  assert.deepEqual(buildStartBody(form()), { inputs: ["fulano.silva"], media: "videos", limit: 50,
    post_processing: "none", auto_export: null });
});

test("cada tipo de mídia vai no corpo", () => {
  for (const media of ["videos", "photos", "covers", "videos_covers", "all"]) {
    assert.equal(buildStartBody(form({ media })).media, media);
  }
  assert.throws(() => buildStartBody(form({ media: "tudo" })), FormError);
});

test("quantidade: personalizada, todas com confirmação explícita", () => {
  assert.equal(buildStartBody(form({ limit: "custom", limitCustom: "250" })).limit, 250);
  assert.throws(() => buildStartBody(form({ limit: "custom", limitCustom: "0" })), /Quantidade/);
  assert.throws(() => buildStartBody(form({ limit: "all" })), /Confirme/);
  const all = buildStartBody(form({ limit: "all", confirmAll: true }));
  assert.equal(all.limit, null);
  assert.equal(all.confirm_all, true);
});

test("período: presets, personalizado e ano atual em dias até hoje", () => {
  assert.equal(periodDays("any"), null);
  assert.equal(periodDays("30"), 30);
  assert.equal(periodDays("custom", "45"), 45);
  assert.throws(() => periodDays("custom", "4000"), /Período/);
  assert.equal(daysSinceYearStart(new Date(2026, 0, 1, 15)), 1);
  assert.equal(daysSinceYearStart(new Date(2026, 1, 1)), 32);
  assert.equal(daysSinceYearStart(new Date(2026, 9, 29, 23, 59)), 302);
  assert.equal(daysSinceYearStart(new Date(2024, 11, 31)), 366); // bissexto
  const body = buildStartBody(form({ period: "year" }), { now: new Date(2026, 2, 10) });
  assert.equal(body.posted_within_days, 69);
});

test("visualizações: só com vídeos/capas; mín ≤ máx; atalhos e personalizado", () => {
  assert.deepEqual(viewsRange("videos", { minViews: "1000", maxViews: "custom", maxViewsCustom: "25000" }),
    { min: 1000, max: 25000 });
  assert.deepEqual(viewsRange("photos", { minViews: "1000" }), { min: null, max: null });
  assert.deepEqual(viewsRange("all", { minViews: "1000" }), { min: null, max: null });
  assert.throws(() => viewsRange("covers", { minViews: "10000", maxViews: "1000" }), /maior ou igual/);
  const body = buildStartBody(form({ media: "videos_covers", minViews: "100000" }));
  assert.equal(body.min_views, 100000);
  assert.equal("max_views" in body, false);
  assert.equal("min_views" in buildStartBody(form({ media: "all", minViews: "100000" })), false);
  assert.equal(viewsLabel(1_000), "1 mil");
  assert.equal(viewsLabel(1_000_000), "1 mi");
});

test("escolhas do fluxo: Só baixar · Editar após baixar · Processar com preset · edição manual", () => {
  assert.deepEqual(FLOW_OPTIONS.map((o) => o.label), ["Só baixar", "Editar após baixar", "Processar com preset",
    "Baixar e aguardar edição manual"]);
  const plain = buildStartBody(form({ flow: "download", edit: EDIT_FORM, presetId: 4 }));
  assert.equal(plain.post_processing, "none");
  assert.equal("edit" in plain, false);
  assert.equal("preset_id" in plain, false);
  const manual = buildStartBody(form({ flow: "manual", edit: EDIT_FORM, presetId: 4 }));
  assert.equal(manual.post_processing, "none");
  assert.equal("edit" in manual || "preset_id" in manual, false);
  const edit = buildStartBody(editing(EDIT_FORM, { presetId: 4 }));
  assert.equal(edit.post_processing, "edit");
  assert.deepEqual(edit.edit, EDIT_SETTINGS);
  assert.equal("preset_id" in edit, false); // exclusivo com o preset
  const preset = buildStartBody(form({ flow: "preset", presetId: "4", edit: EDIT_FORM }));
  assert.equal(preset.post_processing, "preset");
  assert.equal(preset.preset_id, 4);
  assert.equal("edit" in preset, false);
  assert.throws(() => buildStartBody(form({ flow: "preset", presetId: null })), /Escolha um preset/);
});

test("Editar após baixar: cada duração, velocidade e nitidez vão no `edit` do corpo", () => {
  for (const durationMode of ["15", "30", "60"]) {
    assert.deepEqual(buildStartBody(editing({ durationMode })).edit, { duration_mode: durationMode, custom_seconds: null,
      speed: 1, sharpen: false, output_type: "mp4" });
  }
  assert.deepEqual(buildStartBody(editing({ durationMode: "custom", customText: "12,5" })).edit,
    { duration_mode: "custom", custom_seconds: 12.5, speed: 1, sharpen: false, output_type: "mp4" });
  assert.equal(buildStartBody(editing({ durationMode: "custom", customText: "7200" })).edit.custom_seconds, 7200);
  for (const speed of [1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.5, 1.07]) {
    assert.deepEqual(buildStartBody(editing({ speed })).edit, { duration_mode: "original", custom_seconds: null, speed,
      sharpen: false, output_type: "mp4" });
  }
  assert.deepEqual(buildStartBody(editing({ sharpen: true })).edit, { duration_mode: "original", custom_seconds: null,
    speed: 1, sharpen: true, output_type: "mp4" });
});

test("Editar após baixar: validação igual ao app (sem edição bloqueia)", () => {
  assert.equal(editProblem(DEFAULT_EDIT), NO_CHANGES_TEXT);
  assert.equal(NO_CHANGES_TEXT, "Nenhuma edição selecionada.");
  assert.throws(() => buildStartBody(editing(DEFAULT_EDIT)), /Nenhuma edição selecionada/);
  assert.equal(editProblem({ ...DEFAULT_EDIT, sharpen: true }), null);
  assert.equal(editProblem({ ...DEFAULT_EDIT, durationMode: "15" }), null);
  assert.equal(editProblem({ ...DEFAULT_EDIT, speed: 1.01 }), null);
  assert.equal(editProblem({ ...DEFAULT_EDIT, durationMode: "custom", customText: "" }),
    "Informe a duração personalizada em segundos.");
  assert.match(editProblem({ ...DEFAULT_EDIT, durationMode: "custom", customText: "7200,5" }), /7200/);
  assert.match(editProblem({ ...DEFAULT_EDIT, durationMode: "custom", customText: "0" }), /maior que zero/);
  assert.match(editProblem({ ...DEFAULT_EDIT, speed: 1.6 }), /velocidade/);
  // Só vale para "Editar após baixar" e só quando há vídeo: fotos e capas só são baixadas.
  assert.equal(flowProblem(form({ flow: "download", edit: DEFAULT_EDIT }), "videos"), null);
  assert.equal(flowProblem(editing(DEFAULT_EDIT), "covers"), null);
  assert.equal(flowProblem(editing(DEFAULT_EDIT), "videos"), NO_CHANGES_TEXT);
  assert.equal(flowProblem(form({ flow: "preset", presetId: null }), "all"), "Escolha um preset de processamento.");
  const covers = buildStartBody(editing(DEFAULT_EDIT, { media: "covers" }));
  assert.equal(covers.post_processing, "none");
  assert.equal(buildStartBody(form({ flow: "preset", presetId: 2, media: "photos" })).post_processing, "none");
  assert.equal(buildStartBody(editing(EDIT_FORM, { media: "all" })).post_processing, "edit");
});

test("exportação automática: editados ou ambos só com edição ou preset", () => {
  assert.deepEqual(buildStartBody(form({ autoExport: true })).auto_export, { content: "originals" });
  assert.deepEqual(buildStartBody(editing(EDIT_FORM, { autoExport: true, autoExportContent: "both" })).auto_export,
    { content: "both" });
  assert.deepEqual(buildStartBody(form({ flow: "preset", presetId: 3, autoExport: true, autoExportContent: "edited" }))
    .auto_export, { content: "edited" });
  for (const flow of ["download", "manual"]) {
    for (const content of ["edited", "both"]) {
      assert.throws(() => buildStartBody(form({ flow, autoExport: true, autoExportContent: content })),
        /Editar após baixar/);
    }
  }
  assert.throws(() => buildStartBody(editing(EDIT_FORM, { media: "covers", autoExport: true,
    autoExportContent: "edited" })), /Capas e fotos/);
});

test("resumos: edição, fluxo e corpo enviado", () => {
  assert.equal(editSummary({ durationMode: "15", speed: 1.05, sharpen: true }), "Corte em 15 s · 1,05x · nitidez");
  assert.equal(editSummary({ durationMode: "custom", customText: "12,5", speed: 1, sharpen: false }), "Corte em 12,5 s");
  assert.equal(editSummary({ ...DEFAULT_EDIT, speed: 1.2 }), "1,20x");
  assert.equal(editSummary({ ...DEFAULT_EDIT, sharpen: true }), "Nitidez");
  assert.equal(editSummary(DEFAULT_EDIT), NO_CHANGES_TEXT);
  assert.equal(flowSummary(form()), "Só baixar");
  assert.equal(flowSummary(editing({ durationMode: "30", speed: 1.1, sharpen: false }, { autoExport: true,
    autoExportContent: "both" })), "Corte em 30 s · 1,10x · ZIP automático (originais e editados)");
  assert.equal(flowSummary(form({ flow: "preset", presetId: 7, presetName: "Antigo" }),
    { presets: [{ id: 7, name: "Reels 1080" }] }), "Preset \"Reels 1080\"");
  assert.equal(flowSummary(form({ flow: "preset", presetId: null })), "Nenhum preset escolhido");
  assert.equal(describeStartBody(buildStartBody(editing({ durationMode: "15", speed: 1.05, sharpen: true }))),
    "Somente vídeos · até 50 por perfil · editar após baixar (corte em 15 s · 1,05x · nitidez)");
  assert.equal(describeStartBody(buildStartBody(form({ flow: "preset", presetId: 7 })), [{ id: 7, name: "Leve" }]),
    "Somente vídeos · até 50 por perfil · processar com preset \"Leve\"");
});

test("migração: o antigo flow \"auto\" vira Editar após baixar ou Processar com preset", () => {
  const editProcess = { mode: "edit", durationMode: "60", customText: "", speed: 1.25, sharpen: true, presetId: 9,
    presetName: "Velho" };
  const m1 = migrateFlowForm({ flow: "auto", media: "videos", autoExport: true, autoExportContent: "edited" },
    editProcess);
  assert.equal(m1.changed, true);
  assert.equal(m1.form.flow, "edit");
  assert.deepEqual(m1.form.edit, { durationMode: "60", customText: "", speed: 1.25, sharpen: true });
  assert.equal(m1.form.media, "videos"); // o resto é mantido
  assert.deepEqual(buildStartBody({ ...form(), ...m1.form }).edit, { duration_mode: "60", custom_seconds: null,
    speed: 1.25, sharpen: true, output_type: "mp4" });
  const m2 = migrateFlowForm({ flow: "auto" }, { ...editProcess, mode: "preset" });
  assert.equal(m2.form.flow, "preset");
  assert.equal(m2.form.presetId, 9);
  assert.equal(m2.form.presetName, "Velho");
  // Só `processStored` (formato do corpo) também serve.
  const m3 = migrateFlowForm({ flow: "auto" }, null, { mode: "edit", edit: { duration_mode: "custom",
    custom_seconds: 12.5, speed: 1.1, sharpen: false, output_type: "mp4" } });
  assert.deepEqual([m3.form.flow, m3.form.edit], ["edit", { durationMode: "custom", customText: "12,5", speed: 1.1,
    sharpen: false }]);
  assert.equal(migrateFlowForm({ flow: "auto" }, null, { mode: "preset", presetId: 3 }).form.flow, "preset");
  // Sem configuração da aba Processar: Editar após baixar com os padrões (bloqueia até escolher uma edição).
  const m4 = migrateFlowForm({ flow: "auto" });
  assert.deepEqual([m4.form.flow, m4.form.edit], ["edit", DEFAULT_EDIT]);
  // Formato atual: nada muda.
  const current = { flow: "download", edit: { ...EDIT_FORM }, presetId: null, presetName: null };
  assert.deepEqual(migrateFlowForm(current, editProcess), { form: current, changed: false });
  assert.equal(migrateFlowForm({ flow: "qualquer", edit: EDIT_FORM, presetId: null }).form.flow, "download");
  // Predefinição do usuário antiga.
  const old = { id: "user:1", name: "Meu", builtin: false, values: { media: "all", flow: "auto" } };
  assert.equal(migrateUserPreset(old, editProcess).values.flow, "edit");
  assert.equal(migrateUserPreset(old, { mode: "preset", presetId: 2 }).values.flow, "preset");
  assert.equal(migrateUserPreset(old, editProcess).values.media, "all");
  const fresh = { id: "user:2", name: "Novo", values: { flow: "edit" } };
  assert.equal(migrateUserPreset(fresh, editProcess), fresh);
});

test("vários links e perfis: prévia local e corpo", () => {
  const text = "@ana\nhttps://www.instagram.com/p/ABCDE12345/\nana\ninstagram.com/reel/XYZ12345/\n" +
    "https://example.com/x\nhttps://www.instagram.com/explore/";
  const c = classifyEntries(text);
  assert.deepEqual(c.profiles, ["ana"]);
  assert.deepEqual(c.links, ["https://www.instagram.com/p/ABCDE12345/", "https://www.instagram.com/reel/XYZ12345/"]);
  assert.equal(c.invalid.length, 2);
  assert.equal(c.duplicates, 1);
  const body = buildStartBody(form({ source: "many", text }));
  assert.deepEqual(body.inputs, ["ana", "https://www.instagram.com/p/ABCDE12345/",
    "https://www.instagram.com/reel/XYZ12345/"]);
  assert.equal(body.limit, 50);
  // só links: sem filtros de perfil no corpo
  const links = buildStartBody(form({ source: "many", text: "https://www.instagram.com/p/ABCDE12345/",
    period: "30", minViews: "1000" }));
  assert.deepEqual(Object.keys(links).sort(), ["auto_export", "inputs", "media", "post_processing"]);
  assert.throws(() => buildStartBody(form({ source: "many", text: "https://example.com/x vá!lido" })),
    /pelo menos um/);
});

test("perfil inválido é recusado", () => {
  assert.throws(() => buildStartBody(form({ profile: "" })), FormError);
  assert.throws(() => buildStartBody(form({ profile: "explore" })), FormError);
});

test("botões da publicação dizem a ação completa com edição ou preset", () => {
  assert.deepEqual(postActions("reel", "edit").map((a) => a.label), ["Baixar e editar vídeo", "Baixar capa",
    "Baixar tudo e editar (vídeo e capa)"]);
  assert.deepEqual(postActions("reel", "preset").map((a) => a.label), ["Baixar e processar vídeo", "Baixar capa",
    "Baixar tudo e processar (vídeo e capa)"]);
  assert.deepEqual(postActions("photo", "edit").map((a) => a.label), ["Baixar foto(s)"]);
  assert.deepEqual(postActions("carousel", "edit").map((a) => a.label), ["Baixar todos os itens e editar os vídeos",
    "Só fotos", "Só vídeos e editar", "Capas dos vídeos", "Vídeos e capas (editar os vídeos)"]);
  assert.deepEqual(postActions("reel", "manual").map((a) => a.label), ["Baixar vídeo", "Baixar capa",
    "Baixar tudo (vídeo e capa)"]);
  assert.deepEqual(postActions("reel", "edit").map((a) => a.media), ["videos", "covers", "all"]);
});

test("ações da publicação por tipo", () => {
  // Reel: sem "Vídeo e capa" (igual a "Baixar tudo"); carrossel: as opções próprias, inclusive "Vídeos e capas".
  assert.deepEqual(postActions("reel").map((a) => a.media), ["videos", "covers", "all"]);
  assert.deepEqual(postActions("reel").map((a) => a.label), ["Baixar vídeo", "Baixar capa",
    "Baixar tudo (vídeo e capa)"]);
  assert.deepEqual(postActions("video").map((a) => a.media), ["videos", "covers", "all"]);
  assert.deepEqual(postActions("photo").map((a) => a.media), ["photos"]);
  assert.deepEqual(postActions("carousel").map((a) => a.media), ["all", "photos", "videos", "covers", "videos_covers"]);
  assert.deepEqual(postActions("post").map((a) => a.media), ["all", "photos", "videos", "covers", "videos_covers"]);
});

test("corpo da publicação usa o fluxo guardado", () => {
  const url = "https://www.instagram.com/reel/ABCDE12345/";
  assert.deepEqual(buildPostBody(url, "videos_covers"), { inputs: [url], media: "videos_covers",
    post_processing: "none", auto_export: null });
  const flowForm = { flow: "edit", edit: EDIT_FORM, autoExport: true, autoExportContent: "edited" };
  const body = buildPostBody(url, "videos", flowForm, "id-1");
  assert.equal(body.post_processing, "edit");
  assert.deepEqual(body.edit, EDIT_SETTINGS);
  assert.deepEqual(body.auto_export, { content: "edited" });
  assert.equal(body.client_request_id, "id-1");
  assert.deepEqual(buildPostBody(url, "all", { flow: "preset", presetId: 5 }), { inputs: [url], media: "all",
    post_processing: "preset", preset_id: 5, auto_export: null });
  // Capa: só baixa, mesmo com "Editar após baixar" (e sem edição escolhida não bloqueia a capa).
  assert.deepEqual(buildPostBody(url, "covers", { flow: "edit", edit: DEFAULT_EDIT }), { inputs: [url], media: "covers",
    post_processing: "none", auto_export: null });
  assert.throws(() => buildPostBody(url, "videos", { flow: "edit", edit: DEFAULT_EDIT }), /Nenhuma edição/);
  assert.throws(() => buildPostBody("", "videos"), FormError);
});

test("predefinições: prontas, aplicar e salvar a atual", () => {
  const ids = BUILTIN_PRESETS.map((p) => p.name);
  assert.deepEqual(ids, ["Somente baixar", "Baixar e editar", "Baixar, editar e exportar ZIP", "Baixar e processar",
    "Baixar, processar e exportar", "Baixar tudo sem processamento"]);
  const start = form({ media: "videos", minViews: "1000", confirmAll: true });
  const all = applyPreset(start, BUILTIN_PRESETS.find((p) => p.id === "builtin:all-raw"));
  assert.equal(all.media, "all");
  assert.equal(all.flow, "download");
  assert.equal(all.profile, "@Fulano.Silva"); // origem mantida
  assert.equal(all.confirmAll, false);
  const byId = (id) => BUILTIN_PRESETS.find((p) => p.id === id);
  // "Baixar e editar" usa as últimas configurações de edição.
  const last = form({ edit: { ...EDIT_FORM }, autoExport: true });
  const be = applyPreset(last, byId("builtin:edit"));
  assert.deepEqual([be.flow, be.autoExport], ["edit", false]);
  assert.deepEqual(be.edit, EDIT_FORM);
  assert.deepEqual(buildStartBody(be).edit, EDIT_SETTINGS);
  const bez = applyPreset(last, byId("builtin:edit-export"));
  assert.deepEqual([bez.flow, bez.autoExport, bez.autoExportContent], ["edit", true, "both"]);
  assert.deepEqual(buildStartBody(bez).auto_export, { content: "both" });
  const pe = applyPreset(start, byId("builtin:process-export"));
  assert.equal(pe.flow, "preset");
  assert.deepEqual([pe.autoExport, pe.autoExportContent], [true, "edited"]);
  assert.equal(applyPreset(start, byId("builtin:process")).flow, "preset");
  // Predefinição do usuário guarda o fluxo com a edição e o preset.
  const saved = makeUserPreset("Editar 15", editing({ durationMode: "15", speed: 1, sharpen: false }), () => "user:9");
  assert.deepEqual([saved.values.flow, saved.values.edit.durationMode], ["edit", "15"]);
  assert.equal(applyPreset(form(), saved).edit.durationMode, "15");
  const mine = makeUserPreset("  Meu fluxo ", form({ media: "covers", period: "7", profile: "@x" }), () => "user:1");
  assert.equal(mine.name, "Meu fluxo");
  assert.equal(mine.values.media, "covers");
  assert.equal("profile" in mine.values, false);
  assert.throws(() => makeUserPreset(" ", form()), /nome/);
});

test("idempotência local: mesma id ao repetir o mesmo pedido; nova depois do sucesso", () => {
  let n = 0;
  const keeper = new RequestIdKeeper(() => `uuid-${++n}`);
  const body = buildStartBody(form());
  assert.equal(keeper.idFor(body), "uuid-1");
  assert.equal(keeper.idFor({ ...body, client_request_id: "uuid-1" }), "uuid-1"); // clique repetido / nova tentativa
  assert.equal(keeper.idFor({ ...body, limit: 10 }), "uuid-2"); // pedido diferente
  keeper.succeeded();
  assert.equal(keeper.idFor({ ...body, limit: 10 }), "uuid-3");
});

test("resumo e confirmação de operação grande", () => {
  const body = buildStartBody(form({ media: "covers", period: "30", minViews: "10000", autoExport: true }));
  assert.equal(describeStartBody(body), "Somente capas · até 50 por perfil · últimos 30 dias · mín. 10 mil " +
    "visualizações · ZIP automático (originais)");
  assert.equal(needsConfirmation(body), null);
  assert.match(needsConfirmation({ inputs: ["a", "b", "c"], limit: 500 }), /1\.500/);
  assert.equal(needsConfirmation({ inputs: ["a"], limit: null, confirm_all: true }), null);
});
