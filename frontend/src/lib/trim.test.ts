import { describe, expect, it } from "vitest";
import { DEFAULT_EDIT_FORM, type EditForm, predictedDuration, toEditRequest } from "./editSettings";
import {
  MIN_TRIM,
  filmstripCount,
  filmstripTimes,
  floorMs,
  formatTimecode,
  frameDuration,
  frameStartAt,
  fractionToTime,
  isFullTrim,
  normalizeTrim,
  parseTimecode,
  previewTime,
  rulerLabel,
  rulerStep,
  rulerTicks,
  setTrimEnd,
  setTrimStart,
  snapTime,
  stepFrame,
  stepSeconds,
  timeToFraction,
  toTrimRequest,
  trimLabel,
} from "./trim";

const form = (patch: Partial<EditForm> = {}): EditForm => ({ ...DEFAULT_EDIT_FORM, ...patch });

describe("trim: formatação e digitação", () => {
  it("formatTimecode mostra mm:ss.mmm (h:mm:ss.mmm a partir de 1 h), milissegundo para baixo", () => {
    expect(formatTimecode(0)).toBe("00:00.000");
    expect(formatTimecode(3.25)).toBe("00:03.250");
    expect(formatTimecode(12.8)).toBe("00:12.800");
    expect(formatTimecode(0.29)).toBe("00:00.290"); // 0,29 * 1000 = 289,999... no ponto flutuante
    expect(formatTimecode(65.0005)).toBe("01:05.000");
    expect(formatTimecode(2 / 3)).toBe("00:00.666");
    expect(formatTimecode(3599.999)).toBe("59:59.999");
    expect(formatTimecode(3723.5)).toBe("1:02:03.500");
    expect(formatTimecode(-1)).toBe("00:00.000");
    expect(formatTimecode(Number.NaN)).toBe("00:00.000");
  });

  it("parseTimecode aceita mm:ss.mmm, h:mm:ss, segundos e vírgula", () => {
    expect(parseTimecode("01:05.250")).toBe(65.25);
    expect(parseTimecode("1:05,25")).toBe(65.25);
    expect(parseTimecode("65,25")).toBe(65.25);
    expect(parseTimecode("65.25")).toBe(65.25);
    expect(parseTimecode(" 3 ")).toBe(3);
    expect(parseTimecode("12,8 s")).toBe(12.8);
    expect(parseTimecode("1:02:03.5")).toBe(3723.5);
    expect(parseTimecode("0:00.0004")).toBe(0);
    expect(parseTimecode(".5")).toBe(0.5);
    expect(parseTimecode(formatTimecode(754.321))).toBe(754.321);
  });

  it("parseTimecode recusa texto inválido", () => {
    for (const text of ["", "abc", "1:", ":5", "1:60", "1:75:00", "-3", "1:2:3:4", "1,2,3", "1e3", "1:-5"]) {
      expect(parseTimecode(text), text).toBeNull();
    }
  });

  it("trimLabel: início → fim (duração)", () => {
    expect(trimLabel({ start: 3.25, end: 12.8 })).toBe("Recorte 00:03.250 → 00:12.800 (9,55 s)");
  });
});

describe("trim: quadros e agulha", () => {
  it("frameDuration usa o fps da mídia ou 1/30 s", () => {
    expect(frameDuration(25)).toBeCloseTo(0.04, 10);
    expect(frameDuration(null)).toBeCloseTo(1 / 30, 10);
    expect(frameDuration(0)).toBeCloseTo(1 / 30, 10);
    expect(frameDuration(Number.NaN)).toBeCloseTo(1 / 30, 10);
  });

  it("stepFrame anda um quadro na grade e fica em [0, duração]", () => {
    expect(stepFrame(1, 1, 25, 10)).toBeCloseTo(1.04, 10);
    expect(stepFrame(1, -1, 25, 10)).toBeCloseTo(0.96, 10);
    expect(stepFrame(1.013, 1, 25, 10)).toBeCloseTo(1.04, 10); // fora da grade: volta para a grade
    expect(stepFrame(0, -1, 25, 10)).toBe(0);
    expect(stepFrame(10, 1, 25, 10)).toBe(10);
    // fps desconhecido: 1/30 s, no milissegundo
    expect(stepFrame(1, 1, null, 10)).toBe(1.033);
    // 100 passos de 1 quadro a 30 fps dão exatamente 100 quadros (sem acumular erro)
    let t = 0;
    for (let i = 0; i < 100; i += 1) t = stepFrame(t, 1, 30, 60);
    expect(Math.round(t * 30)).toBe(100);
    expect(t).toBeCloseTo(100 / 30, 9);
  });

  it("stepSeconds pula ±1 s mantendo a grade", () => {
    expect(stepSeconds(2.52, 1, 25, 10)).toBeCloseTo(3.52, 10);
    expect(stepSeconds(0.5, -1, 25, 10)).toBe(0);
    expect(stepSeconds(9.5, 1, null, 10)).toBe(10);
  });

  it("snapTime e frameStartAt", () => {
    expect(snapTime(1.019, 25, 10)).toBeCloseTo(1.0, 10);
    expect(snapTime(1.021, 25, 10)).toBeCloseTo(1.04, 10);
    expect(snapTime(1.23456, null, 10)).toBe(1.235);
    expect(snapTime(-2, 25, 10)).toBe(0);
    expect(snapTime(12, 25, 10)).toBe(10);
    // tocando: o quadro na tela é o que começou antes do instante
    expect(frameStartAt(1.039, 25, 10)).toBeCloseTo(1.0, 10);
    expect(frameStartAt(2 / 30, 30, 10)).toBeCloseTo(2 / 30, 10); // na borda exata (ponto flutuante)
  });

  it("previewTime mostra o quadro certo longe da borda entre quadros", () => {
    // 25 fps: quadro k vai de k*0,04 a (k+1)*0,04
    expect(previewTime(1.0, 25, 10, "at")).toBeCloseTo(1.02, 10);
    expect(previewTime(1.0, 25, 10, "start")).toBeCloseTo(1.02, 10); // primeiro quadro do recorte: o 25
    expect(previewTime(1.01, 25, 10, "start")).toBeCloseTo(1.06, 10); // começa em 1,01 -> o 1º quadro é o 26
    expect(previewTime(2.0, 25, 10, "end")).toBeCloseTo(1.98, 10); // fim exclusivo: o último é o 49
    expect(previewTime(2.01, 25, 10, "end")).toBeCloseTo(2.02, 10); // o quadro 50 (2,00) ainda entra
    expect(previewTime(10, 25, 10, "at")).toBe(9.999);
    expect(previewTime(1.5, null, 10, "at")).toBe(1.5);
    expect(previewTime(1.5, null, 10, "end")).toBeCloseTo(1.499, 10);
  });
});

describe("trim: puxadores", () => {
  const d = 20;

  it("início não passa do fim - 0,1 s nem fica antes de 0", () => {
    const trim = { start: 2, end: 10 };
    expect(setTrimStart(trim, 5, d)).toEqual({ start: 5, end: 10 });
    expect(setTrimStart(trim, 9.95, d).start).toBeCloseTo(10 - MIN_TRIM, 10);
    expect(setTrimStart(trim, 15, d).start).toBeCloseTo(9.9, 10); // não cruza o fim
    expect(setTrimStart(trim, -3, d)).toEqual({ start: 0, end: 10 });
  });

  it("fim não fica antes do início + 0,1 s nem passa da duração", () => {
    const trim = { start: 2, end: 10 };
    expect(setTrimEnd(trim, 12.5, d)).toEqual({ start: 2, end: 12.5 });
    expect(setTrimEnd(trim, 2.05, d).end).toBeCloseTo(2.1, 10);
    expect(setTrimEnd(trim, 1, d).end).toBeCloseTo(2.1, 10); // não cruza o início
    expect(setTrimEnd(trim, 25, d)).toEqual({ start: 2, end: 20 });
  });

  it("normalizeTrim prende um recorte antigo à duração", () => {
    expect(normalizeTrim({ start: 3, end: 30 }, 20)).toEqual({ start: 3, end: 20 });
    expect(normalizeTrim({ start: 25, end: 30 }, 20).start).toBeCloseTo(19.9, 10);
    expect(normalizeTrim({ start: 0, end: 5 }, 0.05)).toEqual({ start: 0, end: 0.05 });
  });

  it("isFullTrim: 0 até a duração não corta nada", () => {
    expect(isFullTrim({ start: 0, end: 20 }, 20)).toBe(true);
    expect(isFullTrim({ start: 0.0004, end: 19.9996 }, 20)).toBe(true);
    expect(isFullTrim({ start: 0.04, end: 20 }, 20)).toBe(false);
    expect(isFullTrim({ start: 0, end: 19.96 }, 20)).toBe(false);
  });
});

describe("trim: linha do tempo", () => {
  it("posição <-> tempo", () => {
    expect(timeToFraction(5, 20)).toBe(0.25);
    expect(timeToFraction(30, 20)).toBe(1);
    expect(timeToFraction(-1, 20)).toBe(0);
    expect(timeToFraction(1, 0)).toBe(0);
    expect(fractionToTime(0.25, 20)).toBe(5);
    expect(fractionToTime(1.2, 20)).toBe(20);
    expect(fractionToTime(-0.1, 20)).toBe(0);
  });

  it("régua: passo com espaço mínimo entre marcas, marcas e rótulos", () => {
    expect(rulerStep(60, 1200)).toBe(5); // 20 px/s -> 5 s = 100 px
    expect(rulerStep(10, 1200)).toBe(1);
    expect(rulerStep(2, 1200)).toBe(0.2);
    expect(rulerStep(7200, 1200)).toBe(600);
    expect(rulerTicks(2, 0.5)).toEqual([0, 0.5, 1, 1.5, 2]);
    expect(rulerTicks(1, 0.2)).toEqual([0, 0.2, 0.4, 0.6, 0.8, 1]);
    expect(rulerLabel(65, 5)).toBe("1:05");
    expect(rulerLabel(5.5, 0.5)).toBe("0:05.5");
    expect(rulerLabel(0.6000000001, 0.2)).toBe("0:00.6");
  });

  it("tira de miniaturas: 10 a 20 quadros no meio de cada fatia", () => {
    expect(filmstripCount(400)).toBe(10);
    expect(filmstripCount(1300)).toBe(14);
    expect(filmstripCount(4000)).toBe(20);
    expect(filmstripTimes(10, 4)).toEqual([1.25, 3.75, 6.25, 8.75]);
    expect(filmstripTimes(0, 4)).toEqual([]);
  });
});

describe("trim: pedido e duração prevista", () => {
  it("toTrimRequest manda milissegundo para baixo (o início não passa do quadro marcado)", () => {
    expect(toTrimRequest(7, { start: 3.25, end: 12.8 })).toEqual({ media_id: 7, start: 3.25, end: 12.8 });
    expect(toTrimRequest(7, { start: 20 / 30, end: 301 / 30 })).toEqual({ media_id: 7, start: 0.666, end: 10.033 });
    expect(floorMs(0.29)).toBe(0.29);
    // recorte mínimo continua com 0,1 s depois do arredondamento
    const min = toTrimRequest(1, { start: 1 / 30, end: 1 / 30 + MIN_TRIM });
    expect(min.end - min.start).toBeGreaterThanOrEqual(MIN_TRIM - 1e-9);
  });

  it("toEditRequest manda `trims` só dos vídeos selecionados com recorte", () => {
    const trims = new Map([
      [3, { start: 1, end: 4.5 }],
      [9, { start: 0, end: 2 }], // fora da seleção: não vai
    ]);
    expect(toEditRequest([3, 4], form({ durationMode: "15", speed: 1.05 }), trims)).toEqual({
      media_ids: [3, 4],
      settings: { duration_mode: "15", custom_seconds: null, speed: 1.05, sharpen: false, output_type: "mp4" },
      trims: [{ media_id: 3, start: 1, end: 4.5 }],
    });
    // sem recorte: o corpo de antes, sem a chave `trims`
    expect(toEditRequest([4], form(), trims)).not.toHaveProperty("trims");
    expect(toEditRequest([4], form(), new Map())).not.toHaveProperty("trims");
    expect(toEditRequest([4], form())).not.toHaveProperty("trims");
  });

  it("predictedDuration com recorte: (fim - início) / velocidade, ignorando a duração geral", () => {
    const trim = { start: 3.25, end: 12.8 };
    expect(predictedDuration(40, form({ durationMode: "15" }), trim)).toBeCloseTo(9.55, 10);
    expect(predictedDuration(40, form({ durationMode: "15", speed: 1.25 }), trim)).toBeCloseTo(7.64, 10);
    expect(predictedDuration(null, form({ speed: 1.5 }), { start: 0, end: 30 })).toBeCloseTo(20, 10);
    expect(predictedDuration(40, form({ durationMode: "15" }), null)).toBeCloseTo(15.03, 6);
  });
});
