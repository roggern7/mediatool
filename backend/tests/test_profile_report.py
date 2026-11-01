from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import timedelta, timezone
from xml.etree import ElementTree

import pytest

from app.integrations.instagram.web.errors import InstagramWebError
from app.services import export_time, profile_analysis_service, profile_report, xlsx_writer
from app.services.profile_report import COLUMNS, HTML_COLUMNS
from profile_fakes import ScriptedFetcher, SimClock, create, install_runner, snapshot

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
STEM = "relatorio-perfis-2026-11-01"
EVIL_NAME = '=HYPERLINK("http://x")<script>alert(1)</script>&'


def finished(harness, names: tuple[str, ...], results: dict) -> int:
    clock = SimClock()
    runner = install_runner(harness, ScriptedFetcher(clock, results), clock)
    analysis = create(harness, *names)
    runner.run_next()
    return analysis["id"]


def report_files(harness, analysis_id: int) -> dict[str, bytes]:
    response = harness.client.get(f"/api/profile-analyses/{analysis_id}/report")
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in archive.infolist())
        return {name: archive.read(name) for name in archive.namelist()}


@pytest.fixture
def full_report(harness):
    results = {
        "ativa1": [snapshot("ativa1", display_name=EVIL_NAME, posts_count=33000, posts_exact=0, posts_text="33K")],
        "privada2": [snapshot("privada2", "private", stories="unverified", highlights="unverified",
                              views_state="unverified", views_total=None, views_reels=None)],
        "sumiu3": [snapshot("sumiu3", "unavailable", error_code="profile_unavailable", error_message="Perfil sumiu.")],
        "duvida4": [snapshot("duvida4", "unverified", error_code="x", error_message="-erro de leitura")],
        "semreels5": [snapshot("semreels5", views_state="none", views_total=None, views_reels=None)],
        "semposts6": [snapshot("semposts6", posts_state="unverified", posts_count=None, posts_exact=None,
                               display_name="@menção")],
    }
    analysis_id = finished(harness, tuple(results), results)
    return harness, analysis_id, report_files(harness, analysis_id)


def csv_rows(content: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content.decode("utf-8-sig"), newline=""), delimiter=";"))


def html_cells(page: str, username: str) -> list[str]:
    """Células da linha do perfil, na ordem de `HTML_COLUMNS`."""
    row = next(found for found in re.findall(r"<tr>.*?</tr>", page) if f">@{username}</a>" in found)
    return re.findall(r"<td[^>]*>(.*?)</td>", row)


def sheet_rows(content: bytes) -> list[list[object]]:
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        for name in package.namelist():
            ElementTree.fromstring(package.read(name))   # todas as partes são XML válido
        root = ElementTree.fromstring(package.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in root.find("m:sheetData", NS):
        values: list[object] = []
        for cell in row:
            if cell.get("t") == "inlineStr":
                values.append(cell.find("m:is/m:t", NS).text or "")
            else:
                value = cell.find("m:v", NS)
                values.append(None if value is None else int(value.text))
        rows.append(values)
    return rows


# ---- ZIP ----------------------------------------------------------------------------------------------------------
def test_zip_entries_and_name(full_report):
    _harness, _id, files = full_report
    assert sorted(files) == [f"{STEM}.csv", f"{STEM}.html", f"{STEM}.xlsx"]


def test_same_columns_same_order_in_all_three(full_report):
    _harness, _id, files = full_report
    assert tuple(csv_rows(files[f"{STEM}.csv"])[0]) == COLUMNS
    assert tuple(sheet_rows(files[f"{STEM}.xlsx"])[0]) == COLUMNS
    page = files[f"{STEM}.html"].decode("utf-8")
    assert tuple(re.findall(r"<th>(.*?)</th>", page)) == HTML_COLUMNS
    assert HTML_COLUMNS == ("Perfil", "Posts", "Views", "Stories", "Destaques", "Status", "Data", "Horário")
    assert COLUMNS == ("Perfil", "Nome", "Posts", "Posts exato", "Views", "Reels somados", "Stories", "Destaques",
                       "Status", "Data", "Horário", "Observação")


def test_reexport_of_old_analysis_never_calls_fetcher(harness_factory):
    first = harness_factory()
    analysis_id = finished(first, ("a1",), {})
    original = report_files(first, analysis_id)
    root = first.root
    first.__exit__(None, None, None)

    second = harness_factory(root=root)

    def forbidden(*_args):
        raise AssertionError("reexportar não pode consultar o Instagram")

    second.ctx.profile_fetcher = forbidden
    assert report_files(second, analysis_id) == original


# ---- XLSX ---------------------------------------------------------------------------------------------------------
def test_xlsx_parts_and_sheet_features(full_report):
    _harness, _id, files = full_report
    with zipfile.ZipFile(io.BytesIO(files[f"{STEM}.xlsx"])) as package:
        assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml",
                "xl/worksheets/sheet1.xml"} == set(package.namelist())
        assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in package.infolist())
        sheet = ElementTree.fromstring(package.read("xl/worksheets/sheet1.xml"))
        styles = ElementTree.fromstring(package.read("xl/styles.xml"))
        workbook = ElementTree.fromstring(package.read("xl/workbook.xml"))
    pane = sheet.find("m:sheetViews/m:sheetView/m:pane", NS)
    assert pane.get("state") == "frozen" and pane.get("ySplit") == "1"
    assert sheet.find("m:autoFilter", NS).get("ref") == "A1:L7"
    assert len(sheet.find("m:cols", NS)) == len(COLUMNS)
    header = sheet.find("m:sheetData", NS)[0]
    assert {cell.get("s") for cell in header} == {"1"}
    bold_style = styles.find("m:cellXfs", NS)[1]
    font = styles.find("m:fonts", NS)[int(bold_style.get("fontId"))]
    fill = styles.find("m:fills", NS)[int(bold_style.get("fillId"))]
    assert font.find("m:b", NS) is not None and fill.find("m:patternFill", NS).get("patternType") == "solid"
    assert workbook.find("m:sheets/m:sheet", NS).get("name") == "Perfis"


def test_xlsx_numbers_are_numbers_and_approximate_posts_keep_the_value(full_report):
    _harness, _id, files = full_report
    rows = {row[0]: row for row in sheet_rows(files[f"{STEM}.xlsx"])[1:]}
    active = rows["@ativa1"]
    assert active[2] == 33000 and active[3] == "Não" and active[4] == 150 and active[5] == 2
    assert active[1] == EVIL_NAME   # string inline: nunca fórmula
    assert rows["@semreels5"][2] == 10 and rows["@semreels5"][3] == "Sim" and rows["@semreels5"][4] == "Sem Reels"


def test_xlsx_drops_invalid_xml_characters():
    target = io.BytesIO()
    xlsx_writer.write_xlsx(target, ["A", "B", "C", "D"], [["ok\x00\x08\x0b￾\ud83d fim", 5, None, 1.5]],
                           row_count=1, widths=[10, 10, 10, 10])
    with zipfile.ZipFile(target) as package:
        root = ElementTree.fromstring(package.read("xl/worksheets/sheet1.xml"))
    assert root.find("m:sheetData", NS)[1][0].find("m:is/m:t", NS).text == "ok fim"


def test_column_names():
    assert [xlsx_writer.column_name(i) for i in (0, 11, 25, 26, 27, 701, 702)] == [
        "A", "L", "Z", "AA", "AB", "ZZ", "AAA"]


# ---- CSV ----------------------------------------------------------------------------------------------------------
def test_csv_format(full_report):
    _harness, _id, files = full_report
    content = files[f"{STEM}.csv"]
    assert content.startswith(b"\xef\xbb\xbf") and content.count(b"\r\n") >= 7
    assert b"\r\n" in content and not re.search(rb"(?<!\r)\n", content)
    assert content.decode("utf-8-sig").split("\r\n")[0] == ";".join(COLUMNS)


def test_csv_values_and_formula_neutralization(full_report):
    _harness, _id, files = full_report
    rows = {row[0]: row for row in csv_rows(files[f"{STEM}.csv"])[1:]}
    assert rows["ativa1"][1] == "'" + EVIL_NAME
    assert rows["ativa1"][2] == "33K" and rows["ativa1"][3] == "Não" and rows["ativa1"][4] == "150"
    assert rows["semposts6"][1] == "'@menção"
    assert rows["semposts6"][2:4] == ["Não verificado", "Não verificado"]
    assert rows["duvida4"][11] == "'-erro de leitura"
    assert rows["duvida4"][2:9] == ["Não verificado"] * 7
    assert rows["sumiu3"][2:8] == ["Não verificado"] * 6 and rows["sumiu3"][8] == "Indisponível"
    assert rows["sumiu3"][9] == "01/11/2026" and rows["sumiu3"][10]
    assert rows["privada2"][8] == "Privada" and rows["privada2"][6:8] == ["Não verificado", "Não verificado"]
    assert rows["privada2"][4] == "Não verificado"
    assert rows["semreels5"][4:6] == ["Sem Reels", "0"]
    assert rows["ativa1"][9] == "01/11/2026" and rows["ativa1"][10] == "12:00:01"


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_neutralize_prefixes(prefix):
    assert profile_report.neutralize(f"{prefix}x") == f"'{prefix}x"


def test_neutralize_leaves_plain_text():
    assert profile_report.neutralize("Maria") == "Maria" and profile_report.neutralize("") == ""


def test_unverified_never_appears_as_no(full_report):
    _harness, _id, files = full_report
    rows = {row[0]: row for row in csv_rows(files[f"{STEM}.csv"])[1:]}
    assert "Não" not in rows["duvida4"] and "Não" not in rows["privada2"][6:8]
    sheet = {row[0]: row for row in sheet_rows(files[f"{STEM}.xlsx"])[1:]}
    assert sheet["@duvida4"][2:9] == ["Não verificado"] * 7
    assert sheet["@privada2"][4:8] == ["Não verificado", "Não verificado", "Não verificado", "Não verificado"]
    page = files[f"{STEM}.html"].decode("utf-8")
    for username, columns in (("duvida4", (1, 2, 3, 4, 5)), ("privada2", (2, 3, 4))):
        cells = html_cells(page, username)
        assert all("Não verificado" in cells[index] for index in columns)
        assert not any(re.search(r">Não<", cell) or cell == "Não" for cell in cells)


def test_cancelled_items_are_not_queried(harness):
    clock = SimClock()
    runner = install_runner(harness, ScriptedFetcher(clock), clock)
    analysis = create(harness, "a1", "a2")
    clock.on_wait = lambda _sim: harness.client.post(f"/api/profile-analyses/{analysis['id']}/cancel")
    runner.run_next()
    files = report_files(harness, analysis["id"])
    rows = {row[0]: row for row in csv_rows(files[f"{STEM}.csv"])[1:]}
    assert rows["a1"][8] == "Ativa"
    assert rows["a2"] == ["a2", "", *["Não consultado"] * 6, "Cancelado", "", "", ""]
    assert sheet_rows(files[f"{STEM}.xlsx"])[2][8] == "Cancelado"
    cells = html_cells(files[f"{STEM}.html"].decode("utf-8"), "a2")
    assert cells[1:] == ['<span class="pill">Não consultado</span>', '<span class="pill">Cancelado</span>',
                         '<span class="pill">Não consultado</span>']
    page = files[f"{STEM}.html"].decode("utf-8")
    assert '<td colspan="4"><span class="pill">Não consultado</span></td>' in page
    assert '<td colspan="2"><span class="pill">Não consultado</span></td>' in page
    assert "<td></td>" not in page and '<td class="nowrap"></td>' not in page


def test_report_uses_server_timezone(harness_factory):
    harness = harness_factory(timezone="America/Sao_Paulo")
    analysis_id = finished(harness, ("a1",), {})
    files = report_files(harness, analysis_id)
    rows = csv_rows(files[f"{STEM}.csv"])
    assert rows[1][9] == "01/11/2026" and rows[1][10] == "09:00:01"


def test_file_date_is_local_start_date():
    zone = export_time.zone_from_tzinfo(timezone(timedelta(hours=-3)))
    analysis = {"started_at": "2026-11-01T01:00:00.000Z", "created_at": "2026-11-01T00:00:00.000Z", "total": 0, "id": 1,
                "status": "completed", "finished_at": None, "error": None, "invalid_count": 0, "duplicate_count": 0,
                "invalid_lines": [], "summary": dict.fromkeys(
                    ("active", "private", "restricted", "unavailable", "unverified", "cancelled", "stories_yes", "highlights_yes"),
                    0)}
    assert profile_report.write_report(io.BytesIO(), analysis, lambda: [], zone) == "relatorio-perfis-2026-10-31.zip"


# ---- HTML ---------------------------------------------------------------------------------------------------------
def test_html_is_self_contained_and_escaped(full_report):
    _harness, _id, files = full_report
    page = files[f"{STEM}.html"].decode("utf-8")
    lowered = page.lower()
    assert "<script" not in lowered and "<link" not in lowered and "<img" not in lowered
    assert "src=" not in lowered and "@import" not in lowered and "url(" not in lowered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&amp;" in page
    assert '<meta charset="utf-8">' in page and "@media print" in page and "<style>" in page
    assert '<a href="https://www.instagram.com/ativa1/"' in page


def test_html_header_cards_and_note(full_report):
    _harness, analysis_id, files = full_report
    page = files[f"{STEM}.html"].decode("utf-8")
    assert f"Análise nº {analysis_id}" in page and "Status: Concluída" in page
    found = re.findall(r'<div class="card"><b>(.*?)</b><span>(.*?)</span>', page)
    cards = {label: value for value, label in found}
    assert cards == {"Total": "6", "Ativas": "3", "Privadas": "1", "Restritas": "0", "Indisponíveis": "1", "Não verificadas": "1",
                     "Não consultados": "0", "Com Stories": "3", "Com Destaques": "0"}
    assert "não o total da vida da conta" in page and "Ativa não significa" in page
    assert "<b>≈</b> marca um valor de Posts arredondado pelo Instagram" in page
    assert "—" not in page and "Fim: 01/11/2026" in page


def test_html_cards_add_up_to_the_total(harness):
    clock = SimClock()
    results = {"a1": [snapshot("a1")], "p2": [snapshot("p2", "private")], "u3": [snapshot("u3", "unavailable")],
               "v4": [snapshot("v4", "unverified")]}
    runner = install_runner(harness, ScriptedFetcher(clock, results), clock)
    analysis = create(harness, *results, "n5", "n6")

    def cancel(username):
        if username == "v4":
            harness.client.post(f"/api/profile-analyses/{analysis['id']}/cancel")

    runner.ctx.profile_fetcher.before = cancel
    runner.run_next()
    page = report_files(harness, analysis["id"])[f"{STEM}.html"].decode("utf-8")
    found = re.findall(r'<div class="card"><b>(.*?)</b><span>(.*?)</span>', page)
    cards = {label: int(value) for value, label in found}
    assert cards["Não consultados"] == 2 and cards["Total"] == 6
    assert sum(cards[label] for label in ("Ativas", "Privadas", "Indisponíveis", "Não verificadas",
                                          "Não consultados")) == cards["Total"]


def test_unavailable_profile_is_not_verified_in_all_three_with_real_date(full_report):
    _harness, _id, files = full_report
    sheet = {row[0]: row for row in sheet_rows(files[f"{STEM}.xlsx"])[1:]}
    assert sheet["@sumiu3"][2:8] == ["Não verificado"] * 6 and sheet["@sumiu3"][8] == "Indisponível"
    assert sheet["@sumiu3"][9] == "01/11/2026" and sheet["@sumiu3"][10]
    cells = html_cells(files[f"{STEM}.html"].decode("utf-8"), "sumiu3")
    assert all("Não verificado" in cell for cell in cells[1:5]) and "Indisponível" in cells[5]
    assert cells[6] == "01/11/2026" and cells[7]


def test_state_text_uses_the_same_class_in_every_column(full_report):
    _harness, _id, files = full_report
    page = files[f"{STEM}.html"].decode("utf-8")
    assert html_cells(page, "semreels5")[2] == '<span class="pill">Sem Reels</span>'
    assert html_cells(page, "semposts6")[1] == '<span class="pill warn">Não verificado</span>'
    assert html_cells(page, "duvida4")[3] == '<span class="pill warn">Não verificado</span>'


def test_html_layout_is_fixed_with_colgroup(full_report):
    _harness, _id, files = full_report
    page = files[f"{STEM}.html"].decode("utf-8")
    assert "table-layout:fixed" in page
    assert page.count("<col ") == len(HTML_COLUMNS) and "<colgroup>" in page
    assert sum(profile_report.HTML_WIDTHS) == 100


def test_html_without_finish_has_no_empty_header():
    zone = export_time.zone_from_tzinfo(timezone(timedelta(hours=-3)))
    summary = dict.fromkeys(profile_analysis_service.SUMMARY_KEYS, 0)
    analysis = {"started_at": "2026-11-01T01:00:00.000Z", "created_at": "2026-11-01T00:00:00.000Z", "total": 0, "id": 1,
                "status": "cancelled", "finished_at": None, "error": None, "invalid_count": 0, "duplicate_count": 0,
                "invalid_lines": [], "summary": summary}
    text = io.StringIO()
    profile_report.write_html(text, analysis, [], zone)
    assert "Fim:" not in text.getvalue() and "Início: 31/10/2026 22:00:00 · Status: Cancelada" in text.getvalue()


def test_html_shows_the_main_columns_with_details_inside_the_cells(full_report):
    _harness, _id, files = full_report
    page = files[f"{STEM}.html"].decode("utf-8")
    assert ">@ativa1</a>" in page and "≈ 33K" in page and "<small>2 Reels</small>" in page


def test_report_refused_when_analysis_is_resumed_while_it_is_written(harness, monkeypatch):
    clock = SimClock()
    runner = install_runner(harness, ScriptedFetcher(clock), clock)
    created = create(harness, "a1", "a2")
    harness.client.post(f"/api/profile-analyses/{created['id']}/cancel")
    url = f"/api/profile-analyses/{created['id']}"
    real = profile_report.write_report

    def resume_meanwhile(*args, **kwargs):
        name = real(*args, **kwargs)
        assert harness.ctx.profile_analyses.resume_analysis(created["id"])
        return name

    monkeypatch.setattr(profile_report, "write_report", resume_meanwhile)
    response = harness.client.get(f"{url}/report")
    assert response.status_code == 409 and response.json()["code"] == "analysis_running"
    del runner


def test_html_lists_ignored_lines_escaped(harness):
    clock = SimClock()
    runner = install_runner(harness, ScriptedFetcher(clock), clock)
    response = harness.client.post("/api/profile-analyses", json={"text": "a1\na1\n<b>ruim</b>"})
    runner.run_next()
    page = report_files(harness, response.json()["id"])[f"{STEM}.html"].decode("utf-8")
    assert "Linhas ignoradas" in page and "&lt;b&gt;ruim&lt;/b&gt;" in page and "<b>ruim" not in page
    assert "1 linha inválida e 1 linha duplicada." in page and "primeiras" not in page


def test_html_says_when_ignored_lines_are_a_sample(harness):
    clock = SimClock()
    runner = install_runner(harness, ScriptedFetcher(clock), clock)
    text = "a1\na1\na1\n" + "\n".join(f"#ruim{i}" for i in range(60))
    response = harness.client.post("/api/profile-analyses", json={"text": text})
    runner.run_next()
    page = report_files(harness, response.json()["id"])[f"{STEM}.html"].decode("utf-8")
    assert "60 linhas inválidas e 2 linhas duplicadas." in page
    assert "só as 50 primeiras inválidas" in page and page.count("<li>") == 50


def test_interrupted_analysis_shows_error_in_report(harness):
    limited = InstagramWebError("rate_limited", "x")
    analysis_id = finished(harness, ("a1", "a2"), {"a1": [limited]})
    files = report_files(harness, analysis_id)
    page = files[f"{STEM}.html"].decode("utf-8")
    assert "Status: Interrompida" in page
    for name in ("a1", "a2"):
        cells = html_cells(page, name)
        assert "seguiu limitando as consultas" in cells[0]              # o motivo está na linha do perfil
        assert cells[1:] == ['<span class="pill">Não consultado</span>'] * 3   # colspan 4, Status, colspan 2
    rows = {row[0]: row for row in csv_rows(files[f"{STEM}.csv"])[1:]}
    for name in ("a1", "a2"):
        row = rows[name]
        assert row[:8] == [name, "", *["Não consultado"] * 6] and row[8] == "Não consultado"
        assert row[9:11] == ["", ""] and "Não consultado" in row[11]
    assert "Cancelado" not in page and "—" not in page
    assert sheet_rows(files[f"{STEM}.xlsx"])[1][8] == "Não consultado"


def test_breaker_interruption_is_not_consulted_in_report(harness):
    refused = InstagramWebError("access_refused", "Recusado.")
    names = tuple(f"p{i}" for i in range(10))
    analysis_id = finished(harness, names, {name: [refused] for name in names})
    rows = csv_rows(report_files(harness, analysis_id)[f"{STEM}.csv"])[1:]
    assert rows[0][8] == "Não verificado" and rows[0][10]         # consultado: tem horário real
    assert rows[-1][8] == "Não consultado" and rows[-1][9:11] == ["", ""]
    assert "Não" not in rows[-1]
