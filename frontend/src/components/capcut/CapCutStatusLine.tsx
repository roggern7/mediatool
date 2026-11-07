import { useState } from "react";
import { api, messageOf } from "../../api/client";
import type { CapCutStatus } from "../../api/types";
import { exportModeStatusText, jobExportModeText, queueSummaryText, whisperModelText } from "../../lib/capcut";
import { useToast } from "../../lib/toast";
import { ErrorLine, Pill, SkeletonRows } from "../ui";

/**
 * Linha compacta do topo: situação do CapCut, modo de exportação, fila global (com o motivo de uma pausa automática)
 * e Pausar/Retomar da fila. Os detalhes (dependências, skill, modelo de transcrição) ficam em "Detalhes".
 */
export function CapCutStatusLine({
  status,
  error,
  onReload,
}: {
  status: CapCutStatus | undefined;
  error: string | null;
  onReload: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  async function toggleQueue(paused: boolean) {
    setBusy(true);
    try {
      if (paused) await api.resumeCapcutQueue();
      else await api.pauseCapcutQueue();
      toast.push("ok", paused ? "Fila de legendas retomada." : "Fila de legendas pausada.");
      onReload();
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  if (status === undefined) {
    return error ? <ErrorLine message={error} onRetry={onReload} /> : <SkeletonRows rows={1} />;
  }

  const mode = exportModeStatusText(status.export_mode, status.isolated);
  const depsOk = Object.values(status.deps).length > 0 && Object.values(status.deps).every(Boolean);
  const allOk = status.available && status.installed && depsOk && Boolean(status.skill.installed) && mode.ok;
  const q = status.queue;

  return (
    <div className="panel cc-status">
      {!status.available ? (
        <div className="alert err" role="alert" style={{ marginBottom: 10 }}>
          <span className="grow">{status.reason ?? "A Edição CapCut não está disponível."}</span>
        </div>
      ) : null}
      <div className="row between">
        <div className="row" style={{ gap: 8 }}>
          <Pill tone={allOk ? "ok" : "warn"}>{allOk ? "Pronto" : "Atenção"}</Pill>
          <span className="small">
            CapCut{" "}
            {status.installed ? `detectado${status.version ? ` (${status.version})` : ""}` : "não encontrado"} · Modo{" "}
            {jobExportModeText(status.export_mode)}
          </span>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <span className="muted small" aria-live="polite">
            {queueSummaryText(q)}
          </span>
          <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void toggleQueue(q.paused)}>
            {q.paused ? "Retomar fila" : "Pausar fila"}
          </button>
        </div>
      </div>
      {q.paused && q.pause_reason ? (
        <div className="alert warn" role="status" style={{ marginTop: 10 }}>
          <strong className="grow">{q.pause_reason}</strong>
        </div>
      ) : null}
      {!mode.ok ? (
        <div className="alert warn" role="status" style={{ marginTop: 10 }}>
          <span className="grow">{mode.text}</span>
        </div>
      ) : null}
      <details className="more" style={{ marginTop: 8 }}>
        <summary className="small">Detalhes</summary>
        <p className="muted small">
          {status.export_mode === "interactive"
            ? "Modo Interativo: durante a exportação o Media Tool traz o CapCut para a frente e usa o mouse por alguns segundos (precisa do PC livre). Se outra janela estiver na frente, ele tenta de novo."
            : "Modo Isolado: o CapCut roda numa área de trabalho separada, que você não vê. Não usa mouse, teclado nem a tela; você pode usar o PC normalmente."}
        </p>
        <div className="form-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}>
          <StatusItem
            label="CapCut"
            ok={status.installed}
            text={status.installed ? `Detectado${status.version ? ` (versão ${status.version})` : ""}` : "Não encontrado"}
          />
          <StatusItem
            label="Dependências"
            ok={depsOk}
            text={
              Object.entries(status.deps)
                .map(([name, ok]) => `${name} ${ok ? "ok" : "faltando"}`)
                .join(", ") || "—"
            }
          />
          <StatusItem
            label="Skill (capcut-agent-skill)"
            ok={Boolean(status.skill.installed)}
            text={status.skill.installed ? `Instalada${status.skill.commit ? ` (${status.skill.commit.slice(0, 8)})` : ""}` : "Não instalada"}
          />
          <StatusItem label="Modo de exportação" ok={mode.ok} text={mode.text} />
          <StatusItem
            label="Modelo de transcrição"
            ok={Boolean(status.models[status.settings.whisper_model])}
            text={whisperModelText(status.models, status.settings.whisper_model)}
          />
        </div>
      </details>
    </div>
  );
}

function StatusItem({ label, ok, text }: { label: string; ok: boolean; text: string }) {
  return (
    <div className="field">
      <span className="field-label">{label}</span>
      <span className="row" style={{ gap: 8 }}>
        <Pill tone={ok ? "ok" : "warn"}>{ok ? "OK" : "Atenção"}</Pill>
        <span className="small">{text}</span>
      </span>
    </div>
  );
}
