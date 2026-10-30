import { describe, expect, it } from "vitest";
import type { Media } from "../api/types";
import { editIdsFromSearch, ignoredText, incomingVideos, searchWithoutIds } from "./editIncoming";
import { mergeSelection } from "./editSettings";

function media(id: number, type: "video" | "image", role: "original" | "cover" = "original"): Media {
  return { id, media_type: type, role } as Media;
}

describe("editIncoming: ?ids= da página Editar", () => {
  it("lê só inteiros positivos, sem repetir, e conta os inválidos", () => {
    expect(editIdsFromSearch("?ids=3,1,2")).toEqual({ ids: [3, 1, 2], ignored: 0 });
    expect(editIdsFromSearch("?ids=1,abc,0,-4,2.5,1,7,")).toEqual({ ids: [1, 7], ignored: 4 });
    expect(editIdsFromSearch("?ids=99999999999999999999")).toEqual({ ids: [], ignored: 1 });
    expect(editIdsFromSearch("?ids=%201%20,%202")).toEqual({ ids: [1, 2], ignored: 0 });
  });

  it("sem o parâmetro: nada pedido", () => {
    expect(editIdsFromSearch("")).toEqual({ ids: [], ignored: 0 });
    expect(editIdsFromSearch("?lote=4")).toEqual({ ids: [], ignored: 0 });
  });

  it("teto de 500 ids; o excedente conta como ignorado", () => {
    const many = Array.from({ length: 503 }, (_, i) => i + 1).join(",");
    const result = editIdsFromSearch(`?ids=${many}`);
    expect(result.ids).toHaveLength(500);
    expect(result.ids[499]).toBe(500);
    expect(result.ignored).toBe(3);
  });

  it("só vídeos entram na seleção (fotos e capas ficam de fora)", () => {
    const items = [media(1, "video"), media(2, "image"), media(3, "video", "cover"), media(4, "image", "cover"),
      media(5, "video")];
    const videos = incomingVideos(items);
    expect(videos.map((m) => m.id)).toEqual([1, 5]);
    const merged = mergeSelection([], videos);
    expect(merged.selection.map((m) => m.id)).toEqual([1, 5]);
  });

  it("aviso dos ignorados", () => {
    expect(ignoredText(0)).toBeNull();
    expect(ignoredText(1)).toBe("1 item ignorado: não é vídeo ou não está mais na biblioteca.");
    expect(ignoredText(4)).toBe("4 itens ignorados: não são vídeos ou não estão mais na biblioteca.");
  });

  it("limpa só o ?ids= da URL", () => {
    expect(searchWithoutIds("?ids=1,2")).toBe("");
    expect(searchWithoutIds("?ids=1,2&x=y")).toBe("?x=y");
    expect(searchWithoutIds("?a=1&ids=3")).toBe("?a=1");
    expect(searchWithoutIds("")).toBe("");
  });
});
