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
    @app_commands.describe(announcement_channel="اختر قناة الإعلانات العامة الموجودة لإعادة استخدامها (اختياري)")
    @app_commands.default_permissions(administrator=True)
    async def setup_server(
        self,
        interaction: discord.Interaction,
        announcement_channel: discord.TextChannel | None = None,
    ) -> None:
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

        STAFF_CATEGORY_NAME = "↳AdSentry - الإداره↲"
        PUBLIC_CATEGORY_NAME = "↳AdSentry - عام↲"

        STAFF_CHANNEL_SPECS = [
            {
                "key": "panel",
                "name": "╔〖⚙️┇〢operations-center",
                "slug": "operations-center",
                "aliases": ["operations-center", "مركز-العمليات", "لوحة-العمليات", "panel"],
                "access": "panel",
                "label": "مركز العمليات",
                "private": True,
            },
            {
                "key": "app1",
                "name": "╠〖📥┇〢join-requests-1",
                "slug": "join-requests-1",
                "aliases": ["join-requests-1", "طلبات-الانضمام-1", "app1"],
                "access": "admin",
                "label": "طلبات الانضمام 1",
                "private": True,
            },
            {
                "key": "app2",
                "name": "╠〖📥┇〢join-requests-2",
                "slug": "join-requests-2",
                "aliases": ["join-requests-2", "طلبات-الانضمام-2", "app2"],
                "access": "admin",
                "label": "طلبات الانضمام 2",
                "private": True,
            },
            {
                "key": "app3",
                "name": "╠〖📥┇〢join-requests-3",
                "slug": "join-requests-3",
                "aliases": ["join-requests-3", "طلبات-الانضمام-3", "app3"],
                "access": "admin",
                "label": "طلبات الانضمام 3",
                "private": True,
            },
            {
                "key": "vacation",
                "name": "╠〖🏖️┇〢vacation-requests",
                "slug": "vacation-requests",
                "aliases": ["vacation-requests", "طلبات-الإجازات", "طلبات-الاجازات", "إجازات", "vacation"],
                "access": "staff",
                "label": "طلبات الإجازات",
                "private": True,
            },
            {
                "key": "complaints",
                "name": "╠〖🚫┇〢staff-complaints",
                "slug": "staff-complaints",
                "aliases": ["staff-complaints", "شكاوى-الطاقم", "شكاوى", "complaints"],
                "access": "complaints",
                "label": "شكاوى الطاقم الإدارية",
                "private": True,
            },
            {
                "key": "review",
                "name": "╚〖📢┇〢ads-and-staff",
                "slug": "ads-and-staff",
                "aliases": ["ads-and-staff", "مراجعة-الإعلانات-والطاقم", "مراجعة-الإعلانات", "review"],
                "access": "staff_review",
                "label": "مراجعة الإعلانات والطاقم",
                "private": True,
            },
        ]

        PUBLIC_CHANNEL_SPECS = [
            {
                "key": "announcements",
                "name": "╚〖📢┇〢announcements",
                "slug": "announcements",
                "aliases": ["announcements", "إعلانات-البوت", "إعلانات", "ads"],
                "access": "public",
                "label": "إعلانات البوت العامة",
                "private": False,
            }
        ]

        def find_category(target_name: str, keywords: list[str]) -> discord.CategoryChannel | None:
            cat = discord.utils.get(guild.categories, name=target_name)
            if cat:
                return cat
            for c in guild.categories:
                c_lower = c.name.lower()
                if all(kw.lower() in c_lower for kw in keywords):
                    return c
            return None

        async def get_or_create_category(target_name: str, private: bool, keywords: list[str]) -> discord.CategoryChannel:
            found = find_category(target_name, keywords)
            if found:
                if found.name != target_name and any(kw in found.name.lower() for kw in keywords):
                    try:
                        await found.edit(name=target_name, reason="AdSentry category name styling")
                    except Exception:
                        pass
                return found
            options = {"overwrites": {guild.default_role: discord.PermissionOverwrite(view_channel=False)}} if private else {}
            return await guild.create_category(target_name, reason="AdSentry channel setup", **options)

        def find_existing_channel(
            saved_id: int | None,
            spec: dict,
            parent_category: discord.CategoryChannel | None,
        ) -> discord.TextChannel | None:
            # 1. Look up by ID from database (preserves manual renames 100%)
            if saved_id:
                ch = guild.get_channel(int(saved_id))
                if isinstance(ch, discord.TextChannel):
                    return ch

            # 2. Look up inside category by exact name, slug or alias
            if parent_category:
                for ch in parent_category.text_channels:
                    if ch.name == spec["name"] or spec["slug"] in ch.name.lower():
                        return ch
                    for alias in spec.get("aliases", []):
                        if alias.lower() in ch.name.lower():
                            return ch

            # 3. Look up across entire guild by exact name, slug or alias
            for ch in guild.text_channels:
                if ch.name == spec["name"] or spec["slug"] in ch.name.lower():
                    return ch
                for alias in spec.get("aliases", []):
                    if alias.lower() in ch.name.lower():
                        return ch

            return None

        stage = "قراءة الإعدادات الحالية من قاعدة البيانات"
        try:
            previous_config = await self.bot.db.get_guild_config(guild.id) or {}
            client = await self.bot.db.get_client()

            # Retrieve recruitment review channels map
            saved_recruitment = (
                await client.table("recruitment_review_channels")
                .select("slot,channel_id")
                .eq("guild_id", guild.id)
                .execute()
            ).data or []
            rec_map = {int(r["slot"]): int(r["channel_id"]) for r in saved_recruitment}

            # Retrieve operations dashboard panel channel
            saved_panel = (
                await client.table("staff_dashboard_panels")
                .select("channel_id,message_id")
                .eq("guild_id", guild.id)
                .limit(1)
                .execute()
            ).data or []
            saved_panel_id = int(saved_panel[0]["channel_id"]) if saved_panel and saved_panel[0].get("channel_id") else None

            # Map known IDs for deduplication
            known_ids: dict[str, int | None] = {
                "announcements": int(previous_config["announcement_channel_id"]) if previous_config.get("announcement_channel_id") else None,
                "review": int(previous_config["staff_review_channel_id"]) if previous_config.get("staff_review_channel_id") else None,
                "vacation": int(previous_config["staffflow_review_channel_id"]) if previous_config.get("staffflow_review_channel_id") else None,
                "complaints": int(previous_config["staffflow_complaint_channel_id"]) if previous_config.get("staffflow_complaint_channel_id") else None,
                "app1": rec_map.get(1),
                "app2": rec_map.get(2),
                "app3": rec_map.get(3),
                "panel": saved_panel_id,
            }

            stage = "إنشاء وتنسيق الفئات"
            public_cat = await get_or_create_category(PUBLIC_CATEGORY_NAME, False, ["adsentry", "عام"])
            staff_cat = await get_or_create_category(STAFF_CATEGORY_NAME, True, ["adsentry", "إدار"])

            # Permission setups
            admin_overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                bot_member: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, embed_links=True, read_message_history=True, manage_messages=True
                ),
            }
            panel_overwrites = dict(admin_overwrites)
            staff_review_overwrites = dict(admin_overwrites)
            staff_vacation_overwrites = dict(admin_overwrites)
            for role_id in Config.STAFF_ROLE_IDS:
                role = guild.get_role(role_id)
                if role:
                    staff_perms = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, embed_links=True, read_message_history=True
                    )
                    panel_overwrites[role] = staff_perms
                    staff_review_overwrites[role] = staff_perms
                    staff_vacation_overwrites[role] = staff_perms

            complaints_overwrites = dict(admin_overwrites)  # Confidential for admins + bot only
            public_overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
                bot_member: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, embed_links=True, manage_webhooks=True, create_public_threads=True
                ),
            }

            channels: dict[str, discord.TextChannel] = {}
            reused_flags: dict[str, bool] = {}
            all_specs = PUBLIC_CHANNEL_SPECS + STAFF_CHANNEL_SPECS

            stage = "فحص وإنشاء وتنسيق القنوات"
            for spec in all_specs:
                key = spec["key"]
                is_private = spec.get("private", True)
                parent = staff_cat if is_private else public_cat

                # Handle custom announcement channel if explicitly provided by user
                if key == "announcements" and announcement_channel is not None:
                    channels[key] = announcement_channel
                    reused_flags[key] = True
                    continue

                known_id = known_ids.get(key)
                channel = find_existing_channel(known_id, spec, parent)

                overwrites = (
                    panel_overwrites if spec["access"] == "panel"
                    else staff_review_overwrites if spec["access"] == "staff_review"
                    else staff_vacation_overwrites if spec["access"] == "staff"
                    else complaints_overwrites if spec["access"] == "complaints"
                    else admin_overwrites if is_private
                    else public_overwrites
                )

                if channel is None:
                    channel = await guild.create_text_channel(
                        spec["name"],
                        category=parent,
                        overwrites=overwrites,
                        reason="AdSentry channel setup",
                    )
                    reused_flags[key] = False
                else:
                    reused_flags[key] = True
                    # Reassert category if channel was placed elsewhere
                    if channel.category_id != parent.id:
                        try:
                            await channel.edit(category=parent, reason="AdSentry category alignment")
                        except Exception:
                            pass
                    # Reassert permissions
                    await channel.edit(overwrites=overwrites, reason="AdSentry channel setup")
                    # If channel still has an old legacy default Arabic name, upgrade to the new stylized name
                    legacy_names = set(spec.get("aliases", [])) | {spec["slug"]}
                    if channel.name in legacy_names:
                        try:
                            await channel.edit(name=spec["name"], reason="AdSentry channel style upgrade")
                        except Exception:
                            pass

                channels[key] = channel

            stage = "حفظ قناة الإعلانات والمراجعة في قاعدة البيانات"
            await self.bot.db.set_guild_config(
                guild_id=guild.id,
                announcement_channel_id=channels["announcements"].id,
                staff_review_channel_id=channels["review"].id,
            )

            stage = "حفظ قنوات الإجازات والشكاوى الإدارية"
            await self.bot.db.set_staffflow_review_channel(guild.id, channels["vacation"].id)
            await self.bot.db.set_staffflow_complaint_channel(guild.id, channels["complaints"].id)

            stage = "حفظ قنوات مراجعة طلبات الانضمام"
            await client.rpc("set_recruitment_review_channels", {
                "p_guild_id": guild.id,
                "p_channel_ids": [channels["app1"].id, channels["app2"].id, channels["app3"].id],
                "p_configured_by": interaction.user.id,
            }).execute()

            stage = "إنشاء لوحة العمليات وربطها"
            dashboard = self.bot.get_cog("DashboardCog")
            if dashboard is None:
                raise RuntimeError("DashboardCog is not loaded")

            saved_panel_record = (
                await client.table("staff_dashboard_panels")
                .select("channel_id,message_id")
                .eq("guild_id", guild.id)
                .limit(1)
                .execute()
            ).data or []
            from cogs.dashboard import PersistentDashboardView

            panel_ch = channels["panel"]
            if (
                saved_panel_record
                and saved_panel_record[0].get("message_id")
                and int(saved_panel_record[0]["channel_id"]) == panel_ch.id
            ):
                try:
                    message = await panel_ch.fetch_message(int(saved_panel_record[0]["message_id"]))
                    await message.edit(
                        embed=await dashboard.dashboard_embed(guild),
                        view=PersistentDashboardView(dashboard),
                    )
                except discord.NotFound:
                    saved_panel_record = []

            if not saved_panel_record or int(saved_panel_record[0]["channel_id"]) != panel_ch.id or not saved_panel_record[0].get("message_id"):
                panel_message = await panel_ch.send(
                    embed=await dashboard.dashboard_embed(guild),
                    view=PersistentDashboardView(dashboard),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await client.table("staff_dashboard_panels").upsert({
                    "guild_id": guild.id,
                    "channel_id": panel_ch.id,
                    "message_id": panel_message.id,
                    "updated_by": interaction.user.id,
                }).execute()
                try:
                    await panel_message.pin(reason="AdSentry permanent operations center")
                except discord.HTTPException:
                    pass

            summary_parts = []
            summary_parts.append(f"📁 **فئة الإدارة:** `{staff_cat.name}`")
            for spec in STAFF_CHANNEL_SPECS:
                ch = channels[spec["key"]]
                tag = "♻️ تم التعرف عليها" if reused_flags.get(spec["key"]) else "✨ جديدة"
                summary_parts.append(f"• {ch.mention} — `{spec['label']}` ({tag})")

            summary_parts.append(f"\n📁 **فئة عام:** `{public_cat.name}`")
            ann_tag = "♻️ تم التعرف عليها" if reused_flags.get("announcements") else "✨ جديدة"
            summary_parts.append(f"• {channels['announcements'].mention} — `إعلانات البوت العامة` ({ann_tag})")

            summary = "\n".join(summary_parts)
            missing_roles = not any(guild.get_role(role_id) for role_id in Config.STAFF_ROLE_IDS)
            note = "\n\n⚠️ لم يتم العثور على رتب الطاقم في `STAFF_ROLE_IDS`؛ القنوات الخاصة ستكون للمدراء والبوت فقط." if missing_roles else ""

            await interaction.followup.send(
                f"✅ **اكتمل إعداد وتنسيق قنوات AdSentry بنجاح!**\n\n"
                f"{summary}{note}\n\n"
                f"🔒 **تعديل الأسماء:** جميع وظائف البوت مرتبطة تلقائياً بـ `Channel ID` في قاعدة البيانات، "
                f"لذلك يمكنك تعديل أسماء القنوات أو ترتيبها في Discord في أي وقت دون أن تتأثر أي ميزة!",
                ephemeral=True,
            )
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
        vac_id = config.get("staffflow_review_channel_id")
        comp_id = config.get("staffflow_complaint_channel_id")

        ann_channel = interaction.guild.get_channel(ann_id) if ann_id else None
        rev_channel = interaction.guild.get_channel(rev_id) if rev_id else None
        vac_channel = interaction.guild.get_channel(vac_id) if vac_id else None
        comp_channel = interaction.guild.get_channel(comp_id) if comp_id else None

        embed = discord.Embed(
            title="⚙️ Current AdSentry Configuration",
            description=f"Active channel settings for **{interaction.guild.name}**:",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="📢 Announcement Channel",
            value=f"{ann_channel.mention if ann_channel else '⚠️ Not Set'} (`{ann_id}`)",
            inline=True,
        )
        embed.add_field(
            name="🛡️ Staff Review Channel",
            value=f"{rev_channel.mention if rev_channel else '⚠️ Not Set'} (`{rev_id}`)",
            inline=True,
        )
        if vac_id:
            embed.add_field(
                name="🏖️ Vacation Channel",
                value=f"{vac_channel.mention if vac_channel else '⚠️ Not Set'} (`{vac_id}`)",
                inline=True,
            )
        if comp_id:
            embed.add_field(
                name="🚫 Complaints Channel",
                value=f"{comp_channel.mention if comp_channel else '⚠️ Not Set'} (`{comp_id}`)",
                inline=True,
            )
        panel_id = config.get("last_panel_message_id")
        embed.add_field(
            name="📋 Active Submission Panel",
            value=f"`Message ID: {panel_id}` (Auto-relocates on approval)" if panel_id else "Not deployed yet (Use `/ad-panel` to deploy)",
            inline=False,
        )
        embed.set_footer(text="To configure or refresh channels, run /setup or /ad-setup")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Extension entry point for loading the Setup cog."""
    await bot.add_cog(SetupCog(bot))
