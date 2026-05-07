from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://fx:fx_secret@localhost:5432/fx_db"
    rate_api_url: str = "https://api.exchangerate-api.com/v4/latest"
    rate_stale_seconds: int = 600
    rate_refresh_interval_seconds: int = 300
    quote_ttl_seconds: int = 60
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    environment: str = "development"


settings = Settings()
