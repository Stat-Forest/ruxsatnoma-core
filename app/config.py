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
    workers_mode: Literal["embedded", "off"] = "embedded"
    oneid_redirect_uri: str = "http://localhost:8000/api/v1/auth/oneid/callback"
    oneid_scope: str = "mock-scope"

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
                for name in ("oneid_mode", "eimzo_mode", "sms_mode", "email_mode")
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
