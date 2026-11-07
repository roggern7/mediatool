import { useState } from "react";
import { ArrowLeft, Download, ExternalLink } from "lucide-react";
import { api, downloadGeneratedFile, messageOf } from "../api/client";
import type { PostTimesAccount, PostTimesReport } from "../api/types";
import { Empty, ErrorLine, PageHeader, Pill, SkeletonRows, Spinner } from "../components/ui";
import { formatDateTime } from "../lib/format";
import {
  barFractions,
  formatDecimal,
  formatLocalDateTime,
  hourLabel,
  localParts,
  mediaKindName,
  postsText,
  postTimesQuery,
  postTimesTargetFromSearch,
  regularityTone,
  SEGMENT_KIND_LABELS,
  sourceLabel,
  type PostTimesTarget,
} from "../lib/postTimesReport";
import { navigate, ROUTES, useSearch } from "../lib/router";
import { useToast } from "../lib/toast";
import { useQuery } from "../lib/useQuery";
import "../styles/postTimes.css";

const ITEMS_PAGE = 50;
const COMPARISON_TAB = "__comparativo__";

function goBack() {
  if (window.history.length > 1) window.history.back();
  else navigate(ROUTES.import);
}

export function PostTimesPage() {
  const target = postTimesTargetFromSearch(useSearch());
  return (
    <div className="page">
      {target ? (
        <ReportView key={`${target.kind}:${target.id}`} target={target} />
      ) : (
        <>
          <Head report={undefined} />
          <Empty title="Nenhum relatório indicado">
            <span className="muted">Abra esta página pelo botão “Ver padrões” de uma importação.</span>
          </Empty>
        </>
      )}
    </div>
  );
}

function ZipButton({ url }: { url: string }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  async function download() {
    setBusy(true);
    try {
      await downloadGeneratedFile(url, "relatorio-padroes.zip");
    } catch (err) {
      toast.push("err", messageOf(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <button type="button" className="btn" disabled={busy} onClick={() => void download()}>
      {busy ? <Spinner /> : <Download size={15} strokeWidth={1.75} aria-hidden="true" />}
      Baixar .zip
    </button>
  );
}

function Head({ report }: { report: PostTimesReport | undefined }) {
  return (
    <PageHeader
      title="Padrões de publicação"
      subtitle={
        report ? (
          <span className="pt-meta">
            <span>{report.title}</span>
            <span>Fuso {report.timezone}</span>
            <span>Gerado em {formatDateTime(report.generated_at)}</span>
          </span>
        ) : undefined
      }
    >
      <button type="button" className="btn btn-ghost" onClick={goBack}>
        <ArrowLeft size={15} strokeWidth={1.75} aria-hidden="true" />
        Voltar
      </button>
      {report ? <ZipButton url={report.zip_url} /> : null}
    </PageHeader>
  );
}

function ReportView({ target }: { target: PostTimesTarget }) {
  const query = useQuery(`post-times-report:${target.kind}:${target.id}`, () =>
    api.postTimesReport(postTimesQuery(target)),
  );
  const report = query.data;
  const [tab, setTab] = useState<string | null>(null);

  if (report === undefined) {
    return (
      <>
        <Head report={undefined} />
        {query.error ? (
          <ErrorLine message={query.error} onRetry={query.reload} />
        ) : (
          <div className="panel">
            <SkeletonRows rows={6} />
          </div>
        )}
      </>
    );
  }

  if (report.accounts.length === 0) {
    return (
      <>
        <Head report={report} />
        <Empty title="Sem publicações para analisar">
          <span className="muted">Este relatório ainda não tem horários de publicações.</span>
        </Empty>
      </>
    );
  }

  const multi = report.accounts.length > 1;
  const current = tab ?? (multi ? COMPARISON_TAB : (report.accounts[0]?.username ?? COMPARISON_TAB));
  const account = report.accounts.find((a) => a.username === current);

  return (
    <>
      <Head report={report} />
      {multi ? (
        <div className="tabs" role="tablist" aria-label="Contas do relatório">
          <button
            type="button"
            role="tab"
            aria-selected={current === COMPARISON_TAB}
            className={`tab${current === COMPARISON_TAB ? " on" : ""}`}
            onClick={() => setTab(COMPARISON_TAB)}
          >
            Comparativo
          </button>
          {report.accounts.map((a) => (
            <button
              key={a.username}
              type="button"
              role="tab"
              aria-selected={current === a.username}
              className={`tab${current === a.username ? " on" : ""}`}
              onClick={() => setTab(a.username)}
            >
              @{a.username}
              <span className="count">{a.posts}</span>
            </button>
          ))}
        </div>
      ) : null}
      {account ? <AccountView key={account.username} account={account} /> : <Comparison report={report} />}
    </>
  );
}

function Comparison({ report }: { report: PostTimesReport }) {
  return (
    <section className="panel" aria-label="Comparativo entre contas">
      <div className="section-head">
        <h2>Comparativo</h2>
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Perfil</th>
              <th className="num">Posts</th>
              <th className="num">Posts por dia</th>
              <th>Intervalo médio</th>
              <th>Intervalo mediano</th>
              <th className="num">Rajadas</th>
              <th>Horário de pico</th>
              <th>Regularidade</th>
            </tr>
          </thead>
          <tbody>
            {report.comparison.map((row) => (
              <tr key={row.username}>
                <td className="row-link">@{row.username}</td>
                <td className="num">{row.posts}</td>
                <td className="num">{formatDecimal(row.posts_per_day)}</td>
                <td className="nowrap">{row.interval_avg_text ?? "-"}</td>
                <td className="nowrap">{row.interval_median_text ?? "-"}</td>
                <td className="num">{row.bursts}</td>
                <td>{row.top_hour === null ? "-" : hourLabel(row.top_hour)}</td>
                <td>
                  {row.regularity ? <Pill tone={regularityTone(row.regularity)}>{row.regularity}</Pill> : "-"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {report.comparison_notes.length > 0 ? (
        <ul className="pt-patterns pt-section">
          {report.comparison_notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function Kpis({ account: a }: { account: PostTimesAccount }) {
  const extras = [
    a.pinned_out > 0 ? `${a.pinned_out} fixado(s) fora` : null,
    a.estimated > 0 ? `${a.estimated} estimado(s)` : null,
  ].filter(Boolean);
  const range =
    a.interval_min_text && a.interval_max_text ? `de ${a.interval_min_text} a ${a.interval_max_text}` : "sem intervalos";
  const cards = [
    { label: "Posts analisados", value: String(a.posts), detail: extras.join(" · ") || "na sequência" },
    { label: "Intervalo médio", value: a.interval_avg_text ?? "-", detail: range },
    { label: "Intervalo mediano", value: a.interval_median_text ?? "-", detail: "metade dos intervalos é menor" },
    {
      label: "Posts por dia",
      value: formatDecimal(a.posts_per_day),
      detail: a.days ? `em ${a.days} ${a.days === 1 ? "dia" : "dias"}` : "-",
    },
    {
      label: "Rajadas",
      value: String(a.bursts),
      detail: a.largest_burst ? `maior: ${postsText(a.largest_burst)}` : "nenhuma",
    },
    {
      label: "Regularidade",
      value: a.regularity ?? "indeterminada",
      detail: a.cv === null ? "-" : `coef. de variação ${formatDecimal(a.cv, 2)}`,
    },
  ];
  return (
    <div className="account-report" role="group" aria-label={`Resumo de @${a.username}`}>
      {cards.map((card) => (
        <div key={card.label} className="account-card">
          <strong className={card.label === "Regularidade" ? `tone-${regularityTone(a.regularity)}` : undefined}>
            {card.value}
          </strong>
          <span>{card.label}</span>
          <span className="muted small">{card.detail}</span>
        </div>
      ))}
    </div>
  );
}

function BarChart({
  title,
  bars,
  unit,
}: {
  title: string;
  bars: { key: string; label: string; count: number; top: boolean }[];
  unit: (n: number) => string;
}) {
  const fractions = barFractions(bars.map((b) => b.count));
  return (
    <figure>
      <figcaption>
        <strong>{title}</strong>
      </figcaption>
      <ul className="pt-bars" aria-label={title}>
        {bars.map((bar, i) => {
          const text = `${bar.label}: ${unit(bar.count)}${bar.top ? " (preferido)" : ""}`;
          return (
            <li
              key={bar.key}
              className={`${bar.top ? "top" : ""}${bar.count === 0 ? " zero" : ""}`}
              title={text}
              aria-label={text}
            >
              <span className="pt-bar-value" aria-hidden="true">
                {bar.count > 0 ? bar.count : ""}
              </span>
              <span className="pt-bar-track" aria-hidden="true">
                <span className="pt-bar-fill" style={{ height: `${(fractions[i] ?? 0) * 100}%` }} />
              </span>
              <span className="pt-bar-label" aria-hidden="true">
                {bar.label}
              </span>
            </li>
          );
        })}
      </ul>
    </figure>
  );
}

function Charts({ account: a }: { account: PostTimesAccount }) {
  const topHours = new Set(a.top_hours);
  const activeDays = new Set(a.most_active_days.map((d) => d.slice(0, 3)));
  const facts = [
    {
      label: "Horários preferidos",
      value: a.top_hours.length
        ? `${a.top_hours.map(hourLabel).join(", ")}${a.top_hours_share === null ? "" : ` (${a.top_hours_share}% dos posts)`}`
        : null,
    },
    { label: "Faixa típica", value: a.typical_band },
    { label: "Dias mais ativos", value: a.most_active_days.length ? a.most_active_days.join(", ") : null },
    { label: "Janela sem posts", value: a.quiet_window },
    { label: "Maior pausa", value: a.largest_pause_text },
    { label: "Tipo mais postado", value: a.top_kind },
  ].filter((f) => f.value);
  return (
    <div className="pt-charts">
      <section className="panel pt-chart">
        <BarChart
          title="Publicações por hora do dia"
          unit={postsText}
          bars={a.hours.map((h) => ({
            key: String(h.hour),
            label: String(h.hour).padStart(2, "0"),
            count: h.count,
            top: topHours.has(h.hour),
          }))}
        />
        {facts.length > 0 ? (
          <dl className="pt-facts">
            {facts.map((f) => (
              <div key={f.label}>
                <dt>{f.label}</dt>
                <dd>{f.value}</dd>
              </div>
            ))}
          </dl>
        ) : null}
      </section>
      <section className="panel pt-chart">
        <BarChart
          title="Publicações por dia da semana"
          unit={postsText}
          bars={a.weekdays.map((d) => ({
            key: d.day,
            label: d.day,
            count: d.count,
            top: activeDays.has(d.day.slice(0, 3)),
          }))}
        />
      </section>
    </div>
  );
}

function Segments({ account: a }: { account: PostTimesAccount }) {
  if (a.segments.length === 0) return null;
  return (
    <section className="panel pt-section" aria-label="Sequência">
      <div className="section-head">
        <h2>Sequência</h2>
        <span className="muted small">{a.segments.length} trecho(s) em ordem cronológica</span>
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th className="num">#</th>
              <th>Tipo</th>
              <th>Início</th>
              <th>Fim</th>
              <th className="num">Posts</th>
              <th>Intervalo médio</th>
              <th>Descrição</th>
            </tr>
          </thead>
          <tbody>
            {a.segments.map((s) => (
              <tr key={s.index}>
                <td className="num dim">{s.index}</td>
                <td>
                  <span className={`pt-badge ${s.kind}`}>{SEGMENT_KIND_LABELS[s.kind] ?? s.kind}</span>
                </td>
                <td className="nowrap">{formatLocalDateTime(s.start)}</td>
                <td className="nowrap">{formatLocalDateTime(s.end)}</td>
                <td className="num">{s.posts}</td>
                <td className="nowrap">{s.avg_text || "-"}</td>
                <td className="muted">{s.text}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Items({ account: a }: { account: PostTimesAccount }) {
  const [shown, setShown] = useState(ITEMS_PAGE);
  const items = a.items.slice(0, shown);
  return (
    <section className="panel pt-section" aria-label="Publicações">
      <div className="section-head">
        <h2>Publicações</h2>
        <span className="muted small">{postsText(a.items.length)}</span>
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th className="num">#</th>
              <th>Data</th>
              <th>Hora</th>
              <th>Dia</th>
              <th>Intervalo</th>
              <th className="num">Trecho</th>
              <th>Rajada</th>
              <th>Tipo</th>
              <th>Horário</th>
              <th>Link</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const p = localParts(item.taken_at);
              return (
                <tr key={`${item.n}:${item.code}`}>
                  <td className="num dim">{item.n}</td>
                  <td className="nowrap">{p?.date ?? "-"}</td>
                  <td className="nowrap">{p?.time ?? "-"}</td>
                  <td>{p?.weekday ?? "-"}</td>
                  <td className="nowrap">{item.interval_text || "-"}</td>
                  <td className="num">{item.segment ?? "-"}</td>
                  <td>{item.in_sequence ? (item.burst ? "Sim" : "Não") : "-"}</td>
                  <td className="nowrap">
                    {mediaKindName(item.kind)}
                    {item.pinned ? (
                      <>
                        {" "}
                        <span className="tag">Fixado</span>
                      </>
                    ) : null}
                    {!item.in_sequence ? (
                      <>
                        {" "}
                        <span className="tag" title="Fora da sequência analisada">
                          Fora da sequência
                        </span>
                      </>
                    ) : null}
                  </td>
                  <td className={`nowrap${item.source === "reels_tab" ? " tone-warn" : ""}`}>
                    {sourceLabel(item.source)}
                  </td>
                  <td>
                    <a href={item.url} target="_blank" rel="noopener noreferrer" className="row small">
                      Abrir
                      <ExternalLink size={12} strokeWidth={1.6} aria-hidden="true" />
                      <span className="sr-only"> no Instagram (nova aba)</span>
                    </a>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {a.items.length > shown ? (
        <div className="pt-more">
          <button type="button" className="btn btn-sm" onClick={() => setShown((n) => n + ITEMS_PAGE)}>
            Mostrar mais ({a.items.length - shown} restantes)
          </button>
        </div>
      ) : null}
    </section>
  );
}

function AccountView({ account: a }: { account: PostTimesAccount }) {
  const first = formatLocalDateTime(a.first_post);
  const last = formatLocalDateTime(a.last_post);
  return (
    <>
      <div className="section-head">
        <h2>@{a.username}</h2>
        {a.first_post ? (
          <span className="muted small">
            De {first} a {last}
          </span>
        ) : null}
      </div>
      <Kpis account={a} />
      {a.estimated > 0 ? (
        <div className="alert warn small pt-section">
          {a.estimated === 1 ? "1 horário foi estimado" : `${a.estimated} horários foram estimados`} pela aba Reels
          (precisão de ~±30 s), marcados como “Estimado (aba Reels)” na tabela de publicações.
        </div>
      ) : null}
      {a.patterns.length > 0 ? (
        <section className="panel pt-section" aria-label="Padrões identificados">
          <div className="section-head">
            <h2>Padrões identificados</h2>
          </div>
          <ul className="pt-patterns">
            {a.patterns.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </section>
      ) : null}
      <Charts account={a} />
      <Segments account={a} />
      <Items account={a} />
    </>
  );
}
