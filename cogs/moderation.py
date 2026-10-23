"""
AdSentry Moderation & Review Cog
Handles staff review dashboard, interactive approval/rejection, flexible auto-expiry,
webhook publishing with discussion threads, and automated background expiry sweeper.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import Config
from utils import publish_ad_via_webhook

logger = logging.getLogger("AdSentry.Moderation")


def is_admin(user: discord.Member | discord.User) -> bool:
    """Server owner or a member with Discord's Administrator permission."""
    return isinstance(user, discord.Member) and (
        user.id == user.guild.owner_id or user.guild_permissions.administrator
    )


def is_staff(user: discord.Member | discord.User) -> bool:
    """Checks whether the user has staff roles or moderation permissions."""
    if not isinstance(user, discord.Member):
        return False
    if (
        user.guild_permissions.administrator
        or user.guild_permissions.manage_messages
        or user.guild_permissions.manage_guild
    ):
        return True
    user_roles = {r.id for r in user.roles}
    return bool(user_roles.intersection(set(Config.STAFF_ROLE_IDS)))


class StaffReviewView(discord.ui.View):
    """Interactive review buttons attached to new submission review cards."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(timeout=None)
        self.submission_id = submission_id

        self.add_item(
            discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                emoji="✅",
                custom_id=f"adsentry:approve:{submission_id}",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Reject",
                style=discord.ButtonStyle.danger,
                emoji="❌",
                custom_id=f"adsentry:reject:{submission_id}",
            )
        )


class ExpiryDurationSelect(discord.ui.Select):
    """Dropdown for staff to select how long the approved ad remains active."""

    def __init__(self, cog: ModerationCog, submission_id: int, review_message: discord.Message) -> None:
        options = [
            discord.SelectOption(
                label="1 Day",
                value="1d",
                description="Auto-deletes after 24 hours",
                emoji="⏱️",
            ),
            discord.SelectOption(
                label="1 Week",
                value="1w",
                description="Auto-deletes after 7 days",
                emoji="📅",
            ),
            discord.SelectOption(
                label="1 Month",
                value="1m",
                description="Auto-deletes after 30 days",
                emoji="🗓️",
            ),
            discord.SelectOption(
                label="1 Year",
                value="1y",
                description="Auto-deletes after 365 days",
                emoji="⏳",
            ),
            discord.SelectOption(
                label="Forever",
                value="forever",
                description="Permanent post (never auto-deletes)",
                emoji="♾️",
            ),
        ]
        super().__init__(
            placeholder="Select ad active duration before expiry...",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.cog = cog
        self.submission_id = submission_id
        self.review_message = review_message

    async def callback(self, interaction: discord.Interaction) -> None:
        """Processes the chosen expiry duration and publishes the ad."""
        selected_duration = self.values[0]
        await self.cog.complete_approval(
            interaction=interaction,
            submission_id=self.submission_id,
            duration_key=selected_duration,
            review_message=self.review_message,
        )


class ExpiryDurationView(discord.ui.View):
    """Ephemeral view housing the expiry duration selection menu."""

    def __init__(self, cog: ModerationCog, submission_id: int, review_message: discord.Message) -> None:
        super().__init__(timeout=180)
        self.add_item(ExpiryDurationSelect(cog, submission_id, review_message))


class RejectionReasonSelect(discord.ui.Select):
    """Dropdown for staff to select a rejection reason or choose custom modal."""

    def __init__(self, cog: ModerationCog, submission_id: int, review_message: discord.Message) -> None:
        options = [
            discord.SelectOption(
                label="محتوى مخالف لسياسات النشر | Prohibited Content",
                value="المحتوى يخالف سياسات وقوانين نشر الإعلانات في السيرفر (Prohibited/Inappropriate Content)",
                description="Contains NSFW, harmful, or prohibited content",
                emoji="🚫",
            ),
            discord.SelectOption(
                label="روابط معطلة أو منتهية | Broken / Invalid Link",
                value="رابط الدعوة أو الموقع غير صالح أو منتهي الصلاحية (Invalid/Expired Invite Link)",
                description="The provided Discord invite or URL is broken",
                emoji="🔗",
            ),
            discord.SelectOption(
                label="قسم غير مناسب للإعلان | Miscategorized Ad",
                value="تم تقديم الإعلان في تصنيف غير مناسب لمحتواه (Submitted in wrong category)",
                description="Please re-submit in the proper channel/category",
                emoji="📌",
            ),
            discord.SelectOption(
                label="إعلان مكرر أو سبام | Duplicate or Spam",
                value="إعلان مكرر أو محاولة سبام غير مصرح بها (Duplicate or spam submission)",
                description="Repeated submissions within a short timeframe",
                emoji="⚠️",
            ),
            discord.SelectOption(
                label="كتابة سبب مخصص يدوي | Custom Reason...",
                value="CUSTOM_MODAL",
                description="Open a text box to write custom rejection feedback",
                emoji="✍️",
            ),
        ]
        super().__init__(
            placeholder="اختر سبب الرفض | Select Rejection Reason...",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.cog = cog
        self.submission_id = submission_id
        self.review_message = review_message

    async def callback(self, interaction: discord.Interaction) -> None:
        """Processes the chosen rejection reason."""
        selected_reason = self.values[0]
        if selected_reason == "CUSTOM_MODAL":
            modal = RejectionModal(self.cog, self.submission_id, self.review_message)
            await interaction.response.send_modal(modal)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        await self.cog.complete_rejection(
            interaction=interaction,
            submission_id=self.submission_id,
            reason=selected_reason,
            review_message=self.review_message,
        )


class RejectionReasonView(discord.ui.View):
    """Ephemeral view presenting the rejection reason dropdown."""

    def __init__(self, cog: ModerationCog, submission_id: int, review_message: discord.Message) -> None:
        super().__init__(timeout=180)
        self.add_item(RejectionReasonSelect(cog, submission_id, review_message))


class RejectionModal(discord.ui.Modal):
    """Modal prompting the staff member for an ad rejection rationale."""

    def __init__(self, cog: ModerationCog, submission_id: int, review_message: discord.Message) -> None:
        super().__init__(title=f"Reject Submission #{submission_id}"[:45])
        self.cog = cog
        self.submission_id = submission_id
        self.review_message = review_message

        self.reason_input = discord.ui.TextInput(
            label="Rejection Reason",
            style=discord.TextStyle.paragraph,
            placeholder="Explain why this ad was rejected (e.g., broken invite link, inappropriate language, miscategorized)...",
            min_length=5,
            max_length=600,
            required=True,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Processes rejection, sends user DM, and updates database records."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        reason = self.reason_input.value.strip()
        await self.cog.complete_rejection(
            interaction=interaction,
            submission_id=self.submission_id,
            reason=reason,
            review_message=self.review_message,
        )


class ModerationCog(commands.Cog, name="Moderation"):
    """Cog handling review interactions, webhook posting, and auto-expiry sweeper."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.expiry_sweeper.start()

    def cog_unload(self) -> None:
        """Cancels background tasks on cog unload."""
        self.expiry_sweeper.cancel()

    # -------------------------------------------------------------
    # 1. Global Component Interaction Listener (Button Routing)
    # -------------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Dispatches custom button clicks for approval and rejection persistently."""
        if interaction.type != discord.InteractionType.component:
            return

        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("adsentry:"):
            return

        parts = custom_id.split(":")
        if len(parts) != 3:
            return

        _, action, raw_id = parts
        if not raw_id.isdigit():
            return

        submission_id = int(raw_id)

        try:
            # Enforce staff authorization
            if not is_staff(interaction.user):
                embed = discord.Embed(
                    title="⛔ Access Denied",
                    description="Your account does not have an approved staff role or moderation permission to review ad submissions.",
                    color=discord.Color.red(),
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Fetch submission state from Supabase
            submission = await self.bot.db.get_submission(submission_id)
            if not submission:
                await interaction.response.send_message(
                    f"❌ Error: Submission #{submission_id} could not be located in the database.",
                    ephemeral=True,
                )
                return

            if submission.get("status") != "PENDING":
                status = submission.get("status")
                await interaction.response.send_message(
                    f"⚠️ This submission has already been resolved as **{status}**.",
                    ephemeral=True,
                )
                return

            if not interaction.message:
                await interaction.response.send_message("❌ Error: Message context not found.", ephemeral=True)
                return

            if action == "approve":
                # Present duration selection
                view = ExpiryDurationView(self, submission_id, interaction.message)
                embed = discord.Embed(
                    title="⏱️ Select Advertisement Duration",
                    description=(
                        f"Approving Submission **#{submission_id}** ({submission.get('title')}).\n\n"
                        "Please choose how long this ad will remain visible in the announcement channel before being automatically cleaned up:"
                    ),
                    color=discord.Color.blue(),
                )
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

            elif action == "reject":
                # Present rejection reasons view (instant response, zero timeout risk)
                view = RejectionReasonView(self, submission_id, interaction.message)
                embed = discord.Embed(
                    title="❌ رفض طلب الإعلان | Reject Submission",
                    description=(
                        f"أنت بصدد رفض الإعلان رقم **#{submission_id}** (`{submission.get('title')}`).\n\n"
                        "اختر سبب الرفض من القائمة أدناه ليتم إرساله تلقائياً لصاحب الإعلان، أو اختر **'كتابة سبب مخصص'** لكتابة تفاصيل إضافية:"
                    ),
                    color=discord.Color.red(),
                )
                embed.set_footer(text="AdSentry Moderation • اختر سبباً لإتمام الرفض فوراً")
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"Error handling staff review interaction for #{submission_id}: {e}", exc_info=True)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"❌ An unexpected error occurred while processing this action: `{e}`",
                    ephemeral=True,
                )

    # -------------------------------------------------------------
    # 2. Approval Flow & Webhook Publishing Execution
    # -------------------------------------------------------------

    async def complete_approval(
        self,
        interaction: discord.Interaction,
        submission_id: int,
        duration_key: str,
        review_message: discord.Message,
    ) -> None:
        """Executes publishing via webhook, calculates expiry, updates DB and notifies user."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        submission = await self.bot.db.get_submission(submission_id)
        if not submission or submission.get("status") != "PENDING":
            await interaction.followup.send("⚠️ This submission is no longer pending review.", ephemeral=True)
            return

        # Calculate expiration timestamp
        now = datetime.now(timezone.utc)
        duration_map = {
            "1d": timedelta(days=1),
            "1w": timedelta(days=7),
            "1m": timedelta(days=30),
            "1y": timedelta(days=365),
            "forever": None,
        }
        duration_delta = duration_map.get(duration_key)
        expires_at = (now + duration_delta) if duration_delta else None

        # Fetch announcement channel dynamically from server_configs
        guild_id = submission.get("guild_id") or (interaction.guild.id if interaction.guild else None)
        guild_config = await self.bot.db.get_guild_config(guild_id) if guild_id else None

        announcement_channel_id = guild_config.get("announcement_channel_id") if guild_config else None
        announcement_channel = None

        if announcement_channel_id:
            announcement_channel = self.bot.get_channel(announcement_channel_id)
            if not announcement_channel:
                try:
                    announcement_channel = await self.bot.fetch_channel(announcement_channel_id)
                except Exception:
                    announcement_channel = None

        if not isinstance(announcement_channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ Error: The announcement channel for this server is not configured or cannot be reached.\n"
                "An administrator must run `/ad-setup`.",
                ephemeral=True,
            )
            return

        # Publish ad via Webhook with submitter's identity & spawn discussion thread
        try:
            published_msg, thread, webhook_url = await publish_ad_via_webhook(
                channel=announcement_channel,
                user_name=submission.get("user_name", "Anonymous"),
                user_avatar_url=submission.get("user_avatar_url"),
                title=submission.get("title") or "Announcement",
                category=submission.get("category", "General"),
                content=submission.get("content", ""),
                user_id=submission.get("user_id"),
                image_url=submission.get("image_url"),
                reviewer_id=interaction.user.id,
                submission_id=submission_id,
            )
        except Exception as e:
            logger.error(f"Failed to publish ad #{submission_id} via webhook: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Failed to publish ad to #{announcement_channel.name}: {e}",
                ephemeral=True,
            )
            return

        # Record in Supabase
        try:
            await self.bot.db.update_submission_status(
                submission_id=submission_id,
                status="APPROVED",
                reviewed_by=interaction.user.id,
            )
            active_post = await self.bot.db.create_active_post(
                submission_id=submission_id,
                channel_id=announcement_channel.id,
                message_id=published_msg.id,
                webhook_url=webhook_url,
                expires_at=expires_at,
                thread_id=thread.id if thread else None,
                guild_id=guild_id,
            )
            # Update button on published ad with the database post_id and thread link
            if active_post and "id" in active_post:
                try:
                    from cogs.features import AdInterestView
                    from utils.webhook_publisher import get_or_create_webhook
                    wh = await get_or_create_webhook(announcement_channel)
                    await wh.edit_message(
                        message_id=published_msg.id,
                        view=AdInterestView(
                            post_id=active_post["id"],
                            interest_count=0,
                            thread_url=thread.jump_url if thread else None,
                        ),
                    )
                except Exception as ex:
                    logger.debug(f"Could not update published message view with post_id: {ex}")
        except Exception as e:
            logger.error(f"Failed to persist approval state to Supabase: {e}", exc_info=True)

        # Notify submitter via DM in their preferred language
        try:
            user = await self.bot.fetch_user(submission["user_id"])
            if user:
                is_arabic = (submission.get("language") == "ar") or any('\u0600' <= c <= '\u06FF' for c in (submission.get("category") or ""))
                if is_arabic:
                    expiry_text = f"<t:{int(expires_at.timestamp())}:F>" if expires_at else "دائم (بدون انتهاء)"
                    dm_embed = discord.Embed(
                        title="🎉 تمت الموافقة على إعلانك!",
                        description=(
                            f"أخبار سارة! تمت الموافقة على إعلانك **{submission.get('title')}** ونشره في {announcement_channel.mention}.\n\n"
                            f"**القسم:** {submission.get('category')}\n"
                            f"**مدة النشر:** {expiry_text}\n"
                            f"**رابط الإعلان:** [عرض الإعلان في القناة]({published_msg.jump_url})"
                        ),
                        color=discord.Color.green(),
                    )
                    dm_embed.set_footer(text="نظام إشعارات AdSentry")
                else:
                    expiry_text = f"<t:{int(expires_at.timestamp())}:F>" if expires_at else "Permanent (No Expiry)"
                    dm_embed = discord.Embed(
                        title="🎉 Your Advertisement Has Been Approved!",
                        description=(
                            f"Great news! Your ad **{submission.get('title')}** has been approved and published to {announcement_channel.mention}.\n\n"
                            f"**Category:** {submission.get('category')}\n"
                            f"**Duration / Expiry:** {expiry_text}\n"
                            f"**Link to Ad:** [View Announcement]({published_msg.jump_url})"
                        ),
                        color=discord.Color.green(),
                    )
                    dm_embed.set_footer(text="AdSentry Notification Service")
                await user.send(embed=dm_embed)
        except discord.Forbidden:
            logger.info(f"Could not send DM to User {submission['user_id']} (DMs closed).")
        except Exception as e:
            logger.warning(f"Error sending approval DM to user: {e}")

        # Update staff review message card in Discord
        try:
            old_embed = review_message.embeds[0] if review_message.embeds else discord.Embed()
            updated_embed = old_embed.copy()
            updated_embed.color = discord.Color.green()
            updated_embed.title = f"✅ Ad Submission #{submission_id} [APPROVED]"
            updated_embed.add_field(
                name="Review Decision",
                value=f"Approved by {interaction.user.mention}\n**Expiry:** {expires_at.strftime('%Y-%m-%d %H:%M UTC') if expires_at else 'Forever'}",
                inline=False,
            )
            updated_embed.add_field(
                name="Published Post",
                value=f"[Jump to Announcement]({published_msg.jump_url})",
                inline=False,
            )
            # Remove buttons
            await review_message.edit(embed=updated_embed, view=None)
        except Exception as e:
            logger.warning(f"Failed to update review message: {e}")

        # Auto-Repost & Relocate submission panel to the bottom of the announcement channel
        try:
            submissions_cog = self.bot.get_cog("Submissions")
            if submissions_cog and hasattr(submissions_cog, "relocate_panel"):
                await submissions_cog.relocate_panel(announcement_channel)
        except Exception as e:
            logger.error(f"Failed to auto-relocate submission panel in #{announcement_channel.name}: {e}", exc_info=True)

        await interaction.followup.send(
            f"✅ Ad #{submission_id} has been approved, published to {announcement_channel.mention}, and tracked for auto-expiry!",
            ephemeral=True,
        )

    # -------------------------------------------------------------
    # 3. Rejection Flow Execution
    # -------------------------------------------------------------

    async def complete_rejection(
        self,
        interaction: discord.Interaction,
        submission_id: int,
        reason: str,
        review_message: discord.Message,
    ) -> None:
        """Records rejection reason, notifies user via DM, and updates review message."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        submission = await self.bot.db.get_submission(submission_id)
        if not submission or submission.get("status") != "PENDING":
            await interaction.followup.send("⚠️ This submission is no longer pending review.", ephemeral=True)
            return

        # Update Supabase
        try:
            await self.bot.db.update_submission_status(
                submission_id=submission_id,
                status="REJECTED",
                reviewed_by=interaction.user.id,
                rejection_reason=reason,
            )
        except Exception as e:
            logger.error(f"Failed to update rejection in Supabase: {e}", exc_info=True)

        # Notify submitter via DM in their preferred language
        try:
            user = await self.bot.fetch_user(submission["user_id"])
            if user:
                is_arabic = (submission.get("language") == "ar") or any('\u0600' <= c <= '\u06FF' for c in (submission.get("category") or ""))
                if is_arabic:
                    dm_embed = discord.Embed(
                        title="❌ تم رفض طلب الإعلان الخاص بك",
                        description=(
                            f"نأسف لإبلاغك بأنه تمت مراجعة إعلانك **{submission.get('title')}** ولم يتم اعتماده في الوقت الحالي.\n\n"
                            f"**سبب الرفض:**\n> {reason}\n\n"
                            "يرجى تعديل إعلانك ليتوافق مع قوانين السيرفر وإعادة التقديم بعد انتهاء فترة الانتظار."
                        ),
                        color=discord.Color.red(),
                    )
                    dm_embed.set_footer(text="فريق إدارة AdSentry")
                else:
                    dm_embed = discord.Embed(
                        title="❌ Your Advertisement Submission Was Rejected",
                        description=(
                            f"Your submission **{submission.get('title')}** was reviewed by server staff and could not be approved at this time.\n\n"
                            f"**Rejection Reason:**\n> {reason}\n\n"
                            "Please adjust your ad to comply with server guidelines and re-submit once your cooldown concludes."
                        ),
                        color=discord.Color.red(),
                    )
                    dm_embed.set_footer(text="AdSentry Moderation Team")
                await user.send(embed=dm_embed)
        except discord.Forbidden:
            logger.info(f"Could not send rejection DM to User {submission['user_id']} (DMs closed).")
        except Exception as e:
            logger.warning(f"Error sending rejection DM to user: {e}")

        # Update staff review message card in Discord
        try:
            old_embed = review_message.embeds[0] if review_message.embeds else discord.Embed()
            updated_embed = old_embed.copy()
            updated_embed.color = discord.Color.red()
            updated_embed.title = f"❌ Ad Submission #{submission_id} [REJECTED]"
            updated_embed.add_field(
                name="Review Decision",
                value=f"Rejected by {interaction.user.mention}\n**Reason:** {reason}",
                inline=False,
            )
            # Remove buttons
            await review_message.edit(embed=updated_embed, view=None)
        except Exception as e:
            logger.warning(f"Failed to update review message: {e}")

        await interaction.followup.send(
            f"❌ Ad #{submission_id} has been marked as REJECTED and the user has been notified with the reason.",
            ephemeral=True,
        )

    # -------------------------------------------------------------
    # 4. Background Expiry Sweeper Task
    # -------------------------------------------------------------

    @tasks.loop(seconds=Config.EXPIRY_SWEEPER_INTERVAL)
    async def expiry_sweeper(self) -> None:
        """Periodically scans active_posts and removes expired announcements & discussion threads."""
        try:
            expired_posts = await self.bot.db.get_expired_posts()
            if not expired_posts:
                return

            logger.info(f"Expiry Sweeper detected {len(expired_posts)} post(s) ready for cleanup.")

            for post in expired_posts:
                post_id = post["id"]
                channel_id = post["channel_id"]
                message_id = post["message_id"]
                thread_id = post.get("thread_id")

                channel = self.bot.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel):
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except Exception:
                        channel = None

                # Delete announcement message
                if channel and isinstance(channel, discord.TextChannel):
                    try:
                        msg = await channel.fetch_message(message_id)
                        await msg.delete()
                        logger.info(f"Deleted expired ad message #{message_id} in #{channel.name}")
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        logger.warning(f"Failed to delete expired message #{message_id}: {e}")

                    # Clean up associated discussion thread
                    if thread_id:
                        try:
                            thread = channel.get_thread(thread_id) or await self.bot.fetch_channel(thread_id)
                            if isinstance(thread, discord.Thread):
                                await thread.delete()
                                logger.info(f"Deleted expired discussion thread #{thread_id}")
                        except discord.NotFound:
                            pass
                        except Exception as e:
                            logger.warning(f"Failed to delete expired thread #{thread_id}: {e}")

                # Mark expired in Supabase
                await self.bot.db.mark_post_expired(post_id)

        except Exception as e:
            logger.error(f"Error encountered during expiry sweeper loop: {e}", exc_info=True)

    @expiry_sweeper.before_loop
    async def before_expiry_sweeper(self) -> None:
        """Ensures bot is ready before starting the sweeper loop."""
        await self.bot.wait_until_ready()

    # -------------------------------------------------------------
    # 5. Cooldown Management Slash Commands (Admin / Staff)
    # -------------------------------------------------------------

    cooldown_group = app_commands.Group(
        name="ad-cooldown",
        description="Manage member advertisement cooldowns (Staff/Admin only)",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @cooldown_group.command(
        name="reset",
        description="Reset ad submission cooldown for a member or yourself (Staff only)",
    )
    @app_commands.describe(
        member="The member whose cooldown to reset (leave empty to reset your own cooldown)",
        clear_pending="Whether to also clear any pending ad awaiting review (default: True)",
    )
    async def reset_cooldown(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        clear_pending: bool = True,
    ) -> None:
        """Resets the submission cooldown for a target member."""
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ **عذراً، هذا الأمر مخصص للإدارة والمشرفين فقط.**\n"
                "You do not have staff permissions to reset cooldowns.",
                ephemeral=True,
            )
            return

        target = member or interaction.user
        await interaction.response.defer(ephemeral=True)

        existing_cd = await self.bot.db.get_user_cooldown(target.id)
        has_pending = await self.bot.db.has_pending_submission(target.id)

        # Reset cooldown in database
        await self.bot.db.reset_user_cooldown(target.id)

        # Cancel pending submissions if requested
        cancelled_count = 0
        if clear_pending and has_pending:
            cancelled_count = await self.bot.db.cancel_pending_submissions(target.id)

        embed = discord.Embed(
            title="🔄 تم تصفير وقت الانتظار | Cooldown Reset",
            description=(
                f"✅ **تم تصفير وقت الانتظار بنجاح للعضو:** {target.mention} (`{target.id}`)\n"
                f"**بواسطة المشرف:** {interaction.user.mention}\n\n"
                "يمكن للعضو الآن تقديم إعلان جديد فوراً دون الحاجة للانتظار! 🎉"
            ),
            color=discord.Color.green(),
        )
        if target.display_avatar:
            embed.set_thumbnail(url=target.display_avatar.url)

        if existing_cd:
            unix_ts = int(existing_cd.timestamp())
            embed.add_field(
                name="⏳ وقت الانتظار السابق | Previous Cooldown",
                value=f"كان ينتهي في: <t:{unix_ts}:F> (<t:{unix_ts}:R>)",
                inline=False,
            )
        else:
            embed.add_field(
                name="⏳ حالة الانتظار | Cooldown Status",
                value="لم يكن العضو في فترة انتظار نشطة (تم تنظيف السجل بالكامل).",
                inline=False,
            )

        if clear_pending:
            embed.add_field(
                name="📋 الطلبات المعلقة | Pending Ads",
                value=(
                    f"تم تنظيف وإلغاء `{cancelled_count}` طلب إعلان كان معلقاً قيد المراجعة."
                    if cancelled_count > 0
                    else "لا توجد طلبات إعلانات معلقة."
                ),
                inline=False,
            )

        embed.set_footer(text="AdSentry Cooldown Manager • نظام تصفير فترات الانتظار")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @cooldown_group.command(
        name="check",
        description="Check if a member (or yourself) is currently on ad submission cooldown",
    )
    @app_commands.describe(
        member="The member to check (leave empty to check your own cooldown)",
    )
    async def check_cooldown(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        """Checks the current cooldown status for a target member."""
        target = member or interaction.user
        await interaction.response.defer(ephemeral=True)

        existing_cd = await self.bot.db.get_user_cooldown(target.id)
        has_pending = await self.bot.db.has_pending_submission(target.id)

        if existing_cd:
            unix_ts = int(existing_cd.timestamp())
            embed = discord.Embed(
                title="⏳ وقت الانتظار نشط | Active Cooldown",
                description=(
                    f"العضو: {target.mention} (`{target.id}`)\n\n"
                    f"⛔ **لا يمكنه التقديم حالياً.**\n"
                    f"**موعد انتهاء الانتظار:** <t:{unix_ts}:F> (<t:{unix_ts}:R>)"
                ),
                color=discord.Color.orange(),
            )
        else:
            embed = discord.Embed(
                title="✅ لا يوجد وقت انتظار | No Cooldown",
                description=(
                    f"العضو: {target.mention} (`{target.id}`)\n\n"
                    "🎉 **يمكنه تقديم إعلان جديد الآن دون أي وقت انتظار!**"
                ),
                color=discord.Color.green(),
            )

        embed.add_field(
            name="📋 الطلبات المعلقة | Pending Status",
            value="⚠️ لديه إعلان قيد مراجعة الإدارة حالياً." if has_pending else "✅ لا توجد طلبات معلقة.",
            inline=False,
        )
        if target.display_avatar:
            embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="AdSentry Cooldown Manager")

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Extension entry point for loading the Moderation cog."""
    await bot.add_cog(ModerationCog(bot))
