import { describe, expect, it } from "vitest";
import {
  FIRST_PAGE,
  cursorQuery,
  currentCursor,
  nextCursor,
  pageStart,
  popPage,
  pushPage,
  shouldResetToFirst,
  stackFor,
} from "./keyset";

const items = (n: number) => Array.from({ length: n }, (_, i) => i);

describe("paginação keyset", () => {
  it("primeira página não manda before_id nem offset", () => {
    expect(currentCursor(FIRST_PAGE)).toBeNull();
    expect(cursorQuery(null)).toEqual({});
  });

  it("segue next_before_id e volta pela pilha", () => {
    const p1 = { items: items(100), total: 250, next_before_id: 901 };
    const s2 = pushPage(FIRST_PAGE, nextCursor(p1, FIRST_PAGE, 100));
    expect(cursorQuery(currentCursor(s2))).toEqual({ before_id: 901 });
    expect(pageStart(s2, 100)).toBe(100);
    const p2 = { items: items(100), total: 250, next_before_id: 801 };
    const s3 = pushPage(s2, nextCursor(p2, s2, 100));
    expect(cursorQuery(currentCursor(s3))).toEqual({ before_id: 801 });
    expect(popPage(s3)).toEqual(s2);
    expect(popPage(popPage(s3))).toEqual(FIRST_PAGE);
    expect(popPage(FIRST_PAGE)).toEqual(FIRST_PAGE);
  });

  it("next_before_id null encerra a paginação", () => {
    const last = { items: items(50), total: 250, next_before_id: null };
    expect(nextCursor(last, [null, { before_id: 5 }], 100)).toBeNull();
    expect(pushPage([null], null)).toEqual([null]);
  });

  it("backend antigo sem next_before_id usa offset", () => {
    const old = { items: items(100), total: 250 };
    expect(nextCursor(old, FIRST_PAGE, 100)).toEqual({ offset: 100 });
    expect(nextCursor(old, [null, { offset: 100 }], 100)).toEqual({ offset: 200 });
    expect(nextCursor(old, [null, { offset: 100 }, { offset: 200 }], 100)).toBeNull();
    expect(cursorQuery({ offset: 200 })).toEqual({ offset: 200 });
  });

  it("página vazia com total > 0 volta para a primeira", () => {
    const empty = { items: [], total: 30, next_before_id: null };
    expect(shouldResetToFirst(empty, [null, { before_id: 10 }])).toBe(true);
    expect(shouldResetToFirst(empty, FIRST_PAGE)).toBe(false);
    expect(shouldResetToFirst({ items: [], total: 0 }, [null, { before_id: 10 }])).toBe(false);
  });

  it("filtros diferentes começam na primeira página", () => {
    const pager = { scope: "a", stack: [null, { before_id: 9 }] };
    expect(stackFor(pager, "a")).toBe(pager.stack);
    expect(stackFor(pager, "b")).toEqual(FIRST_PAGE);
  });
});
