import assert from "node:assert/strict";
import { test } from "node:test";

// chrome.storage.local falso: guarda cópias, como o real (JSON).
const store = {};
globalThis.chrome = {
  storage: {
    local: {
      async get(keys) {
        const out = {};
        for (const key of keys) if (key in store) out[key] = JSON.parse(JSON.stringify(store[key]));
        return out;
      },
      async set(values) {
        for (const [key, value] of Object.entries(values)) store[key] = JSON.parse(JSON.stringify(value));
      },
      async remove(keys) {
        for (const key of keys) delete store[key];
      },
    },
  },
};

const { loadFlowForm, saveFlowFields } = await import("../src/lib/settings.js");

test("configuração salva com o antigo \"auto\" é migrada e gravada (Importar e Página atual leem a mesma)", async () => {
  store.importForm = { media: "videos", limit: "100", flow: "auto", autoExport: true, autoExportContent: "edited" };
  store.processForm = { mode: "edit", durationMode: "15", customText: "", speed: 1.05, sharpen: true, presetId: null };
  const form = await loadFlowForm();
  assert.equal(form.flow, "edit");
  assert.deepEqual(form.edit, { durationMode: "15", customText: "", speed: 1.05, sharpen: true });
  assert.equal(form.limit, "100");
  assert.equal(store.importForm.flow, "edit"); // gravado: a próxima leitura já vem no formato novo
  assert.deepEqual(store.processForm.durationMode, "15"); // a aba Processar continua com a configuração dela
});

test("o bloco da Página atual grava só o fluxo na mesma configuração", async () => {
  store.importForm = { media: "covers", limit: "10", flow: "download", edit: { durationMode: "original",
    customText: "", speed: 1, sharpen: false }, presetId: null, presetName: null, autoExport: false };
  await saveFlowFields({ flow: "edit", edit: { durationMode: "30", customText: "", speed: 1.1, sharpen: false },
    profile: "@nao-grava" });
  assert.equal(store.importForm.flow, "edit");
  assert.equal(store.importForm.edit.durationMode, "30");
  assert.equal(store.importForm.media, "covers");
  assert.equal(store.importForm.limit, "10");
  assert.equal("profile" in store.importForm, false);
  const again = await loadFlowForm();
  assert.deepEqual([again.flow, again.edit.speed], ["edit", 1.1]);
});

test("preset salvo na aba Processar vira \"Processar com preset\"", async () => {
  store.importForm = { flow: "auto" };
  store.processForm = { mode: "preset", presetId: 4, presetName: "Leve" };
  const form = await loadFlowForm();
  assert.deepEqual([form.flow, form.presetId, form.presetName], ["preset", 4, "Leve"]);
});
