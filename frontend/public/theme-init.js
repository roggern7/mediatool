// Aplica o tema salvo antes de a página desenhar (sem piscar o claro). Arquivo próprio: a CSP do app não permite
// script inline. Mesma chave e regra de src/lib/theme.ts: padrão "dark"; "system" segue o Windows.
(function () {
  var pref = "dark";
  try {
    var saved = window.localStorage.getItem("media-tool-theme");
    if (saved === "dark" || saved === "light" || saved === "system") pref = saved;
  } catch (e) {
    /* sem armazenamento: fica o padrão */
  }
  var dark = pref === "dark" || (pref === "system" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
})();
