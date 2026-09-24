"""Generic document renderer — HTML layout + values -> PDF/A-1b bytes (stage 16).

Lifted from `app/modules/permits/render.py`, which stays as it is: its PDF
hashes are signed (ruling 3 of 3.11a), and nothing here may move a byte of
them. Read that module for the history of every guard below; the short form:

* **Deterministic.** The only clock the document sees is `created`, so two
  renders of one snapshot are byte-identical.
* **Not a template engine.** `{{ field }}` substitution by a whitelist regex,
  every value HTML-escaped; `fragments` are the caller's own pre-escaped HTML
  (a repeated block rendered through `fill` itself) and are inserted as-is.
* **Offline and self-contained.** The face is bundled in `assets/` and embedded;
  the fetcher serves those assets and `data:` URIs and nothing else.
* **Loud.** An unfilled placeholder, a character the face cannot draw, or a
  host face sneaking into the PDF is `ERR-VAL-001`, never a silent gap.
"""

import base64
import io
import os
import re
import sys
import unicodedata
import zlib
from collections.abc import Mapping
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

# The dyld fix-up `permits/render.py` explains at length: WeasyPrint dlopens
# Pango/GLib by leaf name, which fails on macOS unless this is set before the
# import. setdefault; no effect on Linux, in CI or in the container.
if sys.platform == "darwin":  # pragma: no cover - a developer-machine path
    os.environ.setdefault("DYLD_FALLBACK_LIBRARY_PATH", "/opt/homebrew/lib:/usr/local/lib:/usr/lib")

import segno  # noqa: E402 - must follow the dyld fix-up above
from fontTools.ttLib import TTFont  # noqa: E402 - same
from weasyprint import CSS, HTML  # noqa: E402 - same
from weasyprint.text.fonts import FontConfiguration  # noqa: E402 - same
from weasyprint.urls import URLFetcher  # noqa: E402 - same

from app.core.errors import err  # noqa: E402 - same

ASSETS_DIR = Path(__file__).parent / "assets"

PDF_VARIANT = "pdf/a-1b"
QR_ERROR_LEVEL = "m"
QR_BORDER = 4
QR_SCALE = 8

_PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

# A value under this key is an embedded image, never text to check.
QR_FIELD = "qr"

_FONT_FAMILY = "Document Serif"
_FONT_FILES = ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf")

_BASE_FONT_KEY = re.compile(rb"/BaseFont\b")
_BASE_FONT_NAME = re.compile(rb"/BaseFont\s*/([^\s/<>\[\]{}()%]+)")
_SUBSET_TAG = re.compile(rb"^[A-Z]{6}\+")


def _font_css() -> str:
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


def _url_to_path(url: str) -> str:
    """The path urllib will actually open — computed with urllib's own
    primitives (see `permits/render.py::_url_to_path`, review finding I1)."""
    return url2pathname(urlsplit(url).path)


class _AssetFetcher(URLFetcher):
    """Serves `ASSETS_DIR` and `data:` URIs; refuses every other URL."""

    def __init__(self) -> None:
        super().__init__(allowed_protocols=("data", "file"), allow_redirects=False)

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> Any:
        if not url.lower().startswith("data:"):
            try:
                path = Path(_url_to_path(url)).resolve()
                path.relative_to(ASSETS_DIR.resolve())
            except (OSError, ValueError):  # fmt: skip
                raise err(
                    "ERR-VAL-001",
                    details={"reason": "layout_external_resource", "scheme": url[:16]},
                ) from None
        return super().fetch(url, headers)


def qr_png_data_uri(payload: str) -> str:
    """The QR as an embedded `data:image/png;base64,…` URI."""
    buffer = io.BytesIO()
    segno.make(payload, error=QR_ERROR_LEVEL, micro=False).save(
        buffer, kind="png", scale=QR_SCALE, border=QR_BORDER, dark="black", light="white"
    )
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


@lru_cache(maxsize=1)
def _renderable_codepoints() -> frozenset[int]:
    covered: set[int] = set()
    for name in _FONT_FILES:
        covered |= set(TTFont(ASSETS_DIR / name, lazy=True).getBestCmap() or {})
    return frozenset(covered)


def assert_renderable(values: Mapping[str, Any]) -> None:
    """Refuse a value carrying a character the bundled face cannot draw —
    Pango would fall back to a host face per glyph, changing the bytes by
    machine (`permits/render.py::_assert_renderable`, finding I2).

    Public (stage 16 fix wave F1): a caller that can still refuse BEFORE
    committing anything — `decision.reject`, before it spends the head's ERI —
    calls this directly over what the citizen typed, so an unrenderable
    character in a REQUEST field is a loud 422 at the moment it is entered,
    never a signed, frozen document nobody can ever download."""
    renderable = _renderable_codepoints()
    offenders: dict[str, list[str]] = {}
    for name, value in values.items():
        if name == QR_FIELD or value is None:
            continue
        bad = sorted({c for c in str(value) if not c.isspace() and ord(c) not in renderable})
        if bad:
            offenders[name] = [f"U+{ord(c):04X} {c}" for c in bad]
    if offenders:
        raise err("ERR-VAL-001", details={"reason": "unrenderable_characters", "fields": offenders})


def strip_invisible(text: str) -> str:
    """Remove every character of Unicode category `Cf` (format) — a BOM
    (U+FEFF), a zero-width space/joiner/non-joiner (U+200B..U+200F, U+2060), a
    soft hyphen (U+00AD) — pasted from Word or a chat app into a text field.

    These are invisible to a human reading the field but are ordinary
    codepoints to `min_length=1`, so a field that is only formatting
    characters would otherwise read as non-blank (stage 16 fix wave F1.1/1.2)."""
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


@lru_cache(maxsize=1)
def _replacement_char() -> str:
    """U+FFFD if the bundled face can draw it, else `?` — decided ONCE from
    the face's own cmap, so every substitution `renderable_text` makes across
    every document uses the same glyph rather than one decided ad hoc per
    call."""
    return "�" if 0xFFFD in _renderable_codepoints() else "?"


def renderable_text(text: str) -> str:
    """`strip_invisible`, then every remaining non-space character the
    bundled face cannot draw is REPLACED, never dropped silently.

    For data that was never typed into the request this document's signature
    covers — an applicant's stored name or address, an organization's name —
    `assert_renderable` cannot run before the fact (there is no request to
    refuse), and R6 requires that a document always renders. This is the
    other half of that ruling: a snapshot built from such data goes through
    this function field by field, so an uncovered character never makes the
    WHOLE document unrenderable (stage 16 fix wave F1.4)."""
    cleaned = strip_invisible(text)
    renderable = _renderable_codepoints()
    replacement = _replacement_char()
    return "".join(c if c.isspace() or ord(c) in renderable else replacement for c in cleaned)


def _assert_only_bundled_faces(pdf: bytes) -> None:
    """No face but ours may reach the finished document (the backstop for the
    layout's own static text, which `assert_renderable` never sees)."""
    family = _FONT_FAMILY.replace(" ", "-").encode()
    haystacks = [pdf]
    for stream in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            haystacks.append(zlib.decompress(stream.group(1)))
        except zlib.error:
            continue
    foreign: set[str] = set()
    unparsable = 0
    for haystack in haystacks:
        names = _BASE_FONT_NAME.findall(haystack)
        unparsable += len(_BASE_FONT_KEY.findall(haystack)) - len(names)
        for name in names:
            bare = _SUBSET_TAG.sub(b"", name)
            if bare != family and not bare.startswith(family + b"-"):
                foreign.add(name.decode("latin-1"))
    if foreign or unparsable:
        raise err(
            "ERR-VAL-001",
            details={
                "reason": "host_font_substituted",
                "fonts": sorted(foreign),
                "unparsable_base_font_entries": unparsable,
            },
        )


def fill(
    layout_html: str, values: Mapping[str, Any], *, fragments: Mapping[str, str] | None = None
) -> str:
    """Substitute every `{{ field }}`: a fragment raw, a value HTML-escaped.

    Refuses rather than leaving a gap — a name absent from both maps, a `None`
    value, or braces holding something that is not a field name are all
    reported together (`unfilled_placeholders`). `re.sub` never rescans its
    own replacement, so a citizen typing `{{ x }}` into a field prints it
    literally.
    """
    fragments = fragments or {}
    unfilled: set[str] = set()

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if _FIELD_NAME.match(name) and name in fragments:
            return fragments[name]
        if not _FIELD_NAME.match(name) or values.get(name) is None:
            unfilled.add(name or match.group(0))
            return ""
        return escape(str(values[name]))

    filled = _PLACEHOLDER.sub(_substitute, layout_html)
    if unfilled:
        raise err(
            "ERR-VAL-001", details={"reason": "unfilled_placeholders", "fields": sorted(unfilled)}
        )
    return filled


def render_document(
    layout_html: str,
    values: Mapping[str, Any],
    *,
    fragments: Mapping[str, str] | None = None,
    created: str | None = None,
) -> bytes:
    """Render one document to PDF/A-1b bytes. Blocking C code: call through
    `asyncio.to_thread`. `created` is the document's only clock (metadata
    created/modified), so the bytes are a function of the inputs alone."""
    fragments = fragments or {}
    assert_renderable({**values, **fragments})
    html = fill(layout_html, values, fragments=fragments)
    font_config = FontConfiguration()
    fetcher = _AssetFetcher()
    document = HTML(string=html, base_url=f"{ASSETS_DIR.as_uri()}/", url_fetcher=fetcher).render(
        font_config=font_config,
        stylesheets=[CSS(string=_font_css(), font_config=font_config, url_fetcher=fetcher)],
    )
    document.metadata.created = created
    document.metadata.modified = created
    data = document.write_pdf(pdf_variant=PDF_VARIANT)
    assert data is not None
    _assert_only_bundled_faces(data)
    return data
