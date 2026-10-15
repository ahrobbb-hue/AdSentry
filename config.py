"""
AdSentry Configuration Module
Loads and validates environment variables from .env with fallback defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent

# Load .env file
load_dotenv(BASE_DIR / ".env")


class Config:
    """Central configuration class for AdSentry."""

    # Discord Credentials
    DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "").strip()
    GUILD_ID: int | None = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None

    # Channel IDs
    STAFF_REVIEW_CHANNEL_ID: int = int(os.getenv("STAFF_REVIEW_CHANNEL_ID", "0"))
    ANNOUNCEMENT_CHANNEL_ID: int = int(os.getenv("ANNOUNCEMENT_CHANNEL_ID", "0"))

    # Permissions
    _raw_staff_roles = os.getenv("STAFF_ROLE_IDS", "")
    STAFF_ROLE_IDS: list[int] = [
        int(r.strip()) for r in _raw_staff_roles.split(",") if r.strip().isdigit()
    ]

    # Database
    DATABASE_PATH: Path = BASE_DIR / os.getenv("DATABASE_PATH", "database/adsentry.db")

    # Moderation & Cooldowns
    COOLDOWN_HOURS: int = int(os.getenv("COOLDOWN_HOURS", "24"))
    _raw_forbidden = os.getenv("FORBIDDEN_WORDS", "discord.gg/,t.me/,@everyone,@here")
    FORBIDDEN_WORDS: list[str] = [
        word.strip().lower() for word in _raw_forbidden.split(",") if word.strip()
    ]
    EXPIRY_SWEEPER_INTERVAL: int = int(os.getenv("EXPIRY_SWEEPER_INTERVAL", "60"))

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    @classmethod
    def validate(cls) -> list[str]:
        """Validates critical configuration values and returns a list of missing items."""
        missing = []
        if not cls.DISCORD_TOKEN:
            missing.append("DISCORD_TOKEN is missing or empty.")
        if cls.STAFF_REVIEW_CHANNEL_ID == 0:
            missing.append("STAFF_REVIEW_CHANNEL_ID must be configured.")
        if cls.ANNOUNCEMENT_CHANNEL_ID == 0:
            missing.append("ANNOUNCEMENT_CHANNEL_ID must be configured.")
        return missing
