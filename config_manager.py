"""Configuration management with .env validation and type safety."""

import os
from dataclasses import dataclass
from typing import Self

from dotenv import load_dotenv

# Official API servers per forum.json (spec): production + documented alternates
DEFAULT_API_BASE_URL = "https://api.lolz.live"


@dataclass(frozen=True, slots=True)
class BotConfig:
    api_token: str
    img_url: str
    author_url: str


@dataclass(frozen=True, slots=True)
class APIConfig:
    base_url: str
    auth_token: str
    batch_size: int


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    path: str


@dataclass(frozen=True, slots=True)
class SchedulingConfig:
    bump_interval_minutes: float
    bump_delay_seconds: float
    enable_auto_bump: bool
    scheduler_tick_seconds: float


@dataclass(frozen=True, slots=True)
class Config:
    bot: BotConfig
    api: APIConfig
    database: DatabaseConfig
    scheduling: SchedulingConfig
    admin_user_id: int

    @classmethod
    def load(cls, env_path: str = ".env") -> Self:
        if not os.path.exists(env_path):
            raise FileNotFoundError(f".env file not found: {env_path}")

        load_dotenv(env_path)

        bot_token = os.getenv("BOT_API_TOKEN", "")
        api_token = os.getenv("API_AUTH_TOKEN", "")

        try:
            admin_user_id = int(os.getenv("ADMIN_USER_ID", "0"))
        except ValueError:
            raise ValueError(
                "ADMIN_USER_ID must be an integer — set your Telegram user ID in .env"
            )

        cls._validate_tokens(bot_token, api_token, admin_user_id)

        bot = BotConfig(
            api_token=bot_token,
            img_url=os.getenv("BOT_IMG_URL", ""),
            author_url=os.getenv("BOT_AUTHOR_URL", ""),
        )

        batch_size = int(os.getenv("API_BATCH_SIZE", "10"))
        if batch_size < 1 or batch_size > 10:
            raise ValueError("API_BATCH_SIZE must be between 1 and 10")

        api = APIConfig(
            base_url=os.getenv("API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/"),
            auth_token=api_token,
            batch_size=batch_size,
        )

        db = DatabaseConfig(
            path=os.getenv("DB_PATH", "threads.db"),
        )

        interval_minutes = float(os.getenv("BUMP_INTERVAL_MINUTES", "60"))
        if interval_minutes <= 0:
            raise ValueError("BUMP_INTERVAL_MINUTES must be positive")

        tick = float(os.getenv("SCHEDULER_TICK_SECONDS", "60"))
        if tick <= 0:
            raise ValueError("SCHEDULER_TICK_SECONDS must be positive")

        scheduling = SchedulingConfig(
            bump_interval_minutes=interval_minutes,
            bump_delay_seconds=float(os.getenv("BUMP_DELAY_SECONDS", "2")),
            enable_auto_bump=os.getenv("ENABLE_AUTO_BUMP", "true").lower() == "true",
            scheduler_tick_seconds=tick,
        )

        return cls(
            bot=bot,
            api=api,
            database=db,
            scheduling=scheduling,
            admin_user_id=admin_user_id,
        )

    @staticmethod
    def _validate_tokens(bot_token: str, api_token: str, admin_user_id: int) -> None:
        if not bot_token or "YOUR_" in bot_token:
            raise ValueError(
                "BOT_API_TOKEN not configured — set your Telegram bot token in .env"
            )
        if not api_token or "YOUR_" in api_token:
            raise ValueError(
                "API_AUTH_TOKEN not configured — set your Lolz API token in .env"
            )
        if admin_user_id == 0:
            raise ValueError(
                "ADMIN_USER_ID not configured — set your Telegram user ID in .env"
            )
