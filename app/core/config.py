from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    # Defaults match the benchmarked configuration (journie-data providers.json).
    gemini_temperature: float = Field(default=0.0, ge=0, le=2)
    gemini_max_output_tokens: int = Field(default=4096, ge=1)
    gemini_timeout_seconds: float = Field(default=60.0, gt=0)

    # POI retrieval store (Azure Database for PostgreSQL). Unset: POST /retrieval returns 503.
    database_url: SecretStr | None = None
    database_schema: str = "public"
    database_pool_max_size: int = Field(default=3, ge=1)
    database_timeout_seconds: float = Field(default=10.0, gt=0)
    # True behind a transaction-mode PgBouncer (Azure built-in, port 6432).
    database_pgbouncer: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
