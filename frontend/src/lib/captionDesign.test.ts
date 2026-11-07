import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, capcutFontFileUrl, capcutPreviewFileUrl } from "../api/client";
import type { CapCutDesign } from "../api/types";
import {
  DEFAULT_DESIGN,
  FONT_PX_PER_SIZE,
  animationKind,
  captionLayout,
  chunkCaption,
  chunkDurationMs,
  colorInputValue,
  designWarnings,
  designsEqual,
  duplicateName,
  fontFamilyName,
  fontKey,
  hexToRgb,
  isEditableStyle,
  isPreviewActive,
  normalizeDesign,
  normalizeHex,
  patchDesign,
  positionPresetOf,
  previewFontStack,
  previewFrameAt,
  previewProgress,
  previewStatusText,
  rgbaCss,
  startingDesign,
  textShadowCss,
  validateDesign,
  validateStyleName,
} from "./captionDesign";

const D = DEFAULT_DESIGN;

describe("editor de legendas: cores", () => {
  it("converte e normaliza #RRGGBB", () => {
    expect(hexToRgb("#FFD400")).toEqual([255, 212, 0]);
    expect(hexToRgb("FFD400")).toBeNull();
    expect(rgbaCss("#FF0000", 0.5)).toBe("rgba(255, 0, 0, 0.5)");
    expect(rgbaCss("ruim", 2)).toBe("rgba(0, 0, 0, 1)");
    expect(normalizeHex(" ffd400 ")).toBe("#FFD400");
    expect(normalizeHex("#fd0")).toBe("#FFDD00");
    expect(normalizeHex("#ffd40")).toBeNull();
    expect(colorInputValue("#FFD400")).toBe("#ffd400");
    expect(colorInputValue("#FFD4")).toBe("#000000");
  });
});

describe("editor de legendas: patch e normalização", () => {
  it("aplica alterações parciais sem perder os outros campos nem mexer no original", () => {
    const next = patchDesign(D, { size: 9, shadow: { enabled: true }, position: { y: 0.7 }, animation: { in: null } });
    expect(next.size).toBe(9);
    expect(next.shadow).toEqual({ ...D.shadow, enabled: true });
    expect(next.position).toEqual({ x: 0, y: 0.7 });
    expect(next.stroke).toEqual(D.stroke);
    expect(D.shadow.enabled).toBe(false);
    expect(D.position.y).toBe(-0.7);
    const anim = patchDesign(D, { animation: { out: { id: "1", name: "Fade-out", duration_ms: 300 } } });
    expect(anim.animation).toEqual({ in: null, out: { id: "1", name: "Fade-out", duration_ms: 300 }, loop: null });
  });

  it("completa um design parcial do servidor e descarta campos e tipos estranhos", () => {
    const d = normalizeDesign({
      version: 1,
      font: { source: "windows", id: "", path: "C:/Windows/Fonts/impact.ttf", name: "Impact" },
      size: "grande",
      scale: 2,
      shadow: { enabled: true, blur: 40 },
      align: "justify",
      animation: { in: { id: "6724916044072227332", name: "Fade-in", duration_ms: 400 }, out: { name: "sem id" } },
      extra: 1,
    });
    expect(d.font).toEqual({ source: "windows", id: null, path: "C:/Windows/Fonts/impact.ttf", name: "Impact" });
    expect(d.size).toBe(D.size);
    expect(d.scale).toBe(2);
    expect(d.shadow).toEqual({ ...D.shadow, enabled: true, blur: 40 });
    expect(d.align).toBe("center");
    expect(d.animation.in).toEqual({ id: "6724916044072227332", name: "Fade-in", duration_ms: 400 });
    expect(d.animation.out).toBeNull();
    expect("extra" in d).toBe(false);
    expect(normalizeDesign(null)).toEqual(D);
  });

  it("compara designs independentemente da ordem das chaves", () => {
    const { animation, ...rest } = D;
    const reordered = JSON.parse(JSON.stringify({ animation, ...rest })) as CapCutDesign;
    expect(Object.keys(reordered)[0]).toBe("animation");
    expect(designsEqual(D, reordered)).toBe(true);
    expect(designsEqual(D, patchDesign(D, { bold: !D.bold }))).toBe(false);
  });

  it("decide o que pode ser editado e o ponto de partida do novo modelo", () => {
    expect(isEditableStyle({ builtin: false, design: D })).toBe(true);
    expect(isEditableStyle({ builtin: true, design: D })).toBe(false);
    expect(isEditableStyle({ builtin: false, design: null })).toBe(false);
    const builtin = patchDesign(D, { size: 8 });
    expect(startingDesign([{ builtin: false, design: null }, { builtin: true, design: builtin }]).size).toBe(8);
    expect(startingDesign(undefined)).toEqual(D);
    expect(duplicateName("Amarelo")).toBe("Cópia de Amarelo");
    expect(duplicateName("x".repeat(80))).toHaveLength(60);
  });
});

describe("editor de legendas: fontes", () => {
  it("monta a chave do catálogo e a família CSS", () => {
    expect(fontKey(D.font)).toBe("capcut:7540633113777540353");
    expect(fontKey({ source: "windows", id: null, path: "C:\\Windows\\Fonts\\impact.ttf", name: "Impact" })).toBe(
      "windows:impact.ttf",
    );
    expect(fontKey({ source: "bundled", id: null, path: null, name: "x" })).toBeNull();
    expect(fontKey({ source: "capcut", id: null, path: null, name: "x" })).toBeNull();
    expect(fontFamilyName("windows:impact.ttf")).toBe("mt-caption-windows_impact_ttf");
    expect(previewFontStack("mt-caption-x", 'Mont"serrat')).toBe('"mt-caption-x", "Montserrat", Arial, sans-serif');
    expect(previewFontStack(null, "")).toBe("Arial, sans-serif");
    expect(capcutFontFileUrl("windows:impact.ttf")).toBe("/api/capcut/fonts/file?key=windows%3Aimpact.ttf");
  });
});

describe("editor de legendas: avisos do design (mesma regra do backend)", () => {
  const FADE = { id: "1", name: "Fade", duration_ms: 400 };
  const BG = { enabled: true };

  it("só avisa com caixa de fundo + animação de entrada/saída, sem bloquear a validação", () => {
    expect(designWarnings(D)).toEqual([]);
    expect(designWarnings(patchDesign(D, { background: BG }))).toEqual([]);
    expect(designWarnings(patchDesign(D, { animation: { in: FADE, out: FADE } }))).toEqual([]);
    expect(designWarnings(patchDesign(D, { background: BG, animation: { loop: FADE } }))).toEqual([]);
    const risky = patchDesign(D, { background: BG, animation: { in: FADE } });
    expect(designWarnings(risky)).toEqual([
      "Com a caixa de fundo ligada, a animação de entrada do CapCut atua só no texto: a caixa de fundo aparece " +
        "vazia por alguns instantes. Para evitar, desligue a caixa de fundo ou tire essa animação.",
    ]);
    expect(validateDesign(risky)).toEqual({});
    expect(designWarnings(patchDesign(D, { background: BG, animation: { out: FADE } }))[0]).toContain(
      "animação de saída do CapCut",
    );
    expect(designWarnings(patchDesign(D, { background: BG, animation: { in: FADE, out: FADE } }))[0]).toContain(
      "animação de entrada e de saída do CapCut",
    );
  });
});

describe("editor de legendas: validação", () => {
  it("aceita o padrão e aponta cada campo inválido com mensagem amigável", () => {
    expect(validateDesign(D)).toEqual({});
    const bad = patchDesign(D, {
      size: 40,
      color: "amarelo",
      stroke: { width: 120 },
      shadow: { enabled: true, color: "#12" },
      position: { y: -1.5 },
      animation: { in: { id: "1", name: "Fade-in", duration_ms: 20 } },
      font: { source: "windows", id: null, path: null, name: "Impact" },
    });
    const errors = validateDesign(bad);
    expect(Object.keys(errors).sort()).toEqual(
      ["animation.in", "color", "font", "position.y", "shadow.color", "size", "stroke.width"].sort(),
    );
    expect(errors.size).toBe("O tamanho deve ficar entre 2 e 30.");
    expect(errors.color).toContain("#RRGGBB");
    // Recursos desligados não são validados.
    expect(validateDesign(patchDesign(D, { background: { color: "x" } }))).toEqual({});
    expect(validateDesign(patchDesign(D, { size: Number.NaN }))).toHaveProperty("size");
    const frac = validateDesign(patchDesign(D, { animation: { out: { id: "2", name: "Fade-out", duration_ms: 450.5 } } }));
    expect(frac["animation.out"]).toContain("inteiro");
    expect(validateDesign(patchDesign(D, { letter_spacing: -5, max_line_width: 0.2 }))).toEqual({});
  });

  it("valida o nome do modelo", () => {
    expect(validateStyleName("  ")).toBe("Dê um nome ao modelo.");
    expect(validateStyleName("x".repeat(61))).toContain("60");
    expect(validateStyleName(" Amarelo ")).toBeNull();
  });
});

describe("editor de legendas: geometria da prévia", () => {
  it("mapeia posição e escala do CapCut para o quadro 1080x1920", () => {
    const l = captionLayout(D);
    expect(l.centerX).toBe(540);
    expect(l.centerY).toBeCloseTo(1632, 5); // y -0,7 = 85% da altura, de cima para baixo
    expect(captionLayout(patchDesign(D, { position: { x: -1, y: 1 } }))).toMatchObject({ centerX: 0, centerY: 0 });
    expect(captionLayout(patchDesign(D, { position: { x: 1, y: -1 } }))).toMatchObject({ centerX: 1080, centerY: 1920 });
    expect(l.scale).toBe(1.92);
    // Largura máxima depois da escala = 82% de 1080.
    expect(l.wrapWidth * l.scale).toBeCloseTo(0.82 * 1080, 0);
  });

  it("calibra o tamanho: estilo padrão (size 7, escala 1,919) ≈ 44 px de altura de maiúscula", () => {
    const l = captionLayout(patchDesign(D, { size: 7, scale: 1.919 }));
    const capHeight = l.fontPx * l.scale * 0.7;
    expect(capHeight).toBeGreaterThan(42);
    expect(capHeight).toBeLessThan(46);
    expect(l.fontPx).toBeCloseTo(7 * FONT_PX_PER_SIZE, 1);
  });

  it("contorno, sombra, fundo e espaçamentos", () => {
    const l = captionLayout(D);
    expect(l.strokePx).toBeCloseTo(0.3 * 0.2 * l.fontPx, 1);
    expect(captionLayout(patchDesign(D, { stroke: { enabled: false } })).strokePx).toBe(0);
    expect(l.shadow).toBeNull();
    expect(textShadowCss(l)).toBe("none");
    const s = captionLayout(patchDesign(D, { shadow: { enabled: true, color: "#FF0000", opacity: 0.5, angle: -45 } }));
    expect(s.shadow?.dx).toBeGreaterThan(0);
    expect(s.shadow?.dy).toBeGreaterThan(0); // -45° = para baixo e para a direita
    expect(s.shadow?.dx).toBeCloseTo(s.shadow?.dy ?? 0, 1);
    expect(textShadowCss(s)).toMatch(/^[\d.]+px [\d.]+px [\d.]+px rgba\(255, 0, 0, 0\.5\)$/);
    const up = captionLayout(patchDesign(D, { shadow: { enabled: true, angle: 90 } }));
    expect(up.shadow?.dy).toBeLessThan(0);
    const b = captionLayout(patchDesign(D, { background: { enabled: true, color: "#0000FF", opacity: 0.6, radius: 0 } }));
    expect(b.background).toMatchObject({ color: "rgba(0, 0, 255, 0.6)", radius: 0 });
    const sp = captionLayout(patchDesign(D, { letter_spacing: 10, line_spacing: 4 }));
    expect(sp.letterSpacingPx).toBeCloseTo(0.5 * sp.fontPx, 1);
    expect(sp.lineHeight).toBe(1.4);
  });
});

describe("editor de legendas: texto e animação da prévia", () => {
  it("divide o texto como o pipeline (palavras e caracteres por legenda)", () => {
    expect(chunkCaption("Onde é a feira da Conceição hoje", 3, 24)).toEqual([
      ["Onde", "é", "a"],
      ["feira", "da", "Conceição"],
      ["hoje"],
    ]);
    expect(chunkCaption("palavramuitolonga outra", 3, 10)).toEqual([["palavramuitolonga"], ["outra"]]);
    expect(chunkCaption("   ")).toEqual([]);
  });

  it("percorre as palavras em loop e simula entrada e saída", () => {
    const chunks = [["a", "b"], ["c"]];
    const none = { in: null, out: null, loop: null };
    const d0 = chunkDurationMs(2, 0, 0);
    const d1 = chunkDurationMs(1, 0, 0);
    expect(previewFrameAt(chunks, 0, none)).toMatchObject({ chunk: 0, word: 0, opacity: 1, scale: 1 });
    expect(previewFrameAt(chunks, d0 / 2 + 1, none)).toMatchObject({ chunk: 0, word: 1 });
    expect(previewFrameAt(chunks, d0 + 1, none)).toMatchObject({ chunk: 1, word: 0 });
    expect(previewFrameAt(chunks, d0 + d1 + 1, none)).toMatchObject({ chunk: 0, word: 0 });

    const anim = {
      in: { id: "1", name: "Fade-in", duration_ms: 400 },
      out: { id: "2", name: "Zoom out", duration_ms: 400 },
      loop: null,
    };
    const dur = chunkDurationMs(2, 400, 400);
    expect(previewFrameAt(chunks, 200, anim).opacity).toBeCloseTo(0.5, 2);
    expect(previewFrameAt(chunks, 200, anim).scale).toBe(1);
    const out = previewFrameAt(chunks, dur - 200, anim);
    expect(out.opacity).toBeCloseTo(0.5, 2);
    expect(out.scale).toBeLessThan(1);
    expect(previewFrameAt([], 1000, anim)).toMatchObject({ chunk: 0, word: 0, opacity: 1 });
  });

  it("classifica o efeito pelo nome da animação", () => {
    expect(animationKind("Fade-in")).toBe("fade");
    expect(animationKind("Zoom 2")).toBe("zoom");
    expect(animationKind("Deslizar para cima")).toBe("slide");
    expect(animationKind("Máquina de escrever")).toBe("fade");
  });

  it("atalhos de posição", () => {
    expect(positionPresetOf(-0.7)).toBe("bottom");
    expect(positionPresetOf(0)).toBe("center");
    expect(positionPresetOf(0.7)).toBe("top");
    expect(positionPresetOf(-0.5)).toBeNull();
  });
});

describe("editor de legendas: prévia real", () => {
  const base = { id: "p1", stage: null, progress: 0, detail: null, error_message: null, file_url: null };

  it("progresso, estado e texto", () => {
    expect(isPreviewActive({ status: "running" })).toBe(true);
    expect(isPreviewActive({ status: "completed" })).toBe(false);
    expect(isPreviewActive(null)).toBe(false);
    expect(previewProgress({ status: "completed", progress: 10, stage: null })).toBe(1);
    expect(previewProgress({ status: "running", progress: 10, stage: "exporting" })).toBeCloseTo(0.5, 5);
    expect(previewProgress({ status: "pending", progress: 250, stage: null })).toBe(1);
    expect(previewStatusText({ ...base, status: "running", stage: "exporting" })).toBe("Em andamento: Exportando");
    expect(previewStatusText({ ...base, status: "failed", error_message: "CapCut bloqueou" })).toBe(
      "Erro: CapCut bloqueou",
    );
    expect(previewStatusText({ ...base, status: "waiting", detail: "CapCut aberto" })).toBe("Aguardando: CapCut aberto");
    expect(previewStatusText({ ...base, status: "completed" })).toBe("Concluído");
    expect(capcutPreviewFileUrl({ id: "a b", file_url: null })).toBe("/api/capcut/previews/a%20b/file");
    expect(capcutPreviewFileUrl({ id: 3, file_url: "/api/capcut/previews/3/file" })).toBe("/api/capcut/previews/3/file");
  });
});

describe("editor de legendas: cliente", () => {
  const base = { id: "abc", stage: null, progress: 0, detail: null, error_message: null, file_url: null };
  const fetchMock = vi.fn();
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  beforeEach(() => {
    vi.stubGlobal("window", globalThis);
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("busca fontes e animações só gratuitas por padrão", async () => {
    fetchMock.mockResolvedValue(json({ items: [], total: 0 }));
    await api.capcutFonts({ q: " mont ", limit: 40 });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/capcut/catalog/fonts?q=mont&include_pro=0&limit=40");
    await api.capcutFonts({ source: "windows", include_pro: true });
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/capcut/catalog/fonts?source=windows&include_pro=1");
    await api.capcutAnimations({ type: "out", q: "fade" });
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/capcut/catalog/animations?type=out&q=fade&include_pro=0");
  });

  it("salva pelo editor, atualiza e gera/cancela a prévia real", async () => {
    fetchMock.mockResolvedValue(json({ id: 5 }));
    await api.createCapcutStyle({ name: "Novo", design: D });
    let [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/capcut/styles");
    expect(JSON.parse(String(init.body))).toEqual({ name: "Novo", design: D });
    await api.updateCapcutStyle(5, { name: "Outro", design: D });
    [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect([url, init.method]).toEqual(["/api/capcut/styles/5", "PUT"]);

    fetchMock.mockResolvedValue(json({ id: "abc", status: "pending" }, 202));
    expect(await api.createCapcutPreview({ design: D, media_id: 9 })).toEqual({ id: "abc", status: "pending" });
    [url, init] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect([url, init.method]).toEqual(["/api/capcut/previews", "POST"]);
    expect((init.headers as Record<string, string>)["X-Media-Tool-Client"]).toBe("panel");
    expect(JSON.parse(String(init.body))).toEqual({ design: D, media_id: 9 });
    fetchMock.mockResolvedValue(json({ ...base, status: "cancelled" }));
    await api.capcutPreview("abc");
    await api.cancelCapcutPreview("abc");
    expect(fetchMock.mock.calls[3]?.[0]).toBe("/api/capcut/previews/abc");
    expect(fetchMock.mock.calls[4]?.[0]).toBe("/api/capcut/previews/abc/cancel");
    expect((fetchMock.mock.calls[4]?.[1] as RequestInit).method).toBe("POST");
  });
});
