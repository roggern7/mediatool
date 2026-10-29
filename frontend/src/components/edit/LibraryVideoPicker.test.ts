import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { EditFolder, Media } from "../../api/types";
import { EditFolderList } from "./EditFolderList";
import { LibraryVideoPicker } from "./LibraryVideoPicker";
import { VideoPreviewModal } from "./VideoPreviewModal";
import { VideoThumb } from "./VideoThumb";

// Sem DOM no ambiente de teste (node): confere a marcação renderizada no servidor.

const video = {
  id: 7,
  media_type: "video",
  source_platform: "instagram",
  source_profile: "ana",
  original_filename: "cdn-abc.mp4",
  original_url: "/api/media/7/original",
  thumbnail_url: null,
  duration: null,
  width: null,
  height: null,
  file_size: 2048,
  created_at: "2026-10-06T15:30:00.000Z",
  taken_at: null,
  renditions: [{ id: 70, file_size: 100 }],
} as unknown as Media;

const folders: EditFolder[] = [
  {
    key: "group:1",
    kind: "group",
    import_mode: "profile",
    profiles: ["ana", "bia"],
    created_at: "2026-10-06T15:30:00.000Z",
    day: null,
    video_count: 3,
    rendition_count: 1,
    total_bytes: 4096,
    shared_count: 0,
  },
  {
    key: "local:2026-10-28",
    kind: "local",
    import_mode: null,
    profiles: [],
    created_at: "2026-10-28T12:00:00.000Z",
    day: "2026-10-28",
    video_count: 1,
    rendition_count: 0,
    total_bytes: 10,
    shared_count: 0,
  },
];

describe("LibraryVideoPicker", () => {
  it("abre na lista de pastas (carregando) com busca global e adicionar desabilitado", () => {
    const html = renderToStaticMarkup(
      createElement(LibraryVideoPicker, { selectedIds: new Set<number>(), onAdd: () => undefined }),
    );
    expect(html).toContain('placeholder="Buscar por perfil ou nome do arquivo"');
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Adicionar selecionados<\/button>/);
    expect(html).not.toContain("<table");
    expect(html).not.toContain("Apagar selecionados");
  });

  it("lista de pastas: nome, data, contagem, versões e um botão por pasta", () => {
    const html = renderToStaticMarkup(createElement(EditFolderList, { folders, onOpen: () => undefined }));
    expect(html).toContain("2 perfis: @ana, @bia");
    expect(html).toContain("3 vídeos · 1 versão editada");
    expect(html).toContain("Enviados do computador — 28/10/2026");
    expect(html.match(/<button/g)).toHaveLength(2);
  });
});

describe("VideoThumb", () => {
  it("sem thumbnail: vídeo do original em #t=0.5, só metadata, mudo, como botão de prévia", () => {
    const html = renderToStaticMarkup(createElement(VideoThumb, { media: video, onOpen: () => undefined }));
    expect(html).toContain('src="/api/media/7/original#t=0.5"');
    expect(html).toContain('preload="metadata"');
    expect(html).toContain("muted");
    expect(html).toContain('aria-label="Ver prévia de cdn-abc.mp4"');
  });

  it("com thumbnail: usa a imagem e não carrega o vídeo", () => {
    const media = { ...video, thumbnail_url: "/api/media/7/thumbnail" } as Media;
    const html = renderToStaticMarkup(createElement(VideoThumb, { media }));
    expect(html).toContain('src="/api/media/7/thumbnail"');
    expect(html).not.toContain("<video");
  });
});

describe("VideoPreviewModal", () => {
  it("player com controles, dados do vídeo, versões e ações", () => {
    const html = renderToStaticMarkup(
      createElement(VideoPreviewModal, {
        media: video,
        inSelection: false,
        onClose: () => undefined,
        onAdd: () => undefined,
        onDeleteMedia: () => undefined,
        onDeleteVersions: () => undefined,
      }),
    );
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toMatch(/<video[^>]*src="\/api\/media\/7\/original"[^>]*controls/);
    expect(html).toContain("@ana");
    expect(html).toContain("1 versão editada");
    expect(html).toContain("Apagar versões editadas (1)");
    expect(html).toContain("Apagar vídeo");
    expect(html).toContain("Adicionar à edição");
  });

  it("já na seleção e sem ações de apagar", () => {
    const html = renderToStaticMarkup(
      createElement(VideoPreviewModal, { media: video, inSelection: true, onClose: () => undefined, onAdd: () => undefined }),
    );
    expect(html).toContain("Já está na seleção");
    expect(html).not.toContain("Apagar");
  });
});
