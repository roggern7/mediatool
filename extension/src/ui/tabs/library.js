// Aba "Biblioteca": GET /api/ext/library paginado (keyset), filtros, miniaturas via blob URL, seleção múltipla
// (persistida) e ações: Processar, Exportar, Abrir no Media Tool.
import { selectionText } from "../../lib/selection.js";
import { badge, button, empty, field, formatDay, h, notice, select, setChildren } from "../dom.js";

const PAGE = 30;
const TYPE_OPTIONS = [{ value: "", label: "Todos" }, { value: "video", label: "Vídeos" },
  { value: "image", label: "Fotos" }];
const ROLE_OPTIONS = [{ value: "", label: "Todos" }, { value: "original", label: "Originais" },
  { value: "cover", label: "Capas" }];
const VIEW_OPTIONS = [{ value: "", label: "Todos" }, { value: "processed", label: "Editados" },
  { value: "originals", label: "Sem edição" }];

export function libraryTab(panel, ctx) {
  let items = [];
  let next = null;
  let total = null;
  let loading = false;
  let error = null;
  let filters = { media_type: "", role: "", view: "", profile: "", batch_id: "" };
  let lastKey = null;

  const typeSel = select("media_type", TYPE_OPTIONS);
  const roleSel = select("role", ROLE_OPTIONS);
  const viewSel = select("view", VIEW_OPTIONS);
  const profileIn = h("input", { name: "profile", type: "text", placeholder: "@perfil", spellcheck: "false" });
  const batchIn = h("input", { name: "batch_id", type: "number", min: 1, placeholder: "nº", inputmode: "numeric" });
  const filtersEl = h("form", { class: "filters", id: "library-filters" },
    field("Tipo", typeSel), field("Papel", roleSel), field("Edição", viewSel), field("Perfil", profileIn),
    field("Lote", batchIn));
  const selBar = h("div", { class: "selbar", id: "selbar" });
  const msgEl = h("div", { class: "msg" });
  const grid = h("div", { class: "grid", id: "library-grid" });
  const more = h("div", { class: "row center" });
  setChildren(panel, filtersEl, selBar, msgEl, grid, more);

  function readFilters() {
    filters = { media_type: typeSel.value, role: roleSel.value, view: viewSel.value,
      profile: profileIn.value.trim().replace(/^@/, ""), batch_id: batchIn.value.trim() };
    return filters;
  }

  function writeFilters() {
    typeSel.value = filters.media_type;
    roleSel.value = filters.role;
    viewSel.value = filters.view;
    profileIn.value = filters.profile ? `@${filters.profile}` : "";
    batchIn.value = filters.batch_id;
  }

  async function load(reset) {
    if (loading || !ctx.client) return;
    loading = true;
    if (reset) {
      ctx.thumbs.reset();
      items = [];
      next = null;
      total = null;
      grid.replaceChildren(h("p", { class: "muted", text: "Carregando…" }));
    }
    renderMore();
    try {
      const data = await ctx.client.library({ ...filters, before_id: reset ? null : next, limit: PAGE });
      items = items.concat(data.items || []);
      next = data.next_before_id ?? null;
      total = data.total ?? null;
      error = null;
    } catch (err) {
      error = ctx.handleError(err);
    } finally {
      loading = false;
    }
    render();
  }

  async function selectAllOfBatch() {
    const params = { media_type: filters.media_type, role: filters.role };
    if (filters.batch_id) params.batch_id = filters.batch_id;
    try {
      const data = await ctx.client.libraryIds(params);
      // `items` traz tipo e papel de cada id: o resumo da seleção e o aviso de "não-vídeos" ficam certos.
      const added = Array.isArray(data.items)
        ? ctx.selection.addItems(data.items)
        : ctx.selection.addIds(data.ids || [], filters.media_type
          ? { media_type: filters.media_type, role: filters.role || null } : null);
      setChildren(msgEl, notice("ok", `${added} itens do lote ${filters.batch_id} adicionados à seleção.`));
      await ctx.saveSelection();
    } catch (err) {
      const text = ctx.handleError(err);
      if (text) setChildren(msgEl, notice("err", text));
    }
  }

  function renderSelBar() {
    const n = ctx.selection.size;
    setChildren(selBar,
      h("span", { class: "small", id: "sel-text", text: selectionText(ctx.selection) }),
      h("span", { class: "grow" }),
      items.length ? button("Selecionar visíveis", async () => {
        ctx.selection.addItems(items);
        await ctx.saveSelection();
        render();
      }, "btn small") : null,
      filters.batch_id ? button("Selecionar todos do lote", () => void selectAllOfBatch(), "btn small",
        { id: "select-batch" }) : null,
      n ? button("Limpar", async () => {
        ctx.selection.clear();
        await ctx.saveSelection();
        render();
      }, "btn small") : null,
      n ? button("Processar", () => ctx.go("process"), "btn small primary", { id: "to-process" }) : null,
      n ? button("Exportar", () => ctx.go("export", { source: { kind: "library" } }), "btn small",
        { id: "to-export" }) : null,
      button("Abrir no Media Tool", () => ctx.openApp(filters.profile
        ? `/biblioteca?perfil=${encodeURIComponent(filters.profile)}` : "/biblioteca"), "btn small"));
  }

  function card(item) {
    const selected = ctx.selection.has(item.id);
    const img = h("img", { alt: "" });
    const thumb = h("div", { class: "thumb" }, img);
    if (item.has_thumbnail !== false) ctx.thumbs.load(item.id, img);
    else thumb.classList.add("no-thumb");
    const badges = [badge(item.role === "cover" ? "Capa" : item.media_type === "video" ? "Vídeo" : "Foto")];
    if (item.has_edited) badges.push(badge("Editado"));
    if (item.processing === "pending" || item.processing === "processing") badges.push(badge("Processando", "active"));
    if (item.processing === "failed") badges.push(badge("Falhou", "err"));
    const box = h("input", { type: "checkbox", checked: selected, "aria-label": "Selecionar" });
    const el = h("label", { class: `tile${selected ? " selected" : ""}`, dataset: { id: item.id } },
      thumb, h("div", { class: "tile-badges" }, ...badges), box,
      h("div", { class: "tile-meta small" },
        h("div", { class: "ellipsis", text: item.source_profile ? `@${item.source_profile}` : "perfil não disponível" }),
        h("div", { class: "muted", text: `Publicado: ${formatDay(item.taken_at)}` }),
        h("div", { class: "muted", text: `Baixado: ${formatDay(item.created_at)}` })));
    box.addEventListener("change", async () => {
      const now = ctx.selection.toggle(item);
      box.checked = now;
      el.classList.toggle("selected", now);
      await ctx.saveSelection();
    });
    return el;
  }

  function renderMore() {
    setChildren(more,
      next && !loading ? button("Carregar mais", () => void load(false), "btn", { id: "load-more" }) : null,
      loading && items.length ? h("span", { class: "muted small", text: "Carregando…" }) : null);
  }

  function render() {
    renderSelBar();
    if (error) setChildren(msgEl, notice("err", error, button("Tentar de novo", () => void load(true), "btn small")));
    if (!items.length && !loading) {
      setChildren(grid, error ? null : empty("Nada na Biblioteca com estes filtros",
        "Baixe algo pela aba Importar ou pela Página atual.", button("Abrir no Media Tool",
          () => ctx.openApp("/biblioteca"), "btn")));
    } else {
      setChildren(grid, ...items.map(card));
    }
    if (total !== null && items.length) {
      grid.prepend(h("p", { class: "muted small full-row", text: `${items.length} de ${total} itens` }));
    }
    renderMore();
  }

  filtersEl.addEventListener("change", () => {
    readFilters();
    msgEl.replaceChildren();
    void load(true);
  });
  filtersEl.addEventListener("submit", (event) => event.preventDefault());

  return {
    show(params = {}) {
      if (params.batch_id || params.profile) {
        filters = { media_type: "", role: "", view: "", profile: params.profile ?? "",
          batch_id: params.batch_id ? String(params.batch_id) : "" };
        writeFilters();
      }
      const key = JSON.stringify(filters);
      if (key !== lastKey || params.refresh) {
        lastKey = key;
        void load(true);
      } else render();
    },
    hide() {},
    onSelection() {
      renderSelBar();
    },
  };
}
