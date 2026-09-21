"""
Application configuration.

Loaded from environment variables (see .env.example at repo root).
Never hard-code secrets or environment-specific values here.
"""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- App ---
    APP_NAME: str = "Treasury OS"
    ENVIRONMENT: str = "development"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = True

    # --- Security ---
    SECRET_KEY: str = "CHANGE_ME_IN_ENV"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7
    ALGORITHM: str = "HS256"
    # MFA-ready: flag reserved for future TOTP enforcement per user/role
    MFA_ENFORCED: bool = False

    # --- Database ---
    DATABASE_URL: str = (
        "postgresql+asyncpg://treasury:treasury@localhost:5432/treasury_os"
    )
    DATABASE_URL_SYNC: str = (
        "postgresql+psycopg2://treasury:treasury@localhost:5432/treasury_os"
    )

    # --- Redis / Celery ---
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # --- CORS ---
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # --- AI Copilot (deterministic engine is ALWAYS separate; see PRINCIPLE 9/10) ---
    ANTHROPIC_API_KEY: str | None = None
    AI_COPILOT_ENABLED: bool = False

    # --- Default base currencies (config-driven, NOT hard-coded business logic) ---
    DEFAULT_BASE_CURRENCIES: list[str] = ["NGN", "USD", "GBP", "EUR"]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
