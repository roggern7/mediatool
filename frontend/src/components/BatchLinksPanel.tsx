import { api } from "../api/client";
import type { BatchLink, BatchLinksSummary } from "../api/types";
import { formatCount } from "../lib/format";
import { failedLinks, failedLinksTitle, linksSummaryText } from "../lib/importLinks";
import { useQuery } from "../lib/useQuery";
import { ErrorLine } from "./ui";

const LINKS_PAGE = 200;
/** Páginas lidas no máximo (o teto da API é de 500 links por lote). */
const LINKS_PAGES_MAX = 5;
/** Links com falha listados na tela (os demais entram em "e mais N"). */
const FAILED_SHOWN_MAX = 100;

async function loadLinks(batchId: number): Promise<BatchLink[]> {
  const items: BatchLink[] = [];
  for (let page = 0; page < LINKS_PAGES_MAX; page += 1) {
    const res = await api.listBatchLinks(batchId, LINKS_PAGE, items.length);
    items.push(...res.items);
    if (res.items.length === 0 || items.length >= res.total) break;
  }
  return items;
}

/**
 * Lote por link: resumo dos links e a lista dos que falharam, com o motivo. A lista só é consultada quando o
 * resumo do lote acusa falha, e de novo quando esse número muda.
 */
export function BatchLinksPanel({ batchId, links }: { batchId: number; links: BatchLinksSummary }) {
  const hasFailed = links.failed > 0;
  const query = useQuery(`batch-links:${batchId}:${links.failed}`, () =>
    hasFailed ? loadLinks(batchId) : Promise.resolve([]),
  );
  const failed = hasFailed && query.data ? failedLinks(query.data) : [];
  return (
    <>
      <div className="muted small" style={{ marginTop: 8 }}>
        {linksSummaryText(links)}
      </div>
      {hasFailed && query.error && failed.length === 0 ? (
        <div style={{ marginTop: 10 }}>
          <ErrorLine message={query.error} onRetry={query.reload} />
        </div>
      ) : null}
      {failed.length > 0 ? (
        <div className="alert warn" role="status" style={{ marginTop: 10, flexDirection: "column" }}>
          <strong id={`batch-links-failed-${batchId}`}>{failedLinksTitle(links.failed)}:</strong>
          <ul className="invalid-list" aria-labelledby={`batch-links-failed-${batchId}`}>
            {failed.slice(0, FAILED_SHOWN_MAX).map((link) => (
              <li key={link.position}>
                <span className="mono">{link.url}</span>: {link.reason}
              </li>
            ))}
            {failed.length > FAILED_SHOWN_MAX ? <li>e mais {formatCount(failed.length - FAILED_SHOWN_MAX)}</li> : null}
          </ul>
        </div>
      ) : null}
    </>
  );
}
