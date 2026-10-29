import { describe, expect, it } from "vitest";
import type { Media } from "../api/types";
import {
  EMPTY_FILE_TEXT,
  MAX_UPLOAD_BYTES,
  TOO_LARGE_TEXT,
  UNSUPPORTED_FILE_TEXT,
  UPLOAD_RETRY_DELAYS_MS,
  addFiles,
  checkLocalFile,
  isBusy,
  markDone,
  markError,
  markProgress,
  markStarted,
  nextToStart,
  queueSummary,
  removeItem,
  type UploadItem,
  uploadRetryDelay,
} from "./uploadQueue";

const files = (n: number) => Array.from({ length: n }, (_, i) => ({ name: `v${i}.mp4`, size: 100 + i }));
const media = { id: 7, media_type: "video" } as Media;

function startAll(items: UploadItem[]): UploadItem[] {
  let next = items;
  for (const key of nextToStart(next)) next = markStarted(next, key);
  return next;
}

describe("uploadQueue", () => {
  it("no máximo 2 envios ao mesmo tempo, na ordem da fila", () => {
    let { items } = addFiles([], files(4));
    expect(nextToStart(items)).toHaveLength(2);
    items = startAll(items);
    expect(items.map((i) => i.status)).toEqual(["uploading", "uploading", "waiting", "waiting"]);
    expect(nextToStart(items)).toEqual([]);
    items = markProgress(items, items[0]!.key, 1); // tudo enviado: o servidor analisa, ainda ocupa a vaga
    expect(items[0]!.status).toBe("analyzing");
    expect(nextToStart(items)).toEqual([]);
    items = markDone(items, items[0]!.key, media, false);
    expect(nextToStart(items)).toEqual([items[2]!.key]);
  });

  it("remover um arquivo aguardando ou enviando não afeta os outros", () => {
    let { items } = addFiles([], files(3));
    items = startAll(items);
    items = removeItem(items, items[2]!.key);
    expect(items).toHaveLength(2);
    expect(nextToStart(items)).toEqual([]);
    items = removeItem(items, items[0]!.key);
    expect(items.map((i) => i.status)).toEqual(["uploading"]);
  });

  it("erro em um arquivo não bloqueia os demais", () => {
    let { items } = addFiles([], files(3));
    items = startAll(items);
    items = markError(items, items[0]!.key, "O arquivo enviado não é um vídeo válido.");
    expect(items[0]).toMatchObject({ status: "error", error: "O arquivo enviado não é um vídeo válido." });
    expect(nextToStart(items)).toEqual([items[2]!.key]);
    items = startAll(items);
    items = markDone(items, items[1]!.key, media, true);
    items = markDone(items, items[2]!.key, media, false);
    expect(items.map((i) => i.status)).toEqual(["error", "duplicate", "done"]);
    expect(isBusy(items)).toBe(false);
    expect(queueSummary(items)).toEqual({ ready: 2, failed: 1, total: 3 });
  });

  it("mesmo nome e tamanho é ignorado; o que falhou volta para a fila se escolhido de novo", () => {
    const first = addFiles([], [...files(2), files(1)[0]!]);
    expect(first.items).toHaveLength(2);
    expect(first.ignored).toBe(1);
    const again = addFiles(first.items, files(2));
    expect(again.ignored).toBe(2);
    const failed = markError(first.items, first.items[0]!.key, "erro");
    const retry = addFiles(failed, files(1));
    expect(retry.ignored).toBe(0);
    expect(retry.items.find((i) => i.name === "v0.mp4")).toMatchObject({ status: "waiting", error: null });
    expect(retry.items).toHaveLength(2);
  });

  it("progresso só vale para envio em andamento e fica entre 0 e 1", () => {
    let { items } = addFiles([], files(1));
    items = markProgress(items, items[0]!.key, 0.5);
    expect(items[0]).toMatchObject({ status: "waiting", progress: 0 });
    items = markStarted(items, items[0]!.key);
    items = markProgress(items, items[0]!.key, 0.4);
    expect(items[0]).toMatchObject({ status: "uploading", progress: 0.4 });
    items = markProgress(items, items[0]!.key, 3);
    expect(items[0]).toMatchObject({ status: "analyzing", progress: 1 });
    expect(isBusy(items)).toBe(true);
  });
});

describe("uploadQueue: verificação antes do envio e nova tentativa", () => {
  it("checkLocalFile: teto de 2 GB, arquivo vazio e só MP4/MOV/WebM/MKV", () => {
    expect(MAX_UPLOAD_BYTES).toBe(2 * 1024 ** 3);
    expect(checkLocalFile({ name: "a.mp4", size: MAX_UPLOAD_BYTES })).toBeNull();
    expect(checkLocalFile({ name: "a.mp4", size: MAX_UPLOAD_BYTES + 1 })).toBe(TOO_LARGE_TEXT);
    expect(checkLocalFile({ name: "a.mp4", size: 0 })).toBe(EMPTY_FILE_TEXT);
    for (const name of ["a.MP4", "b.mov", "c.m4v", "d.webm", "e.mkv"]) {
      expect(checkLocalFile({ name, size: 10 })).toBeNull();
    }
    for (const name of ["a.gif", "b.avi", "c.jpg", "d.txt", "video.mp4.exe"]) {
      expect(checkLocalFile({ name, size: 10, type: "video/mp4" })).toBe(UNSUPPORTED_FILE_TEXT);
    }
    expect(checkLocalFile({ name: "sem-extensao", size: 10, type: "video/mp4" })).toBeNull();
    expect(checkLocalFile({ name: "sem-extensao", size: 10, type: "" })).toBe(UNSUPPORTED_FILE_TEXT);
  });

  it("arquivo recusado entra como erro com o motivo e não ocupa vaga", () => {
    const { items, added } = addFiles(
      [],
      [
        { name: "grande.mp4", size: MAX_UPLOAD_BYTES + 1 },
        { name: "anim.gif", size: 10 },
        { name: "ok.mp4", size: 10 },
      ],
      checkLocalFile,
    );
    expect(items.map((i) => [i.status, i.error])).toEqual([
      ["error", TOO_LARGE_TEXT],
      ["error", UNSUPPORTED_FILE_TEXT],
      ["waiting", null],
    ]);
    expect(added).toEqual([items[2]!.key]);
    expect(nextToStart(items)).toEqual([items[2]!.key]);
  });

  it("uploadRetryDelay: só 429 too_many_uploads, com esperas crescentes e limite", () => {
    expect(uploadRetryDelay("too_many_uploads", 0)).toBe(UPLOAD_RETRY_DELAYS_MS[0]);
    expect(uploadRetryDelay("too_many_uploads", 1)).toBeGreaterThan(UPLOAD_RETRY_DELAYS_MS[0]);
    expect(uploadRetryDelay("too_many_uploads", UPLOAD_RETRY_DELAYS_MS.length)).toBeNull();
    expect(uploadRetryDelay("invalid_media", 0)).toBeNull();
    expect(uploadRetryDelay("payload_too_large", 0)).toBeNull();
  });
});
