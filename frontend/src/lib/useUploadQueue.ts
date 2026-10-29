import { useEffect, useRef, useState } from "react";
import { ApiError, api, messageOf } from "../api/client";
import type { Media, UploadResult } from "../api/types";
import {
  addFiles,
  checkLocalFile,
  fileKey,
  isBusy,
  markDone,
  markError,
  markProgress,
  markStarted,
  nextToStart,
  removeItem,
  type UploadItem,
  uploadRetryDelay,
} from "./uploadQueue";

function wait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      resolve();
    });
  });
}

export interface UploadQueueApi {
  items: UploadItem[];
  busy: boolean;
  /** Acrescenta arquivos à fila e começa a enviar (até 2 por vez). Devolve quantos foram ignorados por repetição. */
  add: (files: File[]) => number;
  /** Remove da fila; cancela o envio se ainda estiver em andamento. Devolve a mídia já registrada, se houver. */
  remove: (key: string) => Media | null;
}

/** Fila de envio de vídeos locais. `onReady` recebe cada vídeo registrado (novo ou já existente na biblioteca). */
export function useUploadQueue(onReady: (media: Media) => void): UploadQueueApi {
  const [items, setItems] = useState<UploadItem[]>([]);
  // A fonte da verdade fica na ref (os callbacks do XHR leem o estado atual sem esperar um render).
  const itemsRef = useRef<UploadItem[]>([]);
  const files = useRef(new Map<string, File>());
  const controllers = useRef(new Map<string, AbortController>());
  const onReadyRef = useRef(onReady);
  useEffect(() => {
    onReadyRef.current = onReady;
  }, [onReady]);

  useEffect(() => {
    const running = controllers.current;
    // Sair da página cancela os envios em andamento (o servidor apaga o temporário).
    return () => {
      for (const controller of running.values()) controller.abort();
      running.clear();
    };
  }, []);

  function commit(next: UploadItem[]) {
    itemsRef.current = next;
    setItems(next);
  }

  function pump() {
    for (const key of nextToStart(itemsRef.current)) {
      const file = files.current.get(key);
      if (!file) {
        commit(markError(itemsRef.current, key, "Arquivo indisponível. Escolha de novo."));
        continue;
      }
      commit(markStarted(itemsRef.current, key));
      void send(key, file);
    }
  }

  async function send(key: string, file: File) {
    const controller = new AbortController();
    controllers.current.set(key, controller);
    let lastPercent = -1;
    const onProgress = (fraction: number) => {
      const percent = Math.floor(fraction * 100);
      if (percent === lastPercent) return;
      lastPercent = percent;
      commit(markProgress(itemsRef.current, key, fraction));
    };
    try {
      let result: UploadResult | null = null;
      for (let attempt = 0; result === null; attempt += 1) {
        try {
          result = await api.uploadLocalVideo(file, onProgress, controller.signal);
        } catch (err) {
          // 429: as vagas do servidor ainda estão ocupadas; tenta de novo após uma espera curta.
          const delay = err instanceof ApiError ? uploadRetryDelay(err.code, attempt) : null;
          if (delay === null || controller.signal.aborted) throw err;
          lastPercent = -1;
          commit(markStarted(itemsRef.current, key));
          await wait(delay, controller.signal);
          if (controller.signal.aborted) return;
        }
      }
      if (controller.signal.aborted) return;
      commit(markDone(itemsRef.current, key, result.media, result.duplicate));
      files.current.delete(key);
      onReadyRef.current(result.media);
    } catch (err) {
      if (controller.signal.aborted) return;
      commit(markError(itemsRef.current, key, messageOf(err)));
    } finally {
      controllers.current.delete(key);
      pump();
    }
  }

  function add(list: File[]): number {
    const result = addFiles(itemsRef.current, list, checkLocalFile);
    for (const file of list) {
      const key = fileKey(file);
      if (result.added.includes(key)) files.current.set(key, file);
    }
    commit(result.items);
    pump();
    return result.ignored;
  }

  function remove(key: string): Media | null {
    const item = itemsRef.current.find((i) => i.key === key) ?? null;
    controllers.current.get(key)?.abort();
    controllers.current.delete(key);
    files.current.delete(key);
    commit(removeItem(itemsRef.current, key));
    pump();
    return item?.media ?? null;
  }

  return { items, busy: isBusy(items), add, remove };
}
