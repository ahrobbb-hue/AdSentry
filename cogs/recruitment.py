"""Staff-managed recruitment openings and private application reviews."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from cogs.moderation import is_staff

logger = logging.getLogger("AdSentry.Recruitment")

DEFAULT_RECRUITMENT_ROLES = (
    "Administrator",
    "Moderator",
    "Ticket Support",
    "Management",
    "Events Team",
    "Design Team",
)


def _can_manage(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and is_staff(interaction.user))


class RecruitmentDetailsModal(discord.ui.Modal):
    def __init__(self, title: str | None, public_channel: discord.TextChannel, review_channel: discord.TextChannel, bot: commands.Bot) -> None:
        super().__init__(title="Create recruitment opening", timeout=600)
        self.role_title = title
        self.public_channel = public_channel
        self.review_channel = review_channel
        self.bot = bot
        if title is None:
            self.custom_title = discord.ui.TextInput(label="Custom role name", placeholder="e.g. Community Manager", max_length=80, required=True)
            self.add_item(self.custom_title)
        self.summary = discord.ui.TextInput(label="Role description", style=discord.TextStyle.paragraph, placeholder="What will the selected person do?", max_length=850, required=True)
        self.requirements = discord.ui.TextInput(label="Requirements", style=discord.TextStyle.paragraph, placeholder="Skills, experience, age requirements, etc.", max_length=850, required=True)
        self.add_item(self.summary)
        self.add_item(self.requirements)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _can_manage(interaction) or not interaction.guild:
            await interaction.response.send_message("Only authorized staff can create recruitment openings.", ephemeral=True)
            return
        title = self.role_title or self.custom_title.value.strip()
        description = f"{self.summary.value.strip()}\n\n**Requirements**\n{self.requirements.value.strip()}"
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await publish_opening(self.bot, interaction, title, description, self.public_channel, self.review_channel)
            await interaction.followup.send("Recruitment is now open.", ephemeral=True)
        except Exception:
            logger.exception("Could not create recruitment through the role picker")
            await interaction.followup.send("Could not open recruitment. Check channel permissions and the Supabase migration.", ephemeral=True)


class RecruitmentRoleSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption], public_channel: discord.TextChannel, review_channel: discord.TextChannel) -> None:
        super().__init__(placeholder="Choose the role you are recruiting for", min_values=1, max_values=1, options=options, row=0)
        self.public_channel = public_channel
        self.review_channel = review_channel

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _can_manage(interaction):
            await interaction.response.send_message("Only authorized staff can create recruitment openings.", ephemeral=True)
            return
        bot: commands.Bot = interaction.client  # type: ignore[assignment]
        value = self.values[0]
        await interaction.response.send_modal(RecruitmentDetailsModal(
            title=None if value == "__other__" else value,
            public_channel=self.public_channel,
            review_channel=self.review_channel,
            bot=bot,
        ))


class RecruitmentRoleSelectView(discord.ui.View):
    def __init__(self, options: list[discord.SelectOption], public_channel: discord.TextChannel, review_channel: discord.TextChannel) -> None:
        super().__init__(timeout=180)
        self.add_item(RecruitmentRoleSelect(options, public_channel, review_channel))


async def publish_opening(
    bot: commands.Bot,
    interaction: discord.Interaction,
    title: str,
    description: str,
    public_channel: discord.TextChannel,
    review_channel: discord.TextChannel,
) -> None:
    """Create the database record and public announcement with rollback on partial failure."""
    assert interaction.guild is not None
    if not title.strip() or len(title) > 80 or not description.strip() or len(description) > 1800:
        raise ValueError("Title or description exceeds the supported length.")
    if public_channel.guild.id != interaction.guild.id or review_channel.guild.id != interaction.guild.id:
        raise ValueError("Both channels must belong to this server.")
    bot_member = interaction.guild.me
    if not bot_member:
        raise RuntimeError("Bot member is unavailable in this server.")
    public_perms = public_channel.permissions_for(bot_member)
    if not public_perms.send_messages or not public_perms.embed_links:
        raise PermissionError("Missing announcement channel permissions.")
    review_perms = review_channel.permissions_for(bot_member)
    if not review_perms.view_channel or not review_perms.send_messages or not review_perms.embed_links:
        raise PermissionError("Missing review channel permissions.")
    active = await bot.db.get_open_recruitments(interaction.guild.id)
    if any(row["title"].casefold() == title.casefold() for row in active):
        raise ValueError("An opening with this role name is already active.")
    opening: dict[str, Any] | None = None
    announcement: discord.Message | None = None
    try:
        opening = await bot.db.create_recruitment({
            "guild_id": interaction.guild.id,
            "title": title.strip(),
            "description": description.strip(),
            "created_by": interaction.user.id,
            "public_channel_id": public_channel.id,
            "review_channel_id": review_channel.id,
            "status": "OPEN",
        })
        embed = discord.Embed(title=f"Now accepting applications: {title.strip()}", description=description.strip(), color=discord.Color.green())
        embed.set_footer(text=f"Opening #{opening['id']} · Apply using the button below")
        announcement = await public_channel.send(embed=embed, view=ApplyView(opening["id"]), allowed_mentions=discord.AllowedMentions.none())
        await bot.db.set_recruitment_message(opening["id"], announcement.id)
        bot.add_view(ApplyView(opening["id"]))
    except Exception:
        if opening:
            try:
                await bot.db.close_recruitment(opening["id"], interaction.guild.id)
            except Exception:
                logger.exception("Could not roll back incomplete opening %s", opening["id"])
        if announcement:
            try:
                await announcement.edit(view=None)
            except discord.HTTPException:
                logger.warning("Could not disable announcement for incomplete opening")
        raise


class ApplicationModal(discord.ui.Modal):
    def __init__(self, recruitment: dict[str, Any], bot: commands.Bot) -> None:
        super().__init__(title=f"Apply: {recruitment['title']}"[:45], timeout=600)
        self.recruitment = recruitment
        self.bot = bot
        self.experience = discord.ui.TextInput(label="Relevant experience", style=discord.TextStyle.paragraph, max_length=900, required=True)
        self.motivation = discord.ui.TextInput(label="Why do you want this role?", style=discord.TextStyle.paragraph, max_length=900, required=True)
        self.availability = discord.ui.TextInput(label="Availability / timezone", max_length=300, required=True)
        self.add_item(self.experience)
        self.add_item(self.motivation)
        self.add_item(self.availability)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or interaction.guild.id != self.recruitment["guild_id"]:
            await interaction.response.send_message("This application is not available here.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            # Re-check status at submission time; stale buttons cannot submit to a closed opening.
            current = await self.bot.db.get_recruitment(self.recruitment["id"])
            if not current or current.get("status") != "OPEN":
                await interaction.followup.send("This application is closed.", ephemeral=True)
                return
            record = await self.bot.db.create_recruitment_application({
                "recruitment_id": current["id"],
                "guild_id": interaction.guild.id,
                "user_id": interaction.user.id,
                "user_name": str(interaction.user),
                "experience": self.experience.value.strip(),
                "motivation": self.motivation.value.strip(),
                "availability": self.availability.value.strip(),
                "status": "PENDING",
            })
            review_channel = self.bot.get_channel(current["review_channel_id"])
            if not isinstance(review_channel, discord.TextChannel):
                review_channel = await self.bot.fetch_channel(current["review_channel_id"])
            if not isinstance(review_channel, discord.TextChannel):
                raise RuntimeError("Configured review destination is not a text channel")
            embed = discord.Embed(title=f"Application #{record['id']} · {current['title']}", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
            embed.add_field(name="Applicant", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
            embed.add_field(name="Experience", value=self.experience.value[:1024], inline=False)
            embed.add_field(name="Motivation", value=self.motivation.value[:1024], inline=False)
            embed.add_field(name="Availability", value=self.availability.value[:1024], inline=False)
            await review_channel.send(embed=embed, view=ApplicationReviewView(record["id"]))
            await interaction.followup.send("Your application was sent to the staff team for review.", ephemeral=True)
        except Exception as exc:
            logger.exception("Could not process recruitment application")
            await interaction.followup.send("We couldn't submit your application. Please contact staff and try again later.", ephemeral=True)


class ApplyView(discord.ui.View):
    def __init__(self, recruitment_id: int) -> None:
        super().__init__(timeout=None)
        self.recruitment_id = recruitment_id
        button = discord.ui.Button(label="Apply now", style=discord.ButtonStyle.success, custom_id=f"adsentry:recruit:apply:{recruitment_id}")
        button.callback = self.apply  # type: ignore[method-assign]
        self.add_item(button)

    async def apply(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Applications are only available in the server.", ephemeral=True)
            return
        bot: commands.Bot = interaction.client  # type: ignore[assignment]
        recruitment = await bot.db.get_recruitment(self.recruitment_id)
        if not recruitment or recruitment.get("status") != "OPEN" or recruitment["guild_id"] != interaction.guild.id:
            await interaction.response.send_message("This application is closed.", ephemeral=True)
            return
        await interaction.response.send_modal(ApplicationModal(recruitment, bot))


class ApplicationReviewView(discord.ui.View):
    def __init__(self, application_id: int) -> None:
        super().__init__(timeout=None)
        for decision, label, style in (("ACCEPTED", "Accept", discord.ButtonStyle.success), ("REJECTED", "Reject", discord.ButtonStyle.danger)):
            button = discord.ui.Button(label=label, style=style, custom_id=f"adsentry:recruit:review:{application_id}:{decision}")
            button.callback = self.review  # type: ignore[method-assign]
            self.add_item(button)

    async def review(self, interaction: discord.Interaction) -> None:
        if not _can_manage(interaction):
            await interaction.response.send_message("Only authorized staff can review applications.", ephemeral=True)
            return
        custom_id = str((interaction.data or {}).get("custom_id", ""))
        parts = custom_id.split(":")
        if len(parts) != 5 or not parts[3].isdigit() or parts[4] not in {"ACCEPTED", "REJECTED"}:
            await interaction.response.send_message("Invalid review action.", ephemeral=True)
            return
        application_id, decision = int(parts[3]), parts[4]
        bot: commands.Bot = interaction.client  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True)
        try:
            application = await bot.db.get_recruitment_application(application_id, interaction.guild.id)  # type: ignore[union-attr]
            if not application:
                await interaction.followup.send("Application not found in this server.", ephemeral=True)
                return
            changed = await bot.db.review_recruitment_application(application_id, interaction.user.id, decision, interaction.guild.id)  # type: ignore[union-attr]
            if not changed:
                await interaction.followup.send("This application was already reviewed.", ephemeral=True)
                return
            if interaction.message:
                embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(title=f"Application #{application_id}")
                embed.color = discord.Color.green() if decision == "ACCEPTED" else discord.Color.red()
                embed.add_field(name="Decision", value=f"{decision.title()} by {interaction.user.mention}", inline=False)
                try:
                    await interaction.message.edit(embed=embed, view=None)
                except discord.HTTPException:
                    logger.exception("Decision saved but review card could not be updated for application %s", application_id)
            try:
                applicant = await bot.fetch_user(application["user_id"])
                opening = await bot.db.get_recruitment(application["recruitment_id"])
                role_title = opening.get("title", "the role") if opening else "the role"
                await applicant.send(f"Your application for **{role_title}** was {decision.lower()} in **{interaction.guild.name}**.")  # type: ignore[union-attr]
            except (discord.HTTPException, discord.NotFound):
                logger.info("Could not DM applicant %s about decision", application["user_id"])
            await interaction.followup.send(f"Application marked {decision.lower()}.", ephemeral=True)
        except Exception:
            logger.exception("Could not review application %s", application_id)
            await interaction.followup.send("Could not save the decision. Please try again.", ephemeral=True)


class RecruitmentCog(commands.Cog):
    recruitment = app_commands.Group(name="recruit", description="Manage staff recruitment openings")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Restore persistent controls after restart so existing openings and reviews remain usable.
        try:
            for opening in await self.bot.db.get_recruitments_with_status("OPEN"):
                self.bot.add_view(ApplyView(opening["id"]))
            pending = await self.bot.db.get_pending_recruitment_applications()
            for application in pending:
                self.bot.add_view(ApplicationReviewView(application["id"]))
        except Exception:
            logger.exception("Could not restore recruitment controls; check that the recruitment SQL migration was applied")

    @recruitment.command(name="create", description="Create an opening using the role picker")
    @app_commands.describe(public_channel="Where the opening will be announced", review_channel="Private staff channel for applications")
    async def create_recruitment(
        self,
        interaction: discord.Interaction,
        public_channel: discord.TextChannel,
        review_channel: discord.TextChannel,
    ) -> None:
        if not _can_manage(interaction) or not interaction.guild:
            await interaction.response.send_message("Only authorized staff can create recruitment openings.", ephemeral=True)
            return
        try:
            options = [discord.SelectOption(label=role, value=role) for role in DEFAULT_RECRUITMENT_ROLES]
            configured = await self.bot.db.get_custom_recruitment_roles(interaction.guild.id)
            existing = {role.casefold() for role in DEFAULT_RECRUITMENT_ROLES}
            for row in configured:
                label = row["label"].strip()
                if label and label.casefold() not in existing and len(options) < 24:
                    options.append(discord.SelectOption(label=label, value=label))
                    existing.add(label.casefold())
            options.append(discord.SelectOption(label="Other", value="__other__", description="Enter a custom role name"))
            await interaction.response.send_message(
                "Choose the role for this opening. Selecting **Other** will ask you to enter its name.",
                view=RecruitmentRoleSelectView(options, public_channel, review_channel),
                ephemeral=True,
            )
        except Exception:
            logger.exception("Could not load recruitment role options")
            await interaction.response.send_message("Could not load role options. Check the recruitment database migration.", ephemeral=True)

    @recruitment.command(name="role-add", description="Add a custom role to the recruitment role picker")
    @app_commands.describe(name="Name shown in the role picker")
    async def add_recruitment_role(self, interaction: discord.Interaction, name: str) -> None:
        if not _can_manage(interaction) or not interaction.guild:
            await interaction.response.send_message("Only authorized staff can manage role options.", ephemeral=True)
            return
        name = name.strip()
        if not 2 <= len(name) <= 80 or name.casefold() == "other":
            await interaction.response.send_message("Enter a role name of 2–80 characters. 'Other' is reserved.", ephemeral=True)
            return
        try:
            if name.casefold() in {role.casefold() for role in DEFAULT_RECRUITMENT_ROLES}:
                await interaction.response.send_message("That role is already included by default.", ephemeral=True)
                return
            configured = await self.bot.db.get_custom_recruitment_roles(interaction.guild.id)
            if any(row["label"].casefold() == name.casefold() for row in configured):
                await interaction.response.send_message("That custom role is already in the picker.", ephemeral=True)
                return
            if len(configured) >= 18:
                await interaction.response.send_message("The picker already has its maximum of 18 custom role options.", ephemeral=True)
                return
            await self.bot.db.add_custom_recruitment_role(interaction.guild.id, name, interaction.user.id)
            await interaction.response.send_message(f"Added **{name}** to `/recruit create`.", ephemeral=True)
        except Exception:
            logger.exception("Could not add recruitment role option")
            await interaction.response.send_message("Could not save that role option. Please check the database migration.", ephemeral=True)

    @recruitment.command(name="role-remove", description="Remove a custom role from the recruitment role picker")
    @app_commands.describe(name="Exact name of the custom option")
    async def remove_recruitment_role(self, interaction: discord.Interaction, name: str) -> None:
        if not _can_manage(interaction) or not interaction.guild:
            await interaction.response.send_message("Only authorized staff can manage role options.", ephemeral=True)
            return
        try:
            configured = await self.bot.db.get_custom_recruitment_roles(interaction.guild.id)
            match = next((row for row in configured if row["label"].casefold() == name.strip().casefold()), None)
            if not match:
                await interaction.response.send_message("Custom role option not found. Default roles cannot be removed.", ephemeral=True)
                return
            await self.bot.db.delete_custom_recruitment_role(interaction.guild.id, match["id"])
            await interaction.response.send_message(f"Removed **{match['label']}** from the picker.", ephemeral=True)
        except Exception:
            logger.exception("Could not remove recruitment role option")
            await interaction.response.send_message("Could not remove that option. Please check the database migration.", ephemeral=True)

    @recruitment.command(name="open", description="Open an application form for a team role")
    @app_commands.describe(title="Role or team", description="What the role involves and eligibility", public_channel="Where to announce the opening", review_channel="Private staff channel for applications")
    async def open_recruitment(self, interaction: discord.Interaction, title: str, description: str, public_channel: discord.TextChannel, review_channel: discord.TextChannel) -> None:
        if not _can_manage(interaction):
            await interaction.response.send_message("Only authorized staff can open recruitment.", ephemeral=True)
            return
        if not interaction.guild or len(title) > 80 or len(description) > 1800:
            await interaction.response.send_message("Use a title under 80 characters and a description under 1,800 characters, in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        opening: dict[str, Any] | None = None
        announcement: discord.Message | None = None
        try:
            perms = public_channel.permissions_for(interaction.guild.me)
            if not perms.send_messages or not perms.embed_links:
                await interaction.followup.send("I need Send Messages and Embed Links in the public channel.", ephemeral=True)
                return
            review_perms = review_channel.permissions_for(interaction.guild.me)
            if not review_perms.view_channel or not review_perms.send_messages or not review_perms.embed_links:
                await interaction.followup.send("I need View Channel, Send Messages, and Embed Links in the staff review channel.", ephemeral=True)
                return
            # Avoid simultaneous openings for the same role (case-insensitive check).
            active = await self.bot.db.get_open_recruitments(interaction.guild.id)
            if any(row["title"].casefold() == title.casefold() for row in active):
                await interaction.followup.send("An opening with this title is already active. Close it before creating another.", ephemeral=True)
                return
            opening = await self.bot.db.create_recruitment({
                "guild_id": interaction.guild.id,
                "title": title.strip(),
                "description": description.strip(),
                "created_by": interaction.user.id,
                "public_channel_id": public_channel.id,
                "review_channel_id": review_channel.id,
                "status": "OPEN",
            })
            embed = discord.Embed(title=f"Now accepting applications: {title.strip()}", description=description.strip(), color=discord.Color.green())
            embed.set_footer(text=f"Opening #{opening['id']} · Apply using the button below")
            announcement = await public_channel.send(embed=embed, view=ApplyView(opening["id"]), allowed_mentions=discord.AllowedMentions.none())
            await self.bot.db.set_recruitment_message(opening["id"], announcement.id)
            self.bot.add_view(ApplyView(opening["id"]))
            await interaction.followup.send(f"Recruitment is open in {public_channel.mention} (opening #{opening['id']}).", ephemeral=True)
        except Exception:
            logger.exception("Could not open recruitment")
            if opening:
                try:
                    await self.bot.db.close_recruitment(opening["id"], interaction.guild.id)  # type: ignore[union-attr]
                except Exception:
                    logger.exception("Could not roll back incomplete opening %s", opening["id"])
            if announcement:
                try:
                    await announcement.edit(view=None)
                except discord.HTTPException:
                    logger.warning("Could not disable announcement for incomplete opening")
            await interaction.followup.send("Could not open recruitment. Check the bot's channel permissions and database migration.", ephemeral=True)

    @recruitment.command(name="close", description="Close an active application form")
    @app_commands.describe(opening_id="Opening ID shown in the announcement")
    async def close_recruitment(self, interaction: discord.Interaction, opening_id: int) -> None:
        if not _can_manage(interaction):
            await interaction.response.send_message("Only authorized staff can close recruitment.", ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            opening = await self.bot.db.get_recruitment(opening_id)
            if not opening or opening["guild_id"] != interaction.guild.id:
                await interaction.followup.send("Opening not found in this server.", ephemeral=True)
                return
            if not await self.bot.db.close_recruitment(opening_id, interaction.guild.id):
                await interaction.followup.send("This opening is already closed.", ephemeral=True)
                return
            channel = self.bot.get_channel(opening["public_channel_id"])
            if isinstance(channel, discord.TextChannel) and opening.get("announcement_message_id"):
                try:
                    message = await channel.fetch_message(opening["announcement_message_id"])
                    embed = message.embeds[0] if message.embeds else discord.Embed(title=opening["title"])
                    embed.color = discord.Color.dark_grey()
                    embed.set_footer(text=f"Opening #{opening_id} · Applications closed")
                    await message.edit(embed=embed, view=None)
                except discord.HTTPException:
                    logger.warning("Could not update announcement for closed opening %s", opening_id)
            await interaction.followup.send(f"Opening #{opening_id} is now closed.", ephemeral=True)
        except Exception:
            logger.exception("Could not close recruitment %s", opening_id)
            await interaction.followup.send("Could not close this opening. Please retry.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecruitmentCog(bot))
