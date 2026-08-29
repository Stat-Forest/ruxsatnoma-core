"""The Eskiz client against httpx.MockTransport — never the live provider (plan
03.5 ruling 2). The most important assertion here is the last one: a failure must
not leak the message text, which for OTP is the code itself (3.4 carry-over)."""

import httpx
import pytest

from app.config import Settings
from app.modules.integrations.adapters.sms import EskizError, EskizSmsSender

SETTINGS = Settings(
    eskiz_base_url="https://notify.test",
    eskiz_email="robot@example.com",
    eskiz_password="s3cret",
    eskiz_sender="4546",
    eskiz_callback_secret="cbsecret",
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
    assert await sender.send(phone="998901234567", text="hi", reference="ref-1") == "9001"
    assert await sender.send(phone="998901234567", text="hi", reference="ref-2") == "9001"
    assert calls.count("/api/auth/login") == 1


async def test_sends_the_documented_fields(db):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        captured.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"id": "42"})

    await _sender(handler).send(phone="+998 90 123-45-67", text="Матн", reference="ref-9")
    assert captured["mobile_phone"] == "998901234567"  # digits only, no '+' (design/04 §4)
    assert captured["message"] == "Матн"
    assert captured["from"] == "4546"
    assert captured["user_sms_id"] == "ref-9"
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

    assert await _sender(handler).send(phone="998901234567", text="hi", reference="r") == "77"
    assert seen.count("/api/auth/login") == 2


async def test_a_persistent_401_raises_rather_than_looping(db):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        return httpx.Response(401, json={"message": "nope"})

    with pytest.raises(EskizError):
        await _sender(handler).send(phone="998901234567", text="hi", reference="r")


async def test_a_failed_login_raises_without_the_password(db):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "bad credentials"})

    with pytest.raises(EskizError) as excinfo:
        await _sender(handler).send(phone="998901234567", text="hi", reference="r")
    assert "s3cret" not in str(excinfo.value)


async def test_an_error_never_leaks_the_message_text_or_the_phone(db):
    """`last_error` is admin-visible and logged; for an OTP the message text IS the
    code (3.4 carry-over, ruling 9 of plan 03.4 / ruling 2 here)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"data": {"token": "tok"}})
        return httpx.Response(400, json={"message": "invalid"})

    with pytest.raises(EskizError) as excinfo:
        await _sender(handler).send(phone="998901234567", text="code 123456", reference="r")
    rendered = f"{excinfo.value!r} {excinfo.value}"
    assert "123456" not in rendered
    assert "998901234567" not in rendered
    assert "400" in rendered  # the status IS useful and carries nothing sensitive


async def test_a_transport_timeout_becomes_an_eskiz_error(db):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(EskizError):
        await _sender(handler).send(phone="998901234567", text="hi", reference="r")


async def test_real_mode_returns_the_eskiz_sender(monkeypatch):
    from app.config import get_settings
    from app.modules.integrations.adapters import sms

    monkeypatch.setenv("SMS_MODE", "real")
    monkeypatch.setenv("ESKIZ_EMAIL", "robot@example.com")
    monkeypatch.setenv("ESKIZ_PASSWORD", "p")
    monkeypatch.setenv("ESKIZ_SENDER", "4546")
    monkeypatch.setenv("ESKIZ_CALLBACK_SECRET", "c")
    get_settings.cache_clear()
    sms.get_sms_sender.cache_clear()
    try:
        assert isinstance(sms.get_sms_sender(), sms.EskizSmsSender)
    finally:
        get_settings.cache_clear()
        sms.get_sms_sender.cache_clear()
