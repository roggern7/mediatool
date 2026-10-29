import { useRef, useState } from "react";
import { Scissors } from "lucide-react";
import { api, messageOf } from "../api/client";
import type { Media } from "../api/types";
import { EditProgressPanel } from "../components/edit/EditProgressPanel";
import { EditSelectionList } from "../components/edit/EditSelectionList";
import { EditSettingsForm } from "../components/edit/EditSettingsForm";
import { LibraryVideoPicker } from "../components/edit/LibraryVideoPicker";
import { LocalFilesDrop } from "../components/edit/LocalFilesDrop";
import { Spinner } from "../components/ui";
import {
  DEFAULT_EDIT_FORM,
  type EditForm,
  MAX_EDIT_ITEMS,
  MAX_ITEMS_TEXT,
  mediaName,
  mergeSelection,
  startLabel,
  toEditRequest,
  validateEditForm,
} from "../lib/editSettings";
import { useToast } from "../lib/toast";
import type { Trim } from "../lib/trim";
import { useUploadQueue } from "../lib/useUploadQueue";

/** Edição de vídeos: escolher (biblioteca e arquivos locais), configurar e acompanhar o lote. */
export function EditPage() {
  const toast = useToast();
  const [selection, setSelection] = useState<Media[]>([]);
  // Cópia atual da seleção para os retornos assíncronos dos envios (sem depender do render).
  const selectionRef = useRef<Media[]>([]);
  const [form, setForm] = useState<EditForm>(DEFAULT_EDIT_FORM);
  // Recorte manual por vídeo da seleção (id da mídia); vídeo que sai da seleção perde o recorte.
  const [trims, setTrims] = useState<ReadonlyMap<number, Trim>>(() => new Map());
  const [starting, setStarting] = useState(false);
  const [running, setRunning] = useState<{ batchId: number; names: Map<number, string> } | null>(null);
  // Sobe a cada envio local pronto (novo ou duplicado): a lista da biblioteca recarrega.
  const [libraryVersion, setLibraryVersion] = useState(0);

  function commitSelection(next: Media[]) {
    selectionRef.current = next;
    setSelection(next);
    const kept = new Set(next.map((m) => m.id));
    setTrims((prev) =>
      [...prev.keys()].every((id) => kept.has(id)) ? prev : new Map([...prev].filter(([id]) => kept.has(id))),
    );
  }

  function setTrim(id: number, trim: Trim | null) {
    setTrims((prev) => {
      const next = new Map(prev);
      if (trim) next.set(id, trim);
      else next.delete(id);
      return next;
    });
  }

  function add(items: Media[]) {
    const result = mergeSelection(selectionRef.current, items);
    if (result.added > 0) commitSelection(result.selection);
    if (result.overflow > 0) toast.push("err", MAX_ITEMS_TEXT);
  }

  function removeFromSelection(id: number) {
    commitSelection(selectionRef.current.filter((m) => m.id !== id));
  }

  /** Vídeos apagados da biblioteca saem da seleção. */
  function removeDeleted(ids: number[]) {
    const gone = new Set(ids);
    if (selectionRef.current.some((m) => gone.has(m.id))) {
      commitSelection(selectionRef.current.filter((m) => !gone.has(m.id)));
    }
  }

  const uploads = useUploadQueue((media) => {
    add([media]);
    setLibraryVersion((v) => v + 1);
  });
  const selectedIds = new Set(selection.map((m) => m.id));
  const formError = validateEditForm(form);
  const blocker =
    selection.length === 0
      ? "Selecione ao menos um vídeo."
      : selection.length > MAX_EDIT_ITEMS
        ? MAX_ITEMS_TEXT
        : uploads.busy
          ? "Aguarde os envios terminarem."
          : formError;

  function removeUpload(key: string) {
    const media = uploads.remove(key);
    if (media) removeFromSelection(media.id);
  }

  async function start() {
    if (blocker || starting) return;
    setStarting(true);
    try {
      const batch = await api.startEdit(toEditRequest(selection.map((m) => m.id), form, trims));
      setRunning({ batchId: batch.id, names: new Map(selection.map((m) => [m.id, mediaName(m)])) });
      toast.push("ok", `Edição iniciada para ${selection.length} ${selection.length === 1 ? "vídeo" : "vídeos"}.`);
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>Editar</h1>
      </div>
      <p className="muted" style={{ marginTop: -8, marginBottom: 16 }}>
        Escolha vídeos da biblioteca ou do computador, ajuste as configurações e inicie. Cada vídeo editado vira uma
        nova versão na biblioteca.
      </p>

      {running ? (
        <EditProgressPanel batchId={running.batchId} names={running.names} onClose={() => setRunning(null)} />
      ) : null}

      <div className="edit-layout">
        <div style={{ minWidth: 0 }}>
          <section className="section">
            <div className="section-head">
              <h2>Vídeos da biblioteca</h2>
            </div>
            <LibraryVideoPicker
              selectedIds={selectedIds}
              onAdd={add}
              onDeleted={removeDeleted}
              version={libraryVersion}
            />
          </section>
          <section className="section">
            <div className="section-head">
              <h2>Arquivos do computador</h2>
            </div>
            <LocalFilesDrop
              queue={uploads}
              onRemove={removeUpload}
              onIgnored={(n) =>
                toast.push("info", n === 1 ? "Um arquivo já estava na lista." : `${n} arquivos já estavam na lista.`)
              }
            />
          </section>
          <section className="section">
            <div className="section-head">
              <h2>Selecionados ({selection.length})</h2>
            </div>
            <EditSelectionList
              items={selection}
              form={form}
              trims={trims}
              onTrim={setTrim}
              onRemove={removeFromSelection}
              onClear={() => commitSelection([])}
            />
          </section>
        </div>

        <aside className="panel edit-aside">
          <EditSettingsForm form={form} onChange={setForm} trimCount={trims.size} />
          <div className="edit-start">
            <button
              type="button"
              className="btn btn-primary"
              disabled={blocker !== null || starting}
              onClick={() => void start()}
            >
              {starting ? <Spinner /> : <Scissors size={14} strokeWidth={1.75} aria-hidden="true" />}
              {startLabel(selection.length)}
            </button>
            {blocker ? <div className="field-hint">{blocker}</div> : null}
          </div>
        </aside>
      </div>
    </div>
  );
}
