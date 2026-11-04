import { useState } from "react";
import { downloadGeneratedFile, messageOf } from "../api/client";
import { hasPostTimesReport, postTimesReportUrl } from "../lib/importUnified";
import { postTimesPageUrl } from "../lib/postTimesReport";
import { navigate } from "../lib/router";
import { useToast } from "../lib/toast";
import { Spinner } from "./ui";

/** "Ver padrões" (página na ferramenta) e "Baixar relatório de padrões (.zip)" do lote/grupo; só aparece com horários de publicações gravados. */
export function PostTimesReportButton({
  target,
  postTimes,
  postTimesCount,
}: {
  target: { kind: "batch" | "group"; id: number };
  postTimes?: boolean;
  postTimesCount?: number;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  if (!hasPostTimesReport({ post_times: postTimes, post_times_count: postTimesCount })) return null;

  async function download() {
    setBusy(true);
    try {
      await downloadGeneratedFile(postTimesReportUrl(target), "relatorio-padroes.zip");
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button type="button" className="btn btn-sm" onClick={() => navigate(postTimesPageUrl(target))}>
        Ver padrões
      </button>
      <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void download()}>
        {busy ? <Spinner /> : null}
        Baixar relatório de padrões (.zip)
      </button>
    </>
  );
}
