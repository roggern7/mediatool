"""Relatório da Análise de Perfis: ZIP com XLSX + CSV + HTML. XLSX e CSV têm todas as colunas (`COLUMNS`); o HTML mostra
as 8 colunas da tela (`HTML_COLUMNS`) e leva nome, Reels somados e observação em linhas menores dentro das células, para
Data e Horário nunca saírem da largura da página.

Gerado sob demanda a partir do banco (nunca consulta o Instagram). Tudo que veio do Instagram é dado não confiável: no
CSV o início de `Nome`/`Observação` é neutralizado contra fórmula, no HTML tudo passa por `html.escape`. Não verificado
nunca aparece como Não. Cada arquivo é escrito linha a linha, numa passada própria pelos itens: a memória não depende do
total de perfis.
"""

from __future__ import annotations

import csv
import html
import io
import shutil
import tempfile
import time as clock
import zipfile
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import IO, Any, TextIO

from app.services import export_time
from app.services.xlsx_writer import write_xlsx

COLUMNS = ("Perfil", "Nome", "Posts", "Posts exato", "Views", "Reels somados", "Stories", "Destaques", "Status", "Data",
           "Horário", "Observação")
WIDTHS = (24, 28, 12, 12, 14, 14, 15, 15, 16, 12, 10, 56)
NAME_COLUMN, NOTE_COLUMN = 1, 11
HTML_COLUMNS = ("Perfil", "Posts", "Views", "Stories", "Destaques", "Status", "Data", "Horário")
HTML_WIDTHS = (28, 10.5, 10.5, 10.5, 10.5, 11, 10, 9)   # % por coluna: `table-layout:fixed` não precisa ler as linhas

YES, NO, NOT_VERIFIED, NOT_QUERIED = "Sim", "Não", "Não verificado", "Não consultado"
NO_REELS = "Sem Reels"
CANCELLED = "Cancelado"
VIEWS_CARD = "Views totais"
TRISTATE = {"yes": YES, "no": NO}
ACCOUNT_LABELS = {"active": "Ativa", "private": "Privada", "restricted": "Restrita", "unavailable": "Indisponível",
                  "unverified": NOT_VERIFIED}
ANALYSIS_LABELS = {"completed": "Concluída", "cancelled": "Cancelada", "interrupted": "Interrompida"}
PILL_TONES = {YES: "ok", "Ativa": "ok", "Privada": "accent", "Restrita": "accent", "Indisponível": "err",
              NOT_VERIFIED: "warn"}
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")

_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0f1113;color:#e7e9eb;font:14px/1.5 Inter,"Segoe UI",system-ui,sans-serif}
main{max-width:1280px;margin:0 auto;padding:32px 20px}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:15px;margin:28px 0 10px}
a{color:#8fa8ff;text-decoration:none}
.meta{color:#9aa2ab;margin:0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:24px 0}
.card{background:#171a1d;border:1px solid #272c31;border-radius:8px;padding:12px 14px}
.card b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.card span{color:#9aa2ab;font-size:12px}
.note{color:#b4bbc2;background:#171a1d;border-left:3px solid #3b4450;padding:10px 14px;margin:0;font-size:13px}
.table-wrap{overflow-x:auto;border:1px solid #272c31;border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:1120px;table-layout:fixed;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #22272c;vertical-align:top;overflow-wrap:anywhere}
th{background:#1b1f23;color:#c5cbd2;font-weight:600;white-space:nowrap}
tbody tr:nth-child(even){background:#14171a}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.nowrap{white-space:nowrap;font-variant-numeric:tabular-nums}
small{display:block;color:#9aa2ab;font-size:12px;white-space:normal}
.pill{display:inline-block;padding:0 7px;border-radius:999px;font-size:12px;white-space:nowrap;background:#23282d;
color:#b4bbc2}
.pill.ok{background:#16352a;color:#7fd8ae}
.pill.accent{background:#232c4d;color:#a9bbff}
.pill.warn{background:#3b3016;color:#e9c46a}
.pill.err{background:#40201f;color:#f0a09b}
ul{margin:6px 0 0;padding-left:20px;color:#b4bbc2;font-size:13px}
@page{size:landscape;margin:12mm}
@media print{
:root{color-scheme:light}
body{background:#fff;color:#111;font-size:11px}
main{max-width:none;padding:0}
a{color:#111}
.meta,.card span,.note,small,ul{color:#333}
.card,.note{background:#fff;border-color:#bbb}
.table-wrap{overflow:visible;border:0}
table{font-size:10px;min-width:0}
th{background:#eee;color:#111}
th,td{border-bottom:1px solid #ccc}
thead{display:table-header-group}
tr{break-inside:avoid}
tbody tr:nth-child(even){background:#f6f6f6}
.pill,.pill.ok,.pill.accent,.pill.warn,.pill.err{background:none;color:#111;padding:0}
}
""".strip()


def local_moment(value: str | None, zone: export_time.Zone) -> datetime | None:
    """Instante gravado (UTC) no fuso de apresentação."""
    moment = export_time.publication_moment(value)
    return None if moment is None else zone(moment)


def local_parts(value: str | None, zone: export_time.Zone) -> tuple[str | None, str | None]:
    """("31/10/2026", "23:58:01") ou (None, None)."""
    moment = local_moment(value, zone)
    if moment is None:
        return None, None
    return f"{moment.day:02d}/{moment.month:02d}/{moment.year:04d}", moment.strftime("%H:%M:%S")


def _cells(item: dict[str, Any], *, numeric_posts: bool = False) -> list[str | int]:
    """Valores de uma linha, na ordem de `COLUMNS`. `numeric_posts`: aproximado como número (a célula do XLSX)."""
    username = item["username"]
    if item["state"] != "done":
        # `cancelled` sem `error_code` = cancelado pelo usuário; com ele = análise interrompida antes deste perfil.
        status = CANCELLED if item["state"] == "cancelled" and not item["error_code"] else NOT_QUERIED
        return [username, "", *[NOT_QUERIED] * 6, status, "", "", item["error_message"] or ""]
    account = item["account_status"]
    date, time = item.get("checked_date") or "", item.get("checked_time") or ""
    note = item["error_message"] or ""
    label = ACCOUNT_LABELS.get(account, NOT_VERIFIED)
    if account not in ("active", "private", "restricted"):
        return [username, item["display_name"] or "", *[NOT_VERIFIED] * 6, label, date, time, note]
    posts: str | int = NOT_VERIFIED
    posts_exact = NOT_VERIFIED
    if item["posts_state"] == "known":
        exact = bool(item["posts_exact"])
        posts = item["posts_count"] if exact or numeric_posts else item["posts_text"] or item["posts_count"]
        posts_exact = YES if exact else NO
    views: str | int = NOT_VERIFIED
    reels: str | int = NOT_VERIFIED
    if item["views_state"] == "known":
        views, reels = item["views_total"], item["views_reels"]
    elif item["views_state"] == "none":
        views, reels = NO_REELS, 0
    return [username, item["display_name"] or "", posts, posts_exact, views, reels,
            TRISTATE.get(item["stories"], NOT_VERIFIED), TRISTATE.get(item["highlights"], NOT_VERIFIED), label,
            date, time, note]


def _xlsx_row(item: dict[str, Any]) -> list[str | int]:
    """No XLSX a célula é texto (nunca fórmula), então o `@` pode ir; no CSV ele fica de fora."""
    row = _cells(item, numeric_posts=True)
    row[0] = f"@{row[0]}"
    return row


def neutralize(value: str) -> str:
    """Texto não confiável no CSV: um início que o Excel leria como fórmula recebe `'`."""
    return f"'{value}" if value.startswith(_FORMULA_START) else value


def write_csv(target: TextIO, items: Iterable[dict[str, Any]]) -> None:
    """`;` e CRLF (Excel pt-BR); `target` em UTF-8 com BOM e sem tradução de fim de linha."""
    writer = csv.writer(target, delimiter=";", lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for item in items:
        row = _cells(item)
        row[NAME_COLUMN] = neutralize(str(row[NAME_COLUMN]))
        row[NOTE_COLUMN] = neutralize(str(row[NOTE_COLUMN]))
        writer.writerow(row)


def _thousands(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def _count(value: int, one: str, many: str) -> str:
    return f"{_thousands(value)} {one if value == 1 else many}"


def _small(value: str | int) -> str:
    return f"<small>{html.escape(str(value), quote=True)}</small>" if value != "" else ""


def _pill(value: str) -> str:
    """Todo texto de estado (Sim, Não verificado, Sem Reels, Não consultado, status da conta) usa esta classe."""
    classes = f"pill {PILL_TONES.get(value, '')}".strip()
    return f'<span class="{classes}">{html.escape(value, quote=True)}</span>'


def _pill_td(value: str, colspan: int = 1) -> str:
    span = f' colspan="{colspan}"' if colspan > 1 else ""
    return f"<td{span}>{_pill(value)}</td>"


def _number_td(value: str | int, *, approximate: bool = False, detail: str = "") -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        text = _thousands(value)
    elif value in (NOT_VERIFIED, NO_REELS):
        text = _pill(value)
    else:
        text = html.escape(str(value), quote=True)
    return f'<td class="num">{"≈ " if approximate else ""}{text}{detail}</td>'


def _row(item: dict[str, Any]) -> str:
    """Linha do HTML, nas colunas de `HTML_COLUMNS`."""
    username, name, posts, posts_exact, views, reels, stories, highlights, status, date, time, note = _cells(item)
    safe = html.escape(str(username), quote=True)
    profile = (f'<td><a href="https://www.instagram.com/{safe}/" rel="noopener noreferrer">@{safe}</a>'
               f"{_small(name)}{_small(note)}</td>")
    if item["state"] != "done":   # nunca consultado: o Status diz o motivo, e não há valor por coluna a mostrar
        return f"<tr>{profile}{_pill_td(NOT_QUERIED, 4)}{_pill_td(status)}{_pill_td(NOT_QUERIED, 2)}</tr>"
    summed = ""
    if isinstance(reels, int) and reels:
        summed = _small(_count(reels, "Reel", "Reels"))
    cells = (profile, _number_td(posts, approximate=posts_exact == NO), _number_td(views, detail=summed),
             _pill_td(stories), _pill_td(highlights), _pill_td(status),
             f'<td class="nowrap">{html.escape(date or NOT_QUERIED)}</td>',
             f'<td class="nowrap">{html.escape(time or NOT_QUERIED)}</td>')
    return f"<tr>{''.join(cells)}</tr>"


def _moment_text(value: str | None, zone: export_time.Zone) -> str:
    date, time = local_parts(value, zone)
    return f"{date} {time}" if date else ""


def account_totals(summary: dict[str, int]) -> dict[str, int]:
    """Resumo da consulta, única regra para tela e relatório: `active` reúne públicas, privadas e restritas (a conta
    existe) e `inactive` são as indisponíveis. Os quatro grupos somam o que já terminou; o resto ainda está na fila."""
    return {"active": summary["active"] + summary["private"] + summary["restricted"],
            "inactive": summary["unavailable"], "unverified": summary["unverified"],
            "not_consulted": summary["cancelled"]}


def _card(label: str, value: int, total: int, detail: str) -> str:
    share = f" · {round(value * 100 / total)}%" if total and label not in ("Total", VIEWS_CARD) else ""
    return (f'<div class="card"><b>{_thousands(value)}</b><span>{label}{share}</span>'
            f"{f'<small>{detail}</small>' if detail else ''}</div>")


def _html_head(analysis: dict[str, Any], zone: export_time.Zone) -> str:
    summary = analysis["summary"]
    accounts = account_totals(summary)
    total = analysis["total"]
    kinds = (f"{_thousands(summary['active'])} públicas · {_thousands(summary['private'])} privadas · "
             f"{_thousands(summary['restricted'])} restritas")
    cards = (("Total", total, ""), ("Contas ativas", accounts["active"], kinds),
             ("Contas inativas", accounts["inactive"], "não encontradas ou removidas"),
             ("Não verificadas", accounts["unverified"], "sem confirmação do Instagram"),
             ("Não consultados", accounts["not_consulted"], ""),
             (VIEWS_CARD, summary["views_total"], "soma dos últimos Reels lidos de cada conta"),
             ("Com Stories", summary["stories_yes"], ""),
             ("Com Destaques", summary["highlights_yes"], ""))
    card_html = "".join(_card(label, int(value), total, detail) for label, value, detail in cards)
    head = "".join(f"<th>{html.escape(column)}</th>" for column in HTML_COLUMNS)
    cols = "".join(f'<col style="width:{width}%">' for width in HTML_WIDTHS)
    status = html.escape(ANALYSIS_LABELS.get(analysis["status"], str(analysis["status"])))
    started = _moment_text(analysis["started_at"] or analysis["created_at"], zone)
    finished = _moment_text(analysis["finished_at"], zone)
    meta = [f"Análise nº {int(analysis['id'])}", f"Início: {started}" if started else "",
            f"Fim: {finished}" if finished else "", f"Status: {status}"]
    error = f'<p class="note">{html.escape(analysis["error"])}</p>' if analysis.get("error") else ""
    return (
        '<!doctype html>\n<html lang="pt-BR"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Análise de Perfis nº {int(analysis['id'])}</title><style>{_CSS}</style></head><body><main>"
        f"<h1>Análise de Perfis</h1><p class=\"meta\">{' · '.join(part for part in meta if part)}</p>"
        f'<section class="cards">{card_html}</section>{error}'
        '<p class="note"><b>Views</b> é a soma dos últimos Reels lidos (quantidade abaixo do número), não o total da'
        " vida da conta. <b>≈</b> marca um valor de Posts arredondado pelo Instagram, que sem login informa, por"
        " exemplo, 33K. <b>Contas ativas</b> reúne públicas, privadas e restritas: a conta existe."
        " <b>Status</b> mostra se o perfil estava acessível sem login: Ativa não significa que ele"
        " publica recentemente. <b>Não verificado</b> quer dizer que a informação não pôde ser confirmada; nunca"
        " significa Não. <b>Não consultado</b> quer dizer que a análise terminou antes de chegar a esse perfil.</p>"
        f'<h2>Perfis</h2><div class="table-wrap"><table><colgroup>{cols}</colgroup><thead><tr>{head}</tr></thead>'
        "<tbody>\n"
    )


def write_html(target: TextIO, analysis: dict[str, Any], items: Iterable[dict[str, Any]],
               zone: export_time.Zone) -> None:
    """Arquivo único: sem JS, sem recurso externo, CSS inline. Tudo escapado."""
    target.write(_html_head(analysis, zone))
    for item in items:
        target.write(f"{_row(item)}\n")
    target.write(f"</tbody></table></div>{_ignored_section(analysis)}</main></body></html>\n")


def _ignored_section(analysis: dict[str, Any]) -> str:
    invalid, duplicates = analysis["invalid_count"], analysis["duplicate_count"]
    if not invalid and not duplicates:
        return ""
    sample = analysis["invalid_lines"]
    lines = "".join(f"<li>{html.escape(entry['line'], quote=True)}: {html.escape(entry['reason'], quote=True)}</li>"
                    for entry in sample)
    listing = f"<ul>{lines}</ul>" if lines else ""
    counts = [_count(invalid, "linha inválida", "linhas inválidas") if invalid else "",
              _count(duplicates, "linha duplicada", "linhas duplicadas") if duplicates else ""]
    limited = f" A lista abaixo mostra só as {len(sample)} primeiras inválidas." if invalid > len(sample) else ""
    return (f"<h2>Linhas ignoradas</h2><p class=\"meta\">{' e '.join(part for part in counts if part)}.{limited}</p>"
            f"{listing}")


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=clock.localtime()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def write_report(target: IO[bytes], analysis: dict[str, Any], items: Callable[[], Iterable[dict[str, Any]]],
                 zone: export_time.Zone) -> str:
    """Escreve o ZIP em `target` e devolve o nome dele. `items` abre uma passada nova pelos itens a cada arquivo. A
    data do nome é a data local do início da análise. O XLSX é um ZIP dentro do ZIP: sai primeiro num temporário."""
    start = local_moment(analysis["started_at"] or analysis["created_at"], zone)
    stem = f"relatorio-perfis-{start.year:04d}-{start.month:02d}-{start.day:02d}"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        with tempfile.TemporaryFile() as sheet:
            write_xlsx(sheet, COLUMNS, (_xlsx_row(item) for item in items()), row_count=analysis["total"],
                       widths=WIDTHS, sheet_name="Perfis")
            sheet.seek(0)
            with archive.open(_entry(f"{stem}.xlsx"), "w") as entry:
                shutil.copyfileobj(sheet, entry)
        with io.TextIOWrapper(archive.open(_entry(f"{stem}.csv"), "w"), encoding="utf-8-sig", newline="") as text:
            write_csv(text, items())
        with io.TextIOWrapper(archive.open(_entry(f"{stem}.html"), "w"), encoding="utf-8", newline="") as text:
            write_html(text, analysis, items(), zone)
    return f"{stem}.zip"
