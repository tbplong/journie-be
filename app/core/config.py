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
    # A second AI Studio account (free tier: separate daily quota). Embedding calls fall back to
    # the other key on HTTP 429; /chat uses gemini_api_key only.
    gemini_api_key2: SecretStr | None = None
    gemini_embedding_model: str = "gemini-embedding-2"
    gemini_embedding_timeout_seconds: float = Field(default=5.0, gt=0)
    # POST /assistant has no login and shares the Gemini key with /chat and /retrieval: requests
    # per minute per client IP and for all clients together (per process); over it: 429.
    assistant_client_rate_per_minute: int = Field(default=6, ge=1)
    assistant_total_rate_per_minute: int = Field(default=20, ge=1)
    # Someone is waiting on the chat: the whole answer gets this many seconds, and when the main
    # model is overloaded (500/502/503), too slow or unreachable, the fallback model answers
    # instead. Unset fallback: one model, the generic retry. /chat is not affected.
    assistant_timeout_seconds: float = Field(default=20.0, gt=0)
    assistant_fallback_model: str | None = "gemini-3.5-flash"

    # POI retrieval store (Azure Database for PostgreSQL). Unset: POST /retrieval returns 503.
    database_url: SecretStr | None = None
    database_schema: str = "public"
    database_pool_max_size: int = Field(default=3, ge=1)
    database_timeout_seconds: float = Field(default=10.0, gt=0)
    # True behind a transaction-mode PgBouncer (Azure built-in, port 6432).
    database_pgbouncer: bool = False

    # Road distances for POST /routing. Unset or failing: straight-line estimate + a warning.
    # Self-hosted in production; the public demo (router.project-osrm.org) is for dev only.
    osrm_url: str | None = None
    osrm_timeout_seconds: float = Field(default=3.0, gt=0)
    routing_time_limit_seconds: float = Field(default=6.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
