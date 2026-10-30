// Aba "Exportar": fonte (seleção da Biblioteca, um lote ou um grupo da Atividade) + conteúdo → UM ZIP por pedido
// (POST /api/ext/exports). O andamento é recuperado ao reabrir (a última exportação fica guardada).
import { EXPORT_CONTENT_OPTIONS } from "../../lib/importModel.js";
import { buildExportBody, exportFinished, exportStatusText, formatBytes } from "../../lib/exportModel.js";
import { selectionText } from "../../lib/selection.js";
import { loadUiState, saveUiState } from "../../lib/settings.js";
import { button, checkbox, confirmBox, field, h, notice, progressBar, select, setChildren } from "../dom.js";

const POLL_MS = 2_000;

export function exportTab(panel, ctx) {
  let sourceKind = "library";
  let sourceId = "";
  let sourceTitle = "";
  let content = "originals";
  let coversOnly = false;
  let current = null; // {id, status}
  let timer = null;
  let visible = false;
  let sending = false;
  let recent = [];
  const msgEl = h("div", { class: "msg" });
  const formEl = h("div", { class: "stack" });
  const statusEl = h("div", { class: "stack", id: "export-status" });
  setChildren(panel, h("p", { class: "section-title", text: "Exportar ZIP" }), msgEl, formEl, statusEl);

  async function loadRecent() {
    try {
      const data = await ctx.client.activity(20);
      recent = (data.items || []).filter((i) => i.state === "done" || i.state === "partial");
    } catch {
      recent = [];
    }
  }

  function render() {
    const kindSel = select("sourceKind", [{ value: "library", label: "Selecionados da Biblioteca" },
      { value: "activity", label: "Um lote ou grupo da Atividade" }], sourceKind === "library" ? "library" : "activity");
    kindSel.addEventListener("change", () => {
      sourceKind = kindSel.value === "library" ? "library" : "batch";
      render();
    });
    const parts = [field("Fonte", kindSel)];
    if (sourceKind === "library") {
      const covers = checkbox("coversOnly", "Só capas (filtra a seleção pelas capas)", coversOnly);
      covers.input.addEventListener("change", () => {
        coversOnly = covers.input.checked;
        render();
      });
      const nCovers = ctx.selection.coverIds().length;
      parts.push(h("p", { class: "small", text: selectionText(ctx.selection) }), covers.el,
        coversOnly ? h("p", { class: "hint", text: `${nCovers} capa${nCovers === 1 ? "" : "s"} na seleção.` }) : null,
        ctx.selection.size ? null : button("Escolher na Biblioteca", () => ctx.go("library"), "btn small"));
    } else {
      const opts = recent.map((i) => ({ value: `${i.kind}:${i.id}`, label: `${i.title || `Lote ${i.id}`}` }));
      if (sourceId && !opts.some((o) => o.value === `${sourceKind}:${sourceId}`)) {
        opts.unshift({ value: `${sourceKind}:${sourceId}`, label: sourceTitle || `${sourceKind === "group" ? "Grupo" :
          "Lote"} ${sourceId}` });
      }
      const pick = select("activitySource", [{ value: "", label: opts.length ? "Escolher…" : "Nenhum lote concluído" },
        ...opts], sourceId ? `${sourceKind}:${sourceId}` : "");
      pick.addEventListener("change", () => {
        const [kind, id] = pick.value.split(":");
        sourceKind = kind === "group" ? "group" : "batch";
        sourceId = id || "";
        sourceTitle = pick.selectedOptions[0]?.textContent ?? "";
      });
      parts.push(field("Lote ou grupo", pick));
    }
    const contentSel = select("content", EXPORT_CONTENT_OPTIONS, content);
    contentSel.addEventListener("change", () => {
      content = contentSel.value;
    });
    parts.push(field("Conteúdo", contentSel, "Capas entram como originais, com _capa no nome do arquivo."),
      h("div", { class: "row" }, button(sending ? "Enviando…" : "Gerar ZIP", () => void start(), "btn primary",
        { disabled: sending, id: "export-start" })),
      h("p", { class: "muted small", text: "Sempre um único ZIP por pedido." }));
    setChildren(formEl, ...parts);
  }

  async function start() {
    if (sending) return;
    let body;
    try {
      const source = sourceKind === "library"
        ? { kind: "library", mediaIds: ctx.selection.ids(), coversOnly, coverIds: ctx.selection.coverIds() }
        : { kind: sourceKind, id: sourceId };
      body = buildExportBody(source, content);
    } catch (err) {
      setChildren(msgEl, notice("err", err.message));
      return;
    }
    const n = body.source.media_ids?.length ?? 0;
    if (n > 200 && !(await confirmBox(`Exportar ${n} itens num ZIP?`, "Pode demorar e ocupar bastante espaço.",
      "Gerar ZIP"))) return;
    sending = true;
    msgEl.replaceChildren();
    render();
    try {
      const result = await ctx.client.createExport(body);
      current = { id: result.id, status: null };
      await saveUiState("lastExport", { id: result.id });
      await poll();
    } catch (err) {
      const text = ctx.handleError(err);
      if (text) setChildren(msgEl, notice("err", text));
    } finally {
      sending = false;
      render();
    }
  }

  async function poll() {
    clearTimeout(timer);
    if (!current || !ctx.client) return;
    try {
      current.status = await ctx.client.exportStatus(current.id);
    } catch (err) {
      const text = ctx.handleError(err);
      if (text) setChildren(msgEl, notice("err", text));
      if (err?.status === 404) {
        current = null;
        await saveUiState("lastExport", null);
      }
    }
    renderStatus();
    if (current && visible && !exportFinished(current.status)) timer = setTimeout(() => void poll(), POLL_MS);
  }

  function renderStatus() {
    if (!current) {
      statusEl.replaceChildren();
      return;
    }
    const s = current.status;
    const fraction = s?.zip_ready ? 1 : s?.total ? (s.processed ?? 0) / s.total : null;
    setChildren(statusEl, h("hr"), h("p", { class: "box-title", text: `Exportação ${current.id}` }),
      progressBar(fraction),
      h("p", { class: `small${s?.status === "failed" ? " err-text" : ""}`, id: "export-text",
        text: s ? `${exportStatusText(s)}${s.zip_bytes ? ` · ${formatBytes(s.zip_bytes)}` : ""}` : "Consultando…" }),
      h("div", { class: "row" },
        s?.zip_ready && s.download_url ? button("Baixar ZIP", () => ctx.openApp(s.download_url), "btn primary",
          { id: "download-zip" }) : null,
        exportFinished(s) ? button("Limpar", async () => {
          current = null;
          await saveUiState("lastExport", null);
          renderStatus();
        }, "btn small") : null));
  }

  return {
    async show(params = {}) {
      visible = true;
      if (params.source) {
        if (params.source.kind === "library") sourceKind = "library";
        else {
          sourceKind = params.source.kind === "group" ? "group" : "batch";
          sourceId = String(params.source.id);
          sourceTitle = params.source.title ?? "";
        }
      }
      await loadRecent();
      render();
      if (!current) {
        const last = await loadUiState("lastExport", null);
        if (last && last.id) current = { id: last.id, status: null };
      }
      void poll();
    },
    hide() {
      visible = false;
      clearTimeout(timer);
    },
    onSelection() {
      if (visible) render();
    },
  };
}
