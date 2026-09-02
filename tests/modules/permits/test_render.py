import re

from app.modules.permits import render

SNAPSHOT = {
    "series": "А",
    "number": "000001",
    "issued_at": "2027-04-01",
    "organization_name": "Бурчмулла ўрмон хўжалиги",
    "activity_name": "Чорва молларини боқиш",
    "holder_name": "Азизов Азиз Азизович",
    "holder_pinfl": "12345678901234",
    "contour_number": "12-3",
    "area_ha": "12.5000",
    "period_from": "2027-05-01",
    "period_to": "2027-09-30",
    "sb_load": "40.0000",
    "amount": "2060000.00",
    "paid_at": "2027-04-01",
}
LAYOUT = '<html><body><h1>{{ series }} № {{ number }}</h1><img src="{{ qr }}"></body></html>'


def test_it_produces_a_pdf_that_declares_itself_pdf_a() -> None:
    """Ruling 2: we assert the structural markers Python can see. A formal ISO
    check is veraPDF's job and belongs to the deploy checklist."""
    pdf = render.render_permit(SNAPSHOT, LAYOUT, "https://example.uz/public/permits/check?qr=x")

    assert pdf.startswith(b"%PDF")
    assert b"/OutputIntent" in pdf, "PDF/A requires an output intent"
    assert b"pdfaid" in pdf, "PDF/A requires the XMP identification schema"


def test_the_same_snapshot_renders_the_same_bytes() -> None:
    """Ruling 3 freezes sha256(pdf). If rendering is not deterministic, that
    hash is a coin flip and every signature over it is unverifiable."""
    url = "https://example.uz/public/permits/check?qr=x"
    assert render.render_permit(SNAPSHOT, LAYOUT, url) == render.render_permit(
        SNAPSHOT, LAYOUT, url
    )


def test_the_qr_is_embedded_not_linked() -> None:
    """A PDF that fetches its own QR over the network is not an archival
    document, and would not render at all on an inspector's offline device."""
    uri = render.qr_png_data_uri("https://example.uz/public/permits/check?qr=abc")
    assert uri.startswith("data:image/png;base64,")


def test_a_placeholder_with_no_value_is_refused_not_left_blank() -> None:
    """A permit is a legal document: an unfilled field is a defect, not a
    cosmetic gap, and it must fail at issuance rather than reach a citizen."""
    import pytest

    from app.core.errors import DomainError

    with pytest.raises(DomainError) as raised:
        render.render_permit({"series": "А"}, LAYOUT, "https://example.uz/x")
    assert raised.value.code == "ERR-VAL-001"
    assert "number" in str(raised.value.details)


def test_the_layout_cannot_execute_anything() -> None:
    """The layout comes from an admin-editable database row (permit_templates).
    Only whitelisted {{ field }} substitution — never a template engine."""
    out = render.render_permit(
        {**SNAPSHOT, "holder_name": "<script>alert(1)</script>"},
        "<html><body>{{ holder_name }}{{ qr }}</body></html>",
        "https://example.uz/x",
    )
    assert out.startswith(b"%PDF")


# --- Beyond the brief's five: what a `%PDF` assertion cannot see ---------------------


def _mapped_codepoints(pdf: bytes) -> set[int]:
    """Every Unicode codepoint the PDF's own ToUnicode CMaps map a glyph to.

    Readable as plain text only because `render.UNCOMPRESSED_PDF` is on; a compressed
    PDF hides these behind zlib. One set across all CMaps — the page uses a regular
    and a bold subset, and a letter present in either has reached the document.
    """
    mapped: set[int] = set()
    for block in re.finditer(rb"beginbfchar(.*?)endbfchar", pdf, re.S):
        for pair in re.finditer(rb"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>", block.group(1)):
            mapped.add(int(pair.group(2), 16))
    return mapped


UZBEK_CYRILLIC = "ЎўҚқҒғҲҳ"
"""U+040E/045E, U+049A/049B, U+0492/0493, U+04B2/04B3 — the four letters Uzbek Cyrillic
adds to the Russian alphabet. Plenty of otherwise-fine Cyrillic faces omit exactly these
and render «Бурчмулла ўрмон хўжалиги» as blank boxes."""


def test_the_bundled_faces_carry_the_uzbek_cyrillic_letters() -> None:
    """Asserted against the shipped .ttf files themselves, because it is the one form
    of this check nothing can rescue: a cmap entry is not enough either, since it may
    point at an empty glyph."""
    from fontTools.ttLib import TTFont  # a WeasyPrint dependency; it does the subsetting

    for name in ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf"):
        font = TTFont(render.ASSETS_DIR / name)
        cmap, glyf = font.getBestCmap(), font["glyf"]
        assert cmap is not None, f"{name} has no usable Unicode cmap at all"
        for char in UZBEK_CYRILLIC:
            glyph_name = cmap.get(ord(char))
            assert glyph_name, f"{name} has no glyph for U+{ord(char):04X} {char}"
            assert glyf[glyph_name].numberOfContours != 0, (
                f"{name} maps U+{ord(char):04X} {char} to an EMPTY glyph"
            )


def test_no_system_font_is_substituted_for_the_uzbek_letters() -> None:
    """The bundled face must be the ONLY face in the document.

    Asserting that the letters merely reached the PDF is not enough, and this test was
    written after that weaker version passed its own negative control: pointing the
    @font-face at a Latin-only file still produced all eight codepoints, because Pango
    silently fell back to the host's Times New Roman. That fallback exists on a
    developer's macOS and not in a container with no Cyrillic system font, so the weak
    assertion is green exactly where it does not matter and green where it lies.
    """
    pdf = render.render_permit(
        {**SNAPSHOT, "holder_name": UZBEK_CYRILLIC},
        '<html><body>{{ holder_name }}<img src="{{ qr }}"></body></html>',
        "https://example.uz/x",
    )

    assert b"/FontFile2" in pdf, "PDF/A requires the face embedded, not merely referenced"
    expected = render._FONT_FAMILY.replace(" ", "-").encode()
    faces = set(re.findall(rb"/BaseFont\s*/[A-Z]{6}\+([A-Za-z0-9\-]+)", pdf))
    assert faces, "no embedded font at all"
    assert all(face.startswith(expected) for face in faces), (
        f"a font other than the bundled {expected!r} was used: {sorted(faces)}"
    )

    mapped = _mapped_codepoints(pdf)
    missing = [f"U+{ord(c):04X} {c}" for c in UZBEK_CYRILLIC if ord(c) not in mapped]
    assert not missing, f"the embedded subset carries no glyph for {missing}"


def test_the_layout_bundled_with_the_module_renders_from_the_issuers_snapshot() -> None:
    """The seeded grazing `permit_templates` row has a NULL `layout_file_id`, which
    means this file (task 1, decision 2). A placeholder in it that the snapshot does
    not carry would surface as ERR-VAL-001 at the first real issuance, not here."""
    pdf = render.render_permit(
        SNAPSHOT, render.default_layout(), "https://example.uz/public/permits/check?qr=x"
    )

    assert pdf.startswith(b"%PDF")
    assert b"pdfaid" in pdf


def test_a_layout_cannot_make_the_server_fetch_a_url_or_read_a_file() -> None:
    """The layout is an admin-editable row. Without `_AssetFetcher` a stored `<img>`
    would be a server-side request from inside the issuing transaction, and a
    `file://` one a local read — the SSRF twin of the template-engine hole above."""
    import pytest

    from app.core.errors import DomainError

    fetcher = render._AssetFetcher()
    for url in ("http://127.0.0.1:9000/probe.png", "https://example.uz/x.png"):
        with pytest.raises((DomainError, ValueError)):
            fetcher.fetch(url)
    with pytest.raises(DomainError) as raised:
        fetcher.fetch("file:///etc/passwd")
    assert raised.value.code == "ERR-VAL-001"

    # The bundled font is the one thing it must serve, or no document has a face.
    served = fetcher.fetch((render.ASSETS_DIR / "DejaVuSerif.ttf").as_uri())
    assert served.read(4) == b"\x00\x01\x00\x00", "a TrueType file starts with its version tag"
