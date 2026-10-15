"""
AdSentry Submissions Cog
Handles category selection, submission modals, auto-moderation, anti-spam cooldowns, and Supabase integration.
"""

from __future__ import annotations

import logging
import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from cogs.moderation import StaffReviewView

logger = logging.getLogger("AdSentry.Submissions")


class AdSubmissionModal(discord.ui.Modal):
    """Modal for collecting advertisement title and content from the user."""

    def __init__(self, category: str, bot: commands.Bot) -> None:
        super().__init__(title=f"Submit Ad: {category[:20]}")
        self.category = category
        self.bot = bot

        self.ad_title = discord.ui.TextInput(
            label="Ad Headline / Title",
            placeholder="e.g. Apex Coding Hub - Join top developers",
            min_length=2,
            max_length=100,
            required=True,
        )
        self.add_item(self.ad_title)

        self.ad_content = discord.ui.TextInput(
            label="Advertisement Content & Links",
            style=discord.TextStyle.paragraph,
            placeholder="Describe your server, bot, or service in detail, including features and links...",
            min_length=10,
            max_length=1800,
            required=True,
        )
        self.add_item(self.ad_content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Processes the ad submission with auto-moderation, cooldown checks, and DB storage."""
        # Defer immediately to prevent Discord's 3-second modal timeout
        await interaction.response.defer(ephemeral=True)

        try:
            title = self.ad_title.value.strip()
            content = self.ad_content.value.strip()
            full_text = f"{title} {content}".lower()

            # -------------------------------------------------------------
            # 1. Auto-Moderation & Link Filtering Check
            # -------------------------------------------------------------
            flagged_words = [word for word in Config.FORBIDDEN_WORDS if word in full_text]
            if flagged_words:
                embed = discord.Embed(
                    title="🛡️ Submission Blocked by Auto-Mod",
                    description=(
                        "Your advertisement could not be submitted because it contains restricted terms or unauthorized links:\n"
                        f"**Detected forbidden patterns:** `{', '.join(flagged_words)}`\n\n"
                        "Please revise your submission in accordance with our server advertising guidelines."
                    ),
                    color=discord.Color.red(),
                )
                embed.set_footer(text="AdSentry Auto-Moderation Guard")
                await interaction.followup.send(embed=embed, ephemeral=True)
                logger.warning(
                    f"Blocked submission from {interaction.user} (ID: {interaction.user.id}) due to forbidden words: {flagged_words}"
                )
                return

            # -------------------------------------------------------------
            # 2. Anti-Spam / Cooldown & Duplicate Pending Checks
            # -------------------------------------------------------------
            cooldown_until = await self.bot.db.get_user_cooldown(interaction.user.id)
            if cooldown_until:
                unix_ts = int(cooldown_until.timestamp())
                embed = discord.Embed(
                    title="⏳ Cooldown Active",
                    description=(
                        "You have recently submitted an advertisement and are currently on cooldown.\n\n"
                        f"**Next submission available:** <t:{unix_ts}:F> (<t:{unix_ts}:R>)"
                    ),
                    color=discord.Color.orange(),
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            has_pending = await self.bot.db.has_pending_submission(interaction.user.id)
            if has_pending:
                embed = discord.Embed(
                    title="⚠️ Submission Already Pending",
                    description=(
                        "You already have an advertisement submission awaiting staff review.\n"
                        "Please wait for your previous submission to be approved or rejected before submitting a new one."
                    ),
                    color=discord.Color.gold(),
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            guild_id = interaction.guild_id
            guild_config = await self.bot.db.get_guild_config(guild_id) if guild_id else None

            if not guild_config or not guild_config.get("staff_review_channel_id"):
                embed = discord.Embed(
                    title="⚙️ Server Setup Required",
                    description=(
                        "This server has not configured an advertisement review channel yet.\n"
                        "An administrator must run `/ad-setup` before submissions can be processed."
                    ),
                    color=discord.Color.gold(),
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            # -------------------------------------------------------------
            # 3. Save to Supabase Submissions Table
            # -------------------------------------------------------------
            submission = None
            try:
                submission = await self.bot.db.create_submission(
                    user_id=interaction.user.id,
                    user_name=str(interaction.user),
                    user_avatar_url=interaction.user.display_avatar.url,
                    category=self.category,
                    title=title,
                    content=content,
                    guild_id=guild_id,
                )
            except Exception as e:
                logger.error(f"Failed to record submission in Supabase: {e}", exc_info=True)
                embed = discord.Embed(
                    title="❌ Database Error",
                    description="An error occurred while saving your submission. Please try again later or contact an administrator.",
                    color=discord.Color.red(),
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            # -------------------------------------------------------------
            # 4. Activate User Cooldown in Database
            # -------------------------------------------------------------
            try:
                await self.bot.db.set_user_cooldown(interaction.user.id)
            except Exception as e:
                logger.error(f"Failed to set user cooldown: {e}", exc_info=True)

            submission_id = submission.get("id", "N/A") if submission else "N/A"

            # -------------------------------------------------------------
            # 5. Forward to Staff Review Channel (Dynamically Configured)
            # -------------------------------------------------------------
            review_channel_id = guild_config.get("staff_review_channel_id")
            review_channel = self.bot.get_channel(review_channel_id)
            if not review_channel and review_channel_id:
                try:
                    review_channel = await self.bot.fetch_channel(review_channel_id)
                except Exception:
                    review_channel = None

            if review_channel and isinstance(review_channel, discord.TextChannel):
                review_embed = discord.Embed(
                    title=f"📥 New Ad Submission #{submission_id}",
                    description=f"**Headline:** {title}\n\n**Category:** {self.category}\n\n```{content}```",
                    color=discord.Color.blue(),
                )
                review_embed.set_author(
                    name=f"{interaction.user} ({interaction.user.id})",
                    icon_url=interaction.user.display_avatar.url,
                )
                review_embed.add_field(name="Category", value=self.category, inline=True)
                review_embed.add_field(name="Status", value="🟡 Pending Staff Review", inline=True)
                review_embed.set_footer(text=f"Submission ID: {submission_id} • AdSentry Review Queue")

                try:
                    review_view = StaffReviewView(submission_id) if isinstance(submission_id, int) else None
                    review_msg = await review_channel.send(embed=review_embed, view=review_view)
                    if submission and isinstance(submission_id, int):
                        await self.bot.db.update_review_message_id(submission_id, review_msg.id)
                except Exception as e:
                    logger.error(f"Failed to send submission to review channel: {e}", exc_info=True)
            else:
                logger.warning(f"Configured review channel ID {review_channel_id} could not be resolved.")

            # -------------------------------------------------------------
            # 6. Notify User of Successful Submission
            # -------------------------------------------------------------
            success_embed = discord.Embed(
                title="✅ Advertisement Submitted!",
                description=(
                    f"Your advertisement for **{self.category}** has been successfully received!\n\n"
                    f"**Headline:** {title}\n"
                    f"**Tracking ID:** `#{submission_id}`\n\n"
                    "Our moderation team will review your ad shortly. You will be notified via DM when a decision is made."
                ),
                color=discord.Color.green(),
            )
            success_embed.set_footer(text="AdSentry Announcement Service")
            await interaction.followup.send(embed=success_embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Unexpected error in AdSubmissionModal.on_submit: {e}", exc_info=True)
            embed = discord.Embed(
                title="❌ Error Processing Submission",
                description=f"An unexpected error occurred while processing your ad:\n`{e}`\n\nPlease try again.",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)


class CategorySelect(discord.ui.Select):
    """Dropdown menu for choosing the advertisement category."""

    def __init__(self, bot: commands.Bot) -> None:
        options = [
            discord.SelectOption(
                label="Programming",
                value="Programming",
                description="Coding projects, bots, software, open source, scripts",
                emoji="💻",
            ),
            discord.SelectOption(
                label="Discord Servers",
                value="Discord Servers",
                description="Community hubs, gaming, hanging out, study clubs",
                emoji="🌐",
            ),
            discord.SelectOption(
                label="Services",
                value="Services",
                description="Freelancing, graphic design, bot hosting, consulting",
                emoji="🚀",
            ),
            discord.SelectOption(
                label="Others",
                value="Others",
                description="General announcements and miscellaneous promotions",
                emoji="📌",
            ),
        ]
        super().__init__(
            placeholder="Select your ad category...",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        """Invoked when user picks a category from the dropdown."""
        selected_category = self.values[0]

        # Pre-check cooldown
        cooldown_until = await self.bot.db.get_user_cooldown(interaction.user.id)
        if cooldown_until:
            unix_ts = int(cooldown_until.timestamp())
            embed = discord.Embed(
                title="⏳ Cooldown Active",
                description=(
                    "You cannot submit a new advertisement while on cooldown.\n\n"
                    f"**Next submission available:** <t:{unix_ts}:F> (<t:{unix_ts}:R>)"
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Pre-check pending submission
        has_pending = await self.bot.db.has_pending_submission(interaction.user.id)
        if has_pending:
            embed = discord.Embed(
                title="⚠️ Submission Already Pending",
                description=(
                    "You already have a pending advertisement request.\n"
                    "Please wait for staff review before submitting another ad."
                ),
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Launch the ad submission modal
        modal = AdSubmissionModal(category=selected_category, bot=self.bot)
        await interaction.response.send_modal(modal)


class CategorySelectView(discord.ui.View):
    """View hosting the category selection dropdown."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=120)
        self.add_item(CategorySelect(bot))


class AdSubmissionPanelView(discord.ui.View):
    """Persistent panel view with a button that presents the category selector."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Submit Advertisement",
        style=discord.ButtonStyle.primary,
        emoji="📢",
        custom_id="adsentry:submit_ad_btn",
    )
    async def submit_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Presents category selector to the clicking user."""
        embed = discord.Embed(
            title="📢 Select Advertisement Category",
            description=(
                "Choose the category that best matches your promotion.\n"
                "Once chosen, a submission form will open for you to enter your details."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=CategorySelectView(self.bot),
            ephemeral=True,
        )


class SubmissionsCog(commands.Cog, name="Submissions"):
    """Cog handling user ad submissions, categories, and moderation."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    ad_group = app_commands.Group(name="ad", description="AdSentry advertisement management")

    @ad_group.command(name="submit", description="Submit a new server or project advertisement")
    async def submit_ad(self, interaction: discord.Interaction) -> None:
        """Slash command to initiate advertisement submission."""
        # Check cooldown
        cooldown_until = await self.bot.db.get_user_cooldown(interaction.user.id)
        if cooldown_until:
            unix_ts = int(cooldown_until.timestamp())
            embed = discord.Embed(
                title="⏳ Cooldown Active",
                description=(
                    "You cannot submit a new advertisement while on cooldown.\n\n"
                    f"**Next submission available:** <t:{unix_ts}:F> (<t:{unix_ts}:R>)"
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Check pending submission
        has_pending = await self.bot.db.has_pending_submission(interaction.user.id)
        if has_pending:
            embed = discord.Embed(
                title="⚠️ Submission Already Pending",
                description=(
                    "You already have a pending advertisement request.\n"
                    "Please wait for staff review before submitting another ad."
                ),
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="📢 Submit an Advertisement",
            description=(
                "Select a category below to open the advertisement submission form.\n"
                "**Guidelines:**\n"
                "• No unauthorized invite links outside your ad\n"
                "• No offensive language or scam content\n"
                "• All submissions are reviewed by staff prior to publishing"
            ),
            color=discord.Color.blurple(),
        )
        view = CategorySelectView(self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @ad_group.command(name="panel", description="Post the persistent Ad Submission panel (Admin/Staff only)")
    @app_commands.default_permissions(administrator=True)
    async def post_panel(self, interaction: discord.Interaction) -> None:
        """Sends a persistent ad submission embed with a button into the current channel."""
        embed = discord.Embed(
            title="📢 Server Advertisement Portal",
            description=(
                "Welcome to our advertisement showcase!\n\n"
                "Looking to promote your server, developer project, bot, or service?\n"
                "Click the **Submit Advertisement** button below to submit your ad for staff review.\n\n"
                "**Submission Rules:**\n"
                "1. Choose the accurate category for your promotion.\n"
                "2. Provide clear descriptions and working links.\n"
                "3. Refrain from spamming or submitting duplicate ads."
            ),
            color=discord.Color.blue(),
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url if self.bot.user else None)
        embed.set_footer(text="Powered by AdSentry • Safe & Moderated Advertising")

        view = AdSubmissionPanelView(self.bot)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("Advertisement panel posted successfully!", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Extension entry point for loading the Submissions cog."""
    # Register persistent view so panel buttons work across restarts
    bot.add_view(AdSubmissionPanelView(bot))
    await bot.add_cog(SubmissionsCog(bot))
