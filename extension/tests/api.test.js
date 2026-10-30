import assert from "node:assert/strict";
import { test } from "node:test";

import { ExtApiError, MESSAGES, baseUrl, createClient, normalizePort } from "../src/lib/api.js";

function fakeFetch(handler) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    return handler(url, init);
  };
  return { calls, impl };
}

function json(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

test("porta e URL base", () => {
  assert.equal(normalizePort("8790"), 8790);
  assert.equal(normalizePort("80"), null);
  assert.equal(normalizePort("abc"), null);
  assert.equal(baseUrl(8790), "http://127.0.0.1:8790");
  assert.equal(baseUrl("x"), "http://127.0.0.1:8765");
});

test("envia o token no Authorization e o header do cliente nas mutações", async () => {
  const { calls, impl } = fakeFetch(() => json(200, { kind: "post" }));
  const client = createClient({ port: 8790, token: "TOKEN-FALSO", fetchImpl: impl });
  await client.pageInfo("https://www.instagram.com/p/ABCDE12345/");
  const { url, init } = calls[0];
  assert.equal(url, "http://127.0.0.1:8790/api/ext/page-info");
  assert.equal(init.method, "POST");
  assert.equal(init.headers.Authorization, "Bearer TOKEN-FALSO");
  assert.equal(init.headers["X-Media-Tool-Client"], "extension");
  assert.equal(init.credentials, "omit");
  assert.deepEqual(JSON.parse(init.body), { url: "https://www.instagram.com/p/ABCDE12345/" });
});

test("pareamento não envia token", async () => {
  const { calls, impl } = fakeFetch(() => json(200, { token: "t", device_id: "d" }));
  await createClient({ fetchImpl: impl }).pair("ABCD-EFGH", "Chrome");
  assert.equal(calls[0].init.headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(calls[0].init.body), { code: "ABCD-EFGH", device_name: "Chrome" });
});

test("sem token: erro de não pareado, sem requisição", async () => {
  const { calls, impl } = fakeFetch(() => json(200, {}));
  await assert.rejects(createClient({ fetchImpl: impl }).status(),
    (err) => err instanceof ExtApiError && err.kind === "unauthorized" && err.code === null);
  assert.equal(calls.length, 0);
});

test("401 vira estado de token revogado", async () => {
  const { impl } = fakeFetch(() => json(401, { detail: "x", code: "extension_unauthorized" }));
  await assert.rejects(createClient({ token: "t", fetchImpl: impl }).status(),
    (err) => err.kind === "unauthorized" && err.code === "extension_unauthorized" && err.message === MESSAGES.unauthorized);
});

test("4xx devolve a mensagem do backend", async () => {
  const { impl } = fakeFetch(() => json(409, { detail: "Configure a sessão do Instagram.", code: "session_missing" }));
  await assert.rejects(createClient({ token: "t", fetchImpl: impl }).start({ inputs: ["fulano"] }),
    (err) => err.kind === "http" && err.status === 409 && err.message === "Configure a sessão do Instagram.");
  const { impl: impl422 } = fakeFetch(() => json(422, { detail: [{ loc: ["body", "limit"], msg: "valor inválido" }] }));
  await assert.rejects(createClient({ token: "t", fetchImpl: impl422 }).start({}),
    (err) => err.message === "limit: valor inválido");
});

test("app desligado vira offline; demora vira timeout", async () => {
  const offline = createClient({ token: "t", fetchImpl: async () => { throw new TypeError("Failed to fetch"); } });
  await assert.rejects(offline.status(), (err) => err.kind === "offline" && err.message === MESSAGES.offline);
  const slow = createClient({
    token: "t", timeoutMs: 10,
    fetchImpl: (_url, init) => new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(new Error("abort")));
    }),
  });
  await assert.rejects(slow.status(), (err) => err.kind === "timeout");
});

test("rotas novas: query, método e caminho", async () => {
  const { calls, impl } = fakeFetch(() => json(200, { items: [] }));
  const client = createClient({ token: "t", fetchImpl: impl });
  await client.activity(20);
  await client.batchItems(7, { beforeId: 100, limit: 50 });
  await client.library({ media_type: "video", role: "", profile: null, before_id: 55, limit: 30 });
  await client.libraryIds({ batch_id: 7 });
  await client.cancel("group", 3);
  await client.cancel("batch", 4);
  await client.editOptions();
  await client.edits({ media_ids: [1], settings: {} });
  await client.process([1, 2], 9);
  await client.createExport({ source: { kind: "batch", id: 7 }, content: "both" });
  await client.exportStatus(12);
  await client.postPreview("https://www.instagram.com/p/ABCDE12345/");
  const got = calls.map((c) => `${c.init.method} ${c.url.replace("http://127.0.0.1:8765", "")}`);
  assert.deepEqual(got, [
    "GET /api/ext/activity?limit=20",
    "GET /api/ext/batches/7/items?before_id=100&limit=50",
    "GET /api/ext/library?media_type=video&before_id=55&limit=30",
    "GET /api/ext/library/ids?batch_id=7",
    "POST /api/ext/groups/3/cancel",
    "POST /api/ext/batches/4/cancel",
    "GET /api/ext/edit/options",
    "POST /api/ext/edits",
    "POST /api/ext/process",
    "POST /api/ext/exports",
    "GET /api/ext/exports/12",
    "POST /api/ext/post-preview",
  ]);
  assert.deepEqual(JSON.parse(calls[8].init.body), { media_ids: [1, 2], preset_id: 9 });
  assert.equal(calls[0].init.headers["X-Media-Tool-Client"], undefined);
  assert.equal(calls[4].init.headers["X-Media-Tool-Client"], "extension");
});

test("perfis de um grupo: rota paginada com token", async () => {
  const { calls, impl } = fakeFetch(() => json(200, { items: [], total: 0, next_after_position: null }));
  const client = createClient({ token: "t", fetchImpl: impl });
  await client.groupChildren(5);
  await client.groupChildren(5, { afterPosition: 20, limit: 20 });
  assert.deepEqual(calls.map((c) => `${c.init.method} ${c.url.replace("http://127.0.0.1:8765", "")}`), [
    "GET /api/ext/groups/5/children?limit=20",
    "GET /api/ext/groups/5/children?after_position=20&limit=20",
  ]);
  assert.equal(calls[0].init.headers.Authorization, "Bearer t");
});

test("429 vira rate_limited com Retry-After", async () => {
  const impl = async () => ({ ok: false, status: 429, headers: { get: (k) => (k === "Retry-After" ? "7" : null) },
    json: async () => ({ detail: "Calma.", code: "rate_limited" }) });
  await assert.rejects(createClient({ token: "t", fetchImpl: impl }).activity(),
    (err) => err.kind === "rate_limited" && err.retryAfter === 7 && err.message === "Calma.");
});

test("miniatura volta como Blob, com token, sem expor URL com token", async () => {
  const { calls, impl } = fakeFetch(() => ({ ok: true, status: 200, blob: async () => "BLOB" }));
  const blob = await createClient({ token: "SEGREDO", fetchImpl: impl }).thumbnail(42);
  assert.equal(blob, "BLOB");
  assert.equal(calls[0].url, "http://127.0.0.1:8765/api/ext/media/42/thumbnail");
  assert.ok(!calls[0].url.includes("SEGREDO"));
  assert.equal(calls[0].init.headers.Authorization, "Bearer SEGREDO");
});
