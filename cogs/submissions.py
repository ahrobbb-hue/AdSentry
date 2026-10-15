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
    """Modal for collecting advertisement title and content from the user in their chosen language."""

    def __init__(self, category: str, bot: commands.Bot, lang: str = "en") -> None:
        self.lang = lang
        self.category = category
        self.bot = bot

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
                if self.lang == "ar":
                    embed = discord.Embed(
                        title="🛡️ تم حظر الإعلان بواسطة نظام الحماية التلقائي",
                        description=(
                            "تعذر إرسال إعلانك لأنه يحتوي على مصطلحات مقيدة أو روابط غير مصرح بها:\n"
                            f"**الأنماط المكتشفة:** `{', '.join(flagged_words)}`\n\n"
                            "يرجى تعديل إعلانك وفقاً لقوانين إعلانات السيرفر."
                        ),
                        color=discord.Color.red(),
                    )
                    embed.set_footer(text="نظام حماية AdSentry")
                else:
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

    def __init__(self, bot: commands.Bot, lang: str = "en") -> None:
        self.bot = bot
        self.lang = lang

        if lang == "ar":
            options = [
                discord.SelectOption(
                    label="برمجة",
                    value="برمجة 💻",
                    description="مشاريع برمجية، بوتات، سكربتات، مفتوح المصدر",
                    emoji="💻",
                ),
                discord.SelectOption(
                    label="سيرفرات ديسكورد",
                    value="سيرفرات ديسكورد 🌐",
                    description="مجتمعات، سيرفرات ألعاب، تواصل، دراسة",
                    emoji="🌐",
                ),
                discord.SelectOption(
                    label="خدمات",
                    value="خدمات 🚀",
                    description="عمل حر، تصميم جرافيك، استضافة بوتات، استشارات",
                    emoji="🚀",
                ),
                discord.SelectOption(
                    label="أخرى",
                    value="أخرى 📌",
                    description="إعلانات عامة وترويج متنوع",
                    emoji="📌",
                ),
            ]
            placeholder = "اختر قسم إعلانك من القائمة..."
        else:
            options = [
                discord.SelectOption(
                    label="Programming",
                    value="Programming 💻",
                    description="Coding projects, bots, software, open source, scripts",
                    emoji="💻",
                ),
                discord.SelectOption(
                    label="Discord Servers",
                    value="Discord Servers 🌐",
                    description="Community hubs, gaming, hanging out, study clubs",
                    emoji="🌐",
                ),
                discord.SelectOption(
                    label="Services",
                    value="Services 🚀",
                    description="Freelancing, graphic design, bot hosting, consulting",
                    emoji="🚀",
                ),
                discord.SelectOption(
                    label="Others",
                    value="Others 📌",
                    description="General announcements and miscellaneous promotions",
                    emoji="📌",
                ),
            ]
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

        # Launch the ad submission modal in the chosen language
        modal = AdSubmissionModal(category=selected_category, bot=self.bot, lang=self.lang)
        await interaction.response.send_modal(modal)


class CategorySelectView(discord.ui.View):
    """View hosting the category selection dropdown."""

    def __init__(self, bot: commands.Bot, lang: str = "en") -> None:
        super().__init__(timeout=120)
        self.add_item(CategorySelect(bot, lang=lang))


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
            view=CategorySelectView(self.bot, lang=self.lang),
            ephemeral=True,
        )


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

    def create_panel_embed(self) -> discord.Embed:
        """Constructs the standard bilingual advertisement submission portal embed."""
        embed = discord.Embed(
            title="📢 Server Advertisement Portal | بوابة نشر الإعلانات",
            description=(
                "**Choose your preferred language to submit an advertisement:**\n"
                "**اختر لغتك المفضلة لبدء تقديم طلب إعلان جديد:**\n\n"
                "🇸🇦 **العربية:**\n"
                "مرحبًا بك! لنشر سيرفرك، مشروعك، أو خدماتك في الروم، اضغط على زر **العربية 🇸🇦** بالأسفل لفتح استمارة التقديم.\n\n"
                "🇬🇧 **English:**\n"
                "Welcome! To promote your server, project, or freelance services, click **English 🇬🇧** below to begin your submission."
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
