import { useState } from "react";
import { api, messageOf } from "../api/client";
import type { InstagramStatus } from "../api/types";
import { actionRequiredText, nextCheckText, pauseScopeText, pauseText } from "../lib/pause";
import { useToast } from "../lib/toast";
import { ConfirmDialog } from "./ui";

/** Faixa de aviso da pausa do Instagram com "Liberar pausa". Só renderize quando a pausa estiver ativa. */
export function PauseBanner({
  status,
  now,
  onReleased,
}: {
  status: InstagramStatus;
  now: number;
  onReleased: () => void;
}) {
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!status.paused_until) return null;

  async function release() {
    setBusy(true);
    try {
      await api.releasePause();
      setConfirm(false);
      toast.push("ok", "Pausa liberada.");
      onReleased();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="alert warn" role="status" style={{ marginBottom: 14, alignItems: "center" }}>
      <span className="grow">
        {pauseText(status.paused_until, status.pause_reason, now)}
        {[
          actionRequiredText(status.action_required, status.pause_reason),
          pauseScopeText(status.pause_scope),
          nextCheckText(status.next_attempt_at, now),
        ]
          .filter((line): line is string => line !== null)
          .map((line) => (
            <span key={line} style={{ display: "block" }}>
              {line}
            </span>
          ))}
      </span>
      <button type="button" className="btn btn-sm" onClick={() => setConfirm(true)}>
        Liberar pausa
      </button>
      {confirm ? (
        <ConfirmDialog
          title="Liberar pausa"
          confirmLabel="Liberar"
          busy={busy}
          onConfirm={() => void release()}
          onCancel={() => setConfirm(false)}
        >
          A pausa protege contra novas recusas do Instagram. Liberar mesmo assim?
        </ConfirmDialog>
      ) : null}
    </div>
  );
}
