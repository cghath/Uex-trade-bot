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
    scanner_steal_threshold: float

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

        # Undervalued Scanner (bot/cogs/scanner.py): how far below an item's 30-day average
        # a sell listing must be priced to count as a "steal", e.g. 0.65 = 65% off. UEX's own
        # API docs (Pricing Parameters table) document a 60% "variation tolerance" for Ore
        # Sales as normal, expected price variance - not an anomaly. Defaulting below that
        # (e.g. the previous 0.20) mostly just flags routine market noise; 0.65 sits above
        # UEX's own documented normal-variance band, so a flagged listing is priced outside
        # what UEX itself would consider ordinary.
        threshold_raw = os.getenv("SCANNER_STEAL_THRESHOLD", "0.65").strip()
        try:
            scanner_steal_threshold = float(threshold_raw)
        except ValueError:
            raise ConfigError(f"SCANNER_STEAL_THRESHOLD must be a number (e.g. 0.65), got '{threshold_raw}'.")

        return cls(
            discord_bot_token=token,
            discord_dev_guild_id=guild_id,
            uex_app_token=uex_app_token,
            uex_secret_key=secret_key,
            database_path=db_path,
            scanner_steal_threshold=scanner_steal_threshold,
        )


PROJECT_ROOT = _PROJECT_ROOT
