import json
from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Kazus Screener"
    api_prefix: str = "/api"
    cors_origins: List[str] = ["*"]

    database_url: str = "postgresql+psycopg://kazus:kazus@db:5432/kazus"

    jwt_secret: str = "change-me-please-32-bytes-minimum-xxxxxx"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 24

    admin_username: str = "kazus"
    admin_password: str = "globalocal"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    refresh_interval_sec: int = 300
    server_metrics_interval_sec: int = 60
    server_metrics_retention_hours: int = 24 * 7
    default_coins: str = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT"

    # Observation Period anchor (operational-visibility only). Env-overridable.
    t0_new: str = "2026-06-13T08:47:38Z"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors(cls, v):
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return ["*"]
            if s.startswith("["):
                try:
                    return json.loads(s)
                except Exception:
                    pass
            return [p.strip() for p in s.split(",") if p.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
