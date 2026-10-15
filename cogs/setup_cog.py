"""
AdSentry Setup & Server Configuration Cog
Provides dynamic administrator commands to bind announcement and staff review channels.
"""

from __future__ import annotations

import logging
import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("AdSentry.Setup")


class SetupCog(commands.Cog, name="Server Setup"):
    """Cog handling dynamic guild configurations and channel routing."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="ad-setup",
        description="Configure the public announcement and staff review channels for AdSentry (Admin only)",
    )
    @app_commands.describe(
        announcement_channel="The public channel where approved announcements are published via webhook",
        review_channel="The private staff channel where new ad submissions appear for review",
    )
    @app_commands.default_permissions(administrator=True)
    async def ad_setup(
        self,
        interaction: discord.Interaction,
        announcement_channel: discord.TextChannel,
        review_channel: discord.TextChannel,
    ) -> None:
        """Configures and saves the guild's announcement and review channels in the database."""
        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be executed within a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        bot_member = guild.me

        # -------------------------------------------------------------
        # 1. Permission Pre-Flight Checks
        # -------------------------------------------------------------
        warnings: list[str] = []

        # Check announcement channel permissions
        ann_perms = announcement_channel.permissions_for(bot_member)
        if not ann_perms.manage_webhooks:
            warnings.append(f"⚠️ Missing `Manage Webhooks` permission in {announcement_channel.mention}.")
        if not ann_perms.send_messages:
            warnings.append(f"⚠️ Missing `Send Messages` permission in {announcement_channel.mention}.")
        if not ann_perms.embed_links:
            warnings.append(f"⚠️ Missing `Embed Links` permission in {announcement_channel.mention}.")
        if not ann_perms.create_public_threads:
            warnings.append(f"⚠️ Missing `Create Public Threads` permission in {announcement_channel.mention}.")

        # Check review channel permissions
        rev_perms = review_channel.permissions_for(bot_member)
        if not rev_perms.view_channel:
            warnings.append(f"⚠️ Missing `View Channel` permission in {review_channel.mention}.")
        if not rev_perms.send_messages:
            warnings.append(f"⚠️ Missing `Send Messages` permission in {review_channel.mention}.")
        if not rev_perms.embed_links:
            warnings.append(f"⚠️ Missing `Embed Links` permission in {review_channel.mention}.")

        # -------------------------------------------------------------
        # 2. Persist to Database
        # -------------------------------------------------------------
        try:
            await self.bot.db.set_guild_config(
                guild_id=guild.id,
                announcement_channel_id=announcement_channel.id,
                staff_review_channel_id=review_channel.id,
            )
        except Exception as e:
            logger.error(f"Failed to save /ad-setup configuration for guild {guild.id}: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Database Error: Could not save configuration. Error details: `{e}`",
                ephemeral=True,
            )
            return

        # -------------------------------------------------------------
        # 3. Build Response Embed
        # -------------------------------------------------------------
        embed = discord.Embed(
            title="⚙️ AdSentry Configuration Updated",
            description=f"AdSentry has been successfully configured for **{guild.name}**!",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="📢 Announcement Channel",
            value=f"{announcement_channel.mention}\n`ID: {announcement_channel.id}`",
            inline=True,
        )
        embed.add_field(
            name="🛡️ Staff Review Channel",
            value=f"{review_channel.mention}\n`ID: {review_channel.id}`",
            inline=True,
        )
        embed.add_field(
            name="Configured By",
            value=f"{interaction.user.mention}",
            inline=False,
        )

        if warnings:
            embed.add_field(
                name="⚠️ Permission Notices",
                value="\n".join(warnings) + "\n\n*Please ensure AdSentry's bot role has the required permissions for full functionality.*",
                inline=False,
            )

        embed.set_footer(text="AdSentry Configuration Manager • Users can now use /ad submit")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="ad-config",
        description="View the current AdSentry channel configuration for this server",
    )
    @app_commands.default_permissions(administrator=True)
    async def ad_config(self, interaction: discord.Interaction) -> None:
        """Displays currently active channel routing for the guild."""
        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be executed within a server.", ephemeral=True)
            return

        config = await self.bot.db.get_guild_config(interaction.guild.id)
        if not config or not config.get("announcement_channel_id"):
            embed = discord.Embed(
                title="⚙️ AdSentry Server Status",
                description=(
                    "This server has **not** been configured yet!\n\n"
                    "An administrator must run `/ad-setup` to assign the announcement and review channels."
                ),
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        ann_id = config.get("announcement_channel_id")
        rev_id = config.get("staff_review_channel_id")

        ann_channel = interaction.guild.get_channel(ann_id)
        rev_channel = interaction.guild.get_channel(rev_id)

        embed = discord.Embed(
            title="⚙️ Current AdSentry Configuration",
            description=f"Active advertising settings for **{interaction.guild.name}**:",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="📢 Announcement Channel",
            value=f"{ann_channel.mention if ann_channel else '⚠️ Channel Not Found'} (`{ann_id}`)",
            inline=True,
        )
        embed.add_field(
            name="🛡️ Staff Review Channel",
            value=f"{rev_channel.mention if rev_channel else '⚠️ Channel Not Found'} (`{rev_id}`)",
            inline=True,
        )
        embed.set_footer(text="To change channels, run /ad-setup")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Extension entry point for loading the Setup cog."""
    await bot.add_cog(SetupCog(bot))
