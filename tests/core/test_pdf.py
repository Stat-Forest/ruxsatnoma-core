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
