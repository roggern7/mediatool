import { useRef, useState } from "react";
import { ApiError, api, messageOf } from "../api/client";
import type { ExportJob, ExportSource } from "../api/types";
import {
  EXPORT_POLL_MS,
  ZIP_READY_POLL_MS,
  buildExportRequest,
  createSingleFlight,
  isExportActive,
  pickResumeExport,
  runningExportId,
  zipDownloadUrl,
} from "./exportFolder";
import { useQuery } from "./useQuery";
import { useToast } from "./toast";

export interface ExportFlow {
  /** Diálogo nativo de pasta aberto (o backend só responde quando o usuário escolhe ou cancela). */
  picking: boolean;
  /** Seletor aberto ou pedido de exportação sendo criado. */
  busy: boolean;
  /** Exportação acompanhada nesta tela. */
  current: ExportJob | null;
  cancelling: boolean;
  /** Exportação cujo download automático foi iniciado nesta tela (só a pedida aqui; nunca em retomada). */
  autoDownloadedId: number | null;
  /** Seletor nativo indisponível neste computador: mensagem para exibir (a pasta padrão é oferecida). */
  unavailable: string | null;
  /** Exporta o pedido que esbarrou no seletor indisponível para a pasta padrão criada pelo servidor. */
  startInDefaultFolder: () => Promise<void>;
  /** `zip`: Baixar ZIP; `pick`: seletor nativo pelo backend; `default`: pasta padrão criada pelo servidor. */
  start: (source: ExportSource, mode: "pick" | "default" | "zip") => Promise<void>;
  cancel: () => Promise<void>;
  open: (exportId: number) => Promise<void>;
  dismiss: () => void;
}

/**
 * Fluxo de "Exportar para pasta": seletor nativo (via backend) -> pedido -> progresso -> resultado.
 * `resumeFor`: ao abrir a tela, consulta as últimas exportações da origem (lote, grupo ou Biblioteca) e retoma a
 * que estiver em andamento ou volta a oferecer o ZIP pronto ainda disponível, sem download automático.
 */
export function useExportFlow(opts: {
  resumeFor?: { batchId?: number; groupId?: number; kind?: "library" };
  onFinished?: () => void;
} = {}): ExportFlow {
  const toast = useToast();
  const [picking, setPicking] = useState(false);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [exportId, setExportId] = useState<number | null>(null);
  const [created, setCreated] = useState<ExportJob | null>(null);
  const [pollMs, setPollMs] = useState<number | null>(null);
  const [fallback, setFallback] = useState<{ source: ExportSource; message: string } | null>(null);
  // ZIP pedido NESTA tela: quando ficar pronto, o download começa sozinho (uma vez).
  const autoDownload = useRef<number | null>(null);
  // Pedido em andamento: barra o segundo clique no mesmo instante, antes de o botão aparecer desabilitado.
  const inFlight = useRef(createSingleFlight());
  const [autoDownloadedId, setAutoDownloadedId] = useState<number | null>(null);
  // onFinished é avisado uma vez por exportação (o ZIP pronto continua sendo consultado em ritmo lento).
  const finishedFor = useRef<number | null>(null);
  const resumeKey = opts.resumeFor
    ? `${opts.resumeFor.batchId ?? ""}:${opts.resumeFor.groupId ?? ""}:${opts.resumeFor.kind ?? ""}`
    : "none";

  useQuery(`export-resume:${resumeKey}`, async () => {
    if (!opts.resumeFor) return null;
    const page = await api.listExports({ ...opts.resumeFor, limit: 5 });
    const decision = pickResumeExport(page.items);
    if (decision) {
      // Retomada ou oferta: nunca dispara o download sozinho (autoDownload só vale para o pedido feito aqui).
      if (decision.action === "offer") finishedFor.current = decision.job.id;
      setCreated((prev) => prev ?? decision.job);
      setExportId((prev) => prev ?? decision.job.id);
      setPollMs(decision.action === "follow" ? EXPORT_POLL_MS : ZIP_READY_POLL_MS);
    }
    return decision;
  });

  const query = useQuery(
    `export:${exportId ?? "none"}`,
    async () => {
      if (exportId === null) return null;
      const job = await api.getExport(exportId);
      const active = isExportActive(job);
      const url = zipDownloadUrl(job);
      setPollMs(active ? EXPORT_POLL_MS : url ? ZIP_READY_POLL_MS : null);
      if (url && autoDownload.current === job.id) {
        autoDownload.current = null;
        setAutoDownloadedId(job.id);
        startBrowserDownload(url);
      }
      // Terminou: quem hospeda recarrega o lote/grupo (o "Abrir pasta" passa a valer para esta exportação).
      if (!active && finishedFor.current !== job.id) {
        finishedFor.current = job.id;
        opts.onFinished?.();
      }
      return job;
    },
    pollMs,
  );

  const loaded = query.data && query.data.id === exportId ? query.data : null;
  const current = loaded ?? (created && created.id === exportId ? created : null);

  async function start(source: ExportSource, mode: "pick" | "default" | "zip") {
    if (picking || starting || !inFlight.current.tryEnter()) return;
    setStarting(mode !== "pick");
    setFallback(null);
    try {
      let request = buildExportRequest(source, { kind: mode === "zip" ? "zip" : "default" });
      if (mode === "pick") {
        setPicking(true);
        const picked = await api.pickExportFolder();
        setPicking(false);
        if (picked.cancelled || !picked.token) return; // diálogo cancelado: não é erro
        request = buildExportRequest(source, { kind: "token", token: picked.token });
      }
      setStarting(true);
      const job = await api.createExport(request);
      if (mode === "zip") autoDownload.current = job.id;
      setCreated(job);
      setExportId(job.id);
      setPollMs(EXPORT_POLL_MS);
    } catch (err) {
      const runningId = err instanceof ApiError ? runningExportId(err.code, err.payload) : null;
      if (runningId !== null) {
        // A mesma origem já tem exportação em andamento: acompanha aquela em vez de mostrar erro.
        setCreated(null);
        setExportId(runningId);
        setPollMs(EXPORT_POLL_MS);
      } else if (err instanceof ApiError && err.code === "picker_unavailable") {
        setFallback({ source, message: err.message });
      } else {
        toast.push("err", messageOf(err));
      }
    } finally {
      inFlight.current.leave();
      setPicking(false);
      setStarting(false);
    }
  }

  async function startInDefaultFolder() {
    if (fallback) await start(fallback.source, "default");
  }

  async function cancel() {
    if (exportId === null || cancelling) return;
    setCancelling(true);
    try {
      await api.cancelExport(exportId);
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setCancelling(false);
      setPollMs(EXPORT_POLL_MS);
      query.reload();
    }
  }

  async function open(id: number) {
    try {
      await api.openExportFolder(id);
      toast.push("ok", "Pasta aberta no Explorer.");
    } catch (err) {
      toast.push("err", messageOf(err));
    }
  }

  function dismiss() {
    setExportId(null);
    setCreated(null);
    setPollMs(null);
    setFallback(null);
  }

  return {
    picking,
    busy: picking || starting,
    current,
    cancelling,
    autoDownloadedId,
    unavailable: fallback?.message ?? null,
    startInDefaultFolder,
    start,
    cancel,
    open,
    dismiss,
  };
}

/**
 * Inicia o download normal do navegador (navegação por link com `download`): o arquivo vai direto para o disco,
 * nunca para a memória da página.
 */
function startBrowserDownload(url: string): void {
  const link = document.createElement("a");
  link.href = url;
  link.download = "";
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
}
