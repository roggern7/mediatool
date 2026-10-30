import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BUILTIN_PRESETS, DEFAULT_FORM, FormError, RequestIdKeeper, applyPreset, buildPostBody, buildStartBody,
  classifyEntries, daysSinceYearStart, describeStartBody, makeUserPreset, needsConfirmation, periodDays, postActions,
  viewsLabel, viewsRange,
} from "../src/lib/importModel.js";

const EDIT = { mode: "edit", edit: { duration_mode: "30", custom_seconds: null, speed: 1.1, sharpen: true,
  output_type: "mp4" } };
const form = (over = {}) => ({ ...DEFAULT_FORM, profile: "@Fulano.Silva", ...over });

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
  const body = buildStartBody(form({ period: "year" }), null, { now: new Date(2026, 2, 10) });
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

test("fluxos: só baixar, processar (edição e preset), edição manual", () => {
  assert.equal(buildStartBody(form({ flow: "download" })).post_processing, "none");
  assert.equal(buildStartBody(form({ flow: "manual" })).post_processing, "none");
  const edit = buildStartBody(form({ flow: "auto" }), EDIT);
  assert.equal(edit.post_processing, "edit");
  assert.deepEqual(edit.edit, EDIT.edit);
  const preset = buildStartBody(form({ flow: "auto" }), { mode: "preset", presetId: 4 });
  assert.equal(preset.post_processing, "preset");
  assert.equal(preset.preset_id, 4);
  assert.throws(() => buildStartBody(form({ flow: "auto" }), null), /Processar/);
});

test("exportação automática", () => {
  assert.deepEqual(buildStartBody(form({ autoExport: true })).auto_export, { content: "originals" });
  assert.deepEqual(buildStartBody(form({ flow: "auto", autoExport: true, autoExportContent: "both" }), EDIT)
    .auto_export, { content: "both" });
  assert.throws(() => buildStartBody(form({ autoExport: true, autoExportContent: "edited" })), /editados/);
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
  const body = buildPostBody(url, "videos", { flow: "auto", autoExport: true, autoExportContent: "edited" }, EDIT, "id-1");
  assert.equal(body.post_processing, "edit");
  assert.deepEqual(body.auto_export, { content: "edited" });
  assert.equal(body.client_request_id, "id-1");
  assert.throws(() => buildPostBody("", "videos"), FormError);
});

test("predefinições: prontas, aplicar e salvar a atual", () => {
  const ids = BUILTIN_PRESETS.map((p) => p.name);
  assert.deepEqual(ids, ["Somente baixar", "Baixar e processar", "Baixar, processar e exportar",
    "Baixar tudo sem processamento"]);
  const start = form({ media: "videos", minViews: "1000", confirmAll: true });
  const all = applyPreset(start, BUILTIN_PRESETS[3]);
  assert.equal(all.media, "all");
  assert.equal(all.flow, "download");
  assert.equal(all.profile, "@Fulano.Silva"); // origem mantida
  assert.equal(all.confirmAll, false);
  const pe = applyPreset(start, BUILTIN_PRESETS[2]);
  assert.equal(pe.flow, "auto");
  assert.deepEqual([pe.autoExport, pe.autoExportContent], [true, "edited"]);
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
