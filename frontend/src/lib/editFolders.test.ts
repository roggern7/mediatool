import { describe, expect, it } from "vitest";
import type { EditFolder, Media, MediaDeleteResult } from "../api/types";
import {
  DELETE_IRREVERSIBLE_TEXT,
  chunkIds,
  deleteConfirmLines,
  deleteResultText,
  folderDeleteTotals,
  folderFullName,
  folderSubtitle,
  folderTitle,
  formatDay,
  mediaDeleteTotals,
  mergeDeleteResults,
  versionCountText,
  versionsDeleteTotals,
  versionsDeletedText,
  videoCountText,
} from "./editFolders";
import { formatDateTime } from "./format";

const CREATED = "2026-10-06T15:30:00.000Z";

function folder(patch: Partial<EditFolder> = {}): EditFolder {
  return {
    key: "batch:1",
    kind: "batch",
    import_mode: "profile",
    profiles: ["ana"],
    created_at: CREATED,
    day: null,
    video_count: 12,
    rendition_count: 0,
    total_bytes: 0,
    shared_count: 0,
    ...patch,
  };
}

function media(id: number, size: number, versions: number[] = []): Media {
  return {
    id,
    file_size: size,
    renditions: versions.map((s, i) => ({ id: id * 10 + i, file_size: s })),
  } as unknown as Media;
}

describe("editFolders: nomes das pastas", () => {
  it("lote de um perfil: @perfil, data/hora local e contagem", () => {
    const f = folder();
    expect(folderTitle(f)).toBe("@ana");
    expect(folderSubtitle(f)).toBe(`${formatDateTime(CREATED)} · 12 vídeos`);
    expect(folderFullName(f)).toBe(`@ana (${formatDateTime(CREATED)})`);
  });

  it("grupo: N perfis com os 3 primeiros nomes", () => {
    expect(folderTitle(folder({ kind: "group", profiles: ["a", "b"] }))).toBe("2 perfis: @a, @b");
    expect(folderTitle(folder({ kind: "group", profiles: ["a", "b", "c", "d", "e"] }))).toBe(
      "5 perfis: @a, @b, @c e mais 2",
    );
  });

  it("importação por link, envios do computador por dia e outros", () => {
    expect(folderTitle(folder({ import_mode: "links", profiles: [] }))).toBe("Importação por link");
    const local = folder({ key: "local:2026-10-28", kind: "local", import_mode: null, day: "2026-10-28", video_count: 1 });
    expect(folderTitle(local)).toBe("Enviados do computador — 28/10/2026");
    expect(folderSubtitle(local)).toBe("1 vídeo");
    const other = folder({ key: "other", kind: "other", import_mode: null, profiles: [] });
    expect(folderTitle(other)).toBe("Outros vídeos");
    expect(folderFullName(other)).toBe("Outros vídeos");
    expect(folderTitle(folder({ profiles: [] }))).toBe("Importação");
  });

  it("dia inválido vira traço; contagens no singular e plural", () => {
    expect(formatDay(null)).toBe("-");
    expect(formatDay("2026-1-2")).toBe("-");
    expect(videoCountText(1)).toBe("1 vídeo");
    expect(videoCountText(1147)).toBe("1.147 vídeos");
    expect(versionCountText(1)).toBe("1 versão editada");
    expect(versionCountText(3)).toBe("3 versões editadas");
  });
});

describe("editFolders: confirmação e resumo de exclusão", () => {
  it("totais de vídeos selecionados, da pasta e só das versões", () => {
    const items = [media(1, 1000, [200, 300]), media(2, 500)];
    expect(mediaDeleteTotals(items)).toEqual({ videos: 2, versions: 2, bytes: 2000 });
    expect(versionsDeleteTotals(items[0] as Media)).toEqual({ videos: 0, versions: 2, bytes: 500 });
    expect(folderDeleteTotals(folder({ rendition_count: 4, total_bytes: 99 }))).toEqual({
      videos: 12,
      versions: 4,
      bytes: 99,
    });
  });

  it("texto da confirmação: quantidade, versões, espaço, pasta e aviso irreversível", () => {
    const lines = deleteConfirmLines({ videos: 3, versions: 2, bytes: 3 * 1024 * 1024 }, "@ana (06/10/2026)");
    expect(lines[0]).toBe("Pasta: @ana (06/10/2026).");
    expect(lines[1]).toBe("Serão apagados 3 vídeos (originais) e 2 versões editadas.");
    expect(lines[2]).toMatch(/^Espaço liberado: 3(,0)? MB\.$/);
    expect(lines[3]).toBe(DELETE_IRREVERSIBLE_TEXT);
    expect(DELETE_IRREVERSIBLE_TEXT).toBe("Isso apaga os arquivos do computador e não pode ser desfeito.");
  });

  it("concordância no singular e no plural", () => {
    expect(deleteConfirmLines({ videos: 1, versions: 0, bytes: 1 })[0]).toBe("Será apagado 1 vídeo (original).");
    expect(deleteConfirmLines({ videos: 1, versions: 1, bytes: 1 })[0]).toBe(
      "Será apagado 1 vídeo (original) e 1 versão editada.",
    );
    expect(deleteConfirmLines({ videos: 0, versions: 1, bytes: 10 })[0]).toBe(
      "Será apagada 1 versão editada; o vídeo original continua na biblioteca.",
    );
    expect(deleteConfirmLines({ videos: 0, versions: 2, bytes: 10 })[0]).toBe(
      "Serão apagadas 2 versões editadas; o vídeo original continua na biblioteca.",
    );
    const one = { deleted: 0, deleted_ids: [], skipped: [{ id: 9, code: "media_busy", message: "x" }], failed: [] };
    expect(deleteResultText(one)).toBe("0 vídeos apagados. 1 não apagado: vídeo 9 (está sendo baixado ou editado agora).");
  });

  it("pasta com vídeos que também estão em outras pastas: aviso na confirmação", () => {
    const totals = { videos: 5, versions: 0, bytes: 1 };
    expect(deleteConfirmLines(totals, "@ana", 2)).toContain(
      "2 desses vídeos também estão em outras pastas e sairão delas.",
    );
    expect(deleteConfirmLines(totals, "@ana", 1)).toContain("1 desses vídeos também está em outra pasta e sairá dela.");
    expect(deleteConfirmLines(totals, "@ana").some((l) => l.includes("outras pastas"))).toBe(false);
  });

  it("seleção grande em blocos de 500 e resumos somados", () => {
    const ids = Array.from({ length: 1147 }, (_, i) => i + 1);
    const chunks = chunkIds(ids);
    expect(chunks.map((c) => c.length)).toEqual([500, 500, 147]);
    expect(chunks.flat()).toEqual(ids);
    expect(chunkIds([])).toEqual([]);
    const merged = mergeDeleteResults([
      { deleted: 2, deleted_ids: [1, 2], skipped: [], failed: [] },
      { deleted: 1, deleted_ids: [3], skipped: [{ id: 4, code: "media_busy", message: "x" }], failed: [] },
    ]);
    expect(merged).toEqual({
      deleted: 3,
      deleted_ids: [1, 2, 3],
      skipped: [{ id: 4, code: "media_busy", message: "x" }],
      failed: [],
    });
  });

  it("resumo com pulados e falhas explicados", () => {
    const result: MediaDeleteResult = {
      deleted: 2,
      deleted_ids: [1, 2],
      skipped: [
        { id: 3, code: "media_busy", message: "x" },
        { id: 4, code: "export_in_progress", message: "x" },
      ],
      failed: [{ id: 5, code: "algo_novo", message: "Erro do servidor." }],
    };
    expect(deleteResultText(result, new Map([[3, "a.mp4"]]))).toBe(
      "2 vídeos apagados. 2 não apagados: a.mp4 (está sendo baixado ou editado agora); vídeo 4 (está numa " +
        "exportação em andamento). 1 com falha: vídeo 5 (Erro do servidor.).",
    );
    expect(deleteResultText({ deleted: 1, deleted_ids: [1], skipped: [], failed: [] })).toBe("1 vídeo apagado.");
    expect(versionsDeletedText(1, [])).toBe("1 versão editada apagada.");
    expect(versionsDeletedText(2, ["Arquivo em uso"])).toBe("2 versões editadas apagadas. 1 não apagada: Arquivo em uso.");
    expect(versionsDeletedText(0, ["Aguarde terminar.", "Em uso."])).toBe(
      "0 versões editadas apagadas. 2 não apagadas: Aguarde terminar; Em uso.",
    );
  });
});
