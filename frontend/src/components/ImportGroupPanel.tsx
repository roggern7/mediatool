import { useState } from "react";
import { api, messageOf } from "../api/client";
import type { Batch } from "../api/types";
import { queueForBatch } from "../lib/batchRoute";
import { formatCount } from "../lib/format";
import {
  childState,
  groupAttentionText,
  groupOptionsText,
  groupStatusLabel,
  groupSummary,
  groupTitle,
  groupViewsText,
  profileProgressText,
  profileStateDetail,
  profileStateLabel,
} from "../lib/importGroup";
import { progressExtrasText } from "../lib/importUnified";
import { useNow } from "../lib/pause";
import { visibleFilter } from "../lib/viewsFilter";
import { POLL_STEPS_MS, nextPollMs } from "../lib/polling";
import { groupExportOffer } from "../lib/exportFolder";
import { ROUTES, navigate } from "../lib/router";
import { useQuery } from "../lib/useQuery";
import { useToast } from "../lib/toast";
import { AutoExportLine } from "./AutoExportLine";
import { ImportExportBar } from "./ExportPanel";
import { ConfirmDialog, ErrorLine, Pill, SkeletonRows, Spinner } from "./ui";

/** Perfis por página (keyset `after_position`); o custo no backend não depende do total de perfis. */
const CHILDREN_PAGE = 100;

/**
 * Acompanhamento de uma importação de vários perfis: cabeçalho com os agregados e lista compacta de perfis,
 * uma página por vez. Só a página aberta é consultada (no ritmo adaptativo que as outras listas já usam).
 */
export function ImportGroupPanel({
  groupId,
  notice,
  openBatchId,
  onOpenBatch,
  onChanged,
  onClose,
}: {
  groupId: number;
  /** Aviso da criação (perfis pulados), quando este grupo acabou de ser criado. */
  notice?: string | null;
  openBatchId: number | null;
  onOpenBatch: (batchId: number) => void;
  onChanged: () => void;
  onClose?: () => void;
}) {
  const toast = useToast();
  const now = useNow();
  // Pilha de cursores `after_position`; vazia = primeira página.
  const [cursors, setCursors] = useState<number[]>([]);
  const after = cursors[cursors.length - 1] ?? null;
  const [pollMs, setPollMs] = useState<number>(POLL_STEPS_MS[0]);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [resumingId, setResumingId] = useState<number | null>(null);

  const query = useQuery(
    `import-group:${groupId}:${after ?? "first"}`,
    async () => {
      const detail = await api.getImportGroup(groupId, { childrenLimit: CHILDREN_PAGE, afterPosition: after });
      setPollMs((prev) => nextPollMs(prev, detail.group.status === "active"));
      return { ...detail, after };
    },
    pollMs,
  );

  function refresh() {
    setPollMs(POLL_STEPS_MS[0]);
    query.reload();
    onChanged();
  }

  async function cancelGroup() {
    setCancelling(true);
    try {
      await api.cancelImportGroup(groupId);
      toast.push("ok", "Importação cancelada. Os perfis já concluídos foram mantidos.");
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setCancelling(false);
      setConfirmCancel(false);
      refresh();
    }
  }

  async function resume(child: Batch) {
    setResumingId(child.id);
    try {
      await api.resumeListing(child.id);
      toast.push("ok", `Importação de @${child.source_profile ?? "?"} retomada.`);
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setResumingId(null);
      refresh();
    }
  }

  const detail = query.data;
  if (!detail) {
    return (
      <div className="panel" style={{ marginTop: 20 }}>
        {query.error ? <ErrorLine message={query.error} onRetry={query.reload} /> : <SkeletonRows rows={3} />}
      </div>
    );
  }

  const { group, children } = detail;
  // Ao trocar de página, o dado anterior fica na tela até o novo chegar: a paginação espera.
  const pageReady = detail.after === after;
  const status = groupStatusLabel(group);
  const exportOffer = groupExportOffer(group);
  const attention = groupAttentionText(group);
  const open = group.profiles.waiting + group.profiles.active + group.profiles.attention;
  const first = children[0]?.group_position ?? null;
  const last = children[children.length - 1]?.group_position ?? null;
  const goTo = (next: number[]) => {
    setCursors(next);
    setPollMs(POLL_STEPS_MS[0]);
  };

  return (
    <div className="panel" style={{ marginTop: 20 }}>
      <div className="row between" style={{ flexWrap: "wrap" }}>
        <div className="row">
          <strong style={{ fontSize: 14 }}>{groupTitle(group)}</strong>
          <Pill tone={status.tone}>{status.label}</Pill>
          {group.status === "active" ? <Spinner /> : null}
        </div>
        <span className="muted small">{groupOptionsText(group)}</span>
      </div>
      <div className="muted small" style={{ marginTop: 6 }} aria-live="polite">
        {groupSummary(group)}
      </div>
      {progressExtrasText(group.progress) ? (
        <div className="muted small" style={{ marginTop: 4 }}>
          {progressExtrasText(group.progress)}
        </div>
      ) : null}
      {groupViewsText({ ...group, views_filter: visibleFilter(group.views_filter) }) ? (
        <div className="muted small" style={{ marginTop: 4 }}>
          {groupViewsText({ ...group, views_filter: visibleFilter(group.views_filter) })}
        </div>
      ) : null}
      {attention ? (
        <div className="alert warn" role="status" style={{ marginTop: 10 }}>
          <span className="grow">{attention}</span>
        </div>
      ) : null}
      {notice ? (
        <div className="alert" role="status" style={{ marginTop: 10 }}>
          <span className="grow">{notice}</span>
        </div>
      ) : null}
      {query.error ? (
        <div style={{ marginTop: 10 }}>
          <ErrorLine message={query.error} onRetry={query.reload} />
        </div>
      ) : null}

      <div className="table-wrap" style={{ marginTop: 12 }}>
        <table className="table">
          <caption className="sr-only">Perfis desta importação</caption>
          <thead>
            <tr>
              <th className="num">#</th>
              <th>Perfil</th>
              <th>Estado</th>
              <th>Mídias</th>
              <th className="actions">Ações</th>
            </tr>
          </thead>
          <tbody>
            {children.map((child) => {
              const state = profileStateLabel(child, now);
              const stateDetail = profileStateDetail(child, now);
              const who = `@${child.source_profile ?? "?"}`;
              return (
                <tr key={child.id} className={child.id === openBatchId ? "selected" : undefined}>
                  <td className="num muted">{formatCount(child.group_position)}</td>
                  <td className="truncate" style={{ maxWidth: 220 }}>
                    <button
                      type="button"
                      className="link-btn"
                      aria-label={`Abrir a importação de ${who}`}
                      onClick={() => onOpenBatch(child.id)}
                    >
                      {who}
                    </button>
                  </td>
                  <td>
                    <Pill tone={state.tone}>{state.label}</Pill>
                    {stateDetail ? <div className="muted small group-detail">{stateDetail}</div> : null}
                  </td>
                  <td className="muted">{profileProgressText(child)}</td>
                  <td className="actions">
                    {childState(child) === "attention" ? (
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={resumingId !== null}
                        aria-label={`Retomar a importação de ${who}`}
                        onClick={() => void resume(child)}
                      >
                        {resumingId === child.id ? <Spinner /> : null}
                        Retomar
                      </button>
                    ) : null}
                    {childState(child) !== "waiting" ? (
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        aria-label={`Ver ${who} na Fila`}
                        onClick={() => navigate(queueForBatch(child.id))}
                      >
                        Fila
                      </button>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="row" style={{ marginTop: 10, flexWrap: "wrap" }}>
        {open > 0 ? (
          <button type="button" className="btn btn-sm" disabled={cancelling} onClick={() => setConfirmCancel(true)}>
            Cancelar importação
          </button>
        ) : null}
        {onClose ? (
          <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>
            Fechar
          </button>
        ) : null}
        <span className="grow" />
        {cursors.length > 0 || detail.next_after_position !== null ? (
          <>
            <span className="muted small">
              {first !== null && last !== null
                ? `Perfis ${formatCount(first)} a ${formatCount(last)} de ${formatCount(group.profiles.total)}`
                : `${formatCount(group.profiles.total)} perfis`}
            </span>
            {cursors.length > 1 ? (
              <button type="button" className="btn btn-sm btn-ghost" onClick={() => goTo([])}>
                Primeira página
              </button>
            ) : null}
            <button
              type="button"
              className="btn btn-sm"
              disabled={cursors.length === 0 || !pageReady}
              onClick={() => goTo(cursors.slice(0, -1))}
            >
              Anterior
            </button>
            <button
              type="button"
              className="btn btn-sm"
              disabled={detail.next_after_position === null || !pageReady}
              onClick={() => {
                if (detail.next_after_position !== null) goTo([...cursors, detail.next_after_position]);
              }}
            >
              Próxima
            </button>
          </>
        ) : null}
      </div>

      <AutoExportLine auto={group.auto_export} />

      <ImportExportBar
        source={{ kind: "group", id: group.id }}
        finished={exportOffer.offer}
        note={exportOffer.note}
        lastExport={group.last_export}
        libraryPath={ROUTES.library}
        contentChoice={group.process}
        onChanged={refresh}
      />

      {confirmCancel ? (
        <ConfirmDialog
          title="Cancelar a importação inteira?"
          confirmLabel="Cancelar importação"
          cancelLabel="Voltar"
          focusCancel
          danger
          busy={cancelling}
          onConfirm={() => void cancelGroup()}
          onCancel={() => setConfirmCancel(false)}
        >
          {open === 1
            ? "1 perfil ainda aberto será cancelado."
            : `${formatCount(open)} perfis ainda abertos serão cancelados.`}{" "}
          Os perfis já concluídos e as mídias já baixadas continuam salvos.
        </ConfirmDialog>
      ) : null}
    </div>
  );
}
