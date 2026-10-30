// Ajudantes de DOM sem bibliotecas. Todo texto entra por textContent (nunca innerHTML).

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function setChildren(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export function select(name, options, value, attrs = {}) {
  const el = h("select", { name, ...attrs });
  for (const opt of options) el.add(new Option(opt.label, opt.value));
  if (value !== undefined && value !== null) el.value = String(value);
  return el;
}

export function field(label, control, hint = null) {
  return h("label", { class: "field" }, h("span", { class: "field-label", text: label }), control,
    hint ? h("span", { class: "hint", text: hint }) : null);
}

export function checkbox(name, label, checked = false, attrs = {}) {
  const input = h("input", { type: "checkbox", name, checked, ...attrs });
  return { input, el: h("label", { class: "check" }, input, h("span", { text: label })) };
}

export function button(label, onclick, cls = "btn", attrs = {}) {
  return h("button", { type: "button", class: cls, onclick, ...attrs }, label);
}

/** Mensagem dentro da aba: tone "info" | "warn" | "err" | "ok". */
export function notice(tone, text, ...extra) {
  return h("div", { class: `notice ${tone}`, role: tone === "err" ? "alert" : "status" }, h("p", { text }), ...extra);
}

export function empty(title, text, ...actions) {
  return h("div", { class: "empty" }, h("p", { class: "empty-title", text: title }),
    text ? h("p", { class: "muted", text }) : null, actions.length ? h("div", { class: "row" }, ...actions) : null);
}

export function formatDate(value) {
  if (!value) return "não disponível";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "não disponível";
  return date.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit",
    minute: "2-digit" });
}

export function formatDay(value) {
  if (!value) return "não disponível";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "não disponível";
  return date.toLocaleDateString("pt-BR");
}

/** Confirmação dentro da página (sem window.confirm, que fecha o popup em alguns Chromes). */
export function confirmBox(title, text, confirmLabel = "Confirmar") {
  return new Promise((resolve) => {
    const dialog = h("dialog", { class: "confirm" });
    const done = (value) => {
      dialog.close();
      dialog.remove();
      resolve(value);
    };
    setChildren(dialog,
      h("p", { class: "confirm-title", text: title }),
      text ? h("p", { class: "muted", text }) : null,
      h("div", { class: "row end" },
        button("Cancelar", () => done(false), "btn", { "data-confirm": "no" }),
        button(confirmLabel, () => done(true), "btn primary", { "data-confirm": "yes" })));
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      done(false);
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}

/** Barra de progresso (fraction null = indeterminada). */
export function progressBar(fraction) {
  const bar = h("div", { class: `bar${fraction === null ? " indeterminate" : ""}`, role: "progressbar" });
  const fill = h("div", { class: "bar-fill" });
  if (fraction !== null) {
    fill.style.width = `${Math.round(fraction * 100)}%`;
    bar.setAttribute("aria-valuenow", String(Math.round(fraction * 100)));
  }
  bar.append(fill);
  return bar;
}

export function badge(text, tone = "") {
  return h("span", { class: `badge${tone ? ` ${tone}` : ""}`, text });
}
