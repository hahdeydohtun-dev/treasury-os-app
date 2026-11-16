"""
Stage 5F: Excel and PDF exports of reconciliation reports.

An export is rendered from the SAME report dictionary the screen shows (built by reconciliation_report_service with the
caller's scope and filters), so it cannot differ from the screen and cannot be broader than the caller's authorization:
this module never queries the database and never sees a wider scope than the report builder applied.

Safety:
* Spreadsheet formula injection: every text cell that could be interpreted as a formula (leading = + - @ or a control
  character, also after leading whitespace) is prefixed with an apostrophe so Excel/LibreOffice show it as text.
* Bank account numbers arrive already masked; internal ids never reach this module's output (rows carry `id` only for
  on-screen drill-down and it is not exported).
* Amounts are written as exact decimals (numbers), per currency, exactly as reported.
"""
from __future__ import annotations

import io
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.sax.saxutils import escape

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

_FORMULA_START = re.compile(r"^[\s]*[=+\-@]|^[\t\r\n]")
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME = "application/pdf"


def neutralize(value: Any) -> Any:
    """Make a text value safe to place in a spreadsheet cell (formula injection defence)."""
    if isinstance(value, str) and _FORMULA_START.search(value):
        return "'" + value
    return value


def _cell(value: Any, numeric_column: bool) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, Decimal)):
        return value
    if numeric_column and isinstance(value, str) and _NUMERIC.match(value):
        try:
            return Decimal(value)
        except InvalidOperation:
            pass
    return neutralize(str(value))


def _info_rows(report: dict[str, Any]) -> list[tuple[str, str]]:
    period = report["reporting_period"]
    rows = [("Report", report["title"]), ("Generated at (UTC)", report["generated_at"]), ("Level", report["level"]),
            ("Reporting period", f"{period['from']} to {period['to']}" + ("" if period.get("applies") else " (not applied to this report)")),
            ("As-of date (ageing)", report["as_of"]), ("Timezone", report["timezone"]),
            ("Entities in scope", "; ".join(report["scope"]["entities"]) or "-"), ("Bank accounts", report["scope"]["accounts"]),
            ("Currency", report["scope"]["currency"])]
    rows += [(f"Filter: {k}", str(v)) for k, v in sorted(report["filters"].items()) if k not in {"date_from", "date_to", "as_of"}]
    return rows


def _unique_sheet_name(name: str, used: set[str]) -> str:
    base = _BAD_SHEET_CHARS.sub("-", name)[:28] or "Sheet"
    candidate, n = base, 2
    while candidate.lower() in used:
        candidate = f"{base[:25]}-{n}"
        n += 1
    used.add(candidate.lower())
    return candidate


def build_xlsx(report: dict[str, Any]) -> bytes:
    wb = openpyxl.Workbook()
    bold, head_fill = Font(bold=True), PatternFill("solid", fgColor="DDE6F2")
    used: set[str] = set()
    ws = wb.active
    ws.title = _unique_sheet_name("Report Info", used)
    ws["A1"] = neutralize(report["title"]); ws["A1"].font = Font(bold=True, size=14)
    for i, (k, v) in enumerate(_info_rows(report), start=3):
        ws.cell(row=i, column=1, value=neutralize(k)).font = bold
        ws.cell(row=i, column=2, value=neutralize(v))
    ws.column_dimensions["A"].width = 26; ws.column_dimensions["B"].width = 90

    if report["kpis"]:
        kpi_ws = wb.create_sheet(_unique_sheet_name("KPIs", used))
        for c, h in enumerate(("Indicator", "Value", "Definition", "Source"), start=1):
            cell = kpi_ws.cell(row=1, column=c, value=h); cell.font = bold; cell.fill = head_fill
        for r, item in enumerate(report["kpis"], start=2):
            kpi_ws.cell(row=r, column=1, value=neutralize(item["label"]))
            kpi_ws.cell(row=r, column=2, value=_cell(item["value"], True) if item["value"] is not None else "n/a")
            kpi_ws.cell(row=r, column=3, value=neutralize(item["definition"])); kpi_ws.cell(row=r, column=4, value=neutralize(item["source"]))
        for letter, width in zip("ABCD", (36, 16, 90, 50)):
            kpi_ws.column_dimensions[letter].width = width

    for t in report["tables"]:
        sheet = wb.create_sheet(_unique_sheet_name(t["title"], used))
        columns = t["columns"]
        for c, column in enumerate(columns, start=1):
            cell = sheet.cell(row=1, column=c, value=neutralize(column["label"])); cell.font = bold; cell.fill = head_fill
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        for r, row in enumerate(t["rows"], start=2):
            for c, column in enumerate(columns, start=1):
                value = _cell(row.get(column["key"]), column["align"] == "right")
                cell = sheet.cell(row=r, column=c, value=value)
                if isinstance(value, Decimal) and value == value.quantize(Decimal("0.01")) and "." in str(value):
                    cell.number_format = "#,##0.00"
        if t.get("note"):
            sheet.cell(row=len(t["rows"]) + 3, column=1, value=neutralize(t["note"]))
        for c, column in enumerate(columns, start=1):
            longest = max([len(str(column["label"]))] + [len(str(r.get(column["key"]) if r.get(column["key"]) is not None else "")) for r in t["rows"][:200]])
            sheet.column_dimensions[get_column_letter(c)].width = min(max(longest + 2, 10), 48)
        sheet.freeze_panes = "A2"

    d = wb.create_sheet(_unique_sheet_name("Definitions", used))
    d["A1"] = "Methodology and definitions"; d["A1"].font = bold
    for i, line in enumerate(report["definitions"] + report.get("notes", []), start=3):
        d.cell(row=i, column=1, value=neutralize(line)).alignment = Alignment(wrap_text=True, vertical="top")
    d.column_dimensions["A"].width = 150
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_pdf(report: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=7, leading=8.5)
    tiny = ParagraphStyle("tiny", parent=styles["BodyText"], fontSize=6, leading=7)
    tiny_right = ParagraphStyle("tiny_right", parent=tiny, alignment=2)
    head_style = ParagraphStyle("head", parent=tiny, fontName="Helvetica-Bold")
    page = landscape(A4)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=10 * mm, rightMargin=10 * mm, topMargin=12 * mm, bottomMargin=14 * mm,
                            title=report["title"], author="Treasury OS", subject="Reconciliation report")
    width = page[0] - 20 * mm

    def p(text: Any, style) -> Paragraph:
        return Paragraph(escape("-" if text is None else str(text)), style)

    def footer(canvas, document):
        canvas.saveState(); canvas.setFont("Helvetica", 7)
        canvas.drawString(10 * mm, 8 * mm, f"{report['title']} - generated {report['generated_at']} UTC - {report['level']}")
        canvas.drawRightString(page[0] - 10 * mm, 8 * mm, f"Page {document.page}")
        canvas.restoreState()

    story: list[Any] = [Paragraph(escape(report["title"]), styles["Title"])]
    info = [[p(k, head_style), p(v, small)] for k, v in _info_rows(report)]
    info_table = Table(info, colWidths=[width * 0.18, width * 0.82])
    info_table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [info_table, Spacer(1, 6)]
    if report["kpis"]:
        story.append(Paragraph("Key indicators", styles["Heading3"]))
        rows = [[p("Indicator", head_style), p("Value", head_style), p("Definition", head_style)]] + [
            [p(k["label"], small), p("n/a" if k["value"] is None else k["value"], small), p(k["definition"], small)] for k in report["kpis"]]
        kt = Table(rows, colWidths=[width * 0.25, width * 0.12, width * 0.63], repeatRows=1)
        kt.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE6F2")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [kt, Spacer(1, 8)]
    for t in report["tables"]:
        story.append(Paragraph(escape(t["title"]), styles["Heading3"]))
        cols = t["columns"]
        if not t["rows"]:
            story += [Paragraph("No records.", small), Spacer(1, 6)]
            continue
        data = [[p(c["label"], head_style) for c in cols]] + [
            [p(r.get(c["key"]), tiny_right if c["align"] == "right" else tiny) for c in cols] for r in t["rows"]]
        # Column widths follow the content (header and the first rows), within sane bounds, so short values such as
        # statuses never wrap mid-word while long text columns get the room they need.
        weights = [min(max(max([len(str(c["label"])) * 0.6] + [len("-" if r.get(c["key"]) is None else str(r.get(c["key"]))) for r in t["rows"][:200]]), 6), 30)
                   for c in cols]
        tb = Table(data, colWidths=[width * w / sum(weights) for w in weights], repeatRows=1)
        tb.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE6F2")),
                                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
        story.append(tb)
        if t.get("note"):
            story.append(Paragraph(escape(t["note"]), small))
        story.append(Spacer(1, 8))
    story.append(Paragraph("Methodology and definitions", styles["Heading3"]))
    story += [Paragraph("- " + escape(line), small) for line in report["definitions"] + report.get("notes", [])]
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def export_filename(report: dict[str, Any], extension: str) -> str:
    period = report["reporting_period"]
    return f"reconciliation_{report['report'].replace('-', '_')}_{period['from']}_{period['to']}.{extension}"
