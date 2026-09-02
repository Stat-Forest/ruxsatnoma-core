"""The permit document renderer: a frozen snapshot plus an HTML layout become PDF/A
bytes with the QR embedded. Pure, synchronous, no session (plan 03.11a ruling 2).

Three properties this module exists to guarantee, each one load-bearing:

**Deterministic.** Task 3 freezes `sha256(pdf_bytes)` as the permit's document identity
and all four ERI signatures are taken over exactly those bytes (ruling 3), so two renders
of one snapshot must be byte-identical or that hash is a coin flip. WeasyPrint's PDF/A
path writes no wall-clock timestamp of its own — unlike its PDF/X path, which calls
`localtime()` — and pydyf derives the trailer `/ID` from an MD5 of the object data, so the
only clock this renderer can see is the one we hand it: `created`/`modified` are pinned to
the snapshot's own `issued_at`. No environment variable is set and no global state is
touched; `SOURCE_DATE_EPOCH` is not read by WeasyPrint 69 at all.

**Not a template engine.** A layout is an admin-editable `permit_templates` row. Rendering
it with `str.format`, an f-string or Jinja would make that row a remote-code-execution
surface, so substitution is the whitelist regex `notifications.service.render` already
uses, widened to `{{ field }}` and — because this output is HTML, not plain text — with
every value HTML-escaped. A placeholder with no value is refused (`ERR-VAL-001`), never
left blank: on a legal document an unfilled field is a defect that must fail at issuance
rather than reach a citizen.

**Offline.** The font ships in `assets/` (PDF/A requires every face embedded, and the
container image's font set is not ours to depend on) and the QR is a `data:` URI, so the
PDF renders on an inspector's device with no network. `_AssetFetcher` enforces that from
the other side: a layout may reference the bundled assets and `data:` URIs and nothing
else, so an admin-authored layout cannot make the server fetch a URL or read a file.
"""

import base64
import io
import re
from collections.abc import Mapping
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

import segno
from weasyprint import CSS, HTML
from weasyprint.text.fonts import FontConfiguration
from weasyprint.urls import URLFetcher

from app.core.errors import err

ASSETS_DIR = Path(__file__).parent / "assets"
_DEFAULT_LAYOUT_PATH = ASSETS_DIR / "default_layout.html"

# PDF/A-3b: the archival variant WeasyPrint writes the sRGB output intent and the
# pdfaid XMP schema for (plan 03.11a ruling 2). -3 rather than -1/-2 because it is the
# level that permits an embedded attachment, which 3.11b's signed-package export will
# want; "b" is visual conformance, which is what an unstructured legal form can
# honestly claim.
PDF_VARIANT = "pdf/a-3b"

# Compression off, deliberately, at roughly 3x the file size (22 KB -> 66 KB for the
# bundled layout). At PDF 1.7 pydyf packs the catalogue into a /Type /ObjStm and Flate-
# compresses the XMP stream, so a stored permit's own conformance declaration — the
# /OutputIntent and the pdfaid schema — becomes unreadable without a zlib pass. These
# are the bytes four ERI signatures are taken over and the bytes an archive keeps: what
# they assert about themselves should be legible in them. It also takes zlib out of the
# reproduction path for everything except the QR image, which arrives already deflated
# inside its PNG. The tests assert exactly these two markers.
UNCOMPRESSED_PDF = True

# QR: error correction M (15%) survives a folded, stamped, photographed paper permit;
# a 4-module quiet zone is what ISO/IEC 18004 requires for a scanner to lock on, and
# scale 8 puts a ~33-module symbol at ~300 px, which is ~270 dpi at the 28 mm the
# layout prints it. All three are pinned so the PNG cannot drift between releases.
QR_ERROR_LEVEL = "m"
QR_BORDER = 4
QR_SCALE = 8

# `{{ field }}`, tolerating any whitespace inside the braces. Deliberately narrower
# than str.format: an admin-authored layout must not be able to reach attributes
# ({{ x.__class__ }}), indexes or anything else Python can evaluate.
_PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

# The field the renderer fills itself rather than reading from the snapshot.
QR_FIELD = "qr"

_FONT_FAMILY = "Permit Serif"


def _font_css() -> str:
    """`@font-face` for the bundled family, as absolute `file://` URLs.

    Applied to every layout — bundled or uploaded — rather than left to the layout's
    own CSS: an administrator editing the form must not be able to lose the embedded
    face, which is the one thing standing between a PDF/A and blank boxes where
    `Бурчмулла ўрмон хўжалиги` should be.
    """
    regular = (ASSETS_DIR / "DejaVuSerif.ttf").as_uri()
    bold = (ASSETS_DIR / "DejaVuSerif-Bold.ttf").as_uri()
    return f"""
    @font-face {{
        font-family: "{_FONT_FAMILY}";
        font-weight: normal;
        font-style: normal;
        src: url("{regular}") format("truetype");
    }}
    @font-face {{
        font-family: "{_FONT_FAMILY}";
        font-weight: bold;
        font-style: normal;
        src: url("{bold}") format("truetype");
    }}
    html {{ font-family: "{_FONT_FAMILY}", serif; }}
    """


class _AssetFetcher(URLFetcher):
    """Serves the bundled assets and `data:` URIs; refuses every other URL.

    The layout comes from a database row an administrator can edit, so without this
    a `<img src="http://…">` would be a server-side request and a `file:///etc/…`
    a local file read, both on the issuing server's behalf. WeasyPrint catches a
    fetcher's exception, logs it and renders the page without the resource, so the
    refusal is fail-closed: the request is never made.
    """

    def __init__(self) -> None:
        super().__init__(allowed_protocols=("data", "file"), allow_redirects=False)

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> Any:
        # A URL scheme is case-insensitive; `DATA:` in an admin layout is a legitimate
        # inline image, and refusing it would silently drop a picture from a permit.
        if not url.lower().startswith("data:"):
            try:
                path = Path(url.removeprefix("file://").split("?")[0]).resolve()
                path.relative_to(ASSETS_DIR.resolve())
            except OSError, ValueError:
                raise err(
                    "ERR-VAL-001",
                    details={"reason": "layout_external_resource", "scheme": url[:16]},
                ) from None
        return super().fetch(url, headers)


def qr_png_data_uri(payload: str) -> str:
    """The QR as a `data:image/png;base64,…` URI — embedded, never linked.

    A PDF that fetches its own QR over the network is not an archival document and
    would not render at all on an inspector's offline device.
    """
    buffer = io.BytesIO()
    segno.make(payload, error=QR_ERROR_LEVEL, micro=False).save(
        buffer, kind="png", scale=QR_SCALE, border=QR_BORDER, dark="black", light="white"
    )
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


@lru_cache(maxsize=1)
def default_layout() -> str:
    """The layout bundled with the module, used when `permit_templates.layout_file_id`
    is NULL — which is what migration 0019's seeded row leaves it as (task 1, decision 2).
    Read once and cached: `render_permit` itself never touches the filesystem."""
    return _DEFAULT_LAYOUT_PATH.read_text(encoding="utf-8")


def fill(layout_html: str, values: Mapping[str, Any]) -> str:
    """Substitute every `{{ field }}` with its HTML-escaped value.

    Refuses rather than leaving a gap. Three ways a placeholder has no value, all of
    them a defect on a legal document and all reported together so an administrator
    fixes the layout in one pass:
      - the name is not in `values`;
      - the name is there but the value is `None` (an unfilled column, which would
        otherwise print the word "None");
      - the braces hold something that is not a field name at all (`{{ Series }}`,
        `{{ x.y }}`) — left alone it would reach the citizen as literal `{{ … }}`.
    """
    unfilled: set[str] = set()

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if not _FIELD_NAME.match(name) or values.get(name) is None:
            unfilled.add(name or match.group(0))
            return ""
        return escape(str(values[name]))

    filled = _PLACEHOLDER.sub(_substitute, layout_html)
    if unfilled:
        raise err(
            "ERR-VAL-001",
            details={"reason": "unfilled_placeholders", "fields": sorted(unfilled)},
        )
    return filled


def render_permit(snapshot: Mapping[str, Any], layout_html: str, qr_url: str) -> bytes:
    """Render one permit to PDF/A bytes. Blocking C code (Pango, HarfBuzz, zlib):
    the caller runs it through `asyncio.to_thread`, never on the event loop.

    `snapshot` is `permits.snapshot` — form 1-ilova's requisites, already stringified
    by the caller (ruling 14). This function does no arithmetic and no formatting on
    them: what the snapshot says is what the document says, so the PDF and the stored
    record cannot disagree. `qr_url` is the public check URL; it reaches the layout as
    `{{ qr }}`, filled with the embedded image rather than from the snapshot.
    """
    values = {**snapshot, QR_FIELD: qr_png_data_uri(qr_url)}
    html = fill(layout_html, values)

    # One FontConfiguration per call, not one per module: `asyncio.to_thread` means
    # concurrent renders in different threads, and this object is mutable state.
    font_config = FontConfiguration()
    fetcher = _AssetFetcher()
    document = HTML(string=html, base_url=f"{ASSETS_DIR.as_uri()}/", url_fetcher=fetcher).render(
        font_config=font_config,
        stylesheets=[CSS(string=_font_css(), font_config=font_config, url_fetcher=fetcher)],
    )

    # The document's only clock. WeasyPrint 69 leaves both unset for PDF/A rather than
    # stamping `localtime()`, so this is belt and braces — but it is the belt that keeps
    # ruling 3's hash reproducible if a later release changes its mind, and the issue
    # date is the date the document itself asserts anyway.
    issued_at = snapshot.get("issued_at")
    document.metadata.created = str(issued_at) if issued_at is not None else None
    document.metadata.modified = document.metadata.created

    # `write_pdf` returns None when handed a target to write into; with no target it
    # always returns the bytes. The assert is the narrowing, not a runtime doubt.
    pdf = document.write_pdf(pdf_variant=PDF_VARIANT, uncompressed_pdf=UNCOMPRESSED_PDF)
    assert pdf is not None
    return pdf
