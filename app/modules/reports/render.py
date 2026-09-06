"""Excel and PDF export for a report. Reuses the tool `permits/render.py`
uses (WeasyPrint), never a second PDF library — but not that module's own
PDF/A hardening (plan "scope cuts"): a report export is an administrative
document a human reviews, not the once-rendered legal instrument four ERI
signatures are taken over. What is legally attested is `report.data`
(`service._report_bytes`), not these bytes — a re-export next week may
render slightly differently (a library upgrade, a font substitution) without
invalidating anything, which is the whole point of the decoupling.

`openpyxl` is a new dependency this branch adds (`pyproject.toml`) — a
`.xlsx` writer, not a PDF-adjacent library.
"""

import html
import io
import os
import sys
from typing import Any

from openpyxl import Workbook

from app.modules.reports.models import Report, ReportForm

# The exact five-line fix `permits/render.py` carries, duplicated rather than
# imported: WeasyPrint loads Pango/GLib/HarfBuzz through dlopen by leaf name,
# which fails on macOS unless DYLD_FALLBACK_LIBRARY_PATH is set before
# `import weasyprint` — and cross-module, non-service imports are not allowed
# here, so two independently-imported modules doing `import weasyprint` each
# need their own copy of this guard. setdefault, so an operator's own value
# wins; no effect on Linux, in CI or in the container.
if sys.platform == "darwin":  # pragma: no cover - a developer-machine path
    os.environ.setdefault("DYLD_FALLBACK_LIBRARY_PATH", "/opt/homebrew/lib:/usr/local/lib:/usr/lib")

from weasyprint import HTML  # noqa: E402 - must follow the dyld fix-up above


def _label(column: dict[str, Any]) -> str:
    label = column.get("label") or {}
    # ru first (an internal administrative export, plan "scope cuts"'s own
    # audience), then whichever Uzbek script the column actually has — uz_latn
    # is the one `LocalizedName` guarantees since decision #90, uz_cyrl is not.
    return str(
        label.get("ru") or label.get("uz_cyrl") or label.get("uz_latn") or column.get("code", "")
    )


def render_excel(report: Report, form: ReportForm) -> bytes:
    """One sheet: a header row of column labels, one row per `report.data["rows"]`
    entry, in the form's own column order. A row missing a column (an older
    generation, before a column existed) prints an empty cell rather than
    raising — an export must not fail on a report a human can still open in
    the API."""
    columns = form.columns
    workbook = Workbook()
    sheet = workbook.active
    # A freshly-constructed Workbook() always has one active sheet — openpyxl's
    # own stubs type `.active` as `Worksheet | None` for the general case (a
    # workbook loaded with none), which does not apply here.
    assert sheet is not None
    sheet.title = form.code[:31]  # Excel's own sheet-name length cap
    sheet.append([_label(column) for column in columns])
    for row in report.data.get("rows", []):
        sheet.append([row.get(column["code"], "") for column in columns])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def render_pdf(report: Report, form: ReportForm) -> bytes:
    """A plain HTML table -> PDF, default fonts, no PDF/A. Landscape-shaped
    by CSS alone (`@page`) — 2-ilova's 28 columns need the width."""
    columns = form.columns
    header_cells = "".join(f"<th>{html.escape(_label(column))}</th>" for column in columns)
    body_rows: list[str] = []
    for row in report.data.get("rows", []):
        cells = "".join(
            f"<td>{html.escape(str(row.get(column['code'], '') or ''))}</td>" for column in columns
        )
        body_rows.append(f"<tr>{cells}</tr>")

    document = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
@page {{ size: A3 landscape; margin: 10mm; }}
body {{ font-family: sans-serif; }}
table {{ border-collapse: collapse; width: 100%; font-size: 7px; }}
th, td {{ border: 1px solid #333; padding: 2px 3px; text-align: left; }}
th {{ background: #eee; }}
</style></head>
<body><table><thead><tr>{header_cells}</tr></thead><tbody>{"".join(body_rows)}</tbody></table></body>
</html>"""

    pdf = HTML(string=document).write_pdf()
    assert pdf is not None
    return pdf
