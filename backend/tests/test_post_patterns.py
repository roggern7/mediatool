"""Padrões de publicação por conta (app.services.post_patterns): métricas, narrativa, fuso e ZIP."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from xml.etree import ElementTree

import pytest

from app.services import xlsx_writer
from app.services.export_time import utc_zone, zone_from_tzinfo
from app.services.post_patterns import (
    POST_COLUMNS,
    SEGMENT_COLUMNS,
    PostTime,
    analyze_account,
    build_report_zip,
    format_duration,
    narrative,
    post_rows,
    report_filename,
    segment_rows,
)

MINUS_3 = zone_from_tzinfo(timezone(timedelta(hours=-3)))
START = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)   # 08:00 em UTC-3 (terça-feira)


def series(username: str, gaps: list[float], *, start: datetime = START, kind: str | None = "reel",
           prefix: str = "C") -> list[PostTime]:
    """Posts com os intervalos dados (minutos), a partir de `start`."""
    moments = [start]
    for gap in gaps:
        moments.append(moments[-1] + timedelta(minutes=gap))
    return [PostTime(username, f"{prefix}{i}", m.strftime("%Y-%m-%dT%H:%M:%SZ"), kind, False)
            for i, m in enumerate(moments)]


def test_intervals_mean_median_and_duration_text():
    account = analyze_account("a", series("a", [60, 120, 30, 90]), utc_zone)
    assert account.intervals == [60, 120, 30, 90]
    assert account.mean == 75 and account.median == 75
    assert (account.minimum, account.maximum) == (30, 120)
    assert account.days == 1 and account.posts_per_day == 5
    assert "Posta em média a cada 1 h 15 min (mediana 1 h 15 min)." in account.patterns
    assert [format_duration(m) for m in (5, 72, 120, 3060, 0.2)] == [
        "5 min", "1 h 12 min", "2 h", "2 dias 3 h", "menos de 1 min"]


def test_narrative_steady_burst_and_pause():
    # 5 posts a cada 72 min, depois um de 5 min (rajada), pausa de 19 h 30 min, mais 2 posts.
    account = analyze_account("a", series("a", [72, 72, 72, 72, 5, 19 * 60 + 30, 72]), MINUS_3)
    lines = narrative(account)
    assert lines[0] == "03/11 08:00 – 1 post a cada ~1 h 12 min (5 posts, 4 intervalos)"
    assert lines[1] == "03/11 12:48 – depois 2 posts com 5 min de intervalo (rajada)"
    assert lines[2] == "03/11 12:53 – depois pausa de 19 h 30 min"
    assert lines[3] == "04/11 08:23 – depois 2 posts com 1 h 12 min de intervalo"
    assert lines[-1] == "04/11 09:35 – último post analisado"
    assert account.bursts == [2] and len(account.pauses) == 1
    assert "Rajadas: 1 vez postou 2 posts em até 15 min." in account.patterns


def test_pinned_older_than_the_sequence_stays_outside():
    posts = series("a", [60, 60, 60], start=START)
    old_pinned = PostTime("a", "OLD", "2025-01-01T10:00:00Z", "photo", True)
    inside_pinned = PostTime("a", "IN", (START + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), "reel", True)
    account = analyze_account("a", [old_pinned, inside_pinned, *posts, posts[0]], utc_zone)
    assert [item.post.code for item in account.pinned_outside] == ["OLD"]
    assert [item.post.code for item in account.sequence] == ["C0", "IN", "C1", "C2", "C3"]
    assert account.intervals == [30, 30, 60, 60]
    assert account.duplicates == 1


def test_local_time_differs_from_utc():
    posts = [PostTime("a", "X", "2026-11-04T01:30:00Z", None, None),
             PostTime("a", "Y", "2026-11-04T02:30:00+00:00", None, None)]
    local = analyze_account("a", posts, MINUS_3)
    assert local.sequence[0].local == datetime(2026, 11, 3, 22, 30)
    assert local.hours[22] == 1 and local.hours[23] == 1 and local.weekdays[1] == 2   # terça
    assert analyze_account("a", posts, utc_zone).hours[1] == 1


@pytest.mark.parametrize("count", [0, 1])
def test_account_with_zero_or_one_post(count):
    posts = series("a", [])[:count] + [PostTime("a", "BAD", "lixo", None, None)]
    account = analyze_account("a", posts, utc_zone)
    assert len(account.undated) == 1 and account.intervals == [] and account.mean is None
    assert account.patterns[0].startswith("Não há intervalos suficientes")
    assert len(narrative(account)) == count


NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
      "ct": "http://schemas.openxmlformats.org/package/2006/content-types"}


def read_zip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def read_csv(data: bytes) -> list[list[str]]:
    assert data.startswith(b"\xef\xbb\xbf") and b"\r\n" in data
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig")), delimiter=";"))


def read_xlsx(data: bytes) -> dict[str, list[list]]:
    """Abas -> linhas (texto inline como str, número como float), conferindo a estrutura do pacote."""
    parts = read_zip(data)
    workbook = ElementTree.fromstring(parts["xl/workbook.xml"])
    rels = ElementTree.fromstring(parts["xl/_rels/workbook.xml.rels"])
    types = ElementTree.fromstring(parts["[Content_Types].xml"])
    targets = {rel.get("Id"): rel.get("Target") for rel in rels.findall("rel:Relationship", NS)}
    overrides = {o.get("PartName") for o in types.findall("ct:Override", NS) if o.get("ContentType").endswith(
        "worksheet+xml")}
    sheets = workbook.findall("m:sheets/m:sheet", NS)
    assert len(overrides) == len(sheets) == len(workbook.findall("m:definedNames/m:definedName", NS))
    assert sum(target == "styles.xml" for target in targets.values()) == 1
    result = {}
    for sheet in sheets:
        name = sheet.get("name")
        assert len(name) <= 31 and not set(name) & set("[]:*?/\\")
        target = "xl/" + targets[sheet.get(f"{{{NS['r']}}}id")]
        assert "/" + target in overrides and target in parts
        root = ElementTree.fromstring(parts[target])
        assert root.find("m:sheetViews/m:sheetView/m:pane", NS).get("state") == "frozen"
        assert root.find("m:autoFilter", NS) is not None and root.find(".//m:f", NS) is None
        rows = []
        for number, row in enumerate(root.find("m:sheetData", NS)):
            if number == 0:
                assert {cell.get("s") for cell in row} == {"1"}   # cabeçalho em negrito
            values = []
            for cell in row:
                text, value = cell.find("m:is/m:t", NS), cell.find("m:v", NS)
                values.append(text.text if text is not None else float(value.text) if value is not None else None)
            rows.append(values)
        result[name] = rows
    return result


def test_zip_one_profile_has_only_its_folder():
    files = read_zip(build_report_zip(series("a.b", [72, 5, 30]), title="Teste", zone=MINUS_3))
    assert sorted(files) == ["a_b/a_b.xlsx", "a_b/padroes.txt", "a_b/posts.csv", "a_b/sequencia.csv"]
    text = files["a_b/padroes.txt"].decode("utf-8")
    assert text.startswith("Padrões de publicação – @a.b\n") and "Fuso: UTC-03:00" in text and "Rajada:" in text
    assert "=== @a.b ===" in text and "Comparativo" not in text
    posts = read_csv(files["a_b/posts.csv"])
    assert tuple(posts[0]) == POST_COLUMNS
    assert posts[1][:4] == ["1", "03/11/2026", "08:00", "terça"]
    assert posts[2][4:8] == ["72,0", "1 h 12 min", "1", "Sim"] and posts[2][-1] == "https://www.instagram.com/p/C1/"
    assert tuple(read_csv(files["a_b/sequencia.csv"])[0]) == SEGMENT_COLUMNS
    sheets = read_xlsx(files["a_b/a_b.xlsx"])
    assert list(sheets) == ["Resumo", "Sequência", "Posts"]
    assert sheets["Resumo"][0] == ["Item", "Valor"] and sheets["Resumo"][1] == ["Posts analisados", 4.0]
    assert ["Padrões identificados", None] in sheets["Resumo"] and ["09h", 3.0] in sheets["Resumo"]
    assert tuple(sheets["Sequência"][0]) == SEGMENT_COLUMNS and tuple(sheets["Posts"][0]) == POST_COLUMNS
    assert sheets["Posts"][2][4] == 72.0 and sheets["Posts"][2][8] == "Reel"
    assert len(sheets["Posts"]) == len(posts) == 5


def test_two_profiles_get_a_comparison_and_never_mix():
    posts = series("rapida", [5, 5, 5, 5]) + series("lenta", [180, 200, 190], prefix="L")
    files = read_zip(build_report_zip(posts, title="Duas", zone=MINUS_3))
    assert {name.split("/")[0] for name in files} == {"comparativo.xlsx", "comparativo.txt", "rapida", "lenta"}
    assert len([name for name in files if "/" in name]) == 8
    comparison = files["comparativo.txt"].decode("utf-8")
    assert "=== Comparativo ===" in comparison and "Quem posta mais: @rapida" in comparison
    assert "=== @rapida ===" not in comparison
    table = read_xlsx(files["comparativo.xlsx"])["Comparativo"]
    assert [row[0] for row in table] == ["Perfil", "@rapida", "@lenta"] and table[1][1] == 5.0
    for own, other in (("rapida", "/p/L"), ("lenta", "/p/C")):
        for name in (f"{own}/{own}.xlsx", f"{own}/posts.csv", f"{own}/sequencia.csv", f"{own}/padroes.txt"):
            data = files[name] if not name.endswith(".xlsx") else b"".join(read_zip(files[name]).values())
            assert other.encode() not in data, name
        text = files[f"{own}/padroes.txt"].decode("utf-8")
        assert f"=== @{own} ===" in text and text.count("=== @") == 1
    assert len(read_csv(files["rapida/posts.csv"])) == 6 and len(read_csv(files["lenta/posts.csv"])) == 5


def test_sheet_segments_match_the_narrative():
    account = analyze_account("a", series("a", [72, 72, 72, 72, 5, 19 * 60 + 30, 72]), MINUS_3)
    segments, posts = segment_rows(account), post_rows(account)
    assert [row[7] for row in segments] == ["Ritmo", "Rajada", "Pausa", "Ritmo"]
    story = narrative(account)
    for line, row in zip(story, segments, strict=False):   # a narrativa tem uma linha a mais ("último post")
        assert line.startswith(f"{row[1][:5]} {row[1][-5:]}") and line.endswith(row[8])
    assert [row[6] for row in posts] == [1, 1, 1, 1, 1, 2, 3, 4]
    assert [row[7] for row in posts] == ["Não"] * 4 + ["Sim", "Sim", "Não", "Não"]
    for row in segments:   # o intervalo médio do trecho é a média dos intervalos dos posts com aquele Trecho
        values = [post[4] for post in posts if post[6] == row[0] and post[4] is not None]
        assert len(values) == row[4] and round(sum(values) / len(values), 1) == row[5]


def test_multi_sheet_writer_validates_names_and_keeps_text_inline():
    target = io.BytesIO()
    xlsx_writer.write_xlsx_sheets(target, [xlsx_writer.Sheet("Um", ["A", "B"], [["=1+1", 2]], [10, 10]),
                                           xlsx_writer.Sheet("Dois", ["C"], [], [10])])
    sheets = read_xlsx(target.getvalue())
    assert sheets == {"Um": [["A", "B"], ["=1+1", 2.0]], "Dois": [["C"]]}
    for bad in ("x" * 32, "a/b", "", "Um"):
        with pytest.raises(ValueError):
            xlsx_writer.write_xlsx_sheets(io.BytesIO(), [xlsx_writer.Sheet("Um", ["A"], [], [10]),
                                                         xlsx_writer.Sheet(bad, ["A"], [], [10])])


def test_estimated_reels_tab_times_are_marked_in_xlsx_csv_and_text():
    posts = series("a", [60, 60])
    posts[1] = PostTime("a", posts[1].code, posts[1].taken_at, "reel", False, source="reels_tab")
    files = read_zip(build_report_zip(posts, title="Teste", zone=MINUS_3))
    sheets = read_xlsx(files["a/a.xlsx"])
    header = sheets["Posts"][0]
    origin = header.index("Origem do horário")
    assert header[-1] == "Link" and [row[origin] for row in sheets["Posts"][1:]] == [
        "Publicação", "Estimado (aba Reels)", "Publicação"]
    assert ["Horários estimados pela aba Reels", "1 (precisão de ~±30 s)"] in sheets["Resumo"]
    assert [row[origin] for row in read_csv(files["a/posts.csv"])[1:]] == [
        "Publicação", "Estimado (aba Reels)", "Publicação"]
    assert "Horários estimados pela aba Reels: 1 (precisão de ~±30 s)" in files["a/padroes.txt"].decode("utf-8")
    plain = read_zip(build_report_zip(series("b", [60]), title="Teste", zone=MINUS_3))
    assert "Horários estimados" not in plain["b/padroes.txt"].decode("utf-8")


def test_report_filename():
    assert report_filename("Meu: teste/1", datetime(2026, 11, 4, 9, 5)) == "padroes_Meu_teste_1_2026-11-04_0905.zip"
