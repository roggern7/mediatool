import { useState } from "react";
import { api, messageOf } from "../../api/client";
import type { CapCutDesign, CapCutDraft, CapCutSettings, CapCutStyle } from "../../api/types";
import { styleSummaryText } from "../../lib/capcut";
import { duplicateName, isEditableStyle, normalizeDesign, startingDesign } from "../../lib/captionDesign";
import { formatDateTime } from "../../lib/format";
import { useQuery } from "../../lib/useQuery";
import { useToast } from "../../lib/toast";
import { ConfirmDialog, ErrorLine, Field, Modal, Pill, SkeletonRows, Spinner } from "../ui";
import { CaptionEditor } from "./CaptionEditor";

interface EditorTarget {
  /** Reinicia o editor a cada abertura. */
  seq: number;
  /** Modelo editado; null = novo (inclusive "Duplicar"). */
  style: CapCutStyle | null;
  design: CapCutDesign;
  name: string;
}

/**
 * Modelos de legenda: lista, editor visual (com a prévia real), "Salvar modelo do CapCut", padrão e exclusão.
 * `onChanged` avisa quando a lista mudou (o seletor de modelo do lote recarrega).
 */
export function CaptionStylesSection({
  settings,
  onChanged,
}: {
  settings: CapCutSettings | undefined;
  onChanged?: () => void;
}) {
  const toast = useToast();
  const styles = useQuery("capcut-styles", () => api.capcutStyles());
  const [list, setList] = useState<CapCutStyle[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState<CapCutStyle | null>(null);
  const [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<EditorTarget | null>(null);
  const items = list ?? styles.data ?? null;

  function openEditor(style: CapCutStyle | null, design: CapCutDesign, name: string) {
    setEditor((prev) => ({ seq: (prev?.seq ?? 0) + 1, style, design, name }));
  }

  async function reloadList() {
    setList(await api.capcutStyles().catch(() => null));
    styles.reload();
    onChanged?.();
  }

  async function makeDefault(style: CapCutStyle) {
    setBusy(true);
    try {
      setList(await api.setDefaultCapcutStyle(style.id));
      onChanged?.();
      toast.push("ok", `"${style.name}" é o modelo padrão.`);
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(style: CapCutStyle) {
    setBusy(true);
    try {
      await api.deleteCapcutStyle(style.id);
      setList(await api.capcutStyles());
      onChanged?.();
      toast.push("ok", "Modelo excluído.");
      setDeleting(null);
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="row" style={{ justifyContent: "flex-end", marginBottom: 10 }}>
        <button
          type="button"
          className="btn btn-sm"
          disabled={editor !== null}
          onClick={() => openEditor(null, startingDesign(items), "")}
        >
          Novo modelo
        </button>
        <button type="button" className="btn btn-sm" onClick={() => setAdding(true)}>
          Salvar modelo do CapCut
        </button>
      </div>
      {items === null ? (
        styles.error ? (
          <ErrorLine message={styles.error} onRetry={styles.reload} />
        ) : (
          <SkeletonRows rows={2} />
        )
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Modelo</th>
                <th>Resumo</th>
                <th>Origem</th>
                <th aria-label="Ações" />
              </tr>
            </thead>
            <tbody>
              {items.map((style) => (
                <tr key={style.id}>
                  <td>
                    {style.name} {style.is_default ? <Pill tone="accent">Padrão</Pill> : null}
                  </td>
                  <td className="muted small">{styleSummaryText(style.summary)}</td>
                  <td className="muted small">{style.builtin ? "Embutido" : (style.source_draft ?? "—")}</td>
                  <td className="nowrap" style={{ textAlign: "right" }}>
                    {isEditableStyle(style) && style.design ? (
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        disabled={busy || editor !== null}
                        onClick={() => openEditor(style, normalizeDesign(style.design), style.name)}
                      >
                        Editar
                      </button>
                    ) : null}
                    {style.design ? (
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        disabled={busy || editor !== null}
                        title="Abre o editor com uma cópia deste modelo."
                        onClick={() => openEditor(null, normalizeDesign(style.design), duplicateName(style.name))}
                      >
                        Duplicar
                      </button>
                    ) : null}
                    {!style.is_default ? (
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        disabled={busy}
                        onClick={() => void makeDefault(style)}
                      >
                        Usar como padrão
                      </button>
                    ) : null}
                    {!style.builtin ? (
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        disabled={busy || editor?.style?.id === style.id}
                        onClick={() => setDeleting(style)}
                      >
                        Excluir
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {items !== null && !items.some((s) => s.design) ? (
        <p className="muted small" style={{ marginBottom: 0 }}>
          Modelos salvos de um projeto do CapCut não podem ser editados aqui; use "Novo modelo" para montar um no editor.
        </p>
      ) : null}
      {editor ? (
        <CaptionEditor
          key={editor.seq}
          style={editor.style}
          initialDesign={editor.design}
          initialName={editor.name}
          maxWords={settings?.max_words}
          maxChars={settings?.max_chars}
          onClose={() => setEditor(null)}
          onSaved={() => void reloadList()}
        />
      ) : null}
      {adding ? (
        <SaveStyleDialog
          onClose={() => setAdding(false)}
          onSaved={async () => {
            setAdding(false);
            await reloadList();
          }}
        />
      ) : null}
      {deleting ? (
        <ConfirmDialog
          title="Excluir modelo"
          confirmLabel="Excluir"
          danger
          busy={busy}
          onConfirm={() => void remove(deleting)}
          onCancel={() => setDeleting(null)}
        >
          <p>
            Excluir o modelo "{deleting.name}"? Os vídeos já legendados não mudam.
            {deleting.is_default ? " O padrão volta a ser o estilo embutido." : ""}
          </p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

function SaveStyleDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const drafts = useQuery("capcut-drafts", () => api.capcutDrafts());
  const [draftName, setDraftName] = useState("");
  const [segment, setSegment] = useState(0);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const chosen: CapCutDraft | undefined = drafts.data?.find((d) => d.name === draftName);

  async function save() {
    setBusy(true);
    try {
      await api.createCapcutStyle({ name: name.trim(), draft_name: draftName, segment_index: segment });
      toast.push("ok", "Modelo salvo.");
      onSaved();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Salvar modelo do CapCut"
      onClose={busy ? () => undefined : onClose}
      wide
      actions={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            Cancelar
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || !draftName || !name.trim()}
            onClick={() => void save()}
          >
            {busy ? <Spinner /> : null}
            Salvar
          </button>
        </>
      }
    >
      <p className="muted small" style={{ marginTop: 0 }}>
        No CapCut, crie um projeto com uma legenda no estilo desejado (fonte, cores, contorno, animação, modelo de
        legenda). Para definir a cor do destaque, pinte uma palavra com essa cor. Feche o CapCut e escolha o projeto
        aqui.
      </p>
      {drafts.error ? <ErrorLine message={drafts.error} onRetry={drafts.reload} /> : null}
      <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}>
        <Field label="Projeto do CapCut" htmlFor="capcut-draft">
          <select
            id="capcut-draft"
            className="select"
            value={draftName}
            onChange={(e) => {
              setDraftName(e.target.value);
              setSegment(0);
            }}
          >
            <option value="">{drafts.data === undefined ? "Carregando…" : "Escolha um projeto"}</option>
            {(drafts.data ?? []).map((d) => (
              <option key={d.name} value={d.name}>
                {d.name}
                {d.modified_at ? ` (${formatDateTime(d.modified_at)})` : ""}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Texto usado como modelo"
          htmlFor="capcut-segment"
          hint={chosen?.preview ? `Primeiro texto: "${chosen.preview}"` : undefined}
        >
          <select
            id="capcut-segment"
            className="select"
            value={segment}
            disabled={!chosen}
            onChange={(e) => setSegment(Number(e.target.value))}
          >
            {Array.from({ length: Math.max(1, chosen?.text_segments ?? 1) }, (_, i) => (
              <option key={i} value={i}>
                {i + 1}º texto
              </option>
            ))}
          </select>
        </Field>
        <Field label="Nome do modelo" htmlFor="capcut-style-name">
          <input
            id="capcut-style-name"
            className="input"
            value={name}
            maxLength={60}
            onChange={(e) => setName(e.target.value)}
            placeholder="Ex.: Amarelo com contorno"
          />
        </Field>
      </div>
      {drafts.data && drafts.data.length === 0 ? (
        <p className="muted small">Nenhum projeto do CapCut com texto foi encontrado.</p>
      ) : null}
    </Modal>
  );
}
