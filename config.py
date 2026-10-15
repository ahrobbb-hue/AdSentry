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

    # Supabase Credentials
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "https://ydwsqjfethnwebkdsmd.supabase.co").strip()
    SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY", "").strip()
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    # Prefer Service Role Key for server-side bot backend operations (bypasses RLS)
    @classmethod
    def get_supabase_key(cls) -> str:
        return cls.SUPABASE_SERVICE_ROLE_KEY or cls.SUPABASE_ANON_KEY

    # Channel IDs
    STAFF_REVIEW_CHANNEL_ID: int = int(os.getenv("STAFF_REVIEW_CHANNEL_ID", "0"))
    ANNOUNCEMENT_CHANNEL_ID: int = int(os.getenv("ANNOUNCEMENT_CHANNEL_ID", "0"))

    # Permissions
    _raw_staff_roles = os.getenv("STAFF_ROLE_IDS", "")
    STAFF_ROLE_IDS: list[int] = [
        int(r.strip()) for r in _raw_staff_roles.split(",") if r.strip().isdigit()
    ]

    # Moderation & Cooldowns
    COOLDOWN_HOURS: int = int(os.getenv("COOLDOWN_HOURS", "24"))
    _raw_forbidden = os.getenv("FORBIDDEN_WORDS", "discord.gg/,t.me/,invite.gg/,@everyone,@here")
    FORBIDDEN_WORDS: list[str] = [
        word.strip().lower() for word in _raw_forbidden.split(",") if word.strip()
    ]
    EXPIRY_SWEEPER_INTERVAL: int = int(os.getenv("EXPIRY_SWEEPER_INTERVAL", "60"))

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # AI Translation (Google Gemini Flash)
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()

    @classmethod
    def validate(cls) -> list[str]:
        """Validates critical configuration values and returns a list of missing items."""
        missing = []
        if not cls.DISCORD_TOKEN:
            missing.append("DISCORD_TOKEN is missing or empty.")
        if not cls.SUPABASE_URL or not cls.get_supabase_key():
            missing.append("SUPABASE_URL or API keys are missing.")
        return missing
