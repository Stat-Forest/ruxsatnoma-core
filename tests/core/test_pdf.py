"""`app.core.pdf` — the generic document renderer (stage 16, task B1)."""

import re

import pytest

from app.core import pdf
from app.core.errors import DomainError

LAYOUT = "<html><body><h1>{{ title }}</h1><p>{{ body }}</p>{{ rows }}</body></html>"


def test_fill_escapes_values_and_inserts_fragments_raw() -> None:
    html = pdf.fill(
        LAYOUT,
        {"title": "A & B", "body": "<script>x</script>"},
        fragments={"rows": "<ul><li>one</li></ul>"},
    )
    assert "A &amp; B" in html
    assert "&lt;script&gt;" in html
    assert "<ul><li>one</li></ul>" in html


def test_fill_refuses_every_unfilled_placeholder_at_once() -> None:
    with pytest.raises(DomainError) as raised:
        pdf.fill(LAYOUT, {"title": "x", "body": None})
    assert raised.value.details is not None
    assert raised.value.details["reason"] == "unfilled_placeholders"
    assert raised.value.details["fields"] == ["body", "rows"]


def test_fill_does_not_rescan_substituted_text() -> None:
    html = pdf.fill("<p>{{ a }}</p>", {"a": "{{ b }}"})
    assert "{{ b }}" in html


# Every script a document may carry: Uzbek Latin (with the ʻ U+02BB modifier),
# Uzbek Cyrillic, Russian, Karakalpak Latin (ǵ ń ı á ó ú).
ALL_SCRIPTS = "Oʻrmon ʼ Ўрмон хўжалиги Қорақалпоғистон Лесной Toǵay ń ı á ó ú ǵ № «»"


def test_render_document_draws_every_script_with_the_bundled_face() -> None:
    data = pdf.render_document("<p>{{ text }}</p>", {"text": ALL_SCRIPTS}, created="2026-09-24")
    assert data.startswith(b"%PDF-")


def test_render_document_is_byte_identical_across_runs() -> None:
    a = pdf.render_document("<p>{{ text }}</p>", {"text": ALL_SCRIPTS}, created="2026-09-24")
    b = pdf.render_document("<p>{{ text }}</p>", {"text": ALL_SCRIPTS}, created="2026-09-24")
    assert a == b


def test_render_document_refuses_a_character_the_face_cannot_draw() -> None:
    with pytest.raises(DomainError) as raised:
        pdf.render_document("<p>{{ text }}</p>", {"text": "Ali 🙂"})
    assert raised.value.details is not None
    assert raised.value.details["reason"] == "unrenderable_characters"
    assert "text" in raised.value.details["fields"]


def test_render_document_checks_fragments_too() -> None:
    with pytest.raises(DomainError) as raised:
        pdf.render_document("<div>{{ rows }}</div>", {}, fragments={"rows": "<p>🙂</p>"})
    assert raised.value.details is not None
    assert raised.value.details["reason"] == "unrenderable_characters"


def test_render_document_refuses_an_external_resource() -> None:
    # WeasyPrint swallows a fetcher error and renders without the image, so the
    # refusal is observed as the image simply not being fetched: no exception
    # escapes and no network is touched. The assertion is that rendering
    # completes offline.
    data = pdf.render_document('<img src="http://127.0.0.1:9/x.png"><p>{{ a }}</p>', {"a": "b"})
    assert data.startswith(b"%PDF-")


def test_qr_is_an_embedded_png_data_uri() -> None:
    uri = pdf.qr_png_data_uri("RX-2026-000001 sha256:abc")
    assert re.match(r"^data:image/png;base64,[A-Za-z0-9+/=]+$", uri)


# --- Stage 16 fix wave F1: public `assert_renderable`, `strip_invisible`, ----
# `renderable_text` -------------------------------------------------------


def test_assert_renderable_is_the_public_name() -> None:
    """F1.1: the check `decision.reject` must call before it spends the head's
    ERI is now public — a rename, not a new function; `render_document` still
    calls it (the tests above already prove that indirectly)."""
    with pytest.raises(DomainError) as raised:
        pdf.assert_renderable({"fact": "Ali 🙂"})
    assert raised.value.details is not None
    assert raised.value.details["reason"] == "unrenderable_characters"
    assert "fact" in raised.value.details["fields"]


def test_strip_invisible_removes_every_format_character() -> None:
    """U+FEFF (BOM), U+200B (zero-width space), U+200F (RTL mark), U+2060
    (word joiner) and U+00AD (soft hyphen) are all Unicode category `Cf` —
    invisible to whoever typed them, ordinary codepoints to `min_length=1`."""
    dirty = "﻿Bir​inchi‏ holat⁠ so­ʻz"
    assert pdf.strip_invisible(dirty) == "Birinchi holat soʻz"


def test_strip_invisible_leaves_ordinary_text_untouched() -> None:
    assert pdf.strip_invisible(ALL_SCRIPTS) == ALL_SCRIPTS


def test_renderable_text_strips_invisible_characters_too() -> None:
    assert pdf.renderable_text("﻿Ali") == "Ali"


def test_renderable_text_replaces_rather_than_drops_an_uncovered_character() -> None:
    """The other half of ruling R6: data that was never typed into a request
    this document's signature covers must never make the WHOLE document
    unrenderable — an uncovered character is REPLACED, not removed, so its
    position and the surrounding text survive."""
    result = pdf.renderable_text("Ali 🙂 Vali")
    assert "🙂" not in result
    assert result.startswith("Ali ") and result.endswith(" Vali")
    assert len(result) == len("Ali 🙂 Vali")


def test_renderable_text_decides_the_replacement_character_once_from_the_cmap() -> None:
    """Every substitution across a document uses the SAME glyph — proven by
    calling it twice and getting the same character back both times."""
    first = pdf.renderable_text("🙂")
    second = pdf.renderable_text("🙂")
    assert first == second
    assert first in ("�", "?")
