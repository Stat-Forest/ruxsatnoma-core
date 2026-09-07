"""Every seeded SMS body must stay inside GSM 03.38.

An operator bills one part per 160 characters only while the whole message is
in that character set; ONE character outside it (`oʻ`'s U+02BB, an em dash, a
typographic quote) switches the entire message to UCS-2 and 70 characters per
part — silently, with no error anywhere, visible only on the invoice. Migration
`0038` normalised the Latin bodies for exactly that reason, and this is what
keeps the next seeded template from putting them back.

`uz_cyrl` and `ru` are outside the set by definition and are not checked: a
Cyrillic SMS is UCS-2 whatever we do, which is why `0009` ruling 20 already
counts them at 70. Only `uz_latn` — the one language that CAN be cheap — and
the OTP text are held to it.
"""

import re

import sqlalchemy as sa

from app.modules.integrations.adapters.otp_sender import OTP_TEXT

# GSM 03.38 basic set. The extension table (`^{}\[~]|€`) is deliberately NOT
# included: those characters cost two septets each and none of our texts needs
# one, so an extension character in a template is a mistake worth failing on.
# `{placeholder}` braces are the one exception — they never reach the operator,
# `render()` substitutes them first — so they are stripped before the check.
# What replaces them is NOT checked here: a `{reason}` an inspector typed in
# Cyrillic makes that one message UCS-2 at runtime, and no test can prevent it.
_PLACEHOLDER = re.compile(r"\{[a-z][a-z0-9_]*\}")
GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)


def _outside(text: str) -> set[str]:
    stripped = _PLACEHOLDER.sub("", text)
    return {char for char in stripped if char not in GSM_BASIC}


def test_otp_text_is_gsm_only() -> None:
    assert _outside(OTP_TEXT) == set()


async def test_every_active_latin_sms_body_is_gsm_only(db) -> None:
    rows = (
        await db.execute(
            sa.text(
                "SELECT event_code, body->>'uz_latn' FROM notification_templates"
                " WHERE channel = 'sms' AND status = 'active'"
                "   AND jsonb_exists(body, 'uz_latn')"
            )
        )
    ).all()
    assert rows, "the seeded SMS templates are missing — check the migrations ran"
    offenders = {event_code: sorted(_outside(body)) for event_code, body in rows if _outside(body)}
    assert offenders == {}, (
        "these SMS bodies leave GSM 03.38 and therefore bill at 70 characters"
        f" per part instead of 160: {offenders}"
    )
