"""
AdSentry Submissions Cog
Handles category selection, submission modals, auto-moderation, anti-spam cooldowns, and Supabase integration.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from cogs.moderation import StaffReviewView, is_staff
from utils import screen_content, resolve_category_theme

logger = logging.getLogger("AdSentry.Submissions")


def is_server_admin_or_owner(user: discord.Member | discord.User, guild: discord.Guild | None) -> bool:
    """Checks whether the user is server owner, administrator, or authorized staff."""
    if not isinstance(user, discord.Member):
        return False
    if guild and guild.owner_id == user.id:
        return True
    if user.guild_permissions.administrator:
        return True
    return is_staff(user)


class AdTemplateSaveModal(discord.ui.Modal):
    """Modal allowing users to save an ad template for recurring use."""

    def __init__(self, template_name: str, category: str, bot: commands.Bot) -> None:
        super().__init__(title=f"حفظ قالب: {template_name}"[:45])
        self.template_name = template_name
        self.category = category
        self.bot = bot

        self.ad_title = discord.ui.TextInput(
            label="عنوان الإعلان الافتراضي",
            placeholder="عنوان جذاب للإعلان...",
            min_length=3,
            max_length=100,
            required=True,
        )
        self.add_item(self.ad_title)

        self.ad_content = discord.ui.TextInput(
            label="محتوى الإعلان الافتراضي",
            style=discord.TextStyle.paragraph,
            placeholder="اكتب نص الإعلان، المميزات، وروابط التواصل...",
            min_length=10,
            max_length=1800,
            required=True,
        )
        self.add_item(self.ad_content)

        self.ad_tags = discord.ui.TextInput(
            label="الوسوم الافتراضية / Tags (اختياري)",
            placeholder="مثال: #متجر #خدمات #خصم",
            required=False,
            max_length=150,
        )
        self.add_item(self.ad_tags)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Parse tags
        tags_raw = self.ad_tags.value.strip()
        tags = [t.strip() for t in re.split(r"[\s,]+", tags_raw) if t.strip()] if tags_raw else []

        try:
            await self.bot.db.save_template(
                user_id=interaction.user.id,
                guild_id=interaction.guild.id,
                name=self.template_name,
                category=self.category,
                title=self.ad_title.value.strip(),
                content=self.ad_content.value.strip(),
                tags=tags,
            )

            embed = discord.Embed(
                title="✅ تم حفظ قالب الإعلان بنجاح",
                description=(
                    f"**اسم القالب:** `{self.template_name}`\n"
                    f"**القسم:** `{self.category}`\n"
                    f"**العنوان:** {self.ad_title.value.strip()}\n\n"
                    f"🚀 **لتقديم إعلان سريع بهذا القالب مستقبلاً:**\n"
                    f"`/ad-template use name:{self.template_name}`"
                ),
                color=discord.Color.green(),
            )
            embed.set_footer(text="AdSentry Templates • قوالب الإعلانات المتكررة")
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error saving template '{self.template_name}': {e}", exc_info=True)
            await interaction.followup.send(f"❌ حدث خطأ أثناء حفظ القالب: `{e}`", ephemeral=True)


class AdSubmissionModal(discord.ui.Modal):
    """Modal for collecting advertisement title, content, tags, and banner image in user's language."""

    def __init__(
        self,
        category: str,
        bot: commands.Bot,
        lang: str = "en",
        is_admin: bool = False,
        default_title: str = "",
        default_content: str = "",
        default_image: str = "",
        default_tags: str = "",
    ) -> None:
        self.lang = lang
        self.category = category
        self.bot = bot
        self.is_admin = is_admin

        if lang == "ar":
            modal_title = f"تقديم إعلان: {category}"[:45]
            title_label = "عنوان الإعلان"
            title_placeholder = "مثال: منصة المطورين - أفضل مجتمع لتعلم البرمجة"
            content_label = "محتوى الإعلان والروابط"
            content_placeholder = "اكتب تفاصيل إعلانك ومميزاته وروابط التواصل والسيرفر هنا..."
            tags_label = "الوسوم / Tags (اختياري)"
            tags_placeholder = "مثال: #خدمات #برمجة #عرض_خاص"
            image_label = "رابط بنر أو صورة الإعلان (اختياري)"
            image_placeholder = "https://i.imgur.com/... أو رابط ينتهي بـ png/jpg/gif"
        else:
            modal_title = f"Submit Ad: {category}"[:45]
            title_label = "Ad Headline / Title"
            title_placeholder = "e.g. Apex Coding Hub - Join top developers"
            content_label = "Advertisement Content & Links"
            content_placeholder = "Describe your server, bot, or service in detail, including features and links..."
            tags_label = "Tags (Optional)"
            tags_placeholder = "e.g. #services #bot #dev"
            image_label = "Banner / Image URL (Optional)"
            image_placeholder = "https://i.imgur.com/... or direct image link"

        super().__init__(title=modal_title)

        self.ad_title = discord.ui.TextInput(
            label=title_label,
            placeholder=title_placeholder,
            default=default_title,
            min_length=2,
            max_length=100,
            required=True,
        )
        self.add_item(self.ad_title)

        self.ad_content = discord.ui.TextInput(
            label=content_label,
            style=discord.TextStyle.paragraph,
            placeholder=content_placeholder,
            default=default_content,
            min_length=10,
            max_length=1800,
            required=True,
        )
        self.add_item(self.ad_content)

        self.ad_tags = discord.ui.TextInput(
            label=tags_label,
            placeholder=tags_placeholder,
            default=default_tags,
            required=False,
            max_length=150,
        )
        self.add_item(self.ad_tags)

        self.ad_image = discord.ui.TextInput(
            label=image_label,
            placeholder=image_placeholder,
            default=default_image,
            required=False,
            max_length=400,
        )
        self.add_item(self.ad_image)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Processes the ad submission with auto-moderation, cooldown checks, and DB storage."""
        # Defer immediately to prevent Discord's 3-second modal timeout
        await interaction.response.defer(ephemeral=True)

        try:
            title = self.ad_title.value.strip()
            content = self.ad_content.value.strip()

            # Process optional image URL from input field or content
            raw_img = self.ad_image.value.strip() if self.ad_image.value else ""
            image_url = None
            if raw_img and (raw_img.startswith("http://") or raw_img.startswith("https://")):
                image_url = raw_img
            else:
                img_match = re.search(
                    r'(https?://[^\s<>"\'{}|\\^`]+(?:\.(?:png|jpe?g|gif|webp)|cdn\.discordapp\.com/attachments/|media\.discordapp\.net/attachments/|i\.imgur\.com/)[^\s<>"\'{}|\\^`]*)',
                    content,
                    re.IGNORECASE,
                )
                if img_match:
                    image_url = img_match.group(1).strip()

            # Verify admin/owner privileges for server promotions
            is_admin = self.is_admin or is_server_admin_or_owner(interaction.user, interaction.guild)

            # Block non-admins from submitting under Discord Servers category
            if ("سيرفرات" in self.category or "Servers" in self.category) and not is_admin:
                if self.lang == "ar":
                    embed = discord.Embed(
                        title="⛔ الترويج لسيرفرات الديسكورد مخصص للإدارة فقط",
                        description=(
                            "عذراً، **الترويج لسيرفرات الديسكورد مقتصر حصرياً على مالك وإدارة السيرفر**.\n\n"
                            "لا يمكن للأعضاء العاديين نشر إعلانات ترويجية لسيرفرات ديسكورد أخرى.\n"
                            "يمكنك الترويج لمشاريعك البرمجية، بوتاتك، أو خدماتك فقط."
                        ),
                        color=discord.Color.red(),
                    )
                else:
                    embed = discord.Embed(
                        title="⛔ Discord Server Promotion is Admin-Only",
                        description=(
                            "Advertising Discord servers is strictly reserved for Server Administrators and Owners.\n\n"
                            "Regular members cannot submit advertisements promoting other Discord servers.\n"
                            "You are welcome to promote your software projects, bots, or services."
                        ),
                        color=discord.Color.red(),
                    )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            # -------------------------------------------------------------
            # 1. Advanced Auto-Moderation & Content Guard Screening
            # -------------------------------------------------------------
            is_flagged, flagged_terms = screen_content(title, content, allow_discord_invites=is_admin)
            if is_flagged:
                # Check if flagged specifically for unauthorized Discord server invite
                has_invite_violation = any(
                    "invite" in str(f).lower() or "discord.gg" in str(f).lower() for f in flagged_terms
                )
                if has_invite_violation and not is_admin:
                    if self.lang == "ar":
                        embed = discord.Embed(
                            title="⛔ روابط سيرفرات الديسكورد مخصصة للإدارة فقط",
                            description=(
                                "🚫 **لا يُسمح للأعضاء بنشر روابط دعوة لسيرفرات ديسكورد أخرى.**\n\n"
                                "الترويج لسيرفرات الديسكورد مقتصر حصرياً على **مالك وإدارة السيرفر**.\n\n"
                                "💡 **ما يمكنك الترويج له بحرية كعضو:**\n"
                                "• مشاريعك ومواقعك البرمجية\n"
                                "• بوتاتك وأدواتك التقنية\n"
                                "• خدماتك والعمل الحر\n"
                                "• قنوات اليوتيوب والمصادر التعليمية\n\n"
                                "يرجى تعديل الإعلان وحذف رابط سيرفر الديسكورد ثم إعادة الإرسال."
                            ),
                            color=discord.Color.red(),
                        )
                        embed.set_footer(text="سياسات النشر • AdSentry Content Guard")
                    else:
                        embed = discord.Embed(
                            title="⛔ Discord Server Invites Prohibited",
                            description=(
                                "🚫 **Regular members may not post Discord server invite links.**\n\n"
                                "Promoting Discord servers is strictly reserved for **Server Administrators & Owners**.\n\n"
                                "💡 **What you can advertise as a member:**\n"
                                "• Software projects & websites\n"
                                "• Discord bots & tools\n"
                                "• Freelance developer services\n"
                                "• Tutorials & educational resources\n\n"
                                "Please remove the Discord invite link and submit your ad again."
                            ),
                            color=discord.Color.red(),
                        )
                        embed.set_footer(text="Advertising Guidelines • AdSentry Content Guard")
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return

                if self.lang == "ar":
                    embed = discord.Embed(
                        title="🛡️ تم حظر الإعلان فوراً بواسطة درع الحماية التلقائي",
                        description=(
                            "🚫 **تم رفض الإعلان تلقائياً ولم يتم إرساله للإدارة.**\n\n"
                            "تم رصد محتوى ينتهك سياسات المجتمع (محتوى جنسي أو إباحي، ألفاظ مسيئة أو عنصرية، أو روابط محظورة).\n\n"
                            f"⚠️ **الأنماط المخالفة المكتشفة:** `{', '.join(flagged_terms[:6])}`\n\n"
                            "يرجى الالتزام بقواعد النشر وسياسات الخادم وعدم تكرار المحتوى المخالف."
                        ),
                        color=discord.Color.red(),
                    )
                    embed.set_footer(text="نظام حماية وتصفية الإعلانات • AdSentry AutoMod")
                else:
                    embed = discord.Embed(
                        title="🛡️ Submission Blocked by Content Guard",
                        description=(
                            "🚫 **Your advertisement was automatically rejected and not sent to staff.**\n\n"
                            "Detected content that violates our community standards (sexual/NSFW terms, profanity, hate speech, or prohibited links).\n\n"
                            f"⚠️ **Detected violations:** `{', '.join(flagged_terms[:6])}`\n\n"
                            "Please revise your submission in accordance with server advertising guidelines."
                        ),
                        color=discord.Color.red(),
                    )
                    embed.set_footer(text="AdSentry Auto-Moderation Guard")

                await interaction.followup.send(embed=embed, ephemeral=True)
                logger.warning(
                    f"Blocked offending submission from {interaction.user} (ID: {interaction.user.id}) due to: {flagged_terms}"
                )
                return

            # -------------------------------------------------------------
            # 2. Anti-Spam / Cooldown & Duplicate Pending Checks
            # -------------------------------------------------------------
            cooldown_until = await self.bot.db.get_user_cooldown(interaction.user.id)
            if cooldown_until:
                unix_ts = int(cooldown_until.timestamp())
                if self.lang == "ar":
                    embed = discord.Embed(
                        title="⏳ أنت في فترة الانتظار (Cooldown)",
                        description=(
                            "لقد قمت بتقديم إعلان مؤخراً ولا يمكنك تقديم إعلان جديد حالياً.\n\n"
                            f"**موعد التقديم القادم:** <t:{unix_ts}:F> (<t:{unix_ts}:R>)"
                        ),
                        color=discord.Color.orange(),
                    )
                else:
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
                if self.lang == "ar":
                    embed = discord.Embed(
                        title="⚠️ لديك طلب إعلان معلق بالفعل",
                        description=(
                            "لديك بالفعل إعلان قيد مراجعة الإدارة حالياً.\n"
                            "يرجى الانتظار حتى يتم قبول أو رفض إعلانك السابق قبل إرسال إعلان جديد."
                        ),
                        color=discord.Color.gold(),
                    )
                else:
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
                if self.lang == "ar":
                    embed = discord.Embed(
                        title="⚙️ يلزم إعداد السيرفر أولاً",
                        description=(
                            "لم يتم تكوين قناة مراجعة الإعلانات في هذا السيرفر بعد.\n"
                            "يجب على مشرف السيرفر تشغيل الأمر `/ad-setup`."
                        ),
                        color=discord.Color.gold(),
                    )
                else:
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
            # 2. Extract Tags and Build Preview
            # -------------------------------------------------------------
            raw_tags = self.ad_tags.value.strip() if hasattr(self, "ad_tags") and self.ad_tags.value else ""
            tags_list = [t.lstrip("#").strip() for t in raw_tags.split() if t.strip()] if raw_tags else []

            theme = resolve_category_theme(self.category)
            emoji = theme["emoji"]
            color = theme["color"]
            badge_label = theme["badge"]

            # Build preview embed
            preview_embed = discord.Embed(
                title=f"{emoji} {title}",
                description=f"{content}\n\n──────────────────────────────",
                color=color,
            )
            preview_embed.set_author(
                name=f"{interaction.user.display_name} • معاينة قبل النشر | Preview",
                icon_url=interaction.user.display_avatar.url,
            )
            preview_embed.set_thumbnail(url=interaction.user.display_avatar.url)
            if image_url:
                preview_embed.set_image(url=image_url)

            preview_embed.add_field(name="🏷️ القسم | Category", value=f"`{badge_label}`", inline=True)
            if tags_list:
                preview_embed.add_field(name="🔖 الوسوم | Tags", value=" ".join(f"`#{t}`" for t in tags_list[:5]), inline=True)
            preview_embed.add_field(name="👤 المعلن | Advertiser", value=f"{interaction.user.mention}", inline=True)
            preview_embed.set_footer(text="🔍 هذه معاينة لإعلانك كما سيظهر للأعضاء. اضغط تأكيد للإرسال للمراجعة.")

            view = AdPreviewView(
                bot=self.bot,
                category=self.category,
                title=title,
                content=content,
                image_url=image_url,
                tags=tags_list,
                lang=self.lang,
                is_admin=is_admin,
            )

            msg_text = (
                "🔍 **معاينة الإعلان قبل الإرسال:**\n"
                "راجع إعلانك بعناية، إذا كان كل شيء جاهزاً اضغط **تأكيد وإرسال للمراجعة**، أو اضغط **تعديل** لتغيير المحتوى."
                if self.lang == "ar"
                else (
                    "🔍 **Advertisement Preview Before Submission:**\n"
                    "Review your ad carefully. If everything looks good, click **Confirm & Submit**, or click **Edit** to modify."
                )
            )
            await interaction.followup.send(content=msg_text, embed=preview_embed, view=view, ephemeral=True)

        except Exception as e:
            logger.error(f"Unexpected error in AdSubmissionModal.on_submit: {e}", exc_info=True)
            embed = discord.Embed(
                title="❌ Error Processing Submission",
                description=f"An unexpected error occurred while processing your ad:\n`{e}`\n\nPlease try again.",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)


class AdPreviewView(discord.ui.View):
    """View allowing the user to confirm, edit, or cancel their ad submission after preview."""

    def __init__(
        self,
        bot: commands.Bot,
        category: str,
        title: str,
        content: str,
        image_url: str | None,
        tags: list[str],
        lang: str,
        is_admin: bool,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.category = category
        self.title = title
        self.content = content
        self.image_url = image_url
        self.tags = tags
        self.lang = lang
        self.is_admin = is_admin

        confirm_label = "تأكيد وإرسال للمراجعة ✅" if lang == "ar" else "Confirm & Submit ✅"
        edit_label = "تعديل ✏️" if lang == "ar" else "Edit ✏️"
        cancel_label = "إلغاء ❌" if lang == "ar" else "Cancel ❌"

        confirm_btn = discord.ui.Button(label=confirm_label, style=discord.ButtonStyle.success)
        confirm_btn.callback = self.confirm_submission
        self.add_item(confirm_btn)

        edit_btn = discord.ui.Button(label=edit_label, style=discord.ButtonStyle.secondary)
        edit_btn.callback = self.edit_submission
        self.add_item(edit_btn)

        cancel_btn = discord.ui.Button(label=cancel_label, style=discord.ButtonStyle.danger)
        cancel_btn.callback = self.cancel_submission
        self.add_item(cancel_btn)

    async def edit_submission(self, interaction: discord.Interaction) -> None:
        """Re-opens the submission modal populated with current inputs."""
        raw_tags = " ".join(f"#{t}" for t in self.tags)
        modal = AdSubmissionModal(
            category=self.category,
            bot=self.bot,
            lang=self.lang,
            is_admin=self.is_admin,
            default_title=self.title,
            default_content=self.content,
            default_image=self.image_url or "",
            default_tags=raw_tags,
        )
        await interaction.response.send_modal(modal)

    async def cancel_submission(self, interaction: discord.Interaction) -> None:
        """Cancels submission."""
        msg = "❌ تم إلغاء تقديم الإعلان." if self.lang == "ar" else "❌ Ad submission cancelled."
        await interaction.response.edit_message(content=msg, embed=None, view=None)

    async def confirm_submission(self, interaction: discord.Interaction) -> None:
        """Finalizes the submission into Supabase and sends it to staff review channel."""
        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild_id
        guild_config = await self.bot.db.get_guild_config(guild_id) if guild_id else None

        if not guild_config or not guild_config.get("staff_review_channel_id"):
            msg = (
                "لم يتم تكوين قناة مراجعة الإعلانات في هذا السيرفر بعد. يجب على المشرف تشغيل `/ad-setup`."
                if self.lang == "ar"
                else "Staff review channel is not configured. An admin must run `/ad-setup`."
            )
            await interaction.followup.send(f"⚙️ {msg}", ephemeral=True)
            return

        # Double check pending and cooldown
        has_pending = await self.bot.db.has_pending_submission(interaction.user.id)
        if has_pending:
            msg = "⚠️ لديك طلب إعلان معلق بالفعل قيد المراجعة." if self.lang == "ar" else "⚠️ You already have a pending submission."
            await interaction.followup.send(msg, ephemeral=True)
            return

        cooldown_until = await self.bot.db.get_user_cooldown(interaction.user.id)
        if cooldown_until:
            unix_ts = int(cooldown_until.timestamp())
            msg = f"⏳ أنت في فترة الانتظار حتى <t:{unix_ts}:R>."
            await interaction.followup.send(msg, ephemeral=True)
            return

        # Save to Supabase
        submission = None
        try:
            submission = await self.bot.db.create_submission(
                user_id=interaction.user.id,
                user_name=str(interaction.user),
                user_avatar_url=interaction.user.display_avatar.url,
                category=self.category,
                title=self.title,
                content=self.content,
                guild_id=guild_id,
                language=self.lang,
                image_url=self.image_url,
                tags=self.tags,
            )
        except Exception as e:
            logger.error(f"Failed to record submission in Supabase: {e}", exc_info=True)
            await interaction.followup.send("❌ حدث خطأ أثناء حفظ الإعلان في قاعدة البيانات.", ephemeral=True)
            return

        # Set user cooldown
        try:
            await self.bot.db.set_user_cooldown(interaction.user.id)
        except Exception as e:
            logger.error(f"Failed to set user cooldown: {e}", exc_info=True)

        submission_id = submission.get("id", "N/A") if submission else "N/A"

        # Forward to staff review channel
        review_channel_id = guild_config.get("staff_review_channel_id")
        review_channel = self.bot.get_channel(review_channel_id)
        if not review_channel and review_channel_id:
            try:
                review_channel = await self.bot.fetch_channel(review_channel_id)
            except Exception:
                review_channel = None

        if review_channel and isinstance(review_channel, discord.TextChannel):
            lang_tag = "🇸🇦 العربية" if self.lang == "ar" else "🇬🇧 English"
            review_embed = discord.Embed(
                title=f"📥 New Ad Submission #{submission_id}",
                description=f"**Headline:** {self.title}\n\n**Category:** {self.category}\n**Language:** {lang_tag}\n\n```{self.content}```",
                color=discord.Color.blue(),
            )
            review_embed.set_author(
                name=f"{interaction.user} ({interaction.user.id})",
                icon_url=interaction.user.display_avatar.url,
            )
            review_embed.set_thumbnail(url=interaction.user.display_avatar.url)
            if self.image_url:
                review_embed.set_image(url=self.image_url)

            review_embed.add_field(name="Category", value=self.category, inline=True)
            review_embed.add_field(name="Language", value=lang_tag, inline=True)
            if self.tags:
                review_embed.add_field(name="Tags", value=" ".join(f"`#{t}`" for t in self.tags[:5]), inline=True)
            review_embed.add_field(name="Status", value="🟡 Pending Staff Review", inline=True)
            review_embed.set_footer(text=f"Submission ID: {submission_id} • AdSentry Review Queue")

            try:
                review_view = StaffReviewView(submission_id) if isinstance(submission_id, int) else None
                review_msg = await review_channel.send(embed=review_embed, view=review_view)
                if submission and isinstance(submission_id, int):
                    await self.bot.db.update_review_message_id(submission_id, review_msg.id)
            except Exception as e:
                logger.error(f"Failed to send submission to review channel: {e}", exc_info=True)

        # Notify user
        if self.lang == "ar":
            success_embed = discord.Embed(
                title="✅ تم إرسال إعلانك بنجاح للمراجعة!",
                description=(
                    f"تم استلام إعلانك في قسم **{self.category}** بنجاح!\n\n"
                    f"**العنوان:** {self.title}\n"
                    f"**رقم التتبع:** `#{submission_id}`\n\n"
                    "سيقوم طاقم الإدارة بمراجعة طلبك في أقرب وقت. سيصلك إشعار بالنتيجة عبر الخاص."
                ),
                color=discord.Color.green(),
            )
            success_embed.set_footer(text="خدمة إعلانات AdSentry")
        else:
            success_embed = discord.Embed(
                title="✅ Advertisement Submitted!",
                description=(
                    f"Your advertisement for **{self.category}** has been successfully received!\n\n"
                    f"**Headline:** {self.title}\n"
                    f"**Tracking ID:** `#{submission_id}`\n\n"
                    "Our moderation team will review your ad shortly. You will be notified via DM when a decision is made."
                ),
                color=discord.Color.green(),
            )
            success_embed.set_footer(text="AdSentry Announcement Service")

        await interaction.edit_original_response(content=None, embed=success_embed, view=None)


class CategorySelect(discord.ui.Select):
    """Dropdown menu for choosing the advertisement category in the user's selected language."""

    def __init__(self, bot: commands.Bot, lang: str = "en", is_admin: bool = False) -> None:
        self.bot = bot
        self.lang = lang
        self.is_admin = is_admin

        if lang == "ar":
            options = [
                discord.SelectOption(
                    label="برمجة وتطوير",
                    value="برمجة وتطوير 💻",
                    description="مشاريع برمجية، مواقع، تطبيقات، سكربتات",
                    emoji="💻",
                ),
                discord.SelectOption(
                    label="بوتات وأدوات",
                    value="بوتات وأدوات 🤖",
                    description="بوتات ديسكورد، أدوات تقنية، أتمتة",
                    emoji="🤖",
                ),
            ]
            # Only include Discord servers category for Admins / Server Owners
            if is_admin:
                options.append(
                    discord.SelectOption(
                        label="سيرفرات ومجتمعات",
                        value="سيرفرات ومجتمعات 🌐",
                        description="سيرفرات ديسكورد ومجتمعات تقنية (للإدارة)",
                        emoji="🌐",
                    )
                )
            options.extend([
                discord.SelectOption(
                    label="خدمات وعمل حر",
                    value="خدمات وعمل حر 💼",
                    description="خدمات برمجية، تصميم، عمل حر",
                    emoji="💼",
                ),
                discord.SelectOption(
                    label="استضافة وسحابيات",
                    value="استضافة وسحابيات ☁️",
                    description="استضافة بوتات، سيرفرات VPS، مواقع",
                    emoji="☁️",
                ),
                discord.SelectOption(
                    label="تعليم ومصادر",
                    value="تعليم ومصادر 📚",
                    description="شروحات، كورسات، كتب، ومصادر تعلم",
                    emoji="📚",
                ),
                discord.SelectOption(
                    label="وظائف وتوظيف",
                    value="وظائف وتوظيف 🎯",
                    description="فرص عمل، عروض توظيف",
                    emoji="🎯",
                ),
                discord.SelectOption(
                    label="أخرى ومنوعات",
                    value="أخرى ومنوعات 📌",
                    description="إعلانات عامة وترويج متنوع",
                    emoji="📌",
                ),
            ])
            placeholder = "اختر تصنيف إعلانك من القائمة..."
        else:
            options = [
                discord.SelectOption(
                    label="Programming & Dev",
                    value="Programming & Dev 💻",
                    description="Software projects, apps, websites, scripts",
                    emoji="💻",
                ),
                discord.SelectOption(
                    label="Bots & Tools",
                    value="Bots & Tools 🤖",
                    description="Discord bots, developer tools, automation",
                    emoji="🤖",
                ),
            ]
            # Only include Discord servers category for Admins / Server Owners
            if is_admin:
                options.append(
                    discord.SelectOption(
                        label="Discord Servers",
                        value="Servers & Communities 🌐",
                        description="Discord servers & communities (Admin/Owner only)",
                        emoji="🌐",
                    )
                )
            options.extend([
                discord.SelectOption(
                    label="Services & Freelance",
                    value="Services & Freelance 💼",
                    description="Freelance dev, UI/UX design, consulting",
                    emoji="💼",
                ),
                discord.SelectOption(
                    label="Hosting & Cloud",
                    value="Hosting & Cloud ☁️",
                    description="Bot hosting, VPS servers, domains",
                    emoji="☁️",
                ),
                discord.SelectOption(
                    label="Learning & Resources",
                    value="Learning & Resources 📚",
                    description="Tutorials, courses, guides & documentation",
                    emoji="📚",
                ),
                discord.SelectOption(
                    label="Jobs & Hiring",
                    value="Jobs & Hiring 🎯",
                    description="Paid gigs, job openings, recruitment",
                    emoji="🎯",
                ),
                discord.SelectOption(
                    label="Others & General",
                    value="Others & General 📌",
                    description="General announcements and misc promos",
                    emoji="📌",
                ),
            ])
            placeholder = "Select your ad category..."

        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Invoked when user picks a category from the dropdown."""
        selected_category = self.values[0]

        # Pre-check cooldown
        cooldown_until = await self.bot.db.get_user_cooldown(interaction.user.id)
        if cooldown_until:
            unix_ts = int(cooldown_until.timestamp())
            if self.lang == "ar":
                embed = discord.Embed(
                    title="⏳ أنت في فترة الانتظار (Cooldown)",
                    description=(
                        "لا يمكنك تقديم إعلان جديد في الوقت الحالي لأنك ضمن فترة الانتظار.\n\n"
                        f"**موعد تقديم الإعلان القادم:** <t:{unix_ts}:F> (<t:{unix_ts}:R>)"
                    ),
                    color=discord.Color.orange(),
                )
            else:
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
            if self.lang == "ar":
                embed = discord.Embed(
                    title="⚠️ لديك طلب إعلان معلق بالفعل",
                    description=(
                        "لديك بالفعل طلب إعلان قيد مراجعة الإدارة حالياً.\n"
                        "يرجى الانتظار حتى تتم مراجعة إعلانك السابق قبل إرسال إعلان جديد."
                    ),
                    color=discord.Color.gold(),
                )
            else:
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

        # Verify admin permissions and launch the ad submission modal
        is_admin = self.is_admin or is_server_admin_or_owner(interaction.user, interaction.guild)
        modal = AdSubmissionModal(category=selected_category, bot=self.bot, lang=self.lang, is_admin=is_admin)
        await interaction.response.send_modal(modal)


class CategorySelectView(discord.ui.View):
    """View hosting the category selection dropdown."""

    def __init__(self, bot: commands.Bot, lang: str = "en", is_admin: bool = False) -> None:
        super().__init__(timeout=120)
        self.add_item(CategorySelect(bot, lang=lang, is_admin=is_admin))


class LocalizedPanelView(discord.ui.View):
    """Ephemeral localized panel view presenting the submission button in the selected language."""

    def __init__(self, bot: commands.Bot, lang: str = "en") -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.lang = lang

        label = "تقديم إعلان" if lang == "ar" else "Submit Advertisement"
        btn = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.primary,
            emoji="📢",
            custom_id=f"adsentry:open_cat_{lang}",
        )
        btn.callback = self.open_categories
        self.add_item(btn)

    async def open_categories(self, interaction: discord.Interaction) -> None:
        """Opens the category selection dropdown in the specified language."""
        is_admin = is_server_admin_or_owner(interaction.user, interaction.guild)
        if self.lang == "ar":
            embed = discord.Embed(
                title="📢 اختر قسم الإعلان",
                description=(
                    "يرجى اختيار القسم الأنسب لموضوع إعلانك من القائمة أدناه.\n"
                    "بمجرد الاختيار، سيفتح لك النموذج لكتابة التفاصيل."
                ),
                color=discord.Color.blurple(),
            )
        else:
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
            view=CategorySelectView(self.bot, lang=self.lang, is_admin=is_admin),
            ephemeral=True,
        )


class SearchCategorySelect(discord.ui.Select):
    """Dropdown for selecting category to search active published ads."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        options = [
            discord.SelectOption(
                label="جميع الإعلانات النشطة | All Active Ads",
                value="ALL",
                description="عرض أحدث الإعلانات النشطة في السيرفر",
                emoji="🌟",
            ),
            discord.SelectOption(
                label="برمجة وتطوير | Programming & Dev",
                value="برمجة|Programming",
                description="Software, apps, websites, scripts, code",
                emoji="💻",
            ),
            discord.SelectOption(
                label="بوتات وأدوات | Bots & Tools",
                value="بوتات|Bots|Tools",
                description="Discord bots, developer tools, automation",
                emoji="🤖",
            ),
            discord.SelectOption(
                label="سيرفرات ومجتمعات | Servers & Communities",
                value="سيرفرات|Servers|Communities",
                description="Discord servers, tech communities, gaming hubs",
                emoji="🌐",
            ),
            discord.SelectOption(
                label="خدمات وعمل حر | Services & Freelance",
                value="خدمات|Services|Freelance",
                description="Freelance dev, design, commissions, support",
                emoji="💼",
            ),
            discord.SelectOption(
                label="استضافة وسحابيات | Hosting & Cloud",
                value="استضافة|Hosting|Cloud",
                description="Bot hosting, VPS servers, domains & cloud",
                emoji="☁️",
            ),
            discord.SelectOption(
                label="تعليم ومصادر | Learning & Resources",
                value="تعليم|Learning|Resources",
                description="Tutorials, developer courses, guides & assets",
                emoji="📚",
            ),
            discord.SelectOption(
                label="وظائف وتوظيف | Jobs & Hiring",
                value="وظائف|Jobs|Hiring",
                description="Paid gigs, job openings, recruitment",
                emoji="🎯",
            ),
            discord.SelectOption(
                label="أخرى ومنوعات | Others & General",
                value="أخرى|Others|General",
                description="General announcements and miscellaneous",
                emoji="📌",
            ),
        ]
        super().__init__(
            placeholder="اختر التصنيف للبحث / Select category to search...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Processes the chosen search category and displays matching active ads."""
        await interaction.response.defer(ephemeral=True)
        selected_value = self.values[0]
        selected_label = next((opt.label for opt in self.options if opt.value == selected_value), selected_value)

        results = await self.bot.db.search_active_ads(
            guild_id=interaction.guild_id,
            category_query=selected_value,
            limit=10,
        )

        if not results:
            embed = discord.Embed(
                title="🔍 نتائج البحث | Search Results",
                description=(
                    f"لا توجد إعلانات نشطة حالياً في تصنيف **{selected_label}**.\n"
                    f"No active advertisements currently found in **{selected_label}**.\n\n"
                    "💡 *يمكنك تقديم إعلانك الآن عبر الضغط على علم لغتك في اللوحة الرئيسية!*"
                ),
                color=discord.Color.gold(),
            )
            embed.set_footer(text="AdSentry Search Directory")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🔍 الإعلانات النشطة: {selected_label}",
            description=f"تم العثور على **{len(results)}** إعلان نشط في هذا التصنيف:\n\n",
            color=discord.Color.green(),
        )

        for i, post in enumerate(results, 1):
            sub = post.get("submissions") or {}
            title = sub.get("title") or "إعلان بدون عنوان"
            author = sub.get("user_name") or "Anonymous"
            category = sub.get("category") or selected_label
            channel_id = post.get("channel_id")
            message_id = post.get("message_id")
            jump_url = f"https://discord.com/channels/{interaction.guild_id}/{channel_id}/{message_id}"

            expires_at_raw = post.get("expires_at")
            if expires_at_raw:
                try:
                    dt = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
                    ts = int(dt.timestamp())
                    expiry_str = f"ينتهي <t:{ts}:R>"
                except Exception:
                    expiry_str = "مؤقت (Temporary)"
            else:
                expiry_str = "دائم (Permanent)"

            embed.add_field(
                name=f"{i}. {title}",
                value=(
                    f"👤 **الناشر:** {author} • 🏷️ **القسم:** `{category}`\n"
                    f"⏳ **الصلاحية:** {expiry_str}\n"
                    f"🔗 **[اضغط هنا للانتقال مباشرة للإعلان]({jump_url})**"
                ),
                inline=False,
            )

        embed.set_footer(text="AdSentry Directory • اضغط على الرابط لعرض الإعلان الكامل ومناقشته")
        await interaction.followup.send(embed=embed, ephemeral=True)


class SearchCategorySelectView(discord.ui.View):
    """Ephemeral view presenting the category dropdown for searching ads."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=180)
        self.add_item(SearchCategorySelect(bot))


class LanguageSelectView(discord.ui.View):
    """Persistent bilingual language selection view displayed in the main channel."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="العربية",
        style=discord.ButtonStyle.primary,
        emoji="🇸🇦",
        custom_id="adsentry:lang:ar",
        row=0,
    )
    async def arabic_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Presents localized Arabic submission portal interface."""
        embed = discord.Embed(
            title="📢 بوابة نشر الإعلانات",
            description=(
                "مرحبًا بك في منصة نشر الإعلانات الرسمية!\n\n"
                "هل ترغب في الترويج لسيرفرك، مشروعك، بوتك، أو خدماتك؟\n"
                "اضغط على زر **تقديم إعلان** بالأسفل لاختيار القسم وتعبئة تفاصيل الإعلان.\n\n"
                "**قواعد وشروط النشر:**\n"
                "1. اختر القسم المناسب لإعلانك بدقة.\n"
                "2. اكتب تفاصيل واضحة مع روابط فعالة ومباشرة.\n"
                "3. يُمنع السبام أو الروابط الاحتيالية أو المخالفة لسياسات ديسكورد."
            ),
            color=discord.Color.green(),
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="نظام AdSentry الإعلاني • إعلانات موثوقة ومحمية")

        view = LocalizedPanelView(bot=self.bot, lang="ar")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="English",
        style=discord.ButtonStyle.secondary,
        emoji="🇬🇧",
        custom_id="adsentry:lang:en",
        row=0,
    )
    async def english_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Presents localized English submission portal interface."""
        embed = discord.Embed(
            title="📢 Server Advertisement Portal",
            description=(
                "Welcome to our server advertisement portal!\n\n"
                "Looking to promote your Discord server, developer project, bot, or freelance services?\n"
                "Click the **Submit Advertisement** button below to choose your category and submit details.\n\n"
                "**Submission Guidelines:**\n"
                "1. Choose the accurate category for your promotion.\n"
                "2. Provide clear descriptions and working, safe links.\n"
                "3. Refrain from spamming or submitting duplicate promotions."
            ),
            color=discord.Color.blue(),
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="Powered by AdSentry • Safe & Moderated Advertising")

        view = LocalizedPanelView(bot=self.bot, lang="en")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="بحث / Search",
        style=discord.ButtonStyle.secondary,
        emoji="🔍",
        custom_id="adsentry:main_search",
        row=0,
    )
    async def search_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Presents category dropdown to browse and search active published ads."""
        embed = discord.Embed(
            title="🔍 Search Advertisements | البحث في الإعلانات",
            description=(
                "**Select a category below to browse active announcements in this server:**\n"
                "**اختر تصنيفاً من القائمة أدناه لعرض الإعلانات النشطة حالياً في السيرفر:**\n\n"
                "يمكنك تصفح إعلانات البرمجة، البوتات، السيرفرات، الخدمات، وغيرها مع روابط مباشرة للوصول إليها."
            ),
            color=discord.Color.blurple(),
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="AdSentry Search Engine • دليل الإعلانات النشطة")

        view = SearchCategorySelectView(self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class AdSubmissionPanelView(discord.ui.View):
    """Legacy persistent panel view (maintained for backwards compatibility)."""

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
            view=CategorySelectView(self.bot, lang="en"),
            ephemeral=True,
        )


class SubmissionsCog(commands.Cog, name="Submissions"):
    """Cog handling user ad submissions, categories, and moderation."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    ad_group = app_commands.Group(name="ad", description="AdSentry advertisement management")

    @ad_group.command(name="submit", description="Submit a new advertisement (Choose language)")
    async def submit_ad(self, interaction: discord.Interaction) -> None:
        """Slash command to initiate advertisement submission with language selection."""
        # Check cooldown
        cooldown_until = await self.bot.db.get_user_cooldown(interaction.user.id)
        if cooldown_until:
            unix_ts = int(cooldown_until.timestamp())
            embed = discord.Embed(
                title="⏳ Cooldown Active | أنت في فترة الانتظار",
                description=(
                    f"**Next submission available:** <t:{unix_ts}:F> (<t:{unix_ts}:R>)\n"
                    f"**موعد تقديم الإعلان القادم:** <t:{unix_ts}:F> (<t:{unix_ts}:R>)"
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Check pending submission
        has_pending = await self.bot.db.has_pending_submission(interaction.user.id)
        if has_pending:
            embed = discord.Embed(
                title="⚠️ Submission Already Pending | لديك إعلان معلق",
                description=(
                    "You already have a pending advertisement request awaiting review.\n"
                    "لديك بالفعل طلب إعلان قيد المراجعة، يرجى الانتظار قبل إرسال إعلان جديد."
                ),
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = self.create_panel_embed()
        view = LanguageSelectView(self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @ad_group.command(name="search", description="Browse and search active advertisements by category")
    async def search_ads_cmd(self, interaction: discord.Interaction) -> None:
        """Slash command to browse and search active published ads."""
        embed = discord.Embed(
            title="🔍 Search Advertisements | البحث في الإعلانات",
            description=(
                "**Select a category below to browse active announcements in this server:**\n"
                "**اختر تصنيفاً من القائمة أدناه لعرض الإعلانات النشطة حالياً في السيرفر:**"
            ),
            color=discord.Color.blurple(),
        )
        view = SearchCategorySelectView(self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @ad_group.command(
        name="reset-cooldown",
        description="Reset ad submission cooldown for a member or yourself (Staff only)",
    )
    @app_commands.describe(
        member="The member whose cooldown to reset (leave empty to reset your own cooldown)",
        clear_pending="Whether to also clear any pending ad awaiting review (default: True)",
    )
    async def reset_cooldown_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        clear_pending: bool = True,
    ) -> None:
        """Slash command to reset cooldown for a user."""
        if not is_staff(interaction.user):
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

    @ad_group.command(
        name="check-cooldown",
        description="Check if a member (or yourself) is on ad cooldown",
    )
    @app_commands.describe(
        member="The member to check (leave empty to check your own cooldown)",
    )
    async def check_cooldown_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        """Slash command to check cooldown status for a member."""
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

    @ad_group.command(
        name="cancel",
        description="إلغاء طلب إعلانك المعلق | Cancel your pending ad submission",
    )
    async def cancel_submission_cmd(self, interaction: discord.Interaction) -> None:
        """Allows users to cancel their own pending advertisement submission."""
        await interaction.response.defer(ephemeral=True)
        
        # Check if user has a pending submission
        has_pending = await self.bot.db.has_pending_submission(interaction.user.id)
        if not has_pending:
            embed = discord.Embed(
                title="❌ لا توجد طلبات معلقة | No Pending Submissions",
                description=(
                    "**لا يوجد لديك أي إعلان معلق حالياً.**\n"
                    "You don't have any pending advertisement submissions.\n\n"
                    "💡 يمكنك تقديم إعلان جديد الآن عبر `/ad submit`"
                ),
                color=discord.Color.gold(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Cancel the pending submission(s)
        try:
            cancelled_count = await self.bot.db.cancel_pending_submissions(interaction.user.id)
            if cancelled_count > 0:
                embed = discord.Embed(
                    title="✅ تم إلغاء الطلب | Submission Cancelled",
                    description=(
                        f"**تم إلغاء طلب إعلانك المعلق بنجاح!**\n"
                        f"Successfully cancelled {cancelled_count} pending submission(s).\n\n"
                        "🎉 يمكنك الآن تقديم إعلان جديد عبر `/ad submit`"
                    ),
                    color=discord.Color.green(),
                )
                if interaction.user.display_avatar:
                    embed.set_thumbnail(url=interaction.user.display_avatar.url)
                embed.set_footer(text="AdSentry • تم إلغاء الطلب المعلق")
            else:
                embed = discord.Embed(
                    title="⚠️ حدث خطأ | Error",
                    description="تعذر إلغاء الطلب. يرجى المحاولة لاحقاً أو التواصل مع الإدارة.",
                    color=discord.Color.red(),
                )
        except Exception as e:
            logger.error(f"Error cancelling submission for user {interaction.user.id}: {e}", exc_info=True)
            embed = discord.Embed(
                title="❌ فشل الإلغاء | Cancellation Failed",
                description=f"حدث خطأ أثناء إلغاء الطلب:\n`{e}`",
                color=discord.Color.red(),
            )
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    def create_panel_embed(self) -> discord.Embed:
        """Constructs the standard bilingual advertisement submission portal embed."""
        embed = discord.Embed(
            title="📢 Server Advertisement Portal | بوابة نشر الإعلانات",
            description=(
                "**Welcome to our advertising showcase! | مرحبًا بك في منصة الإعلانات!**\n\n"
                "🇸🇦 **العربية:**\n"
                "• اضغط على **العربية 🇸🇦** للاطلاع على القواعد واختيار القسم وتقديم طلب إعلان جديد.\n"
                "• اضغط على **بحث / Search 🔍** لتصفح واستكشاف الإعلانات النشطة حالياً في السيرفر.\n\n"
                "🇬🇧 **English:**\n"
                "• Click **English 🇬🇧** to review guidelines, choose your category, and submit an ad.\n"
                "• Click **بحث / Search 🔍** to explore currently active advertisements in this server."
            ),
            color=discord.Color.blue(),
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="Powered by AdSentry • Safe & Moderated Advertising | إعلانات موثوقة")
        return embed

    async def relocate_panel(self, channel: discord.TextChannel) -> discord.Message | None:
        """
        Deletes the previous submission panel (if exists) and posts a fresh bilingual panel
        at the bottom of the channel, updating server_configs.last_panel_message_id.
        """
        guild_id = channel.guild.id
        config = await self.bot.db.get_guild_config(guild_id)
        old_panel_id = config.get("last_panel_message_id") if config else None

        # 1. Attempt to delete prior panel message
        if old_panel_id:
            try:
                old_message = await channel.fetch_message(old_panel_id)
                await old_message.delete()
                logger.info(f"Deleted old submission panel message {old_panel_id} in #{channel.name}")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                logger.debug(f"Could not delete old panel message {old_panel_id}: {e}")

        # 2. Deploy fresh bilingual submission panel at the bottom
        embed = self.create_panel_embed()
        view = LanguageSelectView(self.bot)
        try:
            new_panel_msg = await channel.send(embed=embed, view=view)
            await self.bot.db.update_panel_message_id(guild_id, new_panel_msg.id)
            logger.info(f"Relocated bilingual submission panel #{new_panel_msg.id} in #{channel.name} ({guild_id})")
            return new_panel_msg
        except Exception as e:
            logger.error(f"Failed to post relocated submission panel in #{channel.name}: {e}", exc_info=True)
            return None

    @ad_group.command(name="panel", description="Deploy or relocate the Ad Submission panel (Admin only)")
    @app_commands.default_permissions(administrator=True)
    async def post_panel(self, interaction: discord.Interaction) -> None:
        """Sends or relocates the persistent bilingual ad submission panel in the current channel."""
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("❌ This command must be run in a guild text channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        new_panel = await self.relocate_panel(interaction.channel)
        if new_panel:
            await interaction.followup.send(
                f"✅ Bilingual advertisement submission panel has been deployed to {interaction.channel.mention}!\n"
                f"Tracking ID: `{new_panel.id}`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("❌ Failed to deploy submission panel. Please verify bot permissions.", ephemeral=True)

    @app_commands.command(
        name="ad-panel",
        description="Deploy or relocate the persistent Ad Submission panel (Admin only)",
    )
    @app_commands.describe(target_channel="The channel to post the panel in (defaults to current channel)")
    @app_commands.default_permissions(administrator=True)
    async def ad_panel_standalone(
        self,
        interaction: discord.Interaction,
        target_channel: discord.TextChannel | None = None,
    ) -> None:
        """Standalone shortcut command to deploy or relocate the persistent ad submission panel."""
        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be executed within a server.", ephemeral=True)
            return

        channel = target_channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("❌ Please specify or run this command within a valid text channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        new_panel = await self.relocate_panel(channel)
        if new_panel:
            await interaction.followup.send(
                f"✅ Bilingual advertisement submission panel has been deployed to {channel.mention}!\n"
                f"Previous panel was cleaned up, and tracking ID is `{new_panel.id}`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("❌ Failed to deploy submission panel. Please check permissions.", ephemeral=True)

    # -----------------------------------------------------------------------
    # 📝 Ad Templates System (/ad-template save, list, use, delete)
    # -----------------------------------------------------------------------

    template_group = app_commands.Group(
        name="ad-template",
        description="إدارة نماذج الإعلانات المتكررة | Manage your ad templates",
    )

    @template_group.command(name="save", description="حفظ قالب إعلان جديد | Save a new ad template")
    @app_commands.describe(
        name="اسم القالب للرجوع إليه (مثل: متجري، سيرفري) | Name of this template",
        category="تصنيف الإعلان | Ad category",
    )
    async def template_save(
        self,
        interaction: discord.Interaction,
        name: str,
        category: str,
    ) -> None:
        """Opens a modal to save a new recurring ad template."""
        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
            return

        modal = AdTemplateSaveModal(template_name=name, category=category, bot=self.bot)
        await interaction.response.send_modal(modal)

    @template_group.command(name="list", description="عرض قوالبك المحفوظة | List your saved ad templates")
    async def template_list(self, interaction: discord.Interaction) -> None:
        """Lists all templates saved by the user in this server."""
        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        templates = await self.bot.db.get_user_templates(interaction.user.id, interaction.guild.id)
        if not templates:
            embed = discord.Embed(
                title="📝 قوالب الإعلانات | Ad Templates",
                description=(
                    "**لا توجد لديك أي قوالب محفوظة حالياً في هذا السيرفر.**\n"
                    "You don't have any saved templates in this server.\n\n"
                    "💡 **لحفظ قالب جديد:** استخدم `/ad-template save`"
                ),
                color=discord.Color.gold(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="📝 قوالبك المحفوظة | Your Saved Templates",
            description=f"لديك **{len(templates)}** قالب/قوالب محفوظة يمكنك استخدامها للإعلان بسرعة:",
            color=discord.Color.blue(),
        )

        for t in templates[:10]:
            t_id = t.get("id")
            t_name = t.get("name") or "Template"
            t_cat = t.get("category") or "General"
            t_title = t.get("title") or "بدون عنوان"
            t_tags = " ".join(t.get("tags") or [])
            tags_display = f"\n🏷️ `{t_tags}`" if t_tags else ""
            embed.add_field(
                name=f"📌 {t_name} (`ID: {t_id}`)",
                value=f"📁 **القسم:** `{t_cat}`\n📝 **العنوان:** {t_title}{tags_display}\n🚀 للاستخدام: `/ad-template use name:{t_name}`",
                inline=False,
            )

        embed.set_footer(text="AdSentry Templates • استخدم /ad-template use لتعبئة الإعلان تلقائياً")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @template_group.command(name="use", description="استخدام قالب محفوظ لتقديم إعلان | Use a template to submit an ad")
    @app_commands.describe(name="اسم القالب المحفوظ | Template name")
    async def template_use(self, interaction: discord.Interaction, name: str) -> None:
        """Loads a saved template and opens the submission modal pre-filled."""
        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
            return

        templates = await self.bot.db.get_user_templates(interaction.user.id, interaction.guild.id)
        matched = next((t for t in templates if t.get("name", "").lower() == name.lower()), None)

        if not matched:
            available = ", ".join([f"`{t.get('name')}`" for t in templates]) or "لا توجد قوالب"
            await interaction.response.send_message(
                f"❌ لم يتم العثور على قالب بالاسم `{name}`.\n"
                f"القوالب المتوفرة لديك: {available}",
                ephemeral=True,
            )
            return

        # Check cooldown
        cooldown_until = await self.bot.db.get_user_cooldown(interaction.user.id)
        if cooldown_until:
            unix_ts = int(cooldown_until.timestamp())
            embed = discord.Embed(
                title="⏳ Cooldown Active | أنت في فترة الانتظار",
                description=(
                    f"**Next submission available:** <t:{unix_ts}:F> (<t:{unix_ts}:R>)\n"
                    f"**موعد تقديم الإعلان القادم:** <t:{unix_ts}:F> (<t:{unix_ts}:R>)"
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Check pending submission
        has_pending = await self.bot.db.has_pending_submission(interaction.user.id)
        if has_pending:
            embed = discord.Embed(
                title="⚠️ Submission Already Pending | لديك إعلان معلق",
                description=(
                    "You already have a pending advertisement request awaiting review.\n"
                    "لديك بالفعل طلب إعلان قيد المراجعة، يرجى الانتظار قبل إرسال إعلان جديد."
                ),
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        category = matched.get("category", "General")
        default_title = matched.get("title") or ""
        default_content = matched.get("content") or ""
        tags_list = matched.get("tags") or []
        default_tags = " ".join(tags_list)

        # Open submission modal with pre-filled fields
        modal = AdSubmissionModal(
            category=category,
            bot=self.bot,
            lang="ar",
            is_admin=is_server_admin_or_owner(interaction.user, interaction.guild),
            default_title=default_title,
            default_content=default_content,
            default_tags=default_tags,
        )
        await interaction.response.send_modal(modal)

    @template_use.autocomplete("name")
    async def template_use_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete template names for the user."""
        if not interaction.guild:
            return []
        try:
            templates = await self.bot.db.get_user_templates(interaction.user.id, interaction.guild.id)
            choices = []
            for t in templates:
                t_name = t.get("name", "")
                if current.lower() in t_name.lower():
                    choices.append(app_commands.Choice(name=t_name[:100], value=t_name))
            return choices[:25]
        except Exception:
            return []

    @template_group.command(name="delete", description="حذف قالب محفوظ | Delete a saved ad template")
    @app_commands.describe(name="اسم القالب المراد حذفه | Template name to delete")
    async def template_delete(self, interaction: discord.Interaction, name: str) -> None:
        """Deletes a saved template."""
        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        templates = await self.bot.db.get_user_templates(interaction.user.id, interaction.guild.id)
        matched = next((t for t in templates if t.get("name", "").lower() == name.lower()), None)

        if not matched:
            await interaction.followup.send(f"❌ لم يتم العثور على قالب بالاسم `{name}`.", ephemeral=True)
            return

        success = await self.bot.db.delete_template(matched["id"], interaction.user.id)
        if success:
            embed = discord.Embed(
                title="✅ تم حذف القالب بنجاح",
                description=f"تم حذف القالب `{name}` نهائياً من حسابك.",
                color=discord.Color.green(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send("❌ تعذر حذف القالب، يرجى المحاولة لاحقاً.", ephemeral=True)

    @template_delete.autocomplete("name")
    async def template_delete_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete template names for deletion."""
        if not interaction.guild:
            return []
        try:
            templates = await self.bot.db.get_user_templates(interaction.user.id, interaction.guild.id)
            choices = []
            for t in templates:
                t_name = t.get("name", "")
                if current.lower() in t_name.lower():
                    choices.append(app_commands.Choice(name=t_name[:100], value=t_name))
            return choices[:25]
        except Exception:
            return []


async def setup(bot: commands.Bot) -> None:
    """Extension entry point for loading the Submissions cog."""
    # Register persistent views so buttons work across bot restarts
    bot.add_view(LanguageSelectView(bot))
    bot.add_view(AdSubmissionPanelView(bot))
    await bot.add_cog(SubmissionsCog(bot))
