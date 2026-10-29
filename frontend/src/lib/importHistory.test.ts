import { describe, expect, it } from "vitest";
import type { HistoryHideResult } from "../api/types";
import {
  DELETE_WARNING,
  IN_PROGRESS_HINT,
  deleteMediaLabel,
  historyConfirmText,
  historyConfirmTitle,
  historyHideBody,
  historyResultMessage,
  removeBlockedHint,
  sharedWarning,
} from "./importHistory";

const preview: HistoryHideResult = {
  batches: 2,
  groups: 1,
  video_count: 4,
  total_bytes: 3 * 1024 * 1024,
  shared_count: 0,
  dry_run: true,
  deleted: null,
};

describe("histórico de importações", () => {
  it("corpo: um lote, um grupo ou todos os terminados; apagar vídeos só quando marcado", () => {
    expect(historyHideBody({ kind: "batch", id: 7, label: "@perfil" }, false)).toEqual({ batch_ids: [7] });
    expect(historyHideBody({ kind: "group", id: 3, label: "3 perfis" }, true)).toEqual({
      group_ids: [3],
      delete_media: true,
    });
    expect(historyHideBody({ kind: "all" }, false, true)).toEqual({ all_finished: true, dry_run: true });
  });

  it("em andamento não pode ser removido", () => {
    expect(removeBlockedHint(true)).toBe(IN_PROGRESS_HINT);
    expect(IN_PROGRESS_HINT).toBe("Cancele ou aguarde terminar");
    expect(removeBlockedHint(false)).toBeNull();
  });

  it("textos da confirmação: remover só oculta; a opção de apagar mostra vídeos e espaço", () => {
    const one = { kind: "batch" as const, id: 1, label: "@perfil" };
    expect(historyConfirmTitle(one)).toBe("Remover do histórico");
    expect(historyConfirmTitle({ kind: "all" })).toBe("Limpar histórico");
    expect(historyConfirmText(one, null)).toContain("Os vídeos continuam na Biblioteca");
    expect(historyConfirmText({ kind: "all" }, preview)).toMatch(/^3 importações terminadas saem/);
    expect(deleteMediaLabel(one, null)).toBe("Também apagar os vídeos desta importação");
    expect(deleteMediaLabel({ kind: "all" }, preview)).toMatch(/^Também apagar os vídeos destas importações \(4 vídeos, /);
    expect(DELETE_WARNING).toBe("Isso apaga os arquivos do computador e não pode ser desfeito.");
  });

  it("mensagem do resultado", () => {
    expect(historyResultMessage({ ...preview, dry_run: false, batches: 1, groups: 0 })).toBe(
      "1 importação removida do histórico.",
    );
    expect(historyResultMessage({ ...preview, batches: 0, groups: 0 })).toBe(
      "Nenhuma importação terminada para remover do histórico.",
    );
    const busy = { id: 3, code: "media_busy", message: "Mídia com job em andamento." };
    const deleted = { deleted: 2, deleted_ids: [1, 2], skipped: [busy], failed: [] };
    expect(historyResultMessage({ ...preview, deleted })).toBe(
      "3 importações removidas do histórico. 2 vídeos apagados, 1 pulado (Mídia com job em andamento.).",
    );
    const two = { ...deleted, skipped: [busy, { ...busy, id: 4 }] };
    expect(historyResultMessage({ ...preview, deleted: two })).toContain("2 pulados (Mídia com job em andamento.)");
  });
});

describe("vídeos compartilhados e texto conforme a opção", () => {
  it("avisa quantos vídeos também estão em outras importações", () => {
    expect(sharedWarning(preview)).toBeNull();
    expect(sharedWarning({ ...preview, shared_count: 1 })).toBe(
      "1 desses vídeos também está em outras importações e sairá delas.",
    );
    expect(sharedWarning({ ...preview, shared_count: 3 })).toBe(
      "3 desses vídeos também estão em outras importações e sairão delas.",
    );
  });

  it("com a opção marcada o texto não diz que os vídeos continuam", () => {
    const one = { kind: "batch" as const, id: 1, label: "@perfil" };
    expect(historyConfirmText(one, null, true)).toContain("também serão apagados");
    expect(historyConfirmText(one, null, true)).not.toContain("continuam");
    expect(historyConfirmText({ kind: "all" }, preview, true)).toContain("também serão apagados");
  });
});
