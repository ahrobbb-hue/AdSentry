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
    """Detect staff from Discord permission grants, with optional role-ID overrides."""
    if not isinstance(user, discord.Member):
        return False

    # Use the effective member permissions and individual role permissions so
    # staff recognition works across guilds with different role names.
    staff_permission_flags = (
        "manage_messages",
        "manage_guild",
        "manage_channels",
        "manage_events",
        "manage_roles",
        "view_audit_log",
        "moderate_members",
    )
    if any(getattr(user.guild_permissions, flag, False) for flag in staff_permission_flags):
        return True
    user_roles = {r.id for r in user.roles}
    if user_roles.intersection(set(Config.STAFF_ROLE_IDS)):
        return True

    if any(
        getattr(role.permissions, flag, False)
        for role in user.roles
        for flag in staff_permission_flags
    ):
        return True

    # Backward-compatible fallback for custom Manager roles that have no
    # Discord moderation permissions assigned. Permission-based detection
    # above handles differently named roles in other guilds.
    manager_names = {"manager", "مدير", "المدير", "مدير السيرفر", "مدير الطاقم"}

    def normalized_role_name(name: str) -> str:
        # Discord role names often include emoji and separators, e.g. "🏹 | Manager".
        return " ".join("".join(ch if ch.isalnum() else " " for ch in name.casefold()).split())

    manager_role = next(
        (role for role in user.guild.roles if normalized_role_name(role.name) in manager_names),
        None,
    )
    return bool(manager_role and any(role.position >= manager_role.position for role in user.roles))


class StaffReviewView(discord.ui.View):
    """Interactive review buttons attached to new submission review cards."""

    def __init__(self, submission_id: int, can_request_changes: bool = True) -> None:
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
        if can_request_changes:
            self.add_item(
                discord.ui.Button(
                    label="Request Changes",
                    style=discord.ButtonStyle.secondary,
                    emoji="✏️",
                    custom_id=f"adsentry:request_changes:{submission_id}",
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
                emoji="⏰",
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
                label="محتوى مخالف | Prohibited Content",
                value="المحتوى يخالف سياسات وقوانين نشر الإعلانات في السيرفر (Prohibited/Inappropriate Content)",
                description="Contains NSFW, harmful, or prohibited content",
                emoji="🚫",
            ),
            discord.SelectOption(
                label="روابط معطلة | Broken Link",
                value="رابط الدعوة أو الموقع غير صالح أو منتهي الصلاحية (Invalid/Expired Invite Link)",
                description="The provided Discord invite or URL is broken",
                emoji="🔗",
            ),
            discord.SelectOption(
                label="قسم غير مناسب | Miscategorized",
                value="تم تقديم الإعلان في تصنيف غير مناسب لمحتواه (Submitted in wrong category)",
                description="Please re-submit in the proper channel/category",
                emoji="📌",
            ),
            discord.SelectOption(
                label="إعلان مكرر | Duplicate/Spam",
                value="إعلان مكرر أو محاولة سبام غير مصرح بها (Duplicate or spam submission)",
                description="Repeated submissions within a short timeframe",
                emoji="⚠️",
            ),
            discord.SelectOption(
                label="سبب مخصص | Custom Reason",
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


class ReviewRatingView(discord.ui.View):
    """Interactive rating stars attached to approval DMs for advertisers to rate staff review."""

    def __init__(self, submission_id: int, reviewer_id: int, guild_id: int) -> None:
        super().__init__(timeout=None)
        self.submission_id = submission_id
        self.reviewer_id = reviewer_id
        self.guild_id = guild_id

        for stars in range(1, 6):
            btn = discord.ui.Button(
                label=f"{stars} ⭐",
                style=discord.ButtonStyle.secondary if stars < 4 else discord.ButtonStyle.primary,
                custom_id=f"adsentry:rate:{submission_id}:{stars}",
            )
            self.add_item(btn)


class ChangeRequestModal(discord.ui.Modal):
    """Modal prompting the staff member for specific revision requests on a submission."""

    def __init__(self, cog: ModerationCog, submission_id: int, review_message: discord.Message) -> None:
        super().__init__(title=f"Request Changes #{submission_id}"[:45])
        self.cog = cog
        self.submission_id = submission_id
        self.review_message = review_message

        self.notes_input = discord.ui.TextInput(
            label="ملاحظات التعديل المطلوب | Revision Notes",
            style=discord.TextStyle.paragraph,
            placeholder="اكتب النقاط التي يجب على المعلن تعديلها (مثل: تصحيح الرابط، إزالة ألفاظ، تقليل الأسطر)...",
            min_length=5,
            max_length=800,
            required=True,
        )
        self.add_item(self.notes_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        notes = self.notes_input.value.strip()
        await self.cog.complete_change_request(
            interaction=interaction,
            submission_id=self.submission_id,
            notes=notes,
            review_message=self.review_message,
        )


class UserEditSubmissionView(discord.ui.View):
    """DM view sent to advertiser with an 'Edit Ad' button."""

    def __init__(self, submission_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="تعديل الإعلان | Edit Ad",
                style=discord.ButtonStyle.primary,
                emoji="✏️",
                custom_id=f"adsentry:user_edit:{submission_id}",
            )
        )


class UserEditSubmissionModal(discord.ui.Modal):
    """Modal presented to advertiser in DM to revise their pending submission."""

    def __init__(self, bot: commands.Bot, submission_id: int, current_title: str, current_content: str, current_tags: list[str] | None = None) -> None:
        super().__init__(title=f"تعديل الإعلان #{submission_id}"[:45])
        self.bot = bot
        self.submission_id = submission_id

        self.title_input = discord.ui.TextInput(
            label="عنوان الإعلان | Headline",
            default=current_title,
            min_length=2,
            max_length=100,
            required=True,
        )
        self.add_item(self.title_input)

        self.content_input = discord.ui.TextInput(
            label="المحتوى | Content",
            style=discord.TextStyle.paragraph,
            default=current_content,
            min_length=10,
            max_length=1800,
            required=True,
        )
        self.add_item(self.content_input)

        tags_str = " ".join(f"#{t.lstrip('#')}" for t in (current_tags or []))
        self.tags_input = discord.ui.TextInput(
            label="الوسوم | Tags (اختياري)",
            placeholder="مثال: #خدمات #برمجة #عرض",
            default=tags_str,
            required=False,
            max_length=120,
        )
        self.add_item(self.tags_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        new_title = self.title_input.value.strip()
        new_content = self.content_input.value.strip()
        raw_tags = self.tags_input.value.strip()
        tags_list = [t.lstrip("#").strip() for t in raw_tags.split() if t.strip()] if raw_tags else []

        try:
            # Update database
            res = await self.bot.db.update_submission_content(
                submission_id=self.submission_id,
                title=new_title,
                content=new_content,
                tags=tags_list,
            )
            if not res:
                await interaction.followup.send("⚠️ تعذر تحديث الإعلان أو أن الإعلان لم يعد معلقاً.", ephemeral=True)
                return

            # Post a fresh review card so the revised ad is visible and notifies staff.
            sub = await self.bot.db.get_submission(self.submission_id)
            review_notice_sent = False
            if sub:
                guild_id = sub.get("guild_id")
                cfg = await self.bot.db.get_guild_config(guild_id) if guild_id else None
                channel_id = cfg.get("staff_review_channel_id") if cfg else None
                review_channel = None
                if channel_id:
                    review_channel = self.bot.get_channel(int(channel_id))
                    if not isinstance(review_channel, discord.TextChannel):
                        try:
                            review_channel = await self.bot.fetch_channel(int(channel_id))
                        except discord.NotFound:
                            logger.warning("Staff review channel %s no longer exists", channel_id)
                        except discord.HTTPException:
                            logger.exception("Could not fetch staff review channel %s", channel_id)

                if isinstance(review_channel, discord.TextChannel) and review_channel.guild.id == int(guild_id):
                    curr_reqs = sub.get("change_requests", 0)
                    max_reqs = sub.get("max_change_requests", 3)
                    language = "🇸🇦 العربية" if sub.get("language") == "ar" else "🇬🇧 English"
                    review_embed = discord.Embed(
                        title=f"📥 Ad Submission #{self.submission_id} (✏️ Revision)",
                        description=(
                            f"**Headline:** {new_title}\n\n"
                            f"**Category:** {sub.get('category')}\n"
                            f"**Language:** {language}\n\n"
                            f"```{new_content}```"
                        ),
                        color=discord.Color.gold(),
                    )
                    author_name = f"{sub.get('user_name', 'Advertiser')} ({sub.get('user_id')})"
                    if sub.get("user_avatar_url"):
                        review_embed.set_author(name=author_name, icon_url=sub["user_avatar_url"])
                        review_embed.set_thumbnail(url=sub["user_avatar_url"])
                    else:
                        review_embed.set_author(name=author_name)
                    if sub.get("image_url"):
                        review_embed.set_image(url=sub["image_url"])
                    if tags_list:
                        review_embed.add_field(
                            name="Tags",
                            value=" ".join(f"`#{tag}`" for tag in tags_list[:5]),
                            inline=True,
                        )
                    review_embed.add_field(name="Status", value="🟡 Pending Staff Review", inline=True)
                    review_embed.add_field(
                        name="🔄 Advertiser Revision",
                        value=f"Revision {curr_reqs} of {max_reqs} requested changes; ready for review.",
                        inline=False,
                    )
                    review_embed.set_footer(text=f"Submission ID: {self.submission_id} • AdSentry Review Queue")

                    old_review_message = None
                    old_message_id = sub.get("review_message_id")
                    if old_message_id:
                        try:
                            old_review_message = await review_channel.fetch_message(int(old_message_id))
                        except discord.NotFound:
                            logger.info("Previous review card %s was deleted", old_message_id)
                        except discord.HTTPException:
                            logger.exception("Could not fetch previous review card %s", old_message_id)

                    try:
                        revision_message = await review_channel.send(
                            content=f"🔄 Revised advertisement #{self.submission_id} is ready for staff review.",
                            embed=review_embed,
                            view=StaffReviewView(
                                self.submission_id,
                                can_request_changes=(curr_reqs < max_reqs),
                            ),
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        review_notice_sent = True
                        if not await self.bot.db.update_review_message_id(self.submission_id, revision_message.id):
                            logger.error(
                                "Revision card %s sent, but review_message_id could not be saved for submission %s",
                                revision_message.id,
                                self.submission_id,
                            )
                        if old_review_message:
                            old_embed = old_review_message.embeds[0] if old_review_message.embeds else discord.Embed()
                            old_embed = old_embed.copy()
                            old_embed.add_field(
                                name="Superseded",
                                value=f"Updated revision is available in message {revision_message.id}.",
                                inline=False,
                            )
                            try:
                                await old_review_message.edit(embed=old_embed, view=None)
                            except discord.HTTPException:
                                logger.exception("Could not disable superseded review card for submission %s", self.submission_id)
                    except discord.HTTPException:
                        logger.exception("Could not send revised review card for submission %s", self.submission_id)
                else:
                    logger.error(
                        "Could not notify staff about revised submission %s: review channel is not configured or unavailable",
                        self.submission_id,
                    )
            else:
                logger.error("Could not reload revised submission %s for staff review", self.submission_id)

            if review_notice_sent:
                embed = discord.Embed(
                    title="✅ تم تحديث إعلانك وإرساله للمراجعة!",
                    description="تم حفظ التعديلات وإرسال نسخة جديدة إلى قناة مراجعة الإدارة.",
                    color=discord.Color.green(),
                )
            else:
                embed = discord.Embed(
                    title="⚠️ تم حفظ التعديل لكن تعذر إرساله للإدارة",
                    description=(
                        "تم حفظ التعديلات، لكن لم أتمكن من إرسال نسخة المراجعة إلى الإدارة. "
                        "يرجى التواصل مع الإدارة أو إعادة المحاولة لاحقاً."
                    ),
                    color=discord.Color.orange(),
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error saving user edit for #{self.submission_id}: {e}", exc_info=True)
            await interaction.followup.send(f"❌ حدث خطأ أثناء حفظ التعديل: `{e}`", ephemeral=True)


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
        if len(parts) < 3:
            return

        _, action, raw_id = parts[:3]
        if not raw_id.isdigit():
            return

        submission_id = int(raw_id)

        # ---------------------------------------------------------
        # Handle Advertiser Actions (Rating & User Editing) - No Staff Check
        # ---------------------------------------------------------
        if action == "rate":
            stars = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 5
            try:
                sub = await self.bot.db.get_submission(submission_id)
                if not sub:
                    await interaction.response.send_message("❌ لم يتم العثور على الإعلان.", ephemeral=True)
                    return

                reviewer_id = sub.get("reviewed_by")
                guild_id = sub.get("guild_id") or (interaction.guild.id if interaction.guild else 0)
                if not reviewer_id:
                    await interaction.response.send_message("❌ لم يتم تسجيل المشرف الذي راجع الإعلان.", ephemeral=True)
                    return

                await self.bot.db.save_review_rating(
                    submission_id=submission_id,
                    guild_id=guild_id,
                    user_id=interaction.user.id,
                    reviewer_id=reviewer_id,
                    rating=stars,
                )

                # Disable buttons in DM message
                try:
                    if interaction.message:
                        await interaction.message.edit(view=None)
                except Exception:
                    pass

                await interaction.response.send_message(
                    f"⭐ شكرًا لك! تم تسجيل تقييمك بنجاح ({stars}/5) للمشرف الذي راجع إعلانك.",
                    ephemeral=True,
                )
            except Exception as e:
                logger.error(f"Error handling rate interaction for #{submission_id}: {e}", exc_info=True)
                await interaction.response.send_message("❌ حدث خطأ أثناء تسجيل تقييمك.", ephemeral=True)
            return

        if action == "user_edit":
            try:
                sub = await self.bot.db.get_submission(submission_id)
                if not sub or sub.get("status") != "PENDING":
                    await interaction.response.send_message("⚠️ هذا الإعلان لم يعد معلقاً أو تمت معالجته بالفعل.", ephemeral=True)
                    return

                if interaction.user.id != sub.get("user_id"):
                    await interaction.response.send_message("⛔ لا يمكنك تعديل إعلان يخص مستخدم آخر.", ephemeral=True)
                    return

                curr_reqs = sub.get("change_requests", 0)
                max_reqs = sub.get("max_change_requests", 3)
                if curr_reqs >= max_reqs:
                    await interaction.response.send_message(
                        f"⛔ لقد استنفدت الحد الأقصى لمحاولات التعديل ({max_reqs}/{max_reqs}). يرجى انتظار قرار الإدارة النهائي.",
                        ephemeral=True,
                    )
                    return

                modal = UserEditSubmissionModal(
                    bot=self.bot,
                    submission_id=submission_id,
                    current_title=sub.get("title") or "",
                    current_content=sub.get("content") or "",
                    current_tags=sub.get("tags") or [],
                )
                await interaction.response.send_modal(modal)
            except Exception as e:
                logger.error(f"Error launching user edit modal for #{submission_id}: {e}", exc_info=True)
                await interaction.response.send_message("❌ حدث خطأ أثناء فتح نموذج التعديل.", ephemeral=True)
            return

        try:
            # Enforce staff authorization for staff actions
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

            elif action == "request_changes":
                curr_reqs = submission.get("change_requests", 0)
                max_reqs = submission.get("max_change_requests", 3)
                if curr_reqs >= max_reqs:
                    await interaction.response.send_message(
                        f"⚠️ هذا الإعلان وصل للحد الأقصى لطلبات التعديل ({max_reqs}/{max_reqs}). يرجى اتخاذ قرار القبول أو الرفض.",
                        ephemeral=True,
                    )
                    return

                modal = ChangeRequestModal(self, submission_id, interaction.message)
                await interaction.response.send_modal(modal)

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
                tags=submission.get("tags"),
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
                    dm_embed.set_footer(text="AdSentry Notification Service • Rate your reviewer below")
                rating_view = ReviewRatingView(
                    submission_id=submission_id,
                    reviewer_id=interaction.user.id,
                    guild_id=guild_id or 0,
                )
                await user.send(embed=dm_embed, view=rating_view)
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
    # 3.1. Change Request Flow Execution
    # -------------------------------------------------------------

    async def complete_change_request(
        self,
        interaction: discord.Interaction,
        submission_id: int,
        notes: str,
        review_message: discord.Message,
    ) -> None:
        """Records a change request, increments counter, notifies user with edit button, updates review card."""
        submission = await self.bot.db.get_submission(submission_id)
        if not submission or submission.get("status") != "PENDING":
            await interaction.followup.send("⚠️ This submission is no longer pending review.", ephemeral=True)
            return

        try:
            updated_sub = await self.bot.db.request_changes(
                submission_id=submission_id,
                reviewer_id=interaction.user.id,
                note=notes,
            )
            if not updated_sub:
                await interaction.followup.send("⚠️ تم استنفاد الحد الأقصى لمحاولات التعديل لهذا الإعلان.", ephemeral=True)
                return

            req_num = updated_sub.get("change_requests", 1)
            max_reqs = updated_sub.get("max_change_requests", 3)
            remaining = max(0, max_reqs - req_num)

            # Send DM with Edit button to submitter
            try:
                user = await self.bot.fetch_user(submission["user_id"])
                if user:
                    is_arabic = (submission.get("language") == "ar") or any('\u0600' <= c <= '\u06FF' for c in (submission.get("category") or ""))
                    if is_arabic:
                        dm_embed = discord.Embed(
                            title="✏️ طلب تعديل على إعلانك من إدارة السيرفر",
                            description=(
                                f"قام طاقم الإدارة بمراجعة إعلانك **{submission.get('title')}** وطلب إجراء بعض التعديلات قبل اعتماده:\n\n"
                                f"📝 **الملاحظات والمطلوب تعديله:**\n> {notes}\n\n"
                                f"⚠️ **المحاولات المتبقية:** لديك `{remaining}` محاولة متبقية لتعديل هذا الإعلان (من أصل {max_reqs}).\n"
                                "اضغط على الزر أدناه لتعديل العنوان والمحتوى فوراً."
                            ),
                            color=discord.Color.gold(),
                        )
                        dm_embed.set_footer(text="AdSentry Moderation • طلب تعديل")
                    else:
                        dm_embed = discord.Embed(
                            title="✏️ Revision Requested for Your Advertisement",
                            description=(
                                f"Staff reviewed your submission **{submission.get('title')}** and requested revisions before approval:\n\n"
                                f"📝 **Reviewer Notes:**\n> {notes}\n\n"
                                f"⚠️ **Remaining Attempts:** You have `{remaining}` attempt(s) remaining (out of {max_reqs}).\n"
                                "Click the button below to edit your title and content now."
                            ),
                            color=discord.Color.gold(),
                        )
                        dm_embed.set_footer(text="AdSentry Moderation • Change Request")

                    edit_view = UserEditSubmissionView(submission_id)
                    await user.send(embed=dm_embed, view=edit_view)
            except discord.Forbidden:
                logger.info(f"Could not send change request DM to User {submission['user_id']} (DMs closed).")
            except Exception as e:
                logger.warning(f"Error sending change request DM: {e}")

            # Update staff review card
            try:
                old_embed = review_message.embeds[0] if review_message.embeds else discord.Embed()
                updated_embed = old_embed.copy()
                updated_embed.color = discord.Color.gold()
                updated_embed.title = f"📥 Ad Submission #{submission_id} [✏️ طلب تعديل ({req_num}/{max_reqs})]"
                updated_embed.add_field(
                    name=f"✏️ طلب تعديل بواسطة {interaction.user.display_name}",
                    value=f"**الملاحظات:** {notes}\n**المحاولات:** `{req_num}/{max_reqs}`",
                    inline=False,
                )
                # Keep buttons available for staff if they still have attempts
                view = StaffReviewView(submission_id, can_request_changes=(req_num < max_reqs))
                await review_message.edit(embed=updated_embed, view=view)
            except Exception as e:
                logger.warning(f"Failed to update review message after change request: {e}")

            await interaction.followup.send(
                f"✅ تم إرسال طلب التعديل رقم ({req_num}/{max_reqs}) لصاحب الإعلان بنجاح في الخاص مع زر التعديل.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error completing change request for #{submission_id}: {e}", exc_info=True)
            await interaction.followup.send(f"❌ حدث خطأ أثناء إرسال طلب التعديل: `{e}`", ephemeral=True)

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

            # ---------------------------------------------------------
            # 4.1. Process 24-Hour Expiry Alerts
            # ---------------------------------------------------------
            try:
                alert_posts = await self.bot.db.get_posts_needing_expiry_alert(hours_before=24)
                for ap in alert_posts:
                    ap_id = ap["id"]
                    sub_info = ap.get("submissions") or {}
                    user_id = sub_info.get("user_id")
                    title = sub_info.get("title") or "إعلانك"
                    expires_at_str = ap.get("expires_at")

                    if user_id and expires_at_str:
                        try:
                            user = await self.bot.fetch_user(user_id)
                            if user:
                                dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
                                ts = int(dt.timestamp())
                                alert_embed = discord.Embed(
                                    title="⏰ تنبيه: اقتراب موعد انتهاء إعلانك | Ad Expiring Soon",
                                    description=(
                                        f"نود تذكيرك بأن إعلانك **{title}** سينتهي خلال أقل من 24 ساعة!\n\n"
                                        f"⏳ **موعد الانتهاء:** <t:{ts}:F> (<t:{ts}:R>)\n\n"
                                        "بعد انتهاء الإعلان، ستتمكن من تقديم إعلان جديد مجدداً. شكراً لاستخدامك AdSentry!"
                                    ),
                                    color=discord.Color.gold(),
                                )
                                alert_embed.set_footer(text="AdSentry Expiry Alert System")
                                await user.send(embed=alert_embed)
                        except discord.Forbidden:
                            pass
                        except Exception as dm_err:
                            logger.warning(f"Failed to send expiry alert DM to {user_id}: {dm_err}")

                    await self.bot.db.mark_expiry_alert_sent(ap_id)
            except Exception as alert_ex:
                logger.error(f"Error processing expiry alerts: {alert_ex}", exc_info=True)

            # ---------------------------------------------------------
            # 4.2. Process Expired Pins
            # ---------------------------------------------------------
            try:
                expired_pins = await self.bot.db.get_expired_pins()
                for pin in expired_pins:
                    await self.bot.db.unpin_ad(pin["id"])
                    logger.info(f"Automatically unpinned ad #{pin['id']} after pin duration elapsed.")
            except Exception as pin_ex:
                logger.error(f"Error processing expired pins: {pin_ex}", exc_info=True)

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

    # -------------------------------------------------------------
    # 6. Pinned Ads Management Slash Commands (Staff / Admin)
    # -------------------------------------------------------------

    pin_group = app_commands.Group(
        name="ad-pin",
        description="Pin or unpin featured announcements (Staff/Admin only)",
        default_permissions=discord.Permissions(manage_messages=True),
    )

    @pin_group.command(
        name="add",
        description="Pin an active ad announcement to mark it as featured",
    )
    @app_commands.describe(
        message_id="The Discord message ID of the published advertisement",
        hours="How many hours it should stay pinned (leave empty for permanent pin)",
    )
    async def pin_active_ad(
        self,
        interaction: discord.Interaction,
        message_id: str,
        hours: int | None = None,
    ) -> None:
        """Pins an active advertisement post."""
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ هذا الأمر مخصص للمشرفين فقط.", ephemeral=True)
            return

        if not message_id.isdigit():
            await interaction.response.send_message("❌ يرجى إدخال Message ID صحيح.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            client = await self.bot.db.get_client()
            res = await client.table("active_posts").select("id, channel_id, message_id").eq("message_id", int(message_id)).limit(1).execute()
            if not res.data:
                await interaction.followup.send("❌ لم يتم العثور على إعلان منشور بهذا المعرف (Message ID).", ephemeral=True)
                return

            post = res.data[0]
            await self.bot.db.pin_ad(post_id=post["id"], staff_id=interaction.user.id, duration_hours=hours)

            # Try to Discord pin the message in channel
            channel = self.bot.get_channel(post["channel_id"])
            if channel and isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(int(message_id))
                    await msg.pin(reason=f"AdSentry Featured Pin by {interaction.user}")
                except Exception as ex:
                    logger.warning(f"Could not Discord pin message {message_id}: {ex}")

            duration_str = f"لمدة `{hours}` ساعة" if hours else "بشكل دائم"
            await interaction.followup.send(
                f"📌 تم تثبيت الإعلان بنجاح {duration_str}!",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Error pinning ad {message_id}: {e}", exc_info=True)
            await interaction.followup.send(f"❌ حدث خطأ أثناء تثبيت الإعلان: `{e}`", ephemeral=True)

    @pin_group.command(
        name="remove",
        description="Unpin a featured advertisement announcement",
    )
    @app_commands.describe(
        message_id="The Discord message ID of the published advertisement",
    )
    async def unpin_active_ad(
        self,
        interaction: discord.Interaction,
        message_id: str,
    ) -> None:
        """Unpins an active advertisement post."""
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ هذا الأمر مخصص للمشرفين فقط.", ephemeral=True)
            return

        if not message_id.isdigit():
            await interaction.response.send_message("❌ يرجى إدخال Message ID صحيح.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            client = await self.bot.db.get_client()
            res = await client.table("active_posts").select("id, channel_id, message_id").eq("message_id", int(message_id)).limit(1).execute()
            if not res.data:
                await interaction.followup.send("❌ لم يتم العثور على إعلان منشور بهذا المعرف.", ephemeral=True)
                return

            post = res.data[0]
            await self.bot.db.unpin_ad(post_id=post["id"])

            channel = self.bot.get_channel(post["channel_id"])
            if channel and isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(int(message_id))
                    await msg.unpin(reason=f"AdSentry Unpin by {interaction.user}")
                except Exception:
                    pass

            await interaction.followup.send("📌 تم إلغاء تثبيت الإعلان بنجاح.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error unpinning ad {message_id}: {e}", exc_info=True)
            await interaction.followup.send(f"❌ حدث خطأ أثناء إلغاء التثبيت: `{e}`", ephemeral=True)

    # -------------------------------------------------------------
    # 7. Reviewer Stats Command
    # -------------------------------------------------------------

    @app_commands.command(
        name="reviewer-stats",
        description="⭐ عرض إحصائيات وتقييمات المراجعين في السيرفر | View reviewer ratings & stats",
    )
    @app_commands.describe(
        reviewer="المشرف المراد عرض تقييماته (اتركه فارغاً لعرض تقييماتك)",
    )
    async def reviewer_stats(
        self,
        interaction: discord.Interaction,
        reviewer: discord.Member | None = None,
    ) -> None:
        """Shows reviewer rating stats."""
        target = reviewer or interaction.user
        if not interaction.guild:
            await interaction.response.send_message("❌ هذا الأمر داخل السيرفرات فقط.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        stats = await self.bot.db.get_reviewer_stats(target.id, interaction.guild.id)
        avg = stats.get("avg_rating", 0.0)
        cnt = stats.get("review_count", 0)

        stars_visual = "⭐" * int(round(avg)) if avg > 0 else "لا توجد تقييمات بعد"
        embed = discord.Embed(
            title=f"⭐ تقييم المشرف: {target.display_name}",
            description=(
                f"👤 **المشرف:** {target.mention}\n\n"
                f"📊 **متوسط التقييم:** `{avg}/5.0`\n"
                f"✨ **النجوم:** {stars_visual}\n"
                f"📝 **عدد التقييمات المستلمة:** `{cnt}` تقييم"
            ),
            color=discord.Color.gold(),
        )
        if target.display_avatar:
            embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="AdSentry Staff Quality System")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Extension entry point for loading the Moderation cog."""
    await bot.add_cog(ModerationCog(bot))
