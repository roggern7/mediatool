import assert from "node:assert/strict";
import { test } from "node:test";

import { ExtApiError } from "../src/lib/api.js";
import {
  FormError, buildPostBody, buildProfileBody, describeError, summarizeProgress, viewsLabel,
} from "../src/lib/popupModel.js";

const URL_POST = "https://www.instagram.com/p/ABCDE12345/";

test("corpo das ações de publicação", () => {
  assert.deepEqual(buildPostBody(URL_POST, "videos"), { inputs: [URL_POST], media: "videos", post_processing: "none" });
  assert.equal(buildPostBody(URL_POST, "covers").media, "covers");
  assert.equal(buildPostBody(URL_POST, "all").media, "all");
  assert.throws(() => buildPostBody(URL_POST, "x"), FormError);
  assert.throws(() => buildPostBody("", "videos"), FormError);
});

test("perfil: padrão e presets", () => {
  assert.deepEqual(buildProfileBody("fulano", { media: "videos", limit: "50", period: "any" }),
    { inputs: ["fulano"], media: "videos", post_processing: "none", limit: 50 });
  const body = buildProfileBody("fulano", { media: "covers", limit: "10", period: "30", minViews: "10000",
    maxViews: "1000000" });
  assert.equal(body.posted_within_days, 30);
  assert.equal(body.min_views, 10000);
  assert.equal(body.max_views, 1000000);
});

test("perfil: personalizados", () => {
  const body = buildProfileBody("fulano", { media: "videos", limit: "custom", limitCustom: "250", period: "custom",
    periodCustom: "45", minViews: "custom", minViewsCustom: "1234" });
  assert.equal(body.limit, 250);
  assert.equal(body.posted_within_days, 45);
  assert.equal(body.min_views, 1234);
  assert.throws(() => buildProfileBody("fulano", { limit: "custom", limitCustom: "0" }), FormError);
  assert.throws(() => buildProfileBody("fulano", { limit: "10", period: "custom", periodCustom: "4000" }), FormError);
});

test("perfil: Todas exige confirmação", () => {
  assert.throws(() => buildProfileBody("fulano", { media: "videos", limit: "all" }), /Confirme/);
  const body = buildProfileBody("fulano", { media: "videos", limit: "all", confirmAll: true });
  assert.equal(body.limit, null);
  assert.equal(body.confirm_all, true);
});

test("perfil: visualizações ignoradas com Mídia Todas e máx >= mín", () => {
  const body = buildProfileBody("fulano", { media: "all", limit: "10", minViews: "1000", maxViews: "10000" });
  assert.equal(body.min_views, undefined);
  assert.equal(body.max_views, undefined);
  assert.throws(() => buildProfileBody("fulano", { media: "videos", limit: "10", minViews: "50000", maxViews: "1000" }),
    /maior ou igual/);
});

test("estados de erro do popup", () => {
  assert.equal(describeError(new ExtApiError("offline", "off")).state, "offline");
  assert.equal(describeError(new ExtApiError("timeout", "t")).state, "offline");
  assert.equal(describeError(new ExtApiError("unauthorized", "u", { status: 401 })).state, "unpaired");
  assert.equal(describeError(new ExtApiError("unauthorized", "u", { status: 401, code: "extension_unauthorized" })).state,
    "revoked");
  const http = describeError(new ExtApiError("http", "Sessão ausente", { status: 409, code: "session_missing" }));
  assert.deepEqual(http, { state: "error", message: "Sessão ausente" });
  assert.equal(describeError(new FormError("x")).state, "error");
});

test("resumo do andamento", () => {
  const lines = summarizeProgress({
    group: null,
    batches: [{ id: 3, profile: "fulano", status: "running", found: 10, downloaded: 4, duplicates: 2, filtered: 1,
      failed: 0, finished: false, open_url: "http://127.0.0.1:8765/fila?lote=3" }],
  });
  assert.equal(lines.length, 1);
  assert.match(lines[0].title, /@fulano/);
  assert.equal(lines[0].detail, "10 encontradas · 4 baixadas · 2 já existentes · 1 filtradas");
  assert.equal(lines[0].openUrl, "http://127.0.0.1:8765/fila?lote=3");
  assert.deepEqual(summarizeProgress(null), []);
});

test("rótulos de visualizações", () => {
  assert.equal(viewsLabel(1000), "1 mil");
  assert.equal(viewsLabel(500000), "500 mil");
  assert.equal(viewsLabel(1000000), "1 mi");
});
