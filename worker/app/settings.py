from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://kazus:kazus@db:5432/kazus"
    refresh_interval_sec: int = 300
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_timeframes: str = "D1,H1"  # comma-separated

    d1_bar_limit: int = 500
    h1_bar_limit: int = 900


@lru_cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()
