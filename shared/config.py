"""Application settings loaded from environment variables / .env file.

Single settings object shared by the API, the Telegram bot and the
Reflex admin panel.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Central application configuration."""

    # Database
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/digital_store"
    )
    echo_sql: bool = False

    # FastAPI server
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # Telegram bot (aiogram)
    bot_token: str = ""

    # Payments — Bakong KHQR
    # When payment_dev_mode is true, QR generation and verification are
    # simulated so the full purchase flow can be tested without a Bakong
    # merchant account.
    payment_dev_mode: bool = True
    bakong_token: str = ""
    bakong_account_id: str = ""  # e.g. "yourname@bank"
    merchant_name: str = "Bondom Account"
    merchant_city: str = "Phnom Penh"
    bakong_api_base: str = "https://api-bakong.nbc.gov.kh"

    # Reflex admin panel — empty password means nobody can log in.
    admin_password: str = ""

    model_config = SettingsConfigDict(
        # Use an absolute path so API/bot/admin load the same .env regardless of cwd.
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor so the .env file is parsed only once."""
    return Settings()


settings = get_settings()
