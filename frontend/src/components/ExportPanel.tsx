import { useId, useState } from "react";
import type { ExportContent, ExportJob, ExportSource, LastExport } from "../api/types";
import {
  CONTENT_LABEL,
  DEFAULT_FOLDER_BUTTON,
  PICKING_TEXT,
  ZIP_AUTO_DOWNLOAD_TEXT,
  exportActions,
  exportContentNote,
  exportEndedText,
  exportExtrasText,
  exportFraction,
  exportPartialText,
  exportProgressText,
  exportResultText,
  exportRunningText,
  isExportActive,
  zipAvailableText,
  zipDownloadUrl,
  zipPreparingText,
  zipReadyText,
  zipSpaceNote,
} from "../lib/exportFolder";
import { navigate } from "../lib/router";
import { type ExportFlow, useExportFlow } from "../lib/useExportFlow";
import { ProgressBar, Spinner } from "./ui";

/** Baixar ZIP: preparação com progresso e Cancelar; pronto com "Baixar novamente"; cancelado, falho ou expirado. */
function ZipStatus({ flow, job }: { flow: ExportFlow; job: ExportJob }) {
  if (isExportActive(job)) {
    return (
      <div className="alert" role="status" aria-live="polite" style={{ marginTop: 10, alignItems: "center" }}>
        <div className="grow" style={{ minWidth: 0 }}>
          <strong>{zipPreparingText(job)}</strong>
          <div style={{ marginTop: 6 }}>
            <ProgressBar value={exportFraction(job)} />
          </div>
          <div className="muted small" style={{ marginTop: 5 }}>
            {zipSpaceNote(job.content ?? "originals")}
          </div>
        </div>
        <button type="button" className="btn btn-sm" disabled={flow.cancelling} onClick={() => void flow.cancel()}>
          {flow.cancelling ? <Spinner /> : null}
          Cancelar
        </button>
      </div>
    );
  }
  const url = zipDownloadUrl(job);
  if (url) {
    const extras = exportExtrasText(job);
    const available = zipAvailableText(job);
    return (
      <div className="alert" role="status" style={{ marginTop: 10, alignItems: "center" }}>
        <div className="grow" style={{ minWidth: 0 }}>
          <strong>{zipReadyText(job)}</strong>
          <div className="muted small">
            {[extras, flow.autoDownloadedId === job.id ? ZIP_AUTO_DOWNLOAD_TEXT : null, available]
              .filter(Boolean)
              .join(" · ")}
          </div>
        </div>
        <a className="btn btn-sm" href={url} download>
          Baixar novamente
        </a>
      </div>
    );
  }
  return (
    <div className="alert warn" role="status" style={{ marginTop: 10 }}>
      <span className="grow">{exportEndedText(job) ?? "O ZIP não está mais disponível."}</span>
      <button type="button" className="btn btn-sm btn-ghost" onClick={flow.dismiss}>
        Fechar aviso
      </button>
    </div>
  );
}

/** Estado da exportação em andamento ou recém-terminada: seletor aberto, progresso com Cancelar, ou resultado. */
export function ExportStatus({ flow }: { flow: ExportFlow }) {
  const job = flow.current;
  if (flow.picking) {
    return (
      <div className="alert" role="status" style={{ marginTop: 10 }}>
        <Spinner />
        <span className="grow">{PICKING_TEXT}</span>
      </div>
    );
  }
  if (flow.unavailable) {
    return (
      <div className="alert warn" role="alert" style={{ marginTop: 10, alignItems: "center" }}>
        <span className="grow">{flow.unavailable}</span>
        <button type="button" className="btn btn-sm" disabled={flow.busy} onClick={() => void flow.startInDefaultFolder()}>
          {DEFAULT_FOLDER_BUTTON}
        </button>
        <button type="button" className="btn btn-sm btn-ghost" onClick={flow.dismiss}>
          Fechar aviso
        </button>
      </div>
    );
  }
  if (!job) return null;
  if (job.mode === "zip") return <ZipStatus flow={flow} job={job} />;
  if (isExportActive(job)) {
    return (
      <div className="alert" role="status" aria-live="polite" style={{ marginTop: 10, alignItems: "center" }}>
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row between">
            <span className="truncate" title={job.dest_path}>
              {exportRunningText(job)}
            </span>
            <strong className="nowrap">{exportProgressText(job)}</strong>
          </div>
          <div style={{ marginTop: 6 }}>
            <ProgressBar value={exportFraction(job)} />
          </div>
          <div className="muted small" style={{ marginTop: 5 }}>
            {exportContentNote(job.content ?? "originals")}
          </div>
        </div>
        <button type="button" className="btn btn-sm" disabled={flow.cancelling} onClick={() => void flow.cancel()}>
          {flow.cancelling ? <Spinner /> : null}
          Cancelar exportação
        </button>
      </div>
    );
  }
  const ended = exportEndedText(job);
  if (ended) {
    return (
      <div className="alert warn" role="status" style={{ marginTop: 10 }}>
        <span className="grow">{ended}</span>
        <button type="button" className="btn btn-sm btn-ghost" onClick={flow.dismiss}>
          Fechar aviso
        </button>
      </div>
    );
  }
  const extras = exportExtrasText(job);
  return (
    <div className="alert" role="status" style={{ marginTop: 10, alignItems: "center" }}>
      <div className="grow" style={{ minWidth: 0 }}>
        <div style={{ overflowWrap: "anywhere" }}>{exportResultText(job)}</div>
        {extras ? <div className="muted small">{extras}</div> : null}
        {exportPartialText(job) ? <div className="tone-warn small">{exportPartialText(job)}</div> : null}
      </div>
      <button type="button" className="btn btn-sm" onClick={() => void flow.open(job.id)}>
        Abrir pasta
      </button>
    </div>
  );
}

/** Escolha do que exportar (originais, versões ou os dois); o valor vai no `content` do pedido. */
export function ExportContentSelect({
  value,
  onChange,
  disabled,
}: {
  value: ExportContent;
  onChange: (content: ExportContent) => void;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <span className="row small" style={{ gap: 6 }}>
      <label htmlFor={id} className="muted">
        Conteúdo
      </label>
      <select
        id={id}
        className="select"
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value as ExportContent)}
      >
        {(Object.keys(CONTENT_LABEL) as ExportContent[]).map((option) => (
          <option key={option} value={option}>
            {CONTENT_LABEL[option]}
          </option>
        ))}
      </select>
    </span>
  );
}

/**
 * Ações de uma importação terminada (lote avulso ou grupo): Ver na Biblioteca, Exportar para pasta (seletor nativo
 * pelo backend), Usar pasta padrão e Abrir pasta (da última exportação concluída).
 */
export function ImportExportBar({
  source,
  finished,
  note,
  lastExport,
  libraryPath,
  onChanged,
  contentChoice = false,
}: {
  source: ExportSource & { kind: "batch" | "group" };
  /** "Exportar para pasta" é oferecido (batchExportOffer / groupExportOffer). */
  finished: boolean;
  /** Aviso curto quando a importação não terminou por inteiro. */
  note?: string | null;
  lastExport: LastExport | null | undefined;
  libraryPath: string;
  onChanged?: () => void;
  /** Importação com processamento: oferece exportar as versões processadas (ou originais e versões). */
  contentChoice?: boolean;
}) {
  const hintId = useId();
  // Importação com edição/processamento: o padrão é exportar as versões editadas (o motivo de ter processado).
  const [content, setContent] = useState<ExportContent>(contentChoice ? "edited" : "originals");
  const flow = useExportFlow({
    resumeFor: source.kind === "batch" ? { batchId: source.id } : { groupId: source.id },
    onFinished: onChanged,
  });
  const actions = exportActions({ finished, lastExport, current: flow.current, busy: flow.busy });
  if (!actions.showExport && !flow.current && !flow.picking && !flow.unavailable) return null;

  return (
    <div style={{ marginTop: 12 }}>
      <div className="row" style={{ flexWrap: "wrap" }} role="group" aria-label="Mídias desta importação">
        <button type="button" className="btn btn-sm" onClick={() => navigate(libraryPath)}>
          Ver na Biblioteca
        </button>
        {actions.showExport ? (
          <>
            {contentChoice ? (
              <ExportContentSelect value={content} onChange={setContent} disabled={!actions.canExport} />
            ) : null}
            <button
              type="button"
              className="btn btn-sm btn-primary"
              disabled={!actions.canExport}
              title="Prepara um arquivo .zip com todas as mídias desta importação e inicia o download"
              onClick={() => void flow.start(source, "zip", content)}
            >
              Baixar ZIP
            </button>
            <button
              type="button"
              className="btn btn-sm"
              disabled={!actions.canExport}
              title={exportContentNote(content)}
              onClick={() => void flow.start(source, "pick", content)}
            >
              Exportar para pasta
            </button>
            <button
              type="button"
              className="btn btn-sm"
              disabled={!actions.canOpen}
              aria-describedby={actions.openHint ? hintId : undefined}
              onClick={() => {
                if (actions.openId !== null) void flow.open(actions.openId);
              }}
            >
              Abrir pasta
            </button>
            <button
              type="button"
              className="link-btn small"
              disabled={!actions.canExport}
              title="Cria uma pasta nova em Documentos\Media Tool\Exports e exporta para ela"
              onClick={() => void flow.start(source, "default", content)}
            >
              Usar pasta padrão
            </button>
          </>
        ) : null}
        {actions.openHint ? (
          <span id={hintId} className="muted small">
            {actions.openHint}
          </span>
        ) : null}
      </div>
      {actions.showExport && note ? (
        <div className="muted small" role="note" style={{ marginTop: 6 }}>
          {note}
        </div>
      ) : null}
      <ExportStatus flow={flow} />
    </div>
  );
}
