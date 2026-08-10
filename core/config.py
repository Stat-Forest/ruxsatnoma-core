"""Application settings, read from the environment.

Values come from environment variables, falling back to a local ``.env`` file
during development. Defaults describe the local docker-compose environment
only; every deployed environment sets the variables explicitly.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
Environment = Literal["local", "ci", "staging", "production"]


class Settings(BaseSettings):
    """Runtime configuration of the core service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # The .env file is shared with docker-compose and holds keys that are
        # none of the application's business.
        extra="ignore",
    )

    app_name: str = "ruxsatnoma-core"
    environment: Environment = "local"

    database_url: str = "postgresql+asyncpg://ruxsatnoma:ruxsatnoma@localhost:5442/ruxsatnoma_core"
    database_echo: bool = False
    database_pool_size: int = 10
    database_max_overflow: int = 5

    redis_url: str = "redis://localhost:6389/0"
    rabbitmq_url: str = "amqp://ruxsatnoma:ruxsatnoma@localhost:5682/"

    minio_endpoint: str = "localhost:9010"
    minio_access_key: str = "ruxsatnoma"
    minio_secret_key: str = ""
    minio_secure: bool = False

    log_level: LogLevel = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the settings singleton.

    Cached, because settings are immutable for the lifetime of the process and
    are read from disk on first access.
    """
    return Settings()
