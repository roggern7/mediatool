// Editor visual de modelos de legenda (Legendas CapCut): valores padrão, patch, validação, rótulos e a geometria da
// prévia aproximada (funções puras, testadas em captionDesign.test.ts).

import type {
  CapCutAnimationType,
  CapCutDesign,
  CapCutDesignAnimation,
  CapCutDesignFont,
  CapCutFontSource,
  CapCutPreview,
  CapCutStyle,
} from "../api/types";
import { CAPCUT_ACTIVE, CAPCUT_STATUS_LABEL, stageLabel, stageProgress } from "./capcut";

/* ---------------- Constantes ---------------- */

/** Quadro de referência do CapCut (9:16). */
export const FRAME_W = 1080;
export const FRAME_H = 1920;

/**
 * px de fonte (no quadro de 1080 de largura) por unidade de `size`, antes da escala. Calibrado no resultado real: o
 * estilo padrão (size 7, escala 1,919) dá maiúsculas de ~44 px de altura (altura de maiúscula ≈ 0,7 em).
 */
export const FONT_PX_PER_SIZE = 4.68;

/** Faixas aceitas pelo editor (as mesmas da validação do backend, app/schemas/capcut.py). */
export const DESIGN_LIMITS = {
  size: [2, 30],
  scale: [0.3, 4],
  opacity: [0, 1],
  strokeWidth: [0, 100],
  shadowOpacity: [0, 1],
  shadowBlur: [0, 100],
  shadowDistance: [0, 100],
  shadowAngle: [-180, 180],
  backgroundOpacity: [0, 1],
  backgroundRadius: [0, 1],
  letterSpacing: [-20, 100],
  lineSpacing: [-20, 100],
  position: [-1, 1],
  maxLineWidth: [0.1, 1],
  animationMs: [100, 10_000],
} as const satisfies Record<string, readonly [number, number]>;

export const STYLE_NAME_MAX = 60;

/** Montserrat do catálogo do CapCut (gratuita, verificada no CapCut 9.7.1). */
export const MONTSERRAT_FONT: CapCutDesignFont = {
  source: "capcut",
  id: "7540633113777540353",
  path: null,
  name: "Montserrat",
};

/** Ponto de partida quando o embutido não traz `design`: parecido com o estilo padrão do capcut-agent-skill. */
export const DEFAULT_DESIGN: CapCutDesign = {
  version: 1,
  font: MONTSERRAT_FONT,
  size: 7,
  scale: 1.92,
  bold: true,
  italic: false,
  underline: false,
  uppercase: false,
  color: "#FFFFFF",
  opacity: 1,
  stroke: { enabled: true, color: "#000000", width: 30 },
  shadow: { enabled: false, color: "#000000", opacity: 0.8, blur: 20, distance: 8, angle: -45 },
  background: { enabled: false, color: "#000000", opacity: 0.6, radius: 0.3 },
  align: "center",
  letter_spacing: 0,
  line_spacing: 0,
  position: { x: 0, y: -0.7 },
  max_line_width: 0.82,
  highlight: { enabled: true, color: "#FFD400" },
  animation: { in: null, out: null, loop: null },
};

export const POSITION_PRESETS = [
  { key: "top", label: "Topo", y: 0.7 },
  { key: "center", label: "Centro", y: 0 },
  { key: "bottom", label: "Base", y: -0.7 },
] as const;

export const FONT_SOURCE_LABEL: Record<CapCutFontSource, string> = {
  capcut: "CapCut",
  windows: "Windows",
  bundled: "Embutida no CapCut",
};

export const ANIMATION_TYPE_LABEL: Record<CapCutAnimationType, string> = {
  in: "Entrada",
  out: "Saída",
  loop: "Repetição (loop)",
};

export const ANIMATION_TYPES: CapCutAnimationType[] = ["in", "out", "loop"];

/* ---------------- Números e cores ---------------- */

export function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}

const HEX_RE = /^#[0-9a-fA-F]{6}$/;

export function isHexColor(value: unknown): value is string {
  return typeof value === "string" && HEX_RE.test(value);
}

/** "#ffd400", "ffd400", "#fd0" -> "#FFD400"; texto inválido -> null. */
export function normalizeHex(text: string): string | null {
  let t = text.trim().replace(/^#/, "");
  if (/^[0-9a-fA-F]{3}$/.test(t)) t = t.replace(/./g, (c) => c + c);
  return /^[0-9a-fA-F]{6}$/.test(t) ? `#${t.toUpperCase()}` : null;
}

export function hexToRgb(hex: string): [number, number, number] | null {
  if (!isHexColor(hex)) return null;
  const n = Number.parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** "#FF0000" + 0,5 -> "rgba(255, 0, 0, 0.5)"; cor inválida vira preto. */
export function rgbaCss(hex: string, alpha: number): string {
  const [r, g, b] = hexToRgb(hex) ?? [0, 0, 0];
  const a = Math.round(clamp(alpha, 0, 1) * 1000) / 1000;
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

/** Valor para <input type="color"> (minúsculo; inválido vira preto). */
export function colorInputValue(hex: string): string {
  return isHexColor(hex) ? hex.toLowerCase() : "#000000";
}

/* ---------------- Modelo (design) ---------------- */

type DesignObjectKeys = "stroke" | "shadow" | "background" | "position" | "highlight";

/** Alteração parcial do design: objetos internos podem vir parciais; `font` e `animation.*` são trocados inteiros. */
export type DesignPatch = Partial<Omit<CapCutDesign, DesignObjectKeys | "animation" | "version">> & {
  [K in DesignObjectKeys]?: Partial<CapCutDesign[K]>;
} & { animation?: Partial<CapCutDesign["animation"]> };

export function patchDesign(design: CapCutDesign, patch: DesignPatch): CapCutDesign {
  return {
    ...design,
    ...patch,
    version: 1,
    font: patch.font ? { ...patch.font } : design.font,
    stroke: { ...design.stroke, ...patch.stroke },
    shadow: { ...design.shadow, ...patch.shadow },
    background: { ...design.background, ...patch.background },
    position: { ...design.position, ...patch.position },
    highlight: { ...design.highlight, ...patch.highlight },
    animation: { ...design.animation, ...patch.animation },
  };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function pick<T extends object>(base: T, raw: unknown): T {
  if (!isObject(raw)) return { ...base };
  const out = { ...base } as Record<string, unknown>;
  for (const [key, def] of Object.entries(base)) {
    const v = raw[key];
    if (typeof def === "number" ? typeof v === "number" && Number.isFinite(v) : typeof v === typeof def) out[key] = v;
  }
  return out as T;
}

function normalizeAnimation(raw: unknown): CapCutDesignAnimation | null {
  if (!isObject(raw) || typeof raw.id !== "string" || !raw.id) return null;
  return {
    id: raw.id,
    name: typeof raw.name === "string" && raw.name ? raw.name : raw.id,
    duration_ms: typeof raw.duration_ms === "number" && Number.isFinite(raw.duration_ms) ? raw.duration_ms : 500,
  };
}

/** Design vindo do servidor (ou de versão futura) completado com os valores padrão, sem campos estranhos. */
export function normalizeDesign(raw: unknown): CapCutDesign {
  const d = DEFAULT_DESIGN;
  if (!isObject(raw)) return patchDesign(d, {});
  const top = pick(
    {
      size: d.size,
      scale: d.scale,
      bold: d.bold,
      italic: d.italic,
      underline: d.underline,
      uppercase: d.uppercase,
      color: d.color,
      opacity: d.opacity,
      letter_spacing: d.letter_spacing,
      line_spacing: d.line_spacing,
      max_line_width: d.max_line_width,
    },
    raw,
  );
  const f = isObject(raw.font) ? raw.font : null;
  const source = f && (f.source === "bundled" || f.source === "capcut" || f.source === "windows") ? f.source : null;
  const font: CapCutDesignFont = source
    ? {
        source,
        id: typeof f?.id === "string" && f.id ? f.id : null,
        path: typeof f?.path === "string" && f.path ? f.path : null,
        name: typeof f?.name === "string" ? f.name : "",
      }
    : d.font;
  const anim = isObject(raw.animation) ? raw.animation : {};
  return {
    version: 1,
    ...top,
    font,
    stroke: pick(d.stroke, raw.stroke),
    shadow: pick(d.shadow, raw.shadow),
    background: pick(d.background, raw.background),
    align: raw.align === "left" || raw.align === "right" || raw.align === "center" ? raw.align : d.align,
    position: pick(d.position, raw.position),
    highlight: pick(d.highlight, raw.highlight),
    animation: { in: normalizeAnimation(anim.in), out: normalizeAnimation(anim.out), loop: normalizeAnimation(anim.loop) },
  };
}

function stable(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stable);
  if (isObject(value)) {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((k) => [k, stable(value[k])]),
    );
  }
  return value;
}

/** Igualdade de conteúdo (independe da ordem das chaves). */
export function designsEqual(a: CapCutDesign, b: CapCutDesign): boolean {
  return JSON.stringify(stable(a)) === JSON.stringify(stable(b));
}

/** Só modelos com `design` e não embutidos podem ser editados; o embutido serve de ponto de partida ("Duplicar"). */
export function isEditableStyle(style: Pick<CapCutStyle, "builtin" | "design">): boolean {
  return !style.builtin && Boolean(style.design);
}

/** Design inicial de "Novo modelo": o do embutido, quando vier; senão o padrão do editor. */
export function startingDesign(styles: Pick<CapCutStyle, "builtin" | "design">[] | null | undefined): CapCutDesign {
  const builtin = styles?.find((s) => s.builtin && s.design);
  return normalizeDesign(builtin?.design ?? DEFAULT_DESIGN);
}

export function duplicateName(name: string): string {
  return `Cópia de ${name}`.slice(0, STYLE_NAME_MAX);
}

/** Chave da fonte no catálogo (`capcut:<id>`, `windows:<arquivo>`, `bundled:<arquivo>`), para a prévia. */
export function fontKey(font: CapCutDesignFont): string | null {
  if (font.source === "capcut") return font.id ? `capcut:${font.id}` : null;
  const file = (font.path ?? "").replace(/\\/g, "/").split("/").pop();
  return file ? `${font.source}:${file}` : null;
}

/** Nome de família CSS seguro para a fonte carregada por @font-face. */
export function fontFamilyName(key: string): string {
  return `mt-caption-${key.replace(/[^a-zA-Z0-9]/g, "_")}`;
}

/** Pilha de fontes da prévia: a fonte real carregada; senão o nome (se estiver instalada) e uma genérica. */
export function previewFontStack(loadedFamily: string | null, fontName: string): string {
  const safeName = fontName.replace(/["\\]/g, "").trim();
  const parts = [loadedFamily, safeName].filter(Boolean).map((f) => `"${f}"`);
  return [...parts, "Arial", "sans-serif"].join(", ");
}

export function positionPresetOf(y: number): (typeof POSITION_PRESETS)[number]["key"] | null {
  return POSITION_PRESETS.find((p) => Math.abs(p.y - y) < 0.005)?.key ?? null;
}

/* ---------------- Validação ---------------- */

export type DesignErrors = Record<string, string>;

function inRange(value: number, [min, max]: readonly [number, number]): boolean {
  return Number.isFinite(value) && value >= min && value <= max;
}

function fmt(n: number): string {
  return n.toLocaleString("pt-BR", { maximumFractionDigits: 2 });
}

/** Erros por campo (chave = caminho no design, ex.: "shadow.color"); objeto vazio = válido. */
export function validateDesign(d: CapCutDesign): DesignErrors {
  const errors: DesignErrors = {};
  const L = DESIGN_LIMITS;
  const range = (key: string, value: number, limits: readonly [number, number], label: string) => {
    if (!inRange(value, limits)) errors[key] = `${label} deve ficar entre ${fmt(limits[0])} e ${fmt(limits[1])}.`;
  };
  const color = (key: string, value: string, label: string) => {
    if (!isHexColor(value)) errors[key] = `${label}: use o formato #RRGGBB (ex.: #FFD400).`;
  };
  if (!d.font.name.trim() || (d.font.source === "capcut" ? !d.font.id : !d.font.path)) {
    errors.font = "Escolha uma fonte no catálogo.";
  }
  range("size", d.size, L.size, "O tamanho");
  range("scale", d.scale, L.scale, "A escala");
  color("color", d.color, "Cor do texto");
  range("opacity", d.opacity, L.opacity, "A opacidade");
  if (d.stroke.enabled) {
    color("stroke.color", d.stroke.color, "Cor do contorno");
    range("stroke.width", d.stroke.width, L.strokeWidth, "A espessura do contorno");
  }
  if (d.shadow.enabled) {
    color("shadow.color", d.shadow.color, "Cor da sombra");
    range("shadow.opacity", d.shadow.opacity, L.shadowOpacity, "A opacidade da sombra");
    range("shadow.blur", d.shadow.blur, L.shadowBlur, "O desfoque da sombra");
    range("shadow.distance", d.shadow.distance, L.shadowDistance, "A distância da sombra");
    range("shadow.angle", d.shadow.angle, L.shadowAngle, "O ângulo da sombra");
  }
  if (d.background.enabled) {
    color("background.color", d.background.color, "Cor do fundo");
    range("background.opacity", d.background.opacity, L.backgroundOpacity, "A opacidade do fundo");
    range("background.radius", d.background.radius, L.backgroundRadius, "O arredondamento do fundo");
  }
  range("letter_spacing", d.letter_spacing, L.letterSpacing, "O espaçamento entre letras");
  range("line_spacing", d.line_spacing, L.lineSpacing, "O espaçamento entre linhas");
  range("position.x", d.position.x, L.position, "A posição horizontal");
  range("position.y", d.position.y, L.position, "A posição vertical");
  range("max_line_width", d.max_line_width, L.maxLineWidth, "A largura máxima");
  if (d.highlight.enabled) color("highlight.color", d.highlight.color, "Cor do destaque");
  for (const type of ANIMATION_TYPES) {
    const a = d.animation[type];
    if (!a) continue;
    const label = `A duração da animação de ${ANIMATION_TYPE_LABEL[type].toLowerCase()}`;
    range(`animation.${type}`, a.duration_ms, L.animationMs, label);
    if (!errors[`animation.${type}`] && !Number.isInteger(a.duration_ms)) {
      errors[`animation.${type}`] = `${label} deve ser um número inteiro de milissegundos.`;
    }
  }
  return errors;
}

export function validateStyleName(name: string): string | null {
  const t = name.trim();
  if (!t) return "Dê um nome ao modelo.";
  if (t.length > STYLE_NAME_MAX) return `Use até ${STYLE_NAME_MAX} caracteres no nome.`;
  return null;
}

/**
 * Avisos (não bloqueiam) de combinações que o CapCut renderiza diferente da prévia aproximada. Mesma regra e mesmo
 * texto de `design_warnings` no backend (backend/app/integrations/capcut/design.py). Verificado no CapCut 9.7.1: com
 * a caixa de fundo ligada, as animações de entrada e de saída atuam só no texto e a caixa aparece vazia por alguns
 * instantes; o loop não tem esse efeito.
 */
export function designWarnings(d: Pick<CapCutDesign, "background" | "animation">): string[] {
  if (!d.background?.enabled) return [];
  const kinds = (["in", "out"] as const).filter((t) => d.animation?.[t]).map((t) => (t === "in" ? "entrada" : "saída"));
  if (kinds.length === 0) return [];
  return [
    `Com a caixa de fundo ligada, a animação de ${kinds.join(" e de ")} do CapCut atua só no texto: a caixa de fundo ` +
      "aparece vazia por alguns instantes. Para evitar, desligue a caixa de fundo ou tire essa animação.",
  ];
}

/* ---------------- Prévia aproximada ---------------- */

export interface CaptionLayout {
  /** Centro do texto no quadro 1080x1920 (px). */
  centerX: number;
  centerY: number;
  /** Escala do clip (aplicada por transform). */
  scale: number;
  /** Tamanho da fonte antes da escala (px). */
  fontPx: number;
  /** Largura máxima do bloco antes da escala (px); depois da escala = max_line_width x 1080. */
  wrapWidth: number;
  /** Espessura do contorno antes da escala (px); 0 = sem contorno. */
  strokePx: number;
  shadow: { dx: number; dy: number; blur: number; color: string } | null;
  background: { color: string; padY: number; padX: number; radius: number } | null;
  letterSpacingPx: number;
  lineHeight: number;
}

/**
 * Geometria da prévia no sistema do CapCut: posição -1..1 (y de baixo para cima) sobre o quadro 1080x1920, escala
 * relativa à largura 1080; contorno como no pyCapCut (largura = valor/100 x 0,2 do tamanho), sombra e fundo
 * aproximados pelo resultado real exportado.
 */
export function captionLayout(d: CapCutDesign): CaptionLayout {
  const scale = clamp(d.scale, DESIGN_LIMITS.scale[0], DESIGN_LIMITS.scale[1]);
  const fontPx = clamp(d.size, DESIGN_LIMITS.size[0], DESIGN_LIMITS.size[1]) * FONT_PX_PER_SIZE;
  const x = clamp(d.position.x, -1, 1);
  const y = clamp(d.position.y, -1, 1);
  let shadow: CaptionLayout["shadow"] = null;
  if (d.shadow.enabled) {
    const dist = clamp(d.shadow.distance, 0, 100) * 0.015 * fontPx;
    const rad = (clamp(d.shadow.angle, -180, 180) * Math.PI) / 180;
    shadow = {
      dx: round2(dist * Math.cos(rad)),
      dy: round2(-dist * Math.sin(rad)),
      blur: round2(clamp(d.shadow.blur, 0, 100) * 0.006 * fontPx),
      color: rgbaCss(d.shadow.color, d.shadow.opacity),
    };
  }
  return {
    centerX: round2(((1 + x) / 2) * FRAME_W),
    centerY: round2(((1 - y) / 2) * FRAME_H),
    scale,
    fontPx: round2(fontPx),
    wrapWidth: round2((clamp(d.max_line_width, 0.05, 1) * FRAME_W) / scale),
    strokePx: d.stroke.enabled ? round2((clamp(d.stroke.width, 0, 100) / 100) * 0.2 * fontPx) : 0,
    shadow,
    background: d.background.enabled
      ? {
          color: rgbaCss(d.background.color, d.background.opacity),
          padY: round2(0.4 * fontPx),
          padX: round2(0.5 * fontPx),
          radius: round2(clamp(d.background.radius, 0, 1) * 1.1 * fontPx),
        }
      : null,
    letterSpacingPx: round2(d.letter_spacing * 0.05 * fontPx),
    lineHeight: round2(1.2 + d.line_spacing * 0.05),
  };
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

/** text-shadow da sombra (ou "none"). */
export function textShadowCss(layout: CaptionLayout): string {
  const s = layout.shadow;
  return s ? `${s.dx}px ${s.dy}px ${s.blur}px ${s.color}` : "none";
}

/** Divide o texto de exemplo em legendas como o pipeline faz (até `maxWords` palavras e `maxChars` caracteres). */
export function chunkCaption(text: string, maxWords = 3, maxChars = 24): string[][] {
  const words = text.split(/\s+/).filter(Boolean);
  const chunks: string[][] = [];
  let current: string[] = [];
  for (const word of words) {
    const len = [...current, word].join(" ").length;
    if (current.length > 0 && (current.length >= maxWords || len > maxChars)) {
      chunks.push(current);
      current = [];
    }
    current.push(word);
  }
  if (current.length > 0) chunks.push(current);
  return chunks;
}

export const PREVIEW_WORD_MS = 450;

export type AnimationKind = "fade" | "zoom" | "slide";

/** Tipo aproximado do efeito, pelo nome da animação do catálogo. */
export function animationKind(name: string): AnimationKind {
  const n = name.toLowerCase();
  if (/zoom|escala|pop|ampli|cresc|encolh|salt|bounce|pulo/.test(n)) return "zoom";
  if (/desliz|slide|subir|descer|para cima|para baixo|esquerda|direita|rolar|voar/.test(n)) return "slide";
  return "fade";
}

export interface PreviewFrame {
  chunk: number;
  /** Palavra falada dentro da legenda atual. */
  word: number;
  opacity: number;
  scale: number;
  /** Deslocamento vertical (em em). */
  shiftEm: number;
}

function applyKind(kind: AnimationKind, p: number, frame: PreviewFrame): void {
  const e = clamp(p, 0, 1);
  frame.opacity *= e;
  if (kind === "zoom") frame.scale *= 0.6 + 0.4 * e;
  if (kind === "slide") frame.shiftEm += (1 - e) * 0.6;
}

/** Duração de uma legenda na prévia: uma batida por palavra, com folga para entrada e saída. */
export function chunkDurationMs(words: number, inMs: number, outMs: number): number {
  return Math.max(Math.max(1, words) * PREVIEW_WORD_MS, inMs + outMs + 300);
}

/** Estado da prévia no instante `elapsedMs` (em loop): legenda, palavra destacada e animação simulada. */
export function previewFrameAt(
  chunks: string[][],
  elapsedMs: number,
  animation: CapCutDesign["animation"],
): PreviewFrame {
  const inMs = animation.in?.duration_ms ?? 0;
  const outMs = animation.out?.duration_ms ?? 0;
  const durations = chunks.map((c) => chunkDurationMs(c.length, inMs, outMs));
  const total = durations.reduce((a, b) => a + b, 0);
  const frame: PreviewFrame = { chunk: 0, word: 0, opacity: 1, scale: 1, shiftEm: 0 };
  if (total <= 0) return frame;
  let t = ((elapsedMs % total) + total) % total;
  let i = 0;
  while (i < durations.length - 1 && t >= (durations[i] ?? 0)) {
    t -= durations[i] ?? 0;
    i += 1;
  }
  const dur = durations[i] ?? total;
  const words = Math.max(1, chunks[i]?.length ?? 1);
  frame.chunk = i;
  frame.word = Math.min(words - 1, Math.floor(t / (dur / words)));
  if (animation.in && inMs > 0 && t < inMs) applyKind(animationKind(animation.in.name), t / inMs, frame);
  if (animation.out && outMs > 0 && t > dur - outMs) applyKind(animationKind(animation.out.name), (dur - t) / outMs, frame);
  if (animation.loop) {
    const period = Math.max(200, animation.loop.duration_ms);
    frame.scale *= 1 + 0.04 * Math.sin((2 * Math.PI * t) / period);
  }
  frame.opacity = round2(frame.opacity);
  frame.scale = Math.round(frame.scale * 1000) / 1000;
  frame.shiftEm = round2(frame.shiftEm);
  return frame;
}

/* ---------------- Prévia real (CapCut) ---------------- */

export function isPreviewActive(p: Pick<CapCutPreview, "status"> | null | undefined): boolean {
  return Boolean(p && CAPCUT_ACTIVE.has(p.status));
}

/** Fração (0..1) da barra: concluída = 1; senão o maior entre o progresso gravado (0..100) e o da etapa. */
export function previewProgress(p: Pick<CapCutPreview, "status" | "progress" | "stage">): number {
  if (p.status === "completed") return 1;
  const stored = clamp((p.progress || 0) / 100, 0, 1);
  return p.status === "running" ? Math.max(stored, stageProgress(p.stage, null)) : stored;
}

/** "Em andamento: Exportando" / "Erro: <mensagem>" / "Concluído". */
export function previewStatusText(p: Pick<CapCutPreview, "status" | "stage" | "detail" | "error_message">): string {
  const label = CAPCUT_STATUS_LABEL[p.status] ?? p.status;
  if (p.status === "failed") return `${label}: ${p.error_message || "a prévia não foi gerada."}`;
  if (p.status === "running") return `${label}: ${p.stage ? stageLabel(p.stage) : (p.detail ?? "preparando")}`;
  if (p.status === "waiting" || p.status === "pending") return p.detail ? `${label}: ${p.detail}` : label;
  return label;
}
