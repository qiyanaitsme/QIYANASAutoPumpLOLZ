"""Configuration management with validation and type safety."""

import json
from pathlib import Path
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True)
class BotConfig:
    """Telegram bot configuration."""
    api_token: str
    img_url: str
    author_url: str


@dataclass(frozen=True, slots=True)
class APIConfig:
    """Lolz API configuration."""
    base_url: str
    auth_token: str
    batch_size: int


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """Database configuration."""
    path: str


@dataclass(frozen=True, slots=True)
class SchedulingConfig:
    """Scheduling configuration."""
    bump_interval_hours: float
    bump_delay_seconds: float
    enable_auto_bump: bool


@dataclass(frozen=True, slots=True)
class Config:
    """Application configuration."""
    bot: BotConfig
    api: APIConfig
    database: DatabaseConfig
    scheduling: SchedulingConfig
    
    @classmethod
    def load(cls, config_path: str = "config.json") -> Self:
        """Load and validate configuration from JSON file."""
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        
        # Validate required fields
        cls._validate_config(data)
        
        return cls(
            bot=BotConfig(
                api_token=data["bot"]["api_token"],
                img_url=data["bot"]["img_url"],
                author_url=data["bot"]["author_url"]
            ),
            api=APIConfig(
                base_url=data["api"]["base_url"].rstrip("/"),
                auth_token=data["api"]["auth_token"],
                batch_size=int(data["api"].get("batch_size", 10))
            ),
            database=DatabaseConfig(
                path=data["database"]["path"]
            ),
            scheduling=SchedulingConfig(
                bump_interval_hours=float(data["scheduling"]["bump_interval_hours"]),
                bump_delay_seconds=float(data["scheduling"].get("bump_delay_seconds", 2)),
                enable_auto_bump=bool(data["scheduling"]["enable_auto_bump"])
            )
        )
    
    @staticmethod
    def _validate_config(data: dict) -> None:
        """Validate configuration structure and required fields."""
        required_fields = {
            "bot": ["api_token", "img_url", "author_url"],
            "api": ["base_url", "auth_token"],
            "database": ["path"],
            "scheduling": ["bump_interval_hours", "enable_auto_bump"]
        }
        
        for section, fields in required_fields.items():
            if section not in data:
                raise ValueError(f"Missing config section: {section}")
            
            for field in fields:
                if field not in data[section]:
                    raise ValueError(f"Missing config field: {section}.{field}")
                
                value = data[section][field]
                if isinstance(value, str) and not value.strip():
                    raise ValueError(f"Empty config field: {section}.{field}")
        
        # Validate token formats
        bot_token = data["bot"]["api_token"]
        if "YOUR_" in bot_token or not bot_token:
            raise ValueError("bot.api_token not configured - please set your Telegram bot token")
        
        api_token = data["api"]["auth_token"]
        if "YOUR_" in api_token or not api_token:
            raise ValueError("api.auth_token not configured - please set your Lolz API token")
        
        # Validate interval
        interval = data["scheduling"]["bump_interval_hours"]
        if not isinstance(interval, (int, float)) or interval <= 0:
            raise ValueError("scheduling.bump_interval_hours must be positive number")
        
        # Validate batch size
        batch_size = data["api"].get("batch_size", 10)
        if not isinstance(batch_size, int) or batch_size < 1 or batch_size > 10:
            raise ValueError("api.batch_size must be between 1 and 10")
