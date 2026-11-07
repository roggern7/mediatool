import { useState } from "react";
import { api, capcutPreviewFileUrl, messageOf } from "../../api/client";
import type { CapCutDesign, CapCutPreview } from "../../api/types";
import { designsEqual, isPreviewActive, previewProgress, previewStatusText } from "../../lib/captionDesign";
import { capcutTone } from "../../lib/capcut";
import { nextPollMs } from "../../lib/polling";
import { useQuery } from "../../lib/useQuery";
import { useToast } from "../../lib/toast";
import { Pill, ProgressBar, Spinner } from "../ui";

/**
 * "Gerar prévia real no CapCut": envia o design atual (e o vídeo escolhido) para POST /api/capcut/previews,
 * acompanha o andamento e mostra o MP4 exportado pelo CapCut.
 */
export function RealPreviewPanel({
  design,
  mediaId,
  blockedReason,
}: {
  design: CapCutDesign;
  mediaId: number | null;
  /** Motivo para não gerar (ex.: modelo com erros); null = pode gerar. */
  blockedReason: string | null;
}) {
  const toast = useToast();
  const [started, setStarted] = useState<{ preview: CapCutPreview; design: CapCutDesign } | null>(null);
  const [pollMs, setPollMs] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const id = started?.preview.id ?? null;
  const query = useQuery<CapCutPreview | null>(
    `capcut-preview:${id ?? ""}`,
    () =>
      id === null
        ? Promise.resolve(null)
        : api.capcutPreview(id).then((p) => {
            setPollMs((prev) => (isPreviewActive(p) ? nextPollMs(prev, true) : null));
            return p;
          }),
    id === null ? null : pollMs,
  );
  const preview = query.data && started && query.data.id === id ? query.data : (started?.preview ?? null);
  const active = isPreviewActive(preview);

  async function generate() {
    setBusy(true);
    try {
      const created = await api.createCapcutPreview(mediaId === null ? { design } : { design, media_id: mediaId });
      setStarted({
        preview: {
          id: created.id,
          status: created.status,
          stage: null,
          progress: 0,
          detail: null,
          error_message: null,
          file_url: null,
        },
        design,
      });
      setPollMs(nextPollMs(null, true));
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (id === null) return;
    setBusy(true);
    try {
      await api.cancelCapcutPreview(id);
      setPollMs(nextPollMs(null, true));
      query.reload();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <p className="muted small" style={{ margin: "0 0 8px" }}>
        Gera um trecho curto (até 6 s, 720p) com o CapCut de verdade, usando o vídeo escolhido acima (ou o mais recente
        da biblioteca), no modo de exportação das configurações: no Isolado o CapCut roda numa área de trabalho
        separada e você pode usar o PC; no Interativo ele usa o mouse por alguns segundos e o PC precisa ficar livre.
      </p>
      <div className="row">
        <button
          type="button"
          className="btn btn-sm"
          disabled={busy || active || blockedReason !== null}
          title={blockedReason ?? undefined}
          onClick={() => void generate()}
        >
          {busy && !active ? <Spinner /> : null}
          Gerar prévia real no CapCut
        </button>
        {active ? (
          <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={() => void cancel()}>
            Cancelar prévia
          </button>
        ) : null}
      </div>
      {blockedReason ? <div className="field-hint">{blockedReason}</div> : null}
      {preview ? (
        <div style={{ marginTop: 10 }} aria-live="polite">
          <div className="row" style={{ gap: 8 }}>
            <Pill tone={capcutTone(preview.status)}>{previewStatusText(preview)}</Pill>
          </div>
          {active ? (
            <div style={{ marginTop: 8 }}>
              <ProgressBar value={previewProgress(preview)} />
            </div>
          ) : null}
          {query.error && active ? <div className="field-error">{query.error}</div> : null}
          {preview.status === "completed" ? (
            <>
              <video
                key={capcutPreviewFileUrl(preview)}
                src={capcutPreviewFileUrl(preview)}
                controls
                playsInline
                style={{ width: "100%", maxHeight: 480, background: "#000", borderRadius: 6, marginTop: 8 }}
              />
              {started && !designsEqual(started.design, design) ? (
                <div className="field-hint">O modelo mudou depois desta prévia; gere outra para ver as alterações.</div>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
