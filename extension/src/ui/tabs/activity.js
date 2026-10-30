// Aba "Atividade": tudo vem de GET /api/ext/activity (nada depende do popup aberto). Consulta a cada 2,5 s só
// enquanto a aba está visível e há algo em andamento; o estado mostrado é sempre o da API.
import {
  CHILD_PAGE, autoExportText, childRefreshLimit, childrenCountText, counterList, filtersText, isActive, itemMessage,
  itemTone, mediaSetLabel, mergeChildPage, progressFraction, shouldKeepPolling, stateLabel, stateTone,
} from "../../lib/activityModel.js";
import { badge, button, confirmBox, empty, formatDate, h, notice, progressBar, setChildren } from "../dom.js";

const POLL_MS = 2_500;
const IDLE_POLL_MS = 15_000;

export function activityTab(panel, ctx) {
  let items = null;
  let error = null;
  let timer = null;
  let visible = false;
  let loading = false;
  const expanded = new Map(); // "batch:id" → {items, error}; "group:id" → {children, nextAfter, total, error}
  const childJobs = new Map(); // id do lote filho de um grupo → {items, error}
  const listEl = h("div", { class: "list", id: "activity-list" });
  const msgEl = h("div", { class: "msg" });
  const head = h("div", { class: "row between" }, h("p", { class: "section-title", text: "Atividade recente" }),
    h("div", { class: "row" }, button("Atualizar", () => void refresh(), "btn small"),
      button("Abrir a Fila no Media Tool", () => ctx.openApp("/fila"), "btn small")));
  setChildren(panel, head, msgEl, listEl);

  function schedule() {
    clearTimeout(timer);
    if (!visible) return;
    timer = setTimeout(() => void refresh(), shouldKeepPolling(items) ? POLL_MS : IDLE_POLL_MS);
  }

  async function refresh() {
    if (loading || !ctx.client) return;
    loading = true;
    try {
      const data = await ctx.client.activity(20);
      items = data.items || [];
      error = null;
      ctx.setConnected();
      // Lotes/grupos expandidos ainda em andamento: atualiza a lista de itens (ou de perfis) também.
      await Promise.all([...expanded.keys()].map((key) => {
        const item = items.find((i) => keyOf(i) === key);
        const entry = expanded.get(key);
        const loaded = item && item.kind === "group" ? entry.children : entry.items;
        return item && (isActive(item) || !loaded) ? loadItems(item, false) : null;
      }));
      // Perfis de grupo com os itens abertos: atualiza os que ainda estão em andamento.
      await Promise.all([...childJobs.keys()].map((id) => {
        const child = findChild(id);
        return child && (isActive(child) || !childJobs.get(id).items) ? loadChildJobs(id, false) : null;
      }));
    } catch (err) {
      error = ctx.handleError(err);
    } finally {
      loading = false;
    }
    render();
    schedule();
  }

  const keyOf = (item) => `${item.kind}:${item.id}`;

  function findChild(batchId) {
    for (const [key, entry] of expanded) {
      if (!key.startsWith("group:") || !entry.children) continue;
      const child = entry.children.find((c) => c.id === batchId);
      if (child) return child;
    }
    return null;
  }

  /** Perfis do grupo: atualização do começo da lista aberta, ou a próxima página (`append`). */
  async function loadChildren(item, { append = false, rerender = true } = {}) {
    const key = keyOf(item);
    const entry = expanded.get(key) ?? {};
    try {
      const data = await ctx.client.groupChildren(item.id, append
        ? { afterPosition: entry.nextAfter, limit: CHILD_PAGE } : { limit: childRefreshLimit(entry) });
      if (expanded.has(key)) expanded.set(key, mergeChildPage(expanded.get(key), data, append));
    } catch (err) {
      if (expanded.has(key)) expanded.set(key, { ...expanded.get(key), error: ctx.handleError(err) });
    }
    if (rerender) render();
  }

  async function loadChildJobs(batchId, rerender = true) {
    const entry = childJobs.get(batchId) ?? {};
    try {
      const data = await ctx.client.batchItems(batchId, { limit: 50 });
      if (childJobs.has(batchId)) childJobs.set(batchId, { items: data.items || [], error: null });
    } catch (err) {
      if (childJobs.has(batchId)) childJobs.set(batchId, { ...entry, error: ctx.handleError(err) });
    }
    if (rerender) render();
  }

  async function loadItems(item, rerender = true) {
    const key = keyOf(item);
    const entry = expanded.get(key) ?? {};
    if (item.kind === "group") {
      await loadChildren(item, { rerender });
      return;
    }
    try {
      const data = await ctx.client.batchItems(item.id, { limit: 50 });
      expanded.set(key, { items: data.items || [], error: null });
    } catch (err) {
      expanded.set(key, { ...entry, error: ctx.handleError(err) });
    }
    if (rerender) render();
  }

  async function cancel(item) {
    const ok = await confirmBox("Interromper esta importação?", "Os itens já baixados continuam na Biblioteca.",
      "Interromper");
    if (!ok) return;
    try {
      await ctx.client.cancel(item.kind, item.id);
      setChildren(msgEl, notice("ok", "Pedido de interrupção enviado."));
    } catch (err) {
      const text = ctx.handleError(err);
      if (text) setChildren(msgEl, notice("err", text));
    }
    void refresh();
  }

  function renderJobs(entry) {
    if (!entry) return null;
    if (entry.error) return notice("err", entry.error);
    if (entry.note) return h("p", { class: "muted small", text: entry.note });
    if (!entry.items) return h("p", { class: "muted small", text: "Carregando itens…" });
    if (!entry.items.length) return h("p", { class: "muted small", text: "Nenhum item ainda." });
    return h("ul", { class: "jobs" }, ...entry.items.map((job) => {
      const thumb = h("div", { class: "thumb sm" });
      if (job.media_id) {
        const img = h("img", { alt: "" });
        thumb.append(img);
        ctx.thumbs.load(job.media_id, img);
      }
      const what = job.role === "cover" ? "Capa" : job.media_type === "image" ? "Foto" : job.media_type === "video"
        ? "Vídeo" : "Item";
      return h("li", { class: "job" }, thumb,
        h("div", { class: "grow" }, h("div", { text: `${what}${job.index !== null && job.index !== undefined
          ? ` ${Number(job.index) + 1}` : ""}${job.source_post_id ? ` · ${job.source_post_id}` : ""}` }),
        job.error_message ? h("div", { class: "small err-text", text: job.error_message }) : null),
        badge(job.stage_label ?? "", itemTone(job.stage_label)));
    }));
  }

  function renderItem(item) {
    const key = keyOf(item);
    const open = expanded.has(key);
    const fraction = progressFraction(item);
    const p = item.progress || {};
    const meta = [mediaSetLabel(item.media_set), filtersText(item.filters), formatDate(item.created_at)]
      .filter(Boolean).join(" · ");
    const ae = item.auto_export;
    const note = itemMessage(item);
    const group = item.kind === "group";
    const actions = [
      button(group ? (open ? "Ocultar perfis" : "Ver perfis") : (open ? "Ocultar itens" : "Ver itens"), () => {
        if (open) {
          for (const child of expanded.get(key)?.children ?? []) childJobs.delete(child.id);
          expanded.delete(key);
        } else {
          expanded.set(key, {});
          void loadItems(item);
        }
        render();
      }, "btn small"),
      item.open_url ? button("Abrir no Media Tool", () => ctx.openApp(item.open_url), "btn small") : null,
      isActive(item) ? button("Interromper", () => void cancel(item), "btn small danger") : null,
      item.state === "done" || item.state === "partial"
        ? button("Exportar", () => ctx.go("export", { source: { kind: item.kind, id: item.id, title: item.title } }),
          "btn small") : null,
      item.kind === "batch" && (item.state === "done" || item.state === "partial")
        ? button("Processar", () => ctx.go("library", { batch_id: item.id }), "btn small") : null,
    ];
    return h("article", { class: "item", dataset: { id: key, state: item.state } },
      h("div", { class: "row between" },
        h("div", { class: "grow" }, h("p", { class: "item-title", text: item.title || `Lote ${item.id}` }),
          h("p", { class: "muted small", text: meta })),
        badge(stateLabel(item.state), stateTone(item.state))),
      progressBar(fraction),
      note ? notice(note.tone, note.text) : null,
      h("p", { class: "counters small" }, counterList(p).map((c) => `${c.value} ${c.label}`).join(" · ")),
      p.processing || p.pending ? h("p", { class: "muted small",
        text: `${p.processing ? `${p.processing} processando` : ""}${p.processing && p.pending ? " · " : ""}` +
          `${p.pending ? `${p.pending} na fila` : ""}` }) : null,
      ae ? h("div", { class: "row between auto-export" }, h("span", { class: "small", text: autoExportText(ae) }),
        ae.download_url ? button("Baixar ZIP", () => ctx.openApp(ae.download_url), "btn small primary") : null)
        : null,
      h("div", { class: "row wrap" }, ...actions),
      open ? (group ? renderChildren(item, expanded.get(key)) : renderJobs(expanded.get(key))) : null);
  }

  function renderChildren(item, entry) {
    if (!entry) return null;
    const error = entry.error ? notice("err", entry.error) : null;
    if (!entry.children) return error ?? h("p", { class: "muted small", text: "Carregando perfis…" });
    if (!entry.children.length) return error ?? h("p", { class: "muted small", text: "Nenhum perfil neste grupo." });
    return h("div", { class: "children" },
      error,
      h("p", { class: "muted small", text: childrenCountText(entry) }),
      ...entry.children.map(renderChild),
      entry.nextAfter !== null && entry.nextAfter !== undefined
        ? button("Carregar mais perfis", () => void loadChildren(item, { append: true }), "btn small") : null);
  }

  function renderChild(child) {
    const open = childJobs.has(child.id);
    const note = itemMessage(child);
    return h("div", { class: "child", dataset: { id: `batch:${child.id}`, state: child.state } },
      h("div", { class: "row between" },
        h("span", { class: "grow item-title", text: child.title || `Lote ${child.id}` }),
        badge(stateLabel(child.state), stateTone(child.state))),
      progressBar(progressFraction(child)),
      note ? notice(note.tone, note.text) : null,
      h("p", { class: "counters small" }, counterList(child.progress).map((c) => `${c.value} ${c.label}`).join(" · ")),
      h("div", { class: "row wrap" },
        button(open ? "Ocultar itens" : "Ver itens", () => {
          if (open) childJobs.delete(child.id);
          else {
            childJobs.set(child.id, {});
            void loadChildJobs(child.id);
          }
          render();
        }, "btn small"),
        child.open_url ? button("Abrir no Media Tool", () => ctx.openApp(child.open_url), "btn small") : null),
      open ? renderJobs(childJobs.get(child.id)) : null);
  }

  function render() {
    if (error) setChildren(msgEl, notice("err", error, button("Tentar de novo", () => void refresh(), "btn small")));
    else msgEl.replaceChildren();
    if (items === null) {
      setChildren(listEl, h("p", { class: "muted", text: "Carregando atividade…" }));
      return;
    }
    if (!items.length) {
      setChildren(listEl, empty("Nenhuma atividade recente", "Importações, edições e exportações iniciadas pela " +
        "extensão ou pelo app aparecem aqui.", button("Importar", () => ctx.go("import"), "btn")));
      return;
    }
    setChildren(listEl, ...items.map(renderItem));
  }

  render();
  return {
    show() {
      visible = true;
      void refresh();
    },
    hide() {
      visible = false;
      clearTimeout(timer);
    },
  };
}
