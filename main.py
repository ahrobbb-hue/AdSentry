"""
AdSentry - Advanced Discord Announcement Review Bot
Main Entry Point & Bot Core Lifecycle Setup
"""

from __future__ import annotations

import os
import asyncio
from pathlib import Path
from aiohttp import web
import discord
from discord.ext import commands

from config import Config
from database import SupabaseDatabase
from utils import setup_logger

logger = setup_logger("AdSentry", level=Config.LOG_LEVEL)


async def start_health_check_server() -> web.AppRunner | None:
    """Starts an asynchronous HTTP server to satisfy Render Web Service port checks."""
    port_str = os.getenv("PORT")
    if not port_str:
        return None
    runner: web.AppRunner | None = None
    try:
        port = int(port_str)
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="AdSentry Bot is Online and Healthy!"))
        app.router.add_get("/health", lambda r: web.Response(text="OK"))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"Render Web Service health-check listening on port {port}")
        return runner
    except Exception as e:
        if runner is not None:
            await runner.cleanup()
        logger.warning(f"Could not bind health-check server on PORT {port_str}: {e}")
        return None


def register_persistent_views(bot: commands.Bot, active_post_ids: list[int]) -> None:
    """Registers the post-specific controls needed to handle interactions after restart."""
    from cogs.features import AdInterestView, TranslateView

    bot.add_view(TranslateView(post_id=0, ad_content="", ad_title=""))
    for post_id in dict.fromkeys([0, *active_post_ids]):
        bot.add_view(AdInterestView(post_id=post_id))


class AdSentryBot(commands.Bot):
    """Custom Discord Bot class managing extensions, database, and lifecycle."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
        )

        self.db = SupabaseDatabase()
        self.health_check_runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        """Executes asynchronous setup before the bot connects to Discord gateway."""
        logger.info("Initializing AdSentry Core Systems...")

        # 1. Initialize Supabase Database Connection
        database_ready = False
        try:
            await self.db.connect()
            database_ready = await self.db.check_connection()
            if database_ready:
                logger.info("Supabase database handler initialized.")
            else:
                logger.warning("Supabase is unreachable; database-backed features may be unavailable.")
        except Exception as e:
            logger.warning(f"Notice during Supabase initialization: {e}")

        # 2. Dynamic Cog / Extension Loading
        cogs_dir = Path(__file__).parent / "cogs"
        if cogs_dir.exists():
            for file in cogs_dir.glob("*.py"):
                if file.stem.startswith("__"):
                    continue
                cog_name = f"cogs.{file.stem}"
                try:
                    await self.load_extension(cog_name)
                    logger.info(f"Loaded extension: {cog_name}")
                except Exception as e:
                    logger.error(f"Failed to load extension {cog_name}: {e}", exc_info=True)

        # 3. Restore post-specific persistent views. Discord matches custom IDs
        # exactly; registering an ID of 0 does not cover buttons with a post ID.
        active_post_ids: list[int] = []
        if database_ready:
            try:
                active_post_ids = await self.db.get_active_post_ids()
            except Exception:
                logger.exception("Could not restore active ad button handlers from Supabase.")
        register_persistent_views(self, active_post_ids)
        logger.info("Registered persistent ad interest and translation views.")

        # 4. Synchronize Application (Slash) Commands
        if Config.GUILD_ID:
            guild = discord.Object(id=Config.GUILD_ID)
            # Keep commands scoped to the development guild only; mixing a global
            # registration with guild copies makes Discord show each command twice.
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            logger.info(f"Synced {len(synced)} slash command(s) to development guild [{Config.GUILD_ID}].")
        else:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} global slash command(s).")

        # 5. Start Render Web Service Health-Check Server (if PORT is set)
        self.health_check_runner = await start_health_check_server()

    async def on_ready(self) -> None:
        """Triggered when the bot is connected and ready."""
        logger.info(f"==================================================")
        logger.info(f"  AdSentry Online: {self.user} (ID: {self.user.id})")
        logger.info(f"  Connected to {len(self.guilds)} guild(s)")
        logger.info(f"  Gateway Latency: {self.latency * 1000:.1f}ms")
        logger.info(f"==================================================")

        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="announcements | /ad",
        )
        await self.change_presence(status=discord.Status.online, activity=activity)

        # Remove stale guild-scoped copies left by older versions. Current
        # production commands are registered globally (or only in GUILD_ID).
        for guild in self.guilds:
            if Config.GUILD_ID and guild.id == Config.GUILD_ID:
                continue
            try:
                self.tree.clear_commands(guild=guild)
                synced_guild = await self.tree.sync(guild=guild)
                if synced_guild:
                    logger.info(f"Removed {len(synced_guild)} stale guild-scoped slash command(s) from {guild.name} ({guild.id}).")
            except Exception as e:
                logger.warning(f"Could not clean stale guild-scoped commands in guild {guild.id}: {e}")

    async def close(self) -> None:
        """Gracefully cleanup connections upon bot shutdown."""
        logger.info("Shutting down AdSentry...")
        if self.health_check_runner is not None:
            await self.health_check_runner.cleanup()
            self.health_check_runner = None
        await self.db.close()
        await super().close()


async def main() -> None:
    """Application entrypoint with validation and exception handling."""
    missing_configs = Config.validate()
    if missing_configs:
        for err in missing_configs:
            logger.warning(f"Config Warning: {err}")
        if not Config.DISCORD_TOKEN:
            logger.error("Cannot launch bot: DISCORD_TOKEN is missing. Please populate .env file.")
            return

    bot = AdSentryBot()
    async with bot:
        await bot.start(Config.DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Process terminated by user.")
