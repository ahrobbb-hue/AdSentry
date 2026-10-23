"""
AdSentry Setup & Server Configuration Cog
Provides dynamic administrator commands to bind announcement and staff review channels.
"""

from __future__ import annotations

import logging
import discord
from discord import app_commands
from discord.ext import commands
from config import Config
from cogs.moderation import is_admin

logger = logging.getLogger("AdSentry.Setup")


class SetupCog(commands.Cog, name="Server Setup"):
    """Cog handling dynamic guild configurations and channel routing."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="setup", description="إنشاء وربط قنوات AdSentry ولوحة العمليات")
    @app_commands.default_permissions(administrator=True)
    async def setup_server(self, interaction: discord.Interaction) -> None:
        """Create or reuse AdSentry channels and wire them to the bot."""
        guild = interaction.guild
        if guild is None or not is_admin(interaction.user):
            await interaction.response.send_message("هذا الأمر متاح لمالك السيرفر أو المدير فقط.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            await interaction.followup.send("يحتاج البوت إلى صلاحية **Manage Channels** لإنشاء القنوات.", ephemeral=True)
            return

        public_category_name = "AdSentry · عام"
        staff_category_name = "AdSentry · الإدارة"
        definitions = {
            "announcements": ("إعلانات-البوت", False),
            "review": ("مراجعة-الإعلانات-والطاقم", True),
            "app1": ("طلبات-الانضمام-1", True),
            "app2": ("طلبات-الانضمام-2", True),
            "app3": ("طلبات-الانضمام-3", True),
            "panel": ("مركز-العمليات", True),
        }

        async def category(name: str, private: bool) -> discord.CategoryChannel:
            found = discord.utils.get(guild.categories, name=name)
            if found:
                return found
            options = {"overwrites": {guild.default_role: discord.PermissionOverwrite(view_channel=False)}} if private else {}
            return await guild.create_category(name, reason="AdSentry channel setup", **options)

        stage = "إنشاء الفئات والقنوات"
        try:
            public_cat = await category(public_category_name, False)
            staff_cat = await category(staff_category_name, True)
            admin_overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True, manage_messages=True),
            }
            panel_overwrites = dict(admin_overwrites)
            for role_id in Config.STAFF_ROLE_IDS:
                role = guild.get_role(role_id)
                if role:
                    panel_overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
            public_overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
                bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, manage_webhooks=True, create_public_threads=True),
            }

            channels: dict[str, discord.TextChannel] = {}
            for key, (name, private) in definitions.items():
                parent = staff_cat if private else public_cat
                private_overwrites = panel_overwrites if key == "panel" else admin_overwrites
                channel = discord.utils.get(parent.text_channels, name=name)
                if channel is None:
                    channel = await guild.create_text_channel(
                        name, category=parent,
                        overwrites=private_overwrites if private else public_overwrites,
                        reason="AdSentry channel setup",
                    )
                else:
                    # Reassert bot-managed access controls on channels inside our named categories.
                    await channel.edit(overwrites=private_overwrites if private else public_overwrites, reason="AdSentry channel setup")
                channels[key] = channel

            stage = "حفظ قناة الإعلانات والمراجعة في قاعدة البيانات"
            await self.bot.db.set_guild_config(guild_id=guild.id, announcement_channel_id=channels["announcements"].id, staff_review_channel_id=channels["review"].id)
            client = await self.bot.db.get_client()
            stage = "حفظ قنوات مراجعة طلبات الانضمام"
            await client.rpc("set_recruitment_review_channels", {
                "p_guild_id": guild.id,
                "p_channel_ids": [channels[k].id for k in ("app1", "app2", "app3")],
                "p_configured_by": interaction.user.id,
            }).execute()

            stage = "إنشاء لوحة العمليات وربطها"
            dashboard = self.bot.get_cog("DashboardCog")
            if dashboard is None:
                raise RuntimeError("DashboardCog is not loaded")
            # Reuse the existing persistent panel if it already lives in the configured channel.
            saved = (await client.table("staff_dashboard_panels").select("channel_id,message_id").eq("guild_id", guild.id).limit(1).execute()).data or []
            from cogs.dashboard import PersistentDashboardView
            if saved and saved[0].get("message_id") and int(saved[0]["channel_id"]) == channels["panel"].id:
                try:
                    message = await channels["panel"].fetch_message(int(saved[0]["message_id"]))
                    await message.edit(embed=await dashboard.dashboard_embed(guild), view=PersistentDashboardView(dashboard))
                except discord.NotFound:
                    saved = []
            if not saved or int(saved[0]["channel_id"]) != channels["panel"].id or not saved[0].get("message_id"):
                panel_message = await channels["panel"].send(embed=await dashboard.dashboard_embed(guild), view=PersistentDashboardView(dashboard), allowed_mentions=discord.AllowedMentions.none())
                await client.table("staff_dashboard_panels").upsert({"guild_id": guild.id, "channel_id": channels["panel"].id, "message_id": panel_message.id, "updated_by": interaction.user.id}).execute()
                try:
                    await panel_message.pin(reason="AdSentry permanent operations center")
                except discord.HTTPException:
                    pass

            summary = "\n".join(f"• {channels[k].mention} — {definitions[k][0]}" for k in definitions)
            missing_roles = not any(guild.get_role(role_id) for role_id in Config.STAFF_ROLE_IDS)
            note = "\n\n⚠️ لم يتم العثور على رتب الطاقم في `STAFF_ROLE_IDS`؛ القنوات الخاصة ستكون للمدراء والبوت فقط." if missing_roles else ""
            await interaction.followup.send(f"✅ اكتمل إعداد AdSentry. تم تجهيز **6 قنوات** وربطها، وإنشاء/تحديث لوحة العمليات الثابتة.\n\n{summary}{note}", ephemeral=True)
        except Exception as exc:
            logger.exception("AdSentry setup failed for guild %s", guild.id)
            detail = str(exc).replace("`", "'")[:700] or "لا توجد تفاصيل إضافية"
            await interaction.followup.send(
                f"تعذر الإعداد أثناء: **{stage}**\n`{type(exc).__name__}: {detail}`\n\n"
                "القنوات التي أُنشئت ستبقى كما هي. أرسل هذا الخطأ للمطور أو راجع سجل Render، ثم أعد `/setup` بعد إصلاح السبب.",
                ephemeral=True,
            )

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
        if not is_admin(interaction.user):
            await interaction.response.send_message("هذا الأمر متاح لمالك السيرفر أو لمن لديه صلاحية Administrator فقط.", ephemeral=True)
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
        if not is_admin(interaction.user):
            await interaction.response.send_message("هذا الأمر متاح لمالك السيرفر أو لمن لديه صلاحية Administrator فقط.", ephemeral=True)
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
        panel_id = config.get("last_panel_message_id")
        embed.add_field(
            name="📋 Active Submission Panel",
            value=f"`Message ID: {panel_id}` (Auto-relocates on approval)" if panel_id else "Not deployed yet (Use `/ad-panel` to deploy)",
            inline=False,
        )
        embed.set_footer(text="To change channels, run /ad-setup • To deploy panel, run /ad-panel")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Extension entry point for loading the Setup cog."""
    await bot.add_cog(SetupCog(bot))
