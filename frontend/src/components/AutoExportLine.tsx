import type { AutoExport } from "../api/types";
import { autoExportText } from "../lib/importUnified";

/** Exportação ZIP automática do lote/grupo: o estado e, com o ZIP pronto, o botão de baixar. */
export function AutoExportLine({ auto }: { auto: AutoExport | null | undefined }) {
  const text = autoExportText(auto);
  if (!auto || !text) return null;
  const failed = auto.status === "failed";
  return (
    <div className="row" style={{ marginTop: 6, gap: 8 }}>
      <span className={`small${failed ? " tone-err" : " muted"}`}>{text}</span>
      {auto.zip_ready && auto.download_url ? (
        <a className="btn btn-sm" href={auto.download_url}>
          Baixar ZIP
        </a>
      ) : null}
    </div>
  );
}
