import { useState } from "react";
import { api, messageOf } from "../api/client";
import type { Batch } from "../api/types";
import { batchNotice, listingDroppedNotice } from "../lib/batchState";
import { useNow } from "../lib/pause";
import { useToast } from "../lib/toast";
import { Spinner } from "./ui";

/**
 * Aviso de lote parado (precisa de atenção, pausado por falhas, coleta em pausa) com "Retomar importação".
 * A espera pelo limite de requisições é só informativa (lote ativo): sem o tom de aviso.
 * Mídias descartadas durante a listagem aparecem como aviso de atenção à parte (o lote não falha por isso).
 */
export function BatchNotice({ batch, onResumed }: { batch: Batch; onResumed: () => void }) {
  const toast = useToast();
  const now = useNow();
  const [busy, setBusy] = useState(false);
  const notice = batchNotice(batch, now);
  const dropped = listingDroppedNotice(batch);
  if (!notice && !dropped) return null;

  async function resume() {
    setBusy(true);
    try {
      await api.resumeListing(batch.id);
      toast.push("ok", "Importação retomada.");
      onResumed();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {notice ? (
        <div className={notice.kind === "rate_limit_wait" ? "alert" : "alert warn"} role="status" style={{ marginTop: 10, alignItems: "center" }}>
          <span className="grow">{notice.text}</span>
          {notice.canResume ? (
            <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void resume()}>
              {busy ? <Spinner /> : null}
              Retomar importação
            </button>
          ) : null}
        </div>
      ) : null}
      {dropped ? (
        <div className="alert warn" role="status" style={{ marginTop: 10, alignItems: "center" }}>
          <span className="grow">{dropped}</span>
        </div>
      ) : null}
    </>
  );
}
