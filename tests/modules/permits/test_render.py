import functools
import re
import zlib

from app.modules.permits import render

SNAPSHOT = {
    "series": "А",
    "number": "000001",
    "issued_at": "2027-04-01",
    "authority_name": "Ўзбекистон Республикаси Ўрмон хўжалиги агентлиги",
    "leshoz_name": "Бурчмулла ўрмон хўжалиги",
    "activity_name": "Чорва молларини боқиш",
    "holder_name": "Азизов Азиз Азизович",
    "holder_pinfl": "12345678901234",
    "holder_address": "Тошкент вилояти, Бўстонлиқ тумани, Бурчмулла қишлоғи",
    "contour_number": "12-3",
    "area_ha": "12.5000",
    "heads_large_adult": "Қорамол (катта) — 5",
    "heads_large_young": "От (2 ёшгача) — 2",
    "heads_small_adult": "Қўй ва эчки (6 ойдан катта) — 2",
    "heads_small_young": "—",
    "period_from": "2027-05-01",
    "period_to": "2027-09-30",
    "sb_load": "40.0000",
    "amount": "2060000.00",
    "payment_status": "Тўланган",
    "payment_date": "2027-04-01",
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


def test_a_substituted_value_is_html_escaped_before_it_reaches_the_layout() -> None:
    """The assertion the test above cannot make. `render_permit` returns PDF bytes,
    so "a PDF came out" is true whether the markup was escaped or executed — WeasyPrint
    happily renders an injected `<b>`, an `<img>` that triggers the asset fetcher, or a
    tag that swallows the rest of the document, and every one of those still starts with
    `%PDF` (final fix wave).

    So this asserts on `fill`'s own output, which is the HTML string. The layout is an
    admin-editable database row and the values come from `permits.snapshot`, so escaping
    is a real control on a legal document, not a formality: an unescaped `<` in a
    holder's name would silently change what the permit SAYS."""
    from html import escape

    hostile = '<script>alert("x")</script> & <b>Азизов</b>'
    filled = render.fill("<html><body>{{ holder_name }}</body></html>", {"holder_name": hostile})

    assert escape(hostile) in filled, filled
    assert "<script>" not in filled
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &lt;b&gt;" in filled
    # The layout's OWN markup is untouched — escaping applies to the substituted value
    # and not to the document around it.
    assert filled.startswith("<html><body>") and filled.endswith("</body></html>")


# --- Beyond the brief's five: what a `%PDF` assertion cannot see ---------------------


def _mapped_codepoints(pdf: bytes) -> set[int]:
    """Every Unicode codepoint the PDF's own ToUnicode CMaps map a glyph to.

    A ToUnicode CMap is a *stream*, so it is Flate-compressed like any other — PDF/A-1
    leaves the catalogue and the XMP packet in the clear (PDF 1.4 has no object streams)
    but not stream bodies. So inflate every stream that inflates and read the CMaps out
    of the result. `zlib` is the standard library; nothing is added to the lockfile for
    a test, and nothing here depends on how the renderer chose to compress.

    One set across all CMaps: the page uses a regular and a bold subset, and a letter
    present in either has reached the document.
    """
    haystacks = [pdf]
    for stream in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            haystacks.append(zlib.decompress(stream.group(1)))
        except zlib.error:
            continue  # an image, or a stream using some other filter

    mapped: set[int] = set()
    for haystack in haystacks:
        for block in re.finditer(rb"beginbfchar(.*?)endbfchar", haystack, re.S):
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


# --- Is there a host face on this platform for Pango to substitute at all? ------------


def _faces_in(pdf: bytes) -> set[bytes]:
    """Every `/BaseFont` name in the PDF, subset tag stripped.

    A deliberately INDEPENDENT parse of the shapes `render._BASE_FONT_NAME` reads, so a
    bug in one cannot hide itself in the other. Raw bytes only: PDF/A-1 is based on PDF
    1.4, which has no object streams, so the font dictionaries are in the clear.
    """
    return set(re.findall(rb"/BaseFont\s*/(?:[A-Z]{6}\+)?([^\s/<>\[\]{}()%]+)", pdf))


FALLBACK_SCRIPTS = ("क्ष", "ཀ", "森", "🌲", "ก", "א")
"""Scripts the bundled DejaVu faces do not cover, so Pango must look elsewhere for them.
Reachable through the LAYOUT only: a codepoint that triggers fallback is by construction
missing from the bundled cmap, so `_assert_renderable` catches anything coming from the
snapshot first."""


def _no_backstop(pdf: bytes) -> None:
    """A stand-in for `_assert_only_bundled_faces` that refuses nothing."""
    return None


def _pdf_without_the_backstop(layout: str) -> bytes:
    """`render_permit`'s own output with the backstop neutralised.

    The production pipeline rather than a copy of it, so the probe below cannot drift
    from what the renderer really does — and with the guard REPLACED by a no-op rather
    than consulted, so a broken scanner cannot quietly turn the test it gates into a skip.
    """
    original = render._assert_only_bundled_faces
    render._assert_only_bundled_faces = _no_backstop
    try:
        return render.render_permit(SNAPSHOT, layout, "https://example.uz/x")
    finally:
        render._assert_only_bundled_faces = original


@functools.cache
def _scripts_with_a_host_face() -> tuple[str, ...]:
    """Which of `FALLBACK_SCRIPTS` actually pull a foreign face into the document HERE.

    None of them do on a machine with no system fonts, which is what CI is: `ci.yml`
    installs the Pango/HarfBuzz libraries and no font package at all, so the only face
    fontconfig knows is the one WeasyPrint registered from our own `@font-face` — and
    Pango draws every unbundled script as tofu IN THAT FACE. Nothing foreign is embedded
    and the backstop correctly has nothing to refuse. Reproduced locally by pointing
    `FONTCONFIG_FILE` at a config declaring no font directories.
    """
    ours = render._FONT_FAMILY.replace(" ", "-").encode()
    layout = '<html><body>{script} {{{{ series }}}}<img src="{{{{ qr }}}}"></body></html>'
    return tuple(
        script
        for script in FALLBACK_SCRIPTS
        if any(
            not face.startswith(ours)
            for face in _faces_in(_pdf_without_the_backstop(layout.format(script=script)))
        )
    )


def _substituting_scripts_or_skip() -> tuple[str, ...]:
    """The scripts the test below can use, or a skip that states the detected reason.

    The scanner itself is pinned identically on every platform by
    `test_the_scanner_refuses_a_foreign_face_in_every_shape_and_fails_closed_on_the_rest`,
    which feeds it crafted bytes. What no crafted byte can pin is the end-to-end claim
    that Pango's substitution reaches the finished PDF and is caught there — and on a
    fontless runner there is no substitution to observe. So this skips for a reason
    detected at runtime rather than for a platform name, and switches itself back on the
    day a runner has fonts.
    """
    import pytest

    scripts = _scripts_with_a_host_face()
    if not scripts:
        pytest.skip(
            "no substitutable host face on this platform: Pango draws every script the "
            "bundled faces lack in the bundled face itself, so nothing foreign can reach "
            "the PDF. The scanner is covered directly, against crafted bytes, either way."
        )
    return scripts


def test_no_system_font_is_substituted_for_the_uzbek_letters() -> None:
    """The bundled face must be the ONLY face in the document.

    Asserting that the letters merely reached the PDF is not enough, and this test was
    written after that weaker version passed its own negative control: pointing the
    @font-face at a Latin-only file still produced all eight codepoints, because Pango
    silently fell back to the host's Times New Roman. That fallback exists on a
    developer's macOS and not in a container with no Cyrillic system font, so the weak
    assertion is green exactly where it does not matter and green where it lies.

    Which is also the limit of what this test proves ON that container, measured rather
    than assumed: with no system font there is nothing to substitute, so the negative
    control above (a Latin-only `@font-face`) now passes BOTH halves — the ToUnicode CMap
    records the codepoints Pango was asked to draw, not the ones it could actually draw.
    The substantive claim therefore rests on
    `test_the_bundled_faces_carry_the_uzbek_cyrillic_letters`, which reads the shipped
    `.ttf` files directly and is the same on every machine.
    """
    pdf = render.render_permit(
        {**SNAPSHOT, "holder_name": UZBEK_CYRILLIC},
        '<html><body>{{ holder_name }}<img src="{{ qr }}"></body></html>',
        "https://example.uz/x",
    )

    assert b"/FontFile2" in pdf, "PDF/A requires the face embedded, not merely referenced"
    expected = render._FONT_FAMILY.replace(" ", "-").encode()
    faces = _faces_in(pdf)
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

    assets = render.ASSETS_DIR.as_uri()
    escapes = {
        "plain": "file:///etc/hosts",
        "relative": f"{assets}/../../../../../../etc/hosts",
        # The one that got through. `..%2F` survives Path.resolve() as a single literal
        # segment, so the guard saw a path inside assets/ — and urllib's own
        # url2pathname then decoded it back into `../`. It returned /etc/hosts.
        "percent-encoded": f"{assets}/{'..%2F' * 16}etc/hosts",
        "encoded, with a fragment": f"{assets}/..%2Fetc/hosts#DejaVuSerif.ttf",
        "encoded, with a query": f"{assets}/..%2Fetc/hosts?x=DejaVuSerif.ttf",
    }
    for label, url in escapes.items():
        with pytest.raises((DomainError, OSError)) as escaped:
            fetcher.fetch(url).read()
        if isinstance(escaped.value, DomainError):
            assert escaped.value.code == "ERR-VAL-001", label

    # The bundled font is the one thing it must serve, or no document has a face.
    served = fetcher.fetch((render.ASSETS_DIR / "DejaVuSerif.ttf").as_uri())
    assert served.read(4) == b"\x00\x01\x00\x00", "a TrueType file starts with its version tag"


def test_a_character_the_bundled_font_cannot_draw_is_refused_at_issuance() -> None:
    """Review finding I2. Pango falls back per glyph to whatever the host has, whatever
    the CSS stack says: an emoji in a holder's name embedded Apple Color Emoji and
    Hiragino Mincho into the PDF on the developer's Mac, and would have produced tofu —
    and different bytes, so a different `doc_hash` — in the container. The refusal names
    the character, so the fix is obvious to whoever entered it."""
    import pytest

    from app.core.errors import DomainError

    with pytest.raises(DomainError) as raised:
        render.render_permit(
            {**SNAPSHOT, "holder_name": "Азиз 🌲 森"},
            '<html><body>{{ holder_name }}<img src="{{ qr }}"></body></html>',
            "https://example.uz/x",
        )

    assert raised.value.code == "ERR-VAL-001"
    assert raised.value.details is not None
    assert raised.value.details["reason"] == "unrenderable_characters"
    named = str(raised.value.details["fields"])
    assert "U+1F332" in named and "U+68EE" in named, named

    # Uzbek Cyrillic and ordinary whitespace must of course still pass.
    assert render.render_permit(
        {**SNAPSHOT, "holder_name": f"Азизов\tАзиз\n{UZBEK_CYRILLIC}"},
        '<html><body>{{ holder_name }}<img src="{{ qr }}"></body></html>',
        "https://example.uz/x",
    ).startswith(b"%PDF")


def test_no_host_face_reaches_the_document_even_from_the_layout_itself() -> None:
    """The end-to-end half of the backstop: Pango really does substitute a host face, and
    the guard really does catch it in the finished document.

    A snapshot is not the only way text gets onto the page. The layout is an
    admin-editable row, and its own static labels go through the same per-glyph Pango
    fallback — invisible to `_assert_renderable`, which only inspects substituted values.

    This is the ONLY assertion in the file that depends on the machine having a font we
    do not ship, so it skips for a runtime-detected reason (see
    `_substituting_scripts_or_skip`) rather than for a platform name. The scanner's own
    logic is pinned from crafted bytes below, on every machine, either way.
    """
    import pytest

    from app.core.errors import DomainError

    for script in _substituting_scripts_or_skip():
        with pytest.raises(DomainError) as raised:
            render.render_permit(
                SNAPSHOT,
                f'<html><body>{script} {{{{ series }}}}<img src="{{{{ qr }}}}"></body></html>',
                "https://example.uz/x",
            )
        assert raised.value.code == "ERR-VAL-001", script
        assert raised.value.details is not None
        assert raised.value.details["reason"] == "host_font_substituted", script


def test_a_script_the_bundled_faces_do_cover_is_not_refused() -> None:
    """The control for the test above, and it must run where that one cannot.

    Without it the backstop could "pass" by refusing every layout that is not Cyrillic.
    `U+0531` is genuinely in DejaVu's cmap, so this page renders with our face and
    nothing else — on a machine with a full font set and on a fontless runner alike,
    which is why it carries no skip.
    """
    pdf = render.render_permit(
        SNAPSHOT,
        '<html><body>Ա {{ series }}<img src="{{ qr }}"></body></html>',
        "https://example.uz/x",
    )

    ours = render._FONT_FAMILY.replace(" ", "-").encode()
    faces = _faces_in(pdf)
    assert pdf.startswith(b"%PDF")
    assert faces, "no embedded font at all"
    assert all(face.startswith(ours) for face in faces), sorted(faces)


def test_the_pdf_dates_come_from_the_snapshot_and_never_from_the_clock() -> None:
    """Review finding I3, and the brief's own instruction to assert the pin.

    Deleting the two lines that set `metadata.created`/`modified` left all nine earlier
    tests green, because WeasyPrint 69's PDF/A path happens to write no date of its own —
    so the pin was protecting ruling 3's hash against a future release with nothing
    watching it. `test_the_same_snapshot_renders_the_same_bytes` cannot cover this
    either: two renders one millisecond apart agree even on a build stamping localtime().
    """
    import datetime

    pdf = render.render_permit(
        {**SNAPSHOT, "issued_at": "1999-12-31"}, render.default_layout(), "https://example.uz/x"
    )

    assert b"D:19991231" in pdf, "the issue date is not pinned into the document"
    today = datetime.date.today().strftime("D:%Y%m%d").encode()
    assert today not in pdf, f"a wall-clock date ({today!r}) reached the PDF"


def test_the_scanner_refuses_a_foreign_face_in_every_shape_and_fails_closed_on_the_rest() -> None:
    """The backstop pinned where it can be pinned identically on every machine: against
    crafted bytes, rather than against whatever Pango happened to do here.

    It has failed OPEN twice. First by not existing — a host face substituted per glyph
    reached the document unremarked. Then, after it was added, on a PostScript name
    beginning with a DOT: `[A-Za-z0-9-]` at the capture position matched no such name at
    all, so `.Zither-India` was counted as absent rather than suspect and
    `ZJBCPL+.Zither-India` was embedded in a government document. Both were caught by a
    render-driven test on a developer's Mac — and a render-driven test is exactly what a
    container with no system fonts cannot run: with nothing to substitute FROM, Pango
    draws the tofu in our own face and there is no foreign name to find. So every shape a
    foreign `/BaseFont` can take is asserted here, from bytes, where the machine's font
    set cannot change the answer.
    """
    import pytest

    from app.core.errors import DomainError

    for label, blob, expected in (
        ("a plain name", b"/BaseFont /Times-New-Roman", "Times-New-Roman"),
        ("a subset tag", b"/BaseFont /ESSOFH+Times-New-Roman", "ESSOFH+Times-New-Roman"),
        # The two dotted shapes are the re-review's regression and the reason this test
        # exists: a leading dot is how macOS names its own fallback faces.
        ("a leading dot", b"/BaseFont /.LastResort", ".LastResort"),
        ("a dot behind a tag", b"/BaseFont /ZJBCPL+.Zither-India", "ZJBCPL+.Zither-India"),
        # Observed verbatim on the developer's Mac: the comma is part of the PDF name,
        # and an `[A-Za-z0-9-]+` class truncates it rather than reporting it.
        ("a trailing comma", b"/BaseFont /ESSOFH+Times-New-Roman,", "ESSOFH+Times-New-Roman,"),
        # Inflated as well as raw, so the guard keeps working if `PDF_VARIANT` ever moves
        # to a version that packs the font dictionaries into a `/Type /ObjStm`.
        (
            "hidden in a Flate stream",
            b"stream\n" + zlib.compress(b"<</BaseFont /AAAAAA+.SFNS-Regular>>") + b"\nendstream",
            "AAAAAA+.SFNS-Regular",
        ),
    ):
        with pytest.raises(DomainError) as raised:
            render._assert_only_bundled_faces(blob)
        assert raised.value.code == "ERR-VAL-001", label
        assert raised.value.details is not None, label
        assert raised.value.details["reason"] == "host_font_substituted", label
        assert raised.value.details["fonts"] == [expected], label

    # An entry we cannot READ is suspect, never absent — the principle the `_url_to_path`
    # fix already established, applied here. WeasyPrint does not emit these shapes, but a
    # future variant, or a font it does not name the way we expect, must fail closed.
    for label, blob in (
        ("an indirect reference", b"<</Type/Font/BaseFont 12 0 R>>"),
        ("a truncated dictionary", b"<</BaseFont"),
        ("no name at all", b"/BaseFont  <</x 1>>"),
    ):
        with pytest.raises(DomainError) as raised:
            render._assert_only_bundled_faces(blob)
        assert raised.value.details is not None
        assert raised.value.details["unparsable_base_font_entries"] == 1, label

    # The family comparison is exact: a host face merely PREFIXED by ours is foreign.
    with pytest.raises(DomainError):
        render._assert_only_bundled_faces(b"/BaseFont /AAAAAA+Permit-SerifSomething")

    # ...while our own faces, with and without the style suffix Pango appends, pass.
    for ours in (b"/BaseFont /Permit-Serif", b"/BaseFont /AAAAAA+Permit-Serif-Bold"):
        render._assert_only_bundled_faces(ours)
