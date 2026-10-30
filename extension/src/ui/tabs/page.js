// Aba "Página atual": o que é a aba do Instagram (sem rede: page-info), se já está na Biblioteca, análise da
// publicação (post-preview, 1 leitura), o bloco "Depois de baixar" (mesma configuração da aba Importar) e as ações
// de download conforme o tipo.
import { fromPageInfo, parseInstagramUrl } from "../../lib/instagramUrl.js";
import {
  DEFAULT_EDIT, RequestIdKeeper, buildPostBody, describeStartBody, flowProblem, postActions,
} from "../../lib/importModel.js";
import { loadFlowForm, saveFlowFields } from "../../lib/settings.js";
import { button, empty, h, notice, setChildren } from "../dom.js";
import { fetchEditOptions, flowChooser } from "../flowChooser.js";

const KIND_LABELS = { reel: "Reel", video: "Vídeo", photo: "Foto", carousel: "Carrossel", post: "Publicação",
  profile: "Perfil" };
const NA = "não disponível";

export function pageTab(panel, ctx) {
  let page = { kind: "other" };
  let info = null;
  let preview = null;
  let message = null;
  let busy = false;
  let loadedUrl = null;
  const keeper = new RequestIdKeeper();
  let kind = "post";

  // "Depois de baixar": a mesma escolha da aba Importar, gravada em `importForm` (uma fonte só).
  const chooser = flowChooser({ flow: "download", edit: { ...DEFAULT_EDIT }, presetId: null, presetName: null,
    autoExport: false, autoExportContent: "originals" }, {
    choices: ["download", "edit", "preset"], open: false, collapsible: true, withZip: true,
    onChange: () => {
      const { flow, edit, presetId, presetName } = chooser.state;
      void saveFlowFields({ flow, edit, presetId, presetName });
      renderActions();
    },
  });
  const flowBox = h("div", { class: "box flow-box", id: "flow-box" },
    h("div", { class: "row between" }, h("p", { class: "box-title", text: "Depois de baixar" }),
      button("ZIP e mais opções em Importar", () => ctx.go("import"), "link small")),
    chooser.el);
  const actionsEl = h("div", { class: "stack", id: "post-actions" });

  /** Botões de download com o rótulo da ação completa ("Baixar e editar vídeo"). */
  function renderActions() {
    const flow = chooser.state.flow;
    setChildren(actionsEl, ...postActions(kind, flow).map((a) => {
      const problem = flowProblem(chooser.state, a.media, chooser.options);
      return button(a.label, () => void download(a.media), a.primary ? "btn primary" : "btn",
        { disabled: Boolean(busy) || Boolean(problem), title: problem || null, dataset: { media: a.media } });
    }));
  }

  async function refreshFlow() {
    chooser.set(await loadFlowForm());
    renderActions();
    if (ctx.client) {
      void fetchEditOptions(ctx).then((options) => {
        chooser.setOptions(options);
        renderActions();
      });
    }
  }

  async function load() {
    const url = ctx.pageUrl;
    page = parseInstagramUrl(url);
    info = null;
    preview = null;
    message = null;
    loadedUrl = url;
    render();
    if (page.kind === "other" || !ctx.client) return;
    try {
      info = await ctx.client.pageInfo(url);
      page = { ...page, ...fromPageInfo(info) };
      if (page.kind === "other") page = parseInstagramUrl(url);
    } catch (err) {
      message = { tone: "err", text: ctx.handleError(err) ?? "" };
      if (!message.text) message = null;
    }
    render();
  }

  function row(label, value, cls = "") {
    return h("div", { class: "kv" }, h("span", { class: "muted", text: label }),
      h("span", { class: cls, text: value ?? NA }));
  }

  function libraryBlock() {
    const lib = info && info.library;
    if (!lib) return h("p", { class: "muted small", text: "Situação na Biblioteca: não disponível." });
    if (page.kind === "profile") {
      return h("div", { class: "box" }, h("p", { class: "box-title", text: "Na Biblioteca" }),
        h("p", { text: `${lib.media_count ?? 0} itens deste perfil: ${lib.videos ?? 0} vídeos, ${lib.photos ?? 0} ` +
          `fotos, ${lib.covers ?? 0} capas.` }),
        lib.media_count ? button("Ver na Biblioteca", () => ctx.go("library", { profile: page.username }), "btn small")
          : null);
    }
    if (!lib.in_library) return h("p", { class: "muted small", text: "Ainda não está na Biblioteca." });
    const items = (lib.items || []).map((it) => {
      const what = it.role === "cover" ? "capa" : it.media_type === "video" ? "vídeo" : "foto";
      return h("li", { text: `Item ${Number(it.index ?? 0) + 1}: ${what}${it.has_edited ? " (editado)" : ""}` });
    });
    return h("div", { class: "box" }, h("p", { class: "box-title", text: "Já está na Biblioteca" }),
      h("ul", { class: "plain small" }, ...items));
  }

  function previewBlock() {
    if (!preview) {
      return h("div", { class: "row" }, button(busy === "preview" ? "Analisando…" : "Analisar publicação",
        () => void analyze(), "btn", { disabled: Boolean(busy), id: "analyze" }),
      h("span", { class: "muted small", text: "Faz 1 leitura da publicação pelo Media Tool." }));
    }
    const c = preview.counts || {};
    const items = preview.items || [];
    return h("div", { class: "box", id: "preview" }, h("p", { class: "box-title", text: "Análise da publicação" }),
      row("Tipo", KIND_LABELS[preview.kind] ?? NA),
      row("Itens", items.length ? String(items.length) : NA),
      row("Vídeos", c.videos ?? NA), row("Fotos", c.photos ?? NA), row("Capas", c.covers ?? NA),
      row("Perfil", preview.username ? `@${preview.username}` : NA));
  }

  async function analyze() {
    busy = "preview";
    message = null;
    render();
    try {
      preview = await ctx.client.postPreview(page.canonicalUrl);
    } catch (err) {
      const text = ctx.handleError(err);
      if (text) message = { tone: "err", text };
    } finally {
      busy = false;
      render();
    }
  }

  async function download(media) {
    if (busy) return;
    busy = "start";
    message = null;
    render();
    try {
      const draft = buildPostBody(page.canonicalUrl, media, chooser.state, null, chooser.options);
      const body = { ...draft, client_request_id: keeper.idFor(draft) };
      const result = await ctx.client.start(body);
      keeper.succeeded();
      const invalid = (result.invalid || []).map((i) => `${i.entry}: ${i.reason}`).join("; ");
      message = { tone: invalid ? "warn" : "ok",
        text: invalid ? `Ignorado: ${invalid}`
          : `Enviado ao Media Tool: ${describeStartBody(body, chooser.options?.presets)}.` };
      busy = false;
      render();
      ctx.go("activity", { highlight: result.links_batch_id ?? result.batch_ids?.[0] ?? null });
      return;
    } catch (err) {
      const text = ctx.handleError(err);
      if (text) message = { tone: "err", text };
    }
    busy = false;
    render();
  }

  async function owner() {
    busy = "owner";
    message = null;
    render();
    try {
      const result = await ctx.client.postOwner(page.canonicalUrl);
      busy = false;
      ctx.go("import", { profile: result.username });
      return;
    } catch (err) {
      const text = ctx.handleError(err);
      if (text) message = { tone: "err", text };
    }
    busy = false;
    render();
  }

  function render() {
    const parts = [];
    if (message) parts.push(notice(message.tone, message.text));
    if (page.kind === "other") {
      parts.push(empty("Nenhuma página do Instagram",
        ctx.mode === "full"
          ? "Abra uma publicação, Reel ou perfil do Instagram e clique no ícone da extensão; depois use \"Abrir painel completo\"."
          : "Abra uma publicação, um Reel ou um perfil do Instagram e clique de novo no ícone da extensão.",
        button("Importar perfis ou links", () => ctx.go("import"), "btn")));
      setChildren(panel, ...parts);
      return;
    }
    const kindLabel = KIND_LABELS[preview?.kind] ?? KIND_LABELS[page.kind];
    const username = page.username ?? preview?.username ?? page.libraryProfile ?? null;
    parts.push(h("div", { class: "card" },
      h("p", { class: "kicker", text: kindLabel }),
      row("Endereço", page.canonicalUrl, "mono small wrap"),
      row("Perfil", username ? `@${username}` : NA),
      page.kind !== "profile" ? row("Código", page.shortcode ?? NA, "mono") : null));
    parts.push(libraryBlock());

    if (page.kind === "profile") {
      parts.push(h("div", { class: "stack" },
        button(`Importar @${page.username}`, () => ctx.go("import", { profile: page.username }), "btn primary",
          { id: "import-profile" })));
      setChildren(panel, ...parts);
      return;
    }
    parts.push(previewBlock());
    kind = preview?.kind ?? (page.kind === "reel" ? "reel" : "post");
    parts.push(flowBox);
    if (!preview && kind === "post") {
      parts.push(h("p", { class: "muted small", text: "Tipo ainda não analisado: as opções valem para qualquer publicação." }));
    }
    renderActions();
    parts.push(actionsEl);
    parts.push(h("div", { class: "stack" }, button(busy === "owner" ? "Descobrindo o perfil…" :
      "Importar o perfil desta publicação", () => void owner(), "btn", { disabled: Boolean(busy), id: "post-owner" })));
    setChildren(panel, ...parts);
  }

  return {
    async show() {
      // Relê a configuração única (a aba Importar pode tê-la mudado) antes de desenhar os botões.
      await refreshFlow();
      if (loadedUrl !== ctx.pageUrl || (!info && page.kind !== "other")) void load();
      else render();
    },
  };
}
