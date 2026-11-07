import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Play } from "lucide-react";
import { api, messageOf } from "../../api/client";
import type { CapCutJob, CapCutReview } from "../../api/types";
import {
  approveBody,
  defaultOnlyDoubtful,
  doubtCount,
  editedCount,
  formatReviewTime,
  isRowEdited,
  nextRowIndex,
  reviewCountText,
  reviewRows,
  setRowText,
  visibleRows,
  type ReviewRow,
} from "../../lib/capcutReview";
import { useToast } from "../../lib/toast";
import { ErrorLine, Modal, SkeletonRows, Spinner, Switch } from "../ui";

/**
 * Revisão das legendas de um job parado em "Aguardando revisão": player da fonte transcrita, frases com as palavras
 * duvidosas realçadas, ▶ por frase (toca só o trecho), edição do texto e "Aprovar e exportar" (só as frases
 * alteradas vão no pedido; o servidor realinha cada uma no áudio).
 */
export function ReviewDialog({
  jobId,
  onClose,
  onApproved,
}: {
  jobId: number;
  onClose: () => void;
  onApproved: (job: CapCutJob) => void;
}) {
  const toast = useToast();
  const [review, setReview] = useState<CapCutReview | null>(null);
  const [rows, setRows] = useState<ReviewRow[]>([]);
  const [onlyDoubtful, setOnlyDoubtful] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const stopAt = useRef<number | null>(null);
  const inputs = useRef(new Map<number, HTMLInputElement>());

  useEffect(() => {
    let alive = true;
    api.capcutReview(jobId).then(
      (data) => {
        if (!alive) return;
        const initial = reviewRows(data.segments);
        setReview(data);
        setRows(initial);
        setOnlyDoubtful(defaultOnlyDoubtful(initial));
        setError(null);
      },
      (err: unknown) => {
        if (alive) setError(messageOf(err));
      },
    );
    return () => {
      alive = false;
    };
  }, [jobId, reload]);

  function playRow(row: ReviewRow) {
    const video = videoRef.current;
    if (!video) return;
    stopAt.current = row.end;
    video.currentTime = row.start;
    void video.play().catch(() => undefined);
  }

  function onTimeUpdate() {
    const video = videoRef.current;
    if (video && stopAt.current !== null && video.currentTime >= stopAt.current) {
      video.pause();
      stopAt.current = null;
    }
  }

  async function approve() {
    if (busy) return;
    setBusy(true);
    try {
      const job = await api.approveCapcutReview(jobId, approveBody(rows));
      const edited = editedCount(rows);
      toast.push(
        "ok",
        edited > 0
          ? `Legendas aprovadas (${edited} ${edited === 1 ? "frase corrigida" : "frases corrigidas"}). O vídeo voltou para a fila.`
          : "Legendas aprovadas. O vídeo voltou para a fila.",
      );
      onApproved(job);
    } catch (err) {
      toast.push("err", messageOf(err));
      setBusy(false);
    }
  }

  function onKey(e: KeyboardEvent<HTMLInputElement>, row: ReviewRow) {
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (e.ctrlKey || e.metaKey) {
      void approve();
      return;
    }
    const next = nextRowIndex(shown, row.index);
    if (next !== null) inputs.current.get(next)?.focus();
  }

  const shown = visibleRows(rows, onlyDoubtful);
  const doubts = doubtCount(rows);

  return (
    <Modal
      title="Revisar legendas"
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
            onClick={() => void approve()}
            disabled={busy || review === null}
            title="Ctrl+Enter"
          >
            {busy ? <Spinner /> : null}
            Aprovar e exportar
          </button>
        </>
      }
    >
      {error ? <ErrorLine message={error} onRetry={() => setReload((n) => n + 1)} /> : null}
      {review === null ? (
        error ? null : <SkeletonRows rows={5} />
      ) : (
        <div className="cc-review">
          <video
            ref={videoRef}
            className="cc-review-video"
            src={review.source_url}
            controls
            preload="metadata"
            onTimeUpdate={onTimeUpdate}
            onPause={() => {
              stopAt.current = null;
            }}
          />
          <div className="row between cc-review-bar">
            <span className="small">
              <strong>{reviewCountText(doubts)}</strong>
              <span className="muted"> · {review.job.media_filename ?? `Vídeo ${review.job.media_id ?? ""}`}</span>
            </span>
            <Switch label="Só trechos duvidosos" checked={onlyDoubtful} onChange={setOnlyDoubtful} />
          </div>
          {!review.aligner ? (
            <p className="muted small cc-review-note">
              Alinhador não instalado: os tempos das frases corrigidas são ajustados pelas sílabas.
            </p>
          ) : null}
          {shown.length === 0 ? (
            <p className="muted small">Nenhuma frase para mostrar com este filtro.</p>
          ) : (
            <ol className="cc-review-list">
              {shown.map((row) => (
                <li key={row.index} className={`cc-review-row${row.doubt ? " doubt" : ""}`}>
                  <button
                    type="button"
                    className="btn btn-sm btn-ghost cc-review-play"
                    onClick={() => playRow(row)}
                    aria-label={`Tocar o trecho de ${formatReviewTime(row.start)}`}
                  >
                    <Play size={14} aria-hidden="true" />
                  </button>
                  <div className="cc-review-body">
                    <div className="row cc-review-meta">
                      <span className="muted small mono">
                        {formatReviewTime(row.start)} a {formatReviewTime(row.end)}
                      </span>
                      {isRowEdited(row) ? <span className="cc-review-edited small">Editada</span> : null}
                    </div>
                    {row.doubt ? (
                      <p className="cc-review-words small">
                        {row.words.map((w, i) => (
                          <span key={i}>
                            {i > 0 ? " " : ""}
                            {w.doubt ? <mark className="cc-doubt">{w.text}</mark> : w.text}
                          </span>
                        ))}
                      </p>
                    ) : null}
                    <input
                      ref={(el) => {
                        if (el) inputs.current.set(row.index, el);
                        else inputs.current.delete(row.index);
                      }}
                      className="input"
                      value={row.text}
                      aria-label={`Texto da frase de ${formatReviewTime(row.start)}`}
                      placeholder="Vazio remove a frase"
                      disabled={busy}
                      onChange={(e) => setRows((prev) => setRowText(prev, row.index, e.target.value))}
                      onKeyDown={(e) => onKey(e, row)}
                    />
                  </div>
                </li>
              ))}
            </ol>
          )}
          <p className="muted small cc-review-note">Enter vai para a próxima frase. Ctrl+Enter (ou ⌘+Enter) aprova.</p>
        </div>
      )}
    </Modal>
  );
}
