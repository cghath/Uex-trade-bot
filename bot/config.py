"""Centralized configuration loaded from environment variables / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root regardless of current working directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Config:
    discord_bot_token: str
    discord_dev_guild_id: int | None
    uex_app_token: str
    uex_secret_key: str | None
    database_path: Path

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError(
                "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
            )

        uex_app_token = os.getenv("UEX_APP_TOKEN", "").strip()
        if not uex_app_token:
            raise ConfigError(
                "UEX_APP_TOKEN is not set. Create an app on the UEX 'My Apps' page and "
                "put its Bearer token in .env."
            )

        guild_id_raw = os.getenv("DISCORD_DEV_GUILD_ID", "").strip()
        guild_id = int(guild_id_raw) if guild_id_raw else None

        secret_key = os.getenv("UEX_SECRET_KEY", "").strip() or None

        db_path_raw = os.getenv("DATABASE_PATH", "data/uexbot.sqlite3").strip()
        db_path = Path(db_path_raw)
        if not db_path.is_absolute():
            db_path = _PROJECT_ROOT / db_path

        return cls(
            discord_bot_token=token,
            discord_dev_guild_id=guild_id,
            uex_app_token=uex_app_token,
            uex_secret_key=secret_key,
            database_path=db_path,
        )


PROJECT_ROOT = _PROJECT_ROOT
