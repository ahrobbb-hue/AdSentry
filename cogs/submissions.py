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
from utils import screen_content

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


class AdSubmissionModal(discord.ui.Modal):
    """Modal for collecting advertisement title and content from the user in their chosen language."""

    def __init__(self, category: str, bot: commands.Bot, lang: str = "en", is_admin: bool = False) -> None:
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
        else:
            modal_title = f"Submit Ad: {category}"[:45]
            title_label = "Ad Headline / Title"
            title_placeholder = "e.g. Apex Coding Hub - Join top developers"
            content_label = "Advertisement Content & Links"
            content_placeholder = "Describe your server, bot, or service in detail, including features and links..."

        super().__init__(title=modal_title)

        self.ad_title = discord.ui.TextInput(
            label=title_label,
            placeholder=title_placeholder,
            min_length=2,
            max_length=100,
            required=True,
        )
        self.add_item(self.ad_title)

        self.ad_content = discord.ui.TextInput(
            label=content_label,
            style=discord.TextStyle.paragraph,
            placeholder=content_placeholder,
            min_length=10,
            max_length=1800,
            required=True,
        )
        self.add_item(self.ad_content)

        if lang == "ar":
            image_label = "رابط بنر أو صورة الإعلان (اختياري)"
            image_placeholder = "https://i.imgur.com/... أو رابط ينتهي بـ png/jpg/gif"
        else:
            image_label = "Banner / Image URL (Optional)"
            image_placeholder = "https://i.imgur.com/... or direct image link"

        self.ad_image = discord.ui.TextInput(
            label=image_label,
            placeholder=image_placeholder,
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
                    language=self.lang,
                    image_url=image_url,
                )
            except Exception as e:
                logger.error(f"Failed to record submission in Supabase: {e}", exc_info=True)
                err_desc = (
                    "حدث خطأ أثناء حفظ إعلانك في قاعدة البيانات. يرجى المحاولة لاحقاً."
                    if self.lang == "ar"
                    else "An error occurred while saving your submission. Please try again later or contact an administrator."
                )
                embed = discord.Embed(
                    title="❌ Database Error",
                    description=err_desc,
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
                lang_tag = "🇸🇦 العربية" if self.lang == "ar" else "🇬🇧 English"
                review_embed = discord.Embed(
                    title=f"📥 New Ad Submission #{submission_id}",
                    description=f"**Headline:** {title}\n\n**Category:** {self.category}\n**Language:** {lang_tag}\n\n```{content}```",
                    color=discord.Color.blue(),
                )
                review_embed.set_author(
                    name=f"{interaction.user} ({interaction.user.id})",
                    icon_url=interaction.user.display_avatar.url,
                )
                review_embed.set_thumbnail(url=interaction.user.display_avatar.url)
                if image_url:
                    review_embed.set_image(url=image_url)

                review_embed.add_field(name="Category", value=self.category, inline=True)
                review_embed.add_field(name="Language", value=lang_tag, inline=True)
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
            if self.lang == "ar":
                success_embed = discord.Embed(
                    title="✅ تم إرسال إعلانك بنجاح!",
                    description=(
                        f"تم استلام إعلانك في قسم **{self.category}** بنجاح!\n\n"
                        f"**العنوان:** {title}\n"
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
                    description="مشاريع برمجية، مواقع، تطبيقات، سكربتات، أكواد",
                    emoji="💻",
                ),
                discord.SelectOption(
                    label="بوتات وأدوات",
                    value="بوتات وأدوات 🤖",
                    description="بوتات ديسكورد، أدوات تقنية، أتمتة، مكاتب برمجية",
                    emoji="🤖",
                ),
            ]
            # Only include Discord servers category for Admins / Server Owners
            if is_admin:
                options.append(
                    discord.SelectOption(
                        label="سيرفرات ديسكورد ومجتمعات",
                        value="سيرفرات ومجتمعات 🌐",
                        description="سيرفرات ديسكورد ومجتمعات تقنية (خاص بالإدارة والمالك)",
                        emoji="🌐",
                    )
                )
            options.extend([
                discord.SelectOption(
                    label="خدمات وعمل حر",
                    value="خدمات وعمل حر 💼",
                    description="خدمات برمجية، تصميم جرافيك، عمل حر، استشارات",
                    emoji="💼",
                ),
                discord.SelectOption(
                    label="استضافة وسحابيات",
                    value="استضافة وسحابيات ☁️",
                    description="استضافة بوتات، سيرفرات VPS، واستضافة مواقع",
                    emoji="☁️",
                ),
                discord.SelectOption(
                    label="تعليم ومصادر",
                    value="تعليم ومصادر 📚",
                    description="شروحات برمجية، كورسات، كتب، ومصادر تعلم",
                    emoji="📚",
                ),
                discord.SelectOption(
                    label="وظائف وتوظيف",
                    value="وظائف وتوظيف 🎯",
                    description="فرص عمل، عروض توظيف، والبحث عن مطورين",
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
                    description="Software projects, apps, websites, scripts, code",
                    emoji="💻",
                ),
                discord.SelectOption(
                    label="Bots & Tools",
                    value="Bots & Tools 🤖",
                    description="Discord bots, developer tools, automation, APIs",
                    emoji="🤖",
                ),
            ]
            # Only include Discord servers category for Admins / Server Owners
            if is_admin:
                options.append(
                    discord.SelectOption(
                        label="Discord Servers & Communities",
                        value="Servers & Communities 🌐",
                        description="Discord servers & communities (Admin/Owner only)",
                        emoji="🌐",
                    )
                )
            options.extend([
                discord.SelectOption(
                    label="Services & Freelance",
                    value="Services & Freelance 💼",
                    description="Freelance dev, UI/UX design, consulting, commissions",
                    emoji="💼",
                ),
                discord.SelectOption(
                    label="Hosting & Cloud",
                    value="Hosting & Cloud ☁️",
                    description="Bot hosting, VPS servers, domains & cloud infra",
                    emoji="☁️",
                ),
                discord.SelectOption(
                    label="Learning & Resources",
                    value="Learning & Resources 📚",
                    description="Tutorials, developer courses, guides & documentation",
                    emoji="📚",
                ),
                discord.SelectOption(
                    label="Jobs & Hiring",
                    value="Jobs & Hiring 🎯",
                    description="Paid gigs, job openings, developer recruitment",
                    emoji="🎯",
                ),
                discord.SelectOption(
                    label="Others & General",
                    value="Others & General 📌",
                    description="General announcements and miscellaneous promos",
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


async def setup(bot: commands.Bot) -> None:
    """Extension entry point for loading the Submissions cog."""
    # Register persistent views so buttons work across bot restarts
    bot.add_view(LanguageSelectView(bot))
    bot.add_view(AdSubmissionPanelView(bot))
    await bot.add_cog(SubmissionsCog(bot))
