// Miniaturas: GET /api/ext/media/{id}/thumbnail com o token (fetch) → blob URL. Concorrência limitada e URLs
// liberadas quando a lista é trocada.

export class ThumbLoader {
  constructor(getClient, concurrency = 4) {
    this.getClient = getClient;
    this.concurrency = concurrency;
    this.urls = new Map(); // id → blob URL
    this.queue = [];
    this.running = 0;
    this.generation = 0;
  }

  /** Coloca a miniatura no <img> quando chegar; sem miniatura, o elemento fica com o fundo neutro. */
  load(mediaId, img) {
    const id = Number(mediaId);
    if (this.urls.has(id)) {
      img.src = this.urls.get(id);
      return;
    }
    this.queue.push({ id, img, generation: this.generation });
    // Depois do código síncrono atual: o <img> é criado antes de entrar no DOM.
    queueMicrotask(() => this.pump());
  }

  pump() {
    while (this.running < this.concurrency && this.queue.length) {
      const task = this.queue.shift();
      if (task.generation !== this.generation || !task.img.isConnected) continue;
      this.running += 1;
      this.fetchOne(task).finally(() => {
        this.running -= 1;
        this.pump();
      });
    }
  }

  async fetchOne({ id, img, generation }) {
    try {
      const client = this.getClient();
      if (!client) return;
      const blob = await client.thumbnail(id);
      if (generation !== this.generation) return;
      const url = this.urls.get(id) ?? URL.createObjectURL(blob);
      this.urls.set(id, url);
      if (img.isConnected) img.src = url;
    } catch {
      img.closest(".thumb")?.classList.add("no-thumb");
    }
  }

  /** Libera todas as URLs (lista nova, troca de filtro, aba escondida). */
  reset() {
    this.generation += 1;
    this.queue = [];
    for (const url of this.urls.values()) URL.revokeObjectURL(url);
    this.urls.clear();
  }
}
