// Seleção múltipla da Biblioteca (id → {media_type, role}); compartilhada entre Biblioteca, Processar e Exportar.

export const MAX_SELECTION = 5000;

export class Selection {
  constructor(entries = []) {
    this.map = new Map(entries.map(([id, meta]) => [Number(id), meta ?? null]));
  }

  get size() {
    return this.map.size;
  }

  has(id) {
    return this.map.has(Number(id));
  }

  toggle(item) {
    const id = Number(item.id);
    if (this.map.has(id)) this.map.delete(id);
    else if (this.map.size < MAX_SELECTION) this.map.set(id, { media_type: item.media_type ?? null, role: item.role ?? null });
    return this.map.has(id);
  }

  /** Acrescenta ids (ex.: "selecionar todos do lote"); devolve quantos entraram. */
  addIds(ids, meta = null) {
    let added = 0;
    for (const raw of ids) {
      const id = Number(raw);
      if (!Number.isInteger(id) || id < 1 || this.map.has(id)) continue;
      if (this.map.size >= MAX_SELECTION) break;
      this.map.set(id, meta);
      added += 1;
    }
    return added;
  }

  addItems(items) {
    let added = 0;
    for (const item of items) {
      if (!this.map.has(Number(item.id)) && this.map.size < MAX_SELECTION) {
        this.map.set(Number(item.id), { media_type: item.media_type ?? null, role: item.role ?? null });
        added += 1;
      }
    }
    return added;
  }

  clear() {
    this.map.clear();
  }

  ids() {
    return [...this.map.keys()];
  }

  entries() {
    return [...this.map.entries()];
  }

  /** Só capas (para "exportar só capas"): ids com papel conhecido "cover". */
  coverIds() {
    return this.entries().filter(([, meta]) => meta && meta.role === "cover").map(([id]) => id);
  }

  counts() {
    const c = { total: this.map.size, videos: 0, photos: 0, covers: 0, unknown: 0 };
    for (const meta of this.map.values()) {
      if (!meta || !meta.media_type) c.unknown += 1;
      else if (meta.role === "cover") c.covers += 1;
      else if (meta.media_type === "video") c.videos += 1;
      else c.photos += 1;
    }
    return c;
  }

  toJSON() {
    return this.entries();
  }
}

export function selectionText(selection) {
  const c = selection.counts();
  if (!c.total) return "Nenhum item selecionado";
  const parts = [];
  if (c.videos) parts.push(`${c.videos} vídeo${c.videos > 1 ? "s" : ""}`);
  if (c.photos) parts.push(`${c.photos} foto${c.photos > 1 ? "s" : ""}`);
  if (c.covers) parts.push(`${c.covers} capa${c.covers > 1 ? "s" : ""}`);
  if (c.unknown) parts.push(`${c.unknown} de tipo não identificado`);
  return `${c.total} selecionado${c.total > 1 ? "s" : ""} (${parts.join(", ")})`;
}
