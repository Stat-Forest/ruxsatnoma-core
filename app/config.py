"""Настройки приложения: всё из окружения/.env, никаких хардкодов по коду."""

from functools import lru_cache
from typing import Literal

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
    session_absolute_hours: int = 12
    session_idle_minutes: int = 30
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15
    mfa_token_ttl_minutes: int = 5

    @model_validator(mode="after")
    def _forbid_default_secret_in_prod(self) -> Settings:
        # Защита от молчаливого старта в проде с дефолтным секретом.
        if self.app_env == "prod" and self.secret_key == "change-me":
            raise ValueError(
                "secret_key нельзя оставлять значением по умолчанию (change-me) в app_env=prod"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
