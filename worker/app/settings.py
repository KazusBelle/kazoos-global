from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://kazus:kazus@db:5432/kazus"
    # The worker wakes on every M5 candle boundary (see runner.main). This
    # delay is added after the boundary so Binance has finalised the closed
    # candle before we fetch. Kept small — must stay well under a minute.
    close_check_delay_sec: int = 10
    refresh_interval_sec: int = 300  # retained for config compatibility
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_timeframes: str = "D1,H1,H1-M5"  # comma-separated

    d1_bar_limit: int = 500
    h1_bar_limit: int = 900

    # Headless chart renderer (Stage 4b infra, wired in Stage 5).
    # The worker mints a short-lived HS256 token using the same secret as
    # the backend, so the /chart-export route can hit /api/chart/{symbol}.
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    chart_render_base_url: str = "http://frontend"
    chart_render_username: str = "kazus"
    chart_render_token_ttl_min: int = 15
    # X-zoom for the setup (LTF) chart only: shrinks the visible bar window
    # around the right edge so the drawn FVG/setup is rendered larger in the
    # Telegram image. 1.0 = no zoom. Env-tunable without a code redeploy.
    chart_render_zoom: float = 3.0


@lru_cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()
