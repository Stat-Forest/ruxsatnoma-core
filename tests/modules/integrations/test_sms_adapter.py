"""The Eskiz client against httpx.MockTransport — never the live provider (plan
03.5 ruling 2). The most important assertion here is the last one: a failure must
not leak the message text, which for OTP is the code itself (3.4 carry-over)."""

import httpx
import pytest
from structlog.testing import capture_logs

from app.config import Settings
from app.modules.integrations.adapters.sms import EskizError, EskizSmsSender

SETTINGS = Settings(
    eskiz_base_url="https://notify.test",
    eskiz_email="robot@example.com",
    eskiz_password="s3cret",
    eskiz_sender="4546",
    eskiz_callback_secret="cbsecret",
    # Ruling #124: `callback_url` reads THIS field, never `public_base_url` (the
    # QR-facing one) — the two split apart at stage 7.0.
    eskiz_callback_base_url="https://ruxsatnoma.example",
    # Irrelevant to every test in this file, but config.py refuses to construct
    # a Settings with the callback non-local and this one still local.
    public_base_url="https://ruxsatnoma.example",
)


def _sender(handler) -> EskizSmsSender:
    return EskizSmsSender(SETTINGS, transport=httpx.MockTransport(handler))


async def test_logs_in_once_and_reuses_the_token(db):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"data": {"token": "tok-1"}})
        assert request.headers["Authorization"] == "Bearer tok-1"
        return httpx.Response(200, json={"id": "9001", "status": "waiting"})

    sender = _sender(handler)
    assert await sender.send(phone="998901234567", text="hi", reference="1") == "9001"
    assert await sender.send(phone="998901234567", text="hi", reference="2") == "9001"
    assert calls.count("/api/auth/login") == 1


async def test_sends_the_documented_fields(db):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        captured.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"id": "42"})

    await _sender(handler).send(phone="+998 90 123-45-67", text="Матн", reference="900123")
    assert captured["mobile_phone"] == "998901234567"  # digits only, no '+' (design/04 §4)
    assert captured["message"] == "Матн"
    assert captured["from"] == "4546"
    # Eskiz validates `user_sms_id` as a number of at most twelve digits — a uuid
    # answers `400 user_sms_id is invalid` (measured live, 2026-09-14).
    assert captured["user_sms_id"] == "900123"
    assert captured["callback_url"] == "https://ruxsatnoma.example/api/v1/webhooks/eskiz/cbsecret"


async def test_a_401_triggers_one_relogin_and_a_retry(db):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/auth/login":
            return httpx.Response(
                200, json={"data": {"token": f"tok-{seen.count('/api/auth/login')}"}}
            )
        if request.headers["Authorization"] == "Bearer tok-1":
            return httpx.Response(401, json={"message": "expired"})
        return httpx.Response(200, json={"id": "77"})

    assert await _sender(handler).send(phone="998901234567", text="hi", reference="7") == "77"
    assert seen.count("/api/auth/login") == 2


async def test_a_persistent_401_raises_rather_than_looping(db):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        return httpx.Response(401, json={"message": "nope"})

    with pytest.raises(EskizError):
        await _sender(handler).send(phone="998901234567", text="hi", reference="7")


async def test_a_failed_login_raises_without_the_password(db):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "bad credentials"})

    with pytest.raises(EskizError) as excinfo:
        await _sender(handler).send(phone="998901234567", text="hi", reference="7")
    assert "s3cret" not in str(excinfo.value)


async def test_an_error_never_leaks_the_message_text_or_the_phone(db):
    """`last_error` is admin-visible and logged; for an OTP the message text IS the
    code (3.4 carry-over, ruling 9 of plan 03.4 / ruling 2 here)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        return httpx.Response(400, json={"message": "invalid"})

    with pytest.raises(EskizError) as excinfo:
        await _sender(handler).send(phone="998901234567", text="code 123456", reference="7")
    rendered = f"{excinfo.value!r} {excinfo.value}"
    assert "123456" not in rendered
    assert "998901234567" not in rendered
    assert "400" in rendered  # the status IS useful and carries nothing sensitive


async def test_a_transport_timeout_becomes_an_eskiz_error(db):
    """Every request fails here, so the timeout actually lands on the login call
    (there is no cached token yet) — this covers `_login`'s except block. See
    `test_a_transport_timeout_during_send_becomes_an_eskiz_error` below for
    `_post_send`'s (review finding, task 5: the two branches look alike but are
    reached by different requests)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(EskizError):
        await _sender(handler).send(phone="998901234567", text="hi", reference="7")


async def test_a_transport_timeout_during_send_becomes_an_eskiz_error(db):
    """Login succeeds, so the timeout lands on POST /api/message/sms/send instead
    — this is the branch the test above does NOT reach."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(EskizError):
        await _sender(handler).send(phone="998901234567", text="hi", reference="7")


async def test_real_mode_returns_the_eskiz_sender(monkeypatch):
    from app.config import get_settings
    from app.modules.integrations.adapters import sms

    monkeypatch.setenv("SMS_MODE", "real")
    monkeypatch.setenv("ESKIZ_EMAIL", "robot@example.com")
    monkeypatch.setenv("ESKIZ_PASSWORD", "p")
    monkeypatch.setenv("ESKIZ_SENDER", "4546")
    monkeypatch.setenv("ESKIZ_CALLBACK_SECRET", "c")
    # A localhost origin is rejected outright under sms_mode=real (finding 6);
    # ruling #124 retargeted that guard onto ESKIZ_CALLBACK_BASE_URL, and its own
    # new guard then requires PUBLIC_BASE_URL non-local too, once this one is.
    monkeypatch.setenv("ESKIZ_CALLBACK_BASE_URL", "https://ruxsatnoma.example.uz")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://ruxsatnoma.example.uz")
    get_settings.cache_clear()
    sms.get_sms_sender.cache_clear()
    try:
        assert isinstance(sms.get_sms_sender(), sms.EskizSmsSender)
    finally:
        get_settings.cache_clear()
        sms.get_sms_sender.cache_clear()


async def test_an_otp_send_asks_for_no_delivery_report(db, monkeypatch):
    """Eskiz posts a delivery report for every message carrying a `callback_url`,
    and an OTP's `reference` is a throwaway uuid matching no `notifications` row —
    so every OTP report would dead-letter, forever, with the recipient's phone
    number in the stored payload. We never used delivery confirmation for OTP (the
    user entering the code is the confirmation), so we do not ask for one at all
    (final whole-branch review of 3.5, finding 1)."""
    from app.modules.integrations.adapters import otp_sender

    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        captured.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"id": "1"})

    sender = _sender(handler)
    monkeypatch.setattr(otp_sender, "get_sms_sender", lambda: sender)
    await otp_sender.RealOtpSender().send(target_type="phone", target="998901234567", code="123456")
    assert "callback_url" not in captured[-1]

    # ...while a notification send still asks for one: without it nothing would
    # ever move a notification from 'sent' to 'delivered'.
    await sender.send(phone="998901234567", text="hi", reference="8")
    assert captured[-1]["callback_url"] == (
        "https://ruxsatnoma.example/api/v1/webhooks/eskiz/cbsecret"
    )


async def test_a_reference_that_is_not_a_short_number_is_refused_before_any_request(db):
    """Eskiz's `user_sms_id` is digits only and at most twelve of them (live probe,
    2026-09-14: 999999999999 accepted, 1000000000000 and any uuid refused). A
    reference our own code shaped wrongly is a defect, not a provider failure —
    it must fail here, loudly, never as an `EskizError` the outbox retries."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json={"data": {"token": "tok"}, "id": "1"})

    sender = _sender(handler)
    for bad in ("ref-9", "0198d2a2-5d2e-7b1c-9f3e-2b4c6d8e0f1a", "1000000000000", "", "-5"):
        with pytest.raises(ValueError):
            await sender.send(phone="998901234567", text="hi", reference=bad)
    assert requests == []
    assert await sender.send(phone="998901234567", text="hi", reference="999999999999") == "1"


async def test_a_send_without_a_reference_carries_no_user_sms_id(db):
    """An OTP has no `notifications` row to correlate a report against, so it sends
    no `user_sms_id` at all — a throwaway one would be either refused (a uuid) or
    a lie (a number that names nothing)."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        captured.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"id": "1"})

    await _sender(handler).send(
        phone="998901234567", text="code 1", reference=None, delivery_report=False
    )
    assert "user_sms_id" not in captured
    assert "callback_url" not in captured


# --- What the provider actually says when it refuses (2026-09-23) -----------
#
# A live OTP failed and `outbox_messages.last_error` read, in full,
# `EskizError('eskiz send failed: HTTP 400')` — the body was parsed on the
# success path only, so the one sentence explaining the refusal was discarded and
# recovering it took a throwaway script against the live provider. This is that
# body's `message`, verbatim.
MODERATION_MESSAGE = (
    "Этот смс текст еще не прошёл модерацию. Сначала добавьте его через API - "
    "Шаблоны - Отправить шаблон или через кабинет my.eskiz.uz - СМС - Мои тексты."
)
CALLBACK_URL = "https://ruxsatnoma.example/api/v1/webhooks/eskiz/cbsecret"


def _logged_in(then: httpx.Response):
    """A handler that answers the login and then `then` for the send."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        return then

    return handler


async def test_a_refusal_carries_the_providers_own_reason(db):
    """The whole point of the fix: an administrator reading `last_error` must see
    WHY, not just that something was a 400. The equality also pins what is left
    OUT — `status` ("error", which the HTTP status already says) and `id`."""
    body = {
        "id": "b606e81b-aaaa-bbbb-cccc-dddddddddddd",
        "message": MODERATION_MESSAGE,
        "status": "error",
    }
    with pytest.raises(EskizError) as excinfo:
        await _sender(_logged_in(httpx.Response(400, json=body))).send(
            phone="998901234567", text="Salom", reference="7"
        )
    assert str(excinfo.value) == f"eskiz send failed: HTTP 400: {MODERATION_MESSAGE}"


async def test_a_body_that_echoes_the_request_leaks_neither_phone_nor_text(db):
    """Assume a provider that hands our whole request back in its error body —
    the guarantee has to be mechanical, not a reading of Eskiz's current shape.
    `last_error` is admin-visible AND logged, and for an OTP the text IS the
    code (lessons: *a sender's own diagnostics must never carry what it was
    sending*)."""
    text = "Tasdiqlash kodi: 481902"
    phone = "+998 90 123-45-67"
    body = {
        "message": (
            f"invalid request: message='{text}' to 998901234567 ({phone}), callback {CALLBACK_URL}"
        ),
        # An echo of the whole request: dropped by the allow-list before any
        # value-based filter has to catch it.
        "data": {"mobile_phone": "998901234567", "message": text, "callback_url": CALLBACK_URL},
        "status": "error",
    }
    with capture_logs() as logs:
        with pytest.raises(EskizError) as excinfo:
            await _sender(_logged_in(httpx.Response(400, json=body))).send(
                phone=phone, text=text, reference="7"
            )
    rendered = f"{excinfo.value!r} {excinfo.value} {logs}"
    for leaked in (text, "481902", "998901234567", phone, "cbsecret", CALLBACK_URL):
        assert leaked not in rendered, leaked
    # ...and the diagnosis survives: the status, plus a visible marker that
    # something was taken out rather than a silently shortened sentence.
    assert "HTTP 400" in str(excinfo.value)
    assert "invalid request" in str(excinfo.value)
    assert "[redacted]" in str(excinfo.value)


async def test_an_otp_code_quoted_on_its_own_is_caught_by_its_shape(db):
    """The exact-value filter cannot catch a provider that quotes only PART of
    what we sent. The digit scrub is what stands there — and for an OTP the part
    worth hiding is exactly a run of digits."""
    with pytest.raises(EskizError) as excinfo:
        await _sender(
            _logged_in(httpx.Response(400, json={"message": "code 481902 was rejected"}))
        ).send(phone="998901234567", text="Tasdiqlash kodi: 481902", reference="7")
    assert "481902" not in str(excinfo.value)
    assert "was rejected" in str(excinfo.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"content": b"<html><body>502 Bad Gateway</body></html>"}, id="html"),
        pytest.param({"content": b""}, id="empty"),
        pytest.param({"content": b"not json at all"}, id="text"),
        pytest.param({"content": b"\xff\xfe\x00 binary"}, id="binary"),
        pytest.param({"json": ["a", "list"]}, id="json-array"),
        pytest.param({"json": "a bare string"}, id="json-string"),
        pytest.param({"json": {"unexpected": "shape"}}, id="json-object-without-a-reason"),
    ],
)
async def test_a_body_that_is_not_a_usable_json_object_still_names_the_status(db, kwargs):
    """A provider may answer with anything, and a proxy in front of it certainly
    will. Parsing must never turn a delivery failure into a decode error — the
    outbox would retry either way, but `last_error` would then describe OUR
    parser instead of THEIR refusal."""
    with pytest.raises(EskizError) as excinfo:
        await _sender(_logged_in(httpx.Response(400, **kwargs))).send(
            phone="998901234567", text="hi", reference="7"
        )
    assert str(excinfo.value) == "eskiz send failed: HTTP 400"


async def test_a_giant_reason_is_capped(db):
    """An HTML error page or a JSON dump must not push the useful part out of
    `outbox_messages.last_error` (1000 chars) or out of the 200 the admin XLSX
    export prints."""
    with pytest.raises(EskizError) as excinfo:
        await _sender(_logged_in(httpx.Response(400, json={"message": "verbose " * 500}))).send(
            phone="998901234567", text="hi", reference="7"
        )
    assert "verbose" in str(excinfo.value)
    assert len(repr(excinfo.value)) <= 260


async def test_a_failed_send_is_logged_like_a_successful_one(db):
    """The success path has logged `sms.eskiz_send` with a masked phone since
    3.5; the failure had no line at all, so a send that never arrived left
    nothing in the process log to correlate."""
    with capture_logs() as logs:
        with pytest.raises(EskizError):
            await _sender(
                _logged_in(httpx.Response(400, json={"message": MODERATION_MESSAGE}))
            ).send(phone="+998 90 123-45-67", text="Salom", reference="900123")
    failed = [entry for entry in logs if entry["event"] == "sms.eskiz_send_failed"]
    assert len(failed) == 1
    assert failed[0]["phone"] == "***4567"  # same mask as sms.eskiz_send
    assert failed[0]["status"] == 400
    assert failed[0]["reference"] == "900123"
    assert failed[0]["reason"] == MODERATION_MESSAGE
    assert not [entry for entry in logs if entry["event"] == "sms.eskiz_send"]


async def test_a_failed_login_carries_the_reason_but_not_the_credentials(db):
    """`_login` discarded the body the same way `send` did. The credentials are
    put INSIDE `message` here on purpose: the allow-list alone would not save
    them, so this exercises the redaction rather than the field filter."""
    body = {"message": "Аккаунт заблокирован: robot@example.com / s3cret"}
    with pytest.raises(EskizError) as excinfo:
        await _sender(lambda request: httpx.Response(403, json=body)).send(
            phone="998901234567", text="hi", reference="7"
        )
    assert "eskiz login failed: HTTP 403" in str(excinfo.value)
    assert "Аккаунт заблокирован" in str(excinfo.value)
    assert "robot@example.com" not in str(excinfo.value)
    assert "s3cret" not in str(excinfo.value)


async def test_a_login_answering_something_other_than_json_raises_an_eskiz_error(db):
    """A 200 carrying a maintenance page used to raise `JSONDecodeError` straight
    out of `_login` — a delivery failure reported as a bug in our parser."""
    maintenance = httpx.Response(200, content=b"<html>maintenance</html>")
    with pytest.raises(EskizError) as excinfo:
        await _sender(lambda request: maintenance).send(
            phone="998901234567", text="hi", reference="7"
        )
    assert "eskiz login returned no token: HTTP 200" in str(excinfo.value)


async def test_a_transport_failure_names_what_httpx_said(db):
    """`type(exc).__name__` alone loses the errno; an asyncio-flavour timeout
    loses everything (lessons: log `repr(e)`, not `f"{e}"`). The text goes
    through the same scrub as a provider body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        raise httpx.ConnectError("[Errno 61] Connection refused")

    with pytest.raises(EskizError) as excinfo:
        await _sender(handler).send(phone="998901234567", text="hi", reference="7")
    assert "eskiz send transport error: ConnectError" in str(excinfo.value)
    assert "Connection refused" in str(excinfo.value)
