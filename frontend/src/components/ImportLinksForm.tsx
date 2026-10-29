import { type ReactNode, useDeferredValue, useMemo, useState } from "react";
import { ApiError, api } from "../api/client";
import type { Batch, EditSettings } from "../api/types";
import { formatCount } from "../lib/format";
import { invalidCountText, startLabel } from "../lib/importOptions";
import {
  buildLinksRequest,
  compactLinkEntries,
  interpretLinksStartError,
  linkEntriesSummary,
  linksCountText,
  linksCreatedMessage,
  linksStartBlocker,
  linksStartSummary,
  parseLinkList,
} from "../lib/importLinks";
import { entriesKey, settledPreview } from "../lib/importStart";
import { useDebounced } from "../lib/useDebounced";
import { useQuery } from "../lib/useQuery";
import { useToast } from "../lib/toast";
import { ErrorLine, Field, Spinner } from "./ui";

/** Espera depois da última tecla antes de pedir a pré-validação ao backend. */
const PREVIEW_DEBOUNCE_MS = 400;
/** Entradas inválidas listadas na tela (as demais entram em "e mais N"). */
const INVALID_SHOWN_MAX = 100;

/**
 * Formulário do modo "Por link": links de publicações, um por linha. Quantidade e tipo de mídia não se aplicam.
 * A estimativa local é só o número provisório; válidos, inválidos e repetidos exibidos e enviados vêm do backend.
 */
export function ImportLinksForm({
  text,
  onTextChange,
  sessionOk,
  paused,
  process,
  presetId,
  edit,
  editProblem,
  processing,
  onCreated,
  onFailed,
}: {
  /** Rascunho da lista (fica na tela ao trocar de modo). */
  text: string;
  onTextChange: (text: string) => void;
  sessionOk: boolean;
  /** Pausa global do Instagram vigente. */
  paused: boolean;
  process: boolean;
  presetId: number | null;
  /** "Editar após baixar": configurações da edição (null nas outras escolhas) e problema do formulário. */
  edit: EditSettings | null;
  editProblem: string | null;
  /** Bloco "Processamento" da tela, o mesmo do modo por perfil. */
  processing: ReactNode;
  onCreated: (batch: Batch) => void;
  onFailed: () => void;
}) {
  const toast = useToast();
  const [submitting, setSubmitting] = useState(false);

  const deferredText = useDeferredValue(text);
  const draft = useMemo(
    () => ({ key: entriesKey(compactLinkEntries(deferredText)), estimate: parseLinkList(deferredText).valid }),
    [deferredText],
  );
  const requestedKey = useDebounced(draft.key, PREVIEW_DEBOUNCE_MS);
  const previewQuery = useQuery(`links-preview:${requestedKey}`, async () =>
    requestedKey ? { key: requestedKey, data: await api.previewLinks(requestedKey.split("\n")) } : null,
  );
  const hasText = text.trim() !== "";
  const typing = deferredText !== text;
  const checking = typing || requestedKey !== draft.key || previewQuery.loading;
  const preview = hasText
    ? settledPreview({
        currentKey: draft.key,
        requestedKey,
        typing,
        loading: previewQuery.loading,
        result: previewQuery.data,
      })
    : null;
  const previewError = hasText && !checking && !preview ? previewQuery.error : null;
  const invalidCount = preview ? preview.invalid.length : 0;
  const canSubmit = linksStartBlocker({ sessionOk, paused, submitting, preview, process, presetId, editProblem }) === null;
  const summary = linksStartSummary(preview);

  async function submit() {
    const body = buildLinksRequest(preview, { processAfterDownload: process, presetId, edit });
    if (!canSubmit || !body) return;
    setSubmitting(true);
    try {
      const batch = await api.createLinksImport(body);
      toast.push("ok", linksCreatedMessage(batch, body.links.length, process, edit !== null));
      onCreated(batch);
    } catch (err) {
      const failure = interpretLinksStartError(err instanceof ApiError ? err : null);
      toast.push("err", failure.error);
      if (failure.refreshPreview) previewQuery.reload();
      onFailed();
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (canSubmit) void submit();
      }}
    >
      <Field
        label="Links das publicações"
        htmlFor="imp-links"
        hint="Um link por linha (vírgula e espaço também separam). Aceita links com /p/, /reel/ e /reels/, inclusive com parâmetros no fim. Linhas vazias e repetidas são ignoradas."
      >
        <textarea
          id="imp-links"
          className="input textarea mono"
          rows={5}
          value={text}
          onChange={(e) => onTextChange(e.target.value)}
          placeholder={"https://www.instagram.com/p/CODIGO/\nhttps://www.instagram.com/reel/CODIGO/"}
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          aria-describedby="imp-links-status"
        />
      </Field>
      <div className="muted small" style={{ marginTop: 8 }}>
        Cada link baixa a publicação inteira: foto, Reel, vídeo ou todas as mídias do carrossel.
      </div>

      {processing}

      <div id="imp-links-status" style={{ marginTop: 16 }}>
        {!hasText ? null : preview ? (
          <div className="small" role="status">
            {linkEntriesSummary({
              valid: preview.valid.length,
              duplicates: preview.duplicates,
              invalid: preview.invalid.length,
            })}
          </div>
        ) : previewError ? (
          // Sem conferência não há contagem válida para mostrar.
          <ErrorLine message={previewError} onRetry={previewQuery.reload} />
        ) : !checking ? (
          <div className="muted small" role="status">
            Nenhum link na lista.
          </div>
        ) : (
          <div className="muted small" role="status">
            Conferindo a lista. Estimativa provisória: cerca de {linksCountText(draft.estimate)}; o número final
            aparece quando a conferência terminar.
          </div>
        )}
        {preview && preview.invalid.length > 0 ? (
          <div className="alert warn" role="alert" style={{ marginTop: 8, flexDirection: "column" }}>
            <strong id="imp-links-invalid-title">
              {invalidCountText(preview.invalid.length)}:{" "}
              {preview.valid.length > 0
                ? "ficam de fora; a importação começa só com os links válidos."
                : "nenhum link válido na lista."}
            </strong>
            <ul className="invalid-list" aria-labelledby="imp-links-invalid-title">
              {preview.invalid.slice(0, INVALID_SHOWN_MAX).map((item, i) => (
                <li key={`${i}:${item.entry}`}>
                  <span className="mono">{item.entry}</span>: {item.reason}
                </li>
              ))}
              {preview.invalid.length > INVALID_SHOWN_MAX ? (
                <li>e mais {formatCount(preview.invalid.length - INVALID_SHOWN_MAX)}</li>
              ) : null}
            </ul>
          </div>
        ) : null}
        {summary ? (
          <div style={{ marginTop: 8 }}>
            <strong>{summary}</strong>
          </div>
        ) : null}
      </div>

      <div className="row" style={{ marginTop: 16, flexWrap: "wrap" }}>
        <button type="submit" className="btn btn-primary" disabled={!canSubmit}>
          {submitting ? <Spinner /> : null}
          {startLabel({ process, invalid: invalidCount, edit: edit !== null })}
        </button>
      </div>
    </form>
  );
}
