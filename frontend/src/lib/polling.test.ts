import { describe, expect, it } from "vitest";
import { nextPollMs } from "./polling";

describe("polling adaptativo", () => {
  it("com itens ativos fica em 2 s", () => {
    expect(nextPollMs(null, true)).toBe(2_000);
    expect(nextPollMs(30_000, true)).toBe(2_000);
  });

  it("ocioso cresce 5 s, 15 s, 30 s e para em 30 s", () => {
    const seq: number[] = [];
    let ms: number | null = 2_000;
    for (let i = 0; i < 5; i += 1) {
      ms = nextPollMs(ms, false);
      seq.push(ms);
    }
    expect(seq).toEqual([5_000, 15_000, 30_000, 30_000, 30_000]);
    expect(nextPollMs(null, false)).toBe(5_000);
  });

  it("atividade nova volta para 2 s", () => {
    expect(nextPollMs(nextPollMs(15_000, false), true)).toBe(2_000);
  });
});
