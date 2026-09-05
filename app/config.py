"""Настройки приложения: всё из окружения/.env, никаких хардкодов по коду."""

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://ruxsatnoma:ruxsatnoma@localhost:5432/ruxsatnoma"
    database_url_test: str = (
        "postgresql+asyncpg://ruxsatnoma:ruxsatnoma@localhost:5432/ruxsatnoma_test"
    )
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_format: Literal["console", "json"] = "console"
    secret_key: str = "change-me"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "ruxsatnoma"
    s3_secret_key: str = "ruxsatnoma-secret"
    s3_bucket: str = "ruxsatnoma"
    cookie_secure: bool | None = None
    cors_origins: list[str] = []
    oneid_mode: Literal["mock", "real"] = "mock"
    eimzo_mode: Literal["mock", "real"] = "mock"
    sms_mode: Literal["mock", "real"] = "mock"
    email_mode: Literal["mock", "real"] = "mock"
    # Externally reachable origin — Eskiz posts delivery reports back to it.
    public_base_url: str = "http://localhost:8000"
    eskiz_base_url: str = "https://notify.eskiz.uz"
    eskiz_email: str = ""
    eskiz_password: str = ""
    eskiz_sender: str = ""  # approved alias, e.g. "4546" (tz/12 #9)
    eskiz_callback_secret: str = ""  # the ONLY authentication on the callback route
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    # The permit series (design/02 § permit_counters): CYRILLIC CAPITAL А
    # (U+0410), NOT Latin A (U+0041). The two are indistinguishable on screen and
    # different bytes to Postgres — migration 0019 seeds the counter row under the
    # Cyrillic letter, so a Latin A here makes `UPDATE permit_counters ... WHERE
    # series = :s RETURNING last_number` match zero rows and return None.
    # `permits.repo.next_number` refuses rather than carrying on, so the
    # misconfiguration surfaces as a loud 500 naming the series instead of a
    # permit with no number. A second series is a second counter row, not a
    # migration — this setting is what points issuance at it.
    permit_series: str = "А"
    workers_mode: Literal["embedded", "off"] = "embedded"
    oneid_redirect_uri: str = "http://localhost:8000/api/v1/auth/oneid/callback"
    # Where the OneID callback sends the browser once the session cookies are
    # set. It is a REDIRECT TARGET, not an origin to trust: the callback is
    # reached by a browser returning from an identity provider, and answering
    # it with a JSON body renders `{"user": ...}` as text on the screen.
    admin_base_url: str = "http://localhost:5173"
    oneid_scope: str = "mock-scope"
    # Payme JSON-RPC server (stage 3.10a, design/04 §3). "mock" points at
    # Payme's own SANDBOX cashbox key, not a fake — see
    # integrations/adapters/payme.py's own docstring.
    payme_mode: Literal["mock", "real"] = "mock"
    payme_merchant_id: str | None = None
    payme_cashbox_key: str | None = None
    # External checks (stage 3.9b, tz/09 rows 5-6): both have "medium priority"
    # and no verified contract, unlike the four systems above, and `docs/plan.md`
    # stage 5 schedules neither as real — the gap is indefinite, not a countdown.
    # Deliberately absent from `_forbid_default_secret_in_prod`'s mocked list:
    # PROD MAY START in mock (blocking startup would block every deploy for an
    # integration nothing schedules), but it must not be able to ANSWER a check
    # from a fixture — `integrations/adapters/vet.py`/`cadastre.py`'s own
    # `get_adapter()` refuses (raises `NotImplementedError`) whenever
    # `app_env=prod`, mock or not, so a production check can only go through the
    # paper fallback (`applications.service.add_check`'s `source=
    # "manual_fallback"`, under maker-checker) — never a fixture's fixed
    # verdict indistinguishable from a genuine registry answer. Fix round 1,
    # 2026-09-05 controller ruling; the "prod refuses to start with any mock"
    # rule itself is the 2026-08-28 stage 3.2b entry, ruling 2 — not decision #40.
    vet_mode: Literal["mock", "real"] = "mock"
    cadastre_mode: Literal["mock", "real"] = "mock"

    @model_validator(mode="after")
    def _forbid_default_secret_in_prod(self) -> Settings:
        # Защита от молчаливого старта в проде с дефолтным секретом.
        if self.app_env == "prod" and self.secret_key == "change-me":
            raise ValueError(
                "secret_key нельзя оставлять значением по умолчанию (change-me) в app_env=prod"
            )
        if self.app_env == "prod" and self.s3_secret_key == "ruxsatnoma-secret":
            raise ValueError("s3_secret_key must not keep the default value in app_env=prod")
        if self.app_env == "prod":
            mocked = [
                name
                for name in ("oneid_mode", "eimzo_mode", "sms_mode", "email_mode", "payme_mode")
                if getattr(self, name) == "mock"
            ]
            if mocked:
                raise ValueError(
                    f"mock adapters are not allowed in app_env=prod: {', '.join(mocked)}"
                )
        if self.sms_mode == "real" and not all(
            (self.eskiz_email, self.eskiz_password, self.eskiz_sender, self.eskiz_callback_secret)
        ):
            raise ValueError(
                "sms_mode=real requires eskiz_email, eskiz_password, eskiz_sender "
                "and eskiz_callback_secret"
            )
        if self.sms_mode == "real" and _is_local_origin(self.public_base_url):
            # The only Eskiz setting whose DEFAULT looks like a working value. Get it
            # wrong and SMS still goes out while every delivery report is posted into
            # the void: each notification sits at `sent` forever, with no error
            # anywhere to say so.
            raise ValueError(
                "sms_mode=real requires public_base_url to be the externally reachable "
                "origin — Eskiz posts its delivery reports back to it, and a local "
                f"origin loses every one of them silently (got {self.public_base_url!r})"
            )
        if self.email_mode == "real" and not all((self.smtp_host, self.smtp_from)):
            raise ValueError("email_mode=real requires smtp_host and smtp_from")
        if self.payme_mode == "real" and not all((self.payme_merchant_id, self.payme_cashbox_key)):
            raise ValueError("payme_mode=real requires payme_merchant_id and payme_cashbox_key")
        return self

    def resolve_cookie_secure(self) -> bool:
        """cookie_secure=None (default) follows app_env; an explicit value overrides it
        (e.g. https on a non-prod staging deploy)."""
        return self.cookie_secure if self.cookie_secure is not None else self.app_env == "prod"

    def resolve_cookie_samesite(self) -> Literal["lax", "none"]:
        """Cross-origin adminka (ruling 3) needs SameSite=None, which browsers accept
        only together with Secure — so a non-secure deployment stays on Lax."""
        if self.cors_origins and self.resolve_cookie_secure():
            return "none"
        return "lax"


# Not a bind address — the set of hostnames a PUBLIC_BASE_URL must NOT resolve to.
_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "0.0.0.0", "::1"})  # noqa: S104  # nosec B104


def _is_local_origin(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.endswith(".localhost")


@lru_cache
def get_settings() -> Settings:
    return Settings()
