import { describe, expect, it } from "vitest";
import { DEFAULT_THEME, parseThemePref, resolveTheme, THEME_OPTIONS } from "./theme";

describe("tema", () => {
  it("padrão é o escuro e valor desconhecido volta ao padrão", () => {
    expect(DEFAULT_THEME).toBe("dark");
    expect(parseThemePref(null)).toBe("dark");
    expect(parseThemePref("roxo")).toBe("dark");
    expect(parseThemePref("light")).toBe("light");
    expect(parseThemePref("system")).toBe("system");
  });

  it("sistema segue o Windows; escolha explícita ignora o sistema", () => {
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
    expect(resolveTheme("light", true)).toBe("light");
  });

  it("opções na ordem Escuro, Claro, Sistema", () => {
    expect(THEME_OPTIONS.map((o) => o.label)).toEqual(["Escuro", "Claro", "Sistema"]);
  });
});
