"""Planilha XLSX mínima em stdlib (`zipfile` + XML escapado): o `openpyxl` não faz parte do projeto.

Uma planilha (`write_xlsx`) ou várias abas (`write_xlsx_sheets`): cabeçalho em negrito com preenchimento, painel
congelado, autofiltro, larguras, números como número e strings inline escapadas. Texto de fora (ex.: nome de exibição
do Instagram) passa pelo filtro de caracteres inválidos do XML 1.0, e nunca vira fórmula: toda string é inline. A
planilha é escrita linha a linha, direto na entrada do ZIP.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from xml.sax.saxutils import escape

MAX_CELL_CHARS = 32_767         # limite do Excel
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_INVALID_XML_RE = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES = (
    f'{_XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml"'
    ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml"'
    ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml"'
    ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    f'{_XML}<Relationships xmlns="{_PKG_REL_NS}"><Relationship Id="rId1"'
    f' Type="{_REL_NS}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
)
_WORKBOOK_RELS = (
    f'{_XML}<Relationships xmlns="{_PKG_REL_NS}">'
    f'<Relationship Id="rId1" Type="{_REL_NS}/worksheet" Target="worksheets/sheet1.xml"/>'
    f'<Relationship Id="rId2" Type="{_REL_NS}/styles" Target="styles.xml"/></Relationships>'
)
_STYLES = (
    f'{_XML}<styleSheet xmlns="{_MAIN_NS}">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFDDE3EA"/><bgColor indexed="64"/></patternFill></fill>'
    "</fills>"
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)
Cell = str | int | float | None


def clean_text(value: str) -> str:
    """Só caracteres válidos em XML 1.0, dentro do limite de uma célula."""
    return _INVALID_XML_RE.sub("", value)[:MAX_CELL_CHARS]


def column_name(index: int) -> str:
    """Índice base 0 -> letra da coluna (0 -> A, 26 -> AA)."""
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _cell(ref: str, value: Cell, style: int) -> str:
    attrs = f' r="{ref}"' + (f' s="{style}"' if style else "")
    if value is None:
        return f"<c{attrs}/>"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"<c{attrs}><v>{value}</v></c>"
    text = escape(clean_text(str(value)))
    return f'<c{attrs} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _row(number: int, letters: Sequence[str], values: Sequence[Cell], style: int = 0) -> bytes:
    cells = "".join(_cell(f"{letter}{number}", value, style) for letter, value in zip(letters, values, strict=True))
    return f'<row r="{number}">{cells}</row>'.encode()


def _sheet_head(last: str, widths: Sequence[float], selected: bool = True) -> bytes:
    cols = "".join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>'
                   for i, width in enumerate(widths, start=1))
    tab = ' tabSelected="1"' if selected else ""
    return (
        f'{_XML}<worksheet xmlns="{_MAIN_NS}"><dimension ref="A1:{last}"/>'
        f'<sheetViews><sheetView{tab} workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>'
        f'<sheetFormatPr defaultRowHeight="15"/><cols>{cols}</cols><sheetData>'
    ).encode()


def _absolute(ref: str) -> str:
    letters = ref.rstrip("0123456789")
    return f"${letters}${ref[len(letters):]}"


def _part(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def write_xlsx(target: str | Path | IO[bytes], headers: Sequence[str], rows: Iterable[Sequence[Cell]], *,
               row_count: int, widths: Sequence[float], sheet_name: str = "Planilha") -> None:
    """Pacote XLSX de uma planilha em `target`. `rows` é consumido uma vez, sem ficar em memória; `row_count` (quantas
    linhas ele traz) dá `dimension` e `autoFilter` antes dos dados. `widths`: largura de cada coluna."""
    letters = [column_name(index) for index in range(len(headers))]
    last = f"{letters[-1]}{row_count + 1}"
    name = escape(clean_text(sheet_name))
    workbook = (
        f'{_XML}<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}">'
        f'<sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets>'
        f'<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
        f"'{name}'!$A$1:{_absolute(last)}</definedName></definedNames></workbook>"
    )
    parts = {"[Content_Types].xml": _CONTENT_TYPES, "_rels/.rels": _ROOT_RELS, "xl/workbook.xml": workbook,
             "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS, "xl/styles.xml": _STYLES}
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for part, content in parts.items():
            archive.writestr(_part(part), content.encode("utf-8"))
        with archive.open(_part("xl/worksheets/sheet1.xml"), "w") as sheet:
            sheet.write(_sheet_head(last, widths))
            sheet.write(_row(1, letters, headers, 1))
            for number, row in enumerate(rows, start=2):
                sheet.write(_row(number, letters, row))
            sheet.write(f'</sheetData><autoFilter ref="A1:{last}"/></worksheet>'.encode())


@dataclass(frozen=True)
class Sheet:
    name: str                          # até 31 caracteres, sem []:*?/\
    headers: Sequence[str]
    rows: Sequence[Sequence[Cell]]
    widths: Sequence[float]


_INVALID_SHEET_CHARS = frozenset("[]:*?/\\")
_SHEET_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"


def write_xlsx_sheets(target: str | Path | IO[bytes], sheets: Sequence[Sheet]) -> None:
    """Pacote XLSX com várias planilhas (abas), na ordem dada; a primeira fica selecionada. Cada aba tem o mesmo
    formato da de `write_xlsx` (cabeçalho em negrito, painel congelado, autofiltro, larguras)."""
    names = [sheet.name for sheet in sheets]
    if not sheets or len({name.lower() for name in names}) != len(names) or any(
            not name or len(name) > 31 or _INVALID_SHEET_CHARS & set(name) for name in names):
        raise ValueError(f"nomes de aba inválidos: {names!r}")
    lasts = [f"{column_name(len(sheet.headers) - 1)}{len(sheet.rows) + 1}" for sheet in sheets]
    entries = "".join(f'<sheet name="{escape(sheet.name)}" sheetId="{i}" r:id="rId{i}"/>'
                      for i, sheet in enumerate(sheets, start=1))
    filters = "".join(f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
                      f"'{escape(sheet.name)}'!$A$1:{_absolute(last)}</definedName>"
                      for i, (sheet, last) in enumerate(zip(sheets, lasts, strict=True)))
    workbook = (f'{_XML}<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}"><sheets>{entries}</sheets>'
                f"<definedNames>{filters}</definedNames></workbook>")
    overrides = "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="{_SHEET_TYPE}"/>'
                        for i in range(1, len(sheets) + 1))
    content_types = _CONTENT_TYPES.replace(
        f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{_SHEET_TYPE}"/>', overrides)
    styles_id = len(sheets) + 1
    rels = "".join(f'<Relationship Id="rId{i}" Type="{_REL_NS}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                   for i in range(1, len(sheets) + 1))
    workbook_rels = (f'{_XML}<Relationships xmlns="{_PKG_REL_NS}">{rels}'
                     f'<Relationship Id="rId{styles_id}" Type="{_REL_NS}/styles" Target="styles.xml"/></Relationships>')
    parts = {"[Content_Types].xml": content_types, "_rels/.rels": _ROOT_RELS, "xl/workbook.xml": workbook,
             "xl/_rels/workbook.xml.rels": workbook_rels, "xl/styles.xml": _STYLES}
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for part, content in parts.items():
            archive.writestr(_part(part), content.encode("utf-8"))
        for number, (sheet, last) in enumerate(zip(sheets, lasts, strict=True), start=1):
            letters = [column_name(index) for index in range(len(sheet.headers))]
            with archive.open(_part(f"xl/worksheets/sheet{number}.xml"), "w") as part:
                part.write(_sheet_head(last, sheet.widths, selected=number == 1))
                part.write(_row(1, letters, sheet.headers, 1))
                for row_number, row in enumerate(sheet.rows, start=2):
                    part.write(_row(row_number, letters, row))
                part.write(f'</sheetData><autoFilter ref="A1:{last}"/></worksheet>'.encode())
