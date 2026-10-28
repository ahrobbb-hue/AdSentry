"""A single interactive staff dashboard for AdSentry operations."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.moderation import is_admin, is_staff
from cogs.recruitment import (
    DEFAULT_RECRUITMENT_ROLES,
    RecruitmentRoleSelectView,
    close_announcement,
)
from cogs.eventhub import RegisterEventView, build_event_embed

logger = logging.getLogger("AdSentry.Dashboard")


def _staff(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and is_staff(interaction.user))


def _admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.guild and is_admin(interaction.user))


class SystemDashboardView(discord.ui.View):
    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="التوظيف والإدارة", emoji="👥", style=discord.ButtonStyle.primary, row=0)
    async def recruitment(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _admin(interaction):
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await self.cog.show_recruitment(interaction, edit=True)

    @discord.ui.button(label="الفعاليات", emoji="🎪", style=discord.ButtonStyle.primary, row=0)
    async def events(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction):
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await self.cog.show_events(interaction, edit=True)

    @discord.ui.button(label="تقديم إجازة", emoji="📝", style=discord.ButtonStyle.primary, row=0)
    async def staffflow(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction):
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await self.cog.show_staffflow(interaction, edit=True)

    @discord.ui.button(label="المناوبات", emoji="🕒", style=discord.ButtonStyle.primary, row=0)
    async def shifts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction):
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await self.cog.show_shifts(interaction, edit=True)

    @discord.ui.button(label="شكوى إدارية", emoji="⚠️", style=discord.ButtonStyle.primary, row=0)
    async def complaint(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction):
            await interaction.response.send_message("هذا الإجراء لطاقم السيرفر فقط.", ephemeral=True); return
        await interaction.response.send_message("اختر تصنيف الشكوى ثم اكتب تفاصيلها:", view=ComplaintCategoryView(self.cog), ephemeral=True)

    @discord.ui.button(label="إعداد القنوات", emoji="⚙️", style=discord.ButtonStyle.secondary, row=1)
    async def channel_setup(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _admin(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await interaction.response.send_message("اختر حتى ثلاث قنوات خاصة لاستقبال بطاقات المتقدمين. تأكد أن صلاحيات القنوات مخصصة لفريق المراجعة.", view=ReviewChannelSetupView(self.cog), ephemeral=True)

    @discord.ui.button(label="تحديث", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_home(interaction, edit=True)


class PersistentDashboardView(discord.ui.View):
    """Controls for the permanent operations-center message."""

    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="التوظيف والإدارة", emoji="👥", style=discord.ButtonStyle.primary, custom_id="adsentry:ops:recruitment", row=0)
    async def recruitment(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _admin(interaction): await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await self.cog.show_recruitment(interaction)

    @discord.ui.button(label="الفعاليات", emoji="🎪", style=discord.ButtonStyle.primary, custom_id="adsentry:ops:events", row=0)
    async def events(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction): await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await self.cog.show_events(interaction)

    @discord.ui.button(label="تقديم إجازة", emoji="📝", style=discord.ButtonStyle.primary, custom_id="adsentry:ops:staff", row=0)
    async def staffflow(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction): await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await self.cog.show_staffflow(interaction)

    @discord.ui.button(label="المناوبات", emoji="🕒", style=discord.ButtonStyle.primary, custom_id="adsentry:ops:shifts", row=0)
    async def shifts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction): await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await self.cog.show_shifts(interaction)

    @discord.ui.button(label="شكوى إدارية", emoji="⚠️", style=discord.ButtonStyle.primary, custom_id="adsentry:ops:complaint", row=0)
    async def complaint(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction):
            await interaction.response.send_message("هذا الإجراء لطاقم السيرفر فقط.", ephemeral=True); return
        await interaction.response.send_message("اختر تصنيف الشكوى ثم اكتب تفاصيلها:", view=ComplaintCategoryView(self.cog), ephemeral=True)

    @discord.ui.button(label="إعداد القنوات", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="adsentry:ops:channels", row=1)
    async def channels(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _admin(interaction): await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await interaction.response.send_message("اختر حتى ثلاث قنوات خاصة لبطاقات المتقدمين:", view=ReviewChannelSetupView(self.cog), ephemeral=True)

    @discord.ui.button(label="تحديث", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="adsentry:ops:refresh", row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction): await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        await self.cog.refresh_persistent_panel(interaction)


class ReviewChannelSetupView(discord.ui.View):
    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=180)
        self.cog = cog
        picker = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text], min_values=1, max_values=3,
            placeholder="اختر قناة واحدة أو حتى ثلاث قنوات مراجعة",
        )
        picker.callback = self.save_channels  # type: ignore[method-assign]
        self.add_item(picker)

    async def save_channels(self, interaction: discord.Interaction) -> None:
        if not _admin(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True); return
        channels = list(interaction.data.get("values", [])) if interaction.data else []
        guild = interaction.guild
        resolved = [guild.get_channel(int(channel_id)) for channel_id in channels]
        if not resolved or any(not isinstance(channel, discord.TextChannel) for channel in resolved):
            await interaction.response.send_message("اختر قنوات نصية من هذا السيرفر.", ephemeral=True); return
        for channel in resolved:
            perms = channel.permissions_for(guild.me)  # type: ignore[union-attr]
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                await interaction.response.send_message(f"أحتاج View Channel وSend Messages وEmbed Links في {channel.mention}.", ephemeral=True); return
            if channel.permissions_for(guild.default_role).view_channel:
                await interaction.response.send_message(f"القناة {channel.mention} ظاهرة للجميع. اجعلها خاصة وأزل صلاحية View Channel عن @everyone قبل اعتمادها لمراجعة بيانات المتقدمين.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            client = await self.cog.bot.db.get_client()
            await client.rpc("set_recruitment_review_channels", {
                "p_guild_id": guild.id,
                "p_channel_ids": [channel.id for channel in resolved],
                "p_configured_by": interaction.user.id,
            }).execute()
        except Exception:
            logger.exception("Could not save recruitment review channel settings")
            await interaction.followup.send("تعذر حفظ القنوات. تأكد من تشغيل ترحيل قاعدة البيانات الجديد.", ephemeral=True); return
        await interaction.followup.send("تم حفظ قنوات مراجعة التقديم: " + "، ".join(channel.mention for channel in resolved), ephemeral=True)


class RecruitmentDashboardView(discord.ui.View):
    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="إنشاء تقديم", emoji="✨", style=discord.ButtonStyle.success)
    async def create_opening(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _admin(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            client = await self.cog.bot.db.get_client()
            rows = (await client.table("recruitment_review_channels").select("slot,channel_id").eq("guild_id", interaction.guild.id).order("slot").execute()).data or []
            if not rows:
                await interaction.followup.send("اضبط قناة مراجعة واحدة على الأقل من الصفحة الرئيسية للوحة أولًا.", ephemeral=True); return
            config = await self.cog.bot.db.get_guild_config(interaction.guild.id)
            public_channel = interaction.guild.get_channel(int((config or {}).get("announcement_channel_id") or 0))
            if not isinstance(public_channel, discord.TextChannel):
                await interaction.followup.send("اضبط قناة الإعلان أولًا باستخدام `/ad-setup`.", ephemeral=True); return
            view = OpeningReviewChannelView(self.cog, public_channel, rows)
            await interaction.followup.send("اختر قناة المراجعة التي ستستقبل بطاقات المتقدمين لهذا التقديم:", view=view, ephemeral=True)
        except Exception:
            logger.exception("Could not open recruitment creation flow from dashboard")
            await interaction.followup.send("تعذر فتح نموذج التقديم. تأكد من تشغيل ترحيل لوحة التوظيف وقاعدة البيانات.", ephemeral=True)

    @discord.ui.button(label="تحديث", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_recruitment(interaction, edit=True)

    @discord.ui.button(label="إغلاق تقديم", emoji="🔒", style=discord.ButtonStyle.danger)
    async def close_opening(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not _admin(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        rows = (await client.table("recruitments").select("id,title").eq("guild_id", interaction.guild.id).eq("status", "OPEN").order("created_at", desc=True).limit(25).execute()).data or []
        if not rows: await interaction.followup.send("لا توجد تقديمات مفتوحة.", ephemeral=True); return
        await interaction.followup.send("اختر التقديم لإغلاقه:", view=RecruitmentCloseSelectView(self.cog, rows), ephemeral=True)

    @discord.ui.button(label="الرئيسية", emoji="🏠", style=discord.ButtonStyle.secondary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_home(interaction, edit=True)


class OpeningReviewChannelView(discord.ui.View):
    def __init__(self, cog: "DashboardCog", public_channel: discord.TextChannel, rows: list[dict]) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.public_channel = public_channel
        options = []
        for row in rows[:3]:
            ch = cog.bot.get_channel(int(row["channel_id"]))
            ch_name = f"#{ch.name}" if isinstance(ch, discord.TextChannel) else f"#{row['channel_id']}"
            options.append(discord.SelectOption(
                label=f"قناة المراجعة {row['slot']} — {ch_name}"[:100],
                value=str(row["channel_id"]),
                description=f"الفتحة {row['slot']}",
            ))
        self.select = discord.ui.Select(
            placeholder="اختر قناة من القنوات المضبوطة",
            options=options,
        )
        self.select.callback = self.choose  # type: ignore[method-assign]
        self.add_item(self.select)

    async def choose(self, interaction: discord.Interaction) -> None:
        if not _admin(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True); return
        review = interaction.guild.get_channel(int(self.select.values[0]))
        if not isinstance(review, discord.TextChannel):
            await interaction.response.send_message("قناة المراجعة لم تعد موجودة. أعد ضبط القنوات.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        options = [discord.SelectOption(label=role, value=role) for role in DEFAULT_RECRUITMENT_ROLES]
        configured = await self.cog.bot.db.get_custom_recruitment_roles(interaction.guild.id)
        existing = {role.casefold() for role in DEFAULT_RECRUITMENT_ROLES}
        for row in configured:
            label = row["label"].strip()
            if label and label.casefold() not in existing and len(options) < 24:
                options.append(discord.SelectOption(label=label, value=label)); existing.add(label.casefold())
        options.append(discord.SelectOption(label="أخرى", value="__other__", description="أدخل اسم منصب مخصص"))
        await interaction.followup.send("اختر المنصب المطلوب، ثم أكمل نموذج تفاصيل التقديم:", view=RecruitmentRoleSelectView(options, self.public_channel, review), ephemeral=True)


class EventDateTimePickerView(discord.ui.View):
    """Interactive Date & Time Picker using Discord Select Menus (No manual text typing)."""

    def __init__(
        self,
        cog: "DashboardCog",
        event_channel: discord.TextChannel,
        admin_channel: discord.TextChannel | None,
        title: str,
        description: str,
        capacity: int,
        reminder_hours: int,
    ) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.event_channel = event_channel
        self.admin_channel = admin_channel
        self.title = title
        self.description = description
        self.capacity = capacity
        self.reminder_hours = reminder_hours

        # Use Saudi / Gulf timezone (UTC+3) as standard base
        self.tz = timezone(timedelta(hours=3))
        now_local = datetime.now(self.tz)

        # Default to tomorrow 8:00 PM
        tomorrow = (now_local + timedelta(days=1)).date()
        self.selected_date = tomorrow.isoformat()
        self.selected_time = "20:00"

        # Build Select Menus
        self._build_date_select(now_local)
        self._build_time_select()
        self._build_preset_select()

    def _build_date_select(self, now_local: datetime) -> None:
        arabic_days = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
        arabic_months = [
            "", "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
            "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"
        ]

        options = []
        for i in range(14):
            day = (now_local + timedelta(days=i)).date()
            weekday_name = arabic_days[day.weekday()]
            month_name = arabic_months[day.month]

            tag = " (اليوم)" if i == 0 else " (غداً)" if i == 1 else " (عطلة نهاية الأسبوع)" if day.weekday() in (4, 5) else ""
            label = f"{weekday_name} {day.day} {month_name}{tag}"

            options.append(discord.SelectOption(
                label=label[:100],
                value=day.isoformat(),
                description=f"التاريخ: {day.isoformat()}",
                default=(day.isoformat() == self.selected_date),
            ))

        self.date_select = discord.ui.Select(
            placeholder="📅 اختر تاريخ الفعالية من القائمة",
            options=options,
            row=0,
        )
        self.date_select.callback = self._on_date_change
        self.add_item(self.date_select)

    def _build_time_select(self) -> None:
        time_choices = [
            ("16:00", "04:00 PM (4:00 عصراً)"),
            ("17:00", "05:00 PM (5:00 مساءً)"),
            ("18:00", "06:00 PM (6:00 مساءً - المغرب)"),
            ("19:00", "07:00 PM (7:00 مساءً)"),
            ("20:00", "08:00 PM (8:00 مساءً - العشاء) [وقت مقترح]"),
            ("21:00", "09:00 PM (9:00 مساءً - وقت الذروة)"),
            ("22:00", "10:00 PM (10:00 مساءً)"),
            ("23:00", "11:00 PM (11:00 مساءً)"),
            ("00:00", "12:00 AM (12:00 منتصف الليل)"),
            ("01:00", "01:00 AM (1:00 فجراً)"),
            ("13:00", "01:00 PM (1:00 ظهراً)"),
            ("14:00", "02:00 PM (2:00 ظهراً)"),
            ("15:00", "03:00 PM (3:00 عصراً)"),
            ("10:00", "10:00 AM (10:00 صباحاً)"),
            ("11:00", "11:00 AM (11:00 صباحاً)"),
            ("12:00", "12:00 PM (12:00 ظهراً)"),
        ]

        options = [
            discord.SelectOption(
                label=label,
                value=val,
                default=(val == self.selected_time),
            )
            for val, label in time_choices
        ]

        self.time_select = discord.ui.Select(
            placeholder="⏰ اختر ساعة وتوقيت الفعالية من القائمة",
            options=options,
            row=1,
        )
        self.time_select.callback = self._on_time_change
        self.add_item(self.time_select)

    def _build_preset_select(self) -> None:
        preset_choices = [
            ("+2h", "⚡ بعد ساعتين من الآن"),
            ("+4h", "⚡ بعد 4 ساعات من الآن"),
            ("+6h", "⚡ بعد 6 ساعات من الآن"),
            ("tonight_9pm", "🌙 الليلة الساعة 9:00 مساءً"),
            ("tomorrow_8pm", "📅 غداً الساعة 8:00 مساءً"),
            ("weekend_8pm", "🎉 نهاية الأسبوع (الجمعة 8:30 مساءً)"),
        ]
        options = [
            discord.SelectOption(label=label, value=val)
            for val, label in preset_choices
        ]
        self.preset_select = discord.ui.Select(
            placeholder="⚡ خيارات سريعة جاهزة (اختياري)",
            options=options,
            row=2,
        )
        self.preset_select.callback = self._on_preset_change
        self.add_item(self.preset_select)

    def get_selected_datetime(self) -> datetime:
        d = datetime.fromisoformat(self.selected_date).date()
        h, m = map(int, self.selected_time.split(":"))
        local_dt = datetime(d.year, d.month, d.day, h, m, tzinfo=self.tz)
        return local_dt.astimezone(timezone.utc)

    def render_content(self) -> str:
        dt_utc = self.get_selected_datetime()
        ts = int(dt_utc.timestamp())
        admin_info = f" · إشعارات الإدارة: {self.admin_channel.mention}" if self.admin_channel else ""
        return (
            f"🎪 **تحديد موعد الفعالية: {self.title}**\n\n"
            f"📅 **الموعد المختار حالياً:** <t:{ts}:F> (<t:{ts}:R>)\n"
            f"👥 **المقاعد:** `{self.capacity}`  ·  🔔 **التذكير:** قبل `{self.reminder_hours}` ساعة\n"
            f"📢 **قناة الفعالية:** {self.event_channel.mention}{admin_info}\n\n"
            "👇 **حدد التاريخ والساعة المناسبة من القوائم أدناه، ثم اضغط على زر التأكيد:**"
        )

    def _refresh_defaults(self) -> None:
        for opt in self.date_select.options:
            opt.default = (opt.value == self.selected_date)
        for opt in self.time_select.options:
            opt.default = (opt.value == self.selected_time)

    async def _on_date_change(self, interaction: discord.Interaction) -> None:
        self.selected_date = self.date_select.values[0]
        self._refresh_defaults()
        await interaction.response.edit_message(content=self.render_content(), view=self)

    async def _on_time_change(self, interaction: discord.Interaction) -> None:
        self.selected_time = self.time_select.values[0]
        self._refresh_defaults()
        await interaction.response.edit_message(content=self.render_content(), view=self)

    async def _on_preset_change(self, interaction: discord.Interaction) -> None:
        val = self.preset_select.values[0]
        now_local = datetime.now(self.tz)
        if val == "+2h":
            target = now_local + timedelta(hours=2)
        elif val == "+4h":
            target = now_local + timedelta(hours=4)
        elif val == "+6h":
            target = now_local + timedelta(hours=6)
        elif val == "tonight_9pm":
            target = now_local.replace(hour=21, minute=0, second=0, microsecond=0)
            if target <= now_local:
                target += timedelta(days=1)
        elif val == "tomorrow_8pm":
            target = (now_local + timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
        elif val == "weekend_8pm":
            days_ahead = (4 - now_local.weekday()) % 7
            if days_ahead == 0 and now_local.hour >= 20:
                days_ahead = 7
            target = (now_local + timedelta(days=days_ahead)).replace(hour=20, minute=30, second=0, microsecond=0)
        else:
            target = now_local + timedelta(hours=24)

        self.selected_date = target.date().isoformat()
        self.selected_time = f"{target.hour:02d}:00"
        self._refresh_defaults()
        await interaction.response.edit_message(content=self.render_content(), view=self)

    @discord.ui.button(label="تأكيد ونشر الفعالية 🚀", style=discord.ButtonStyle.success, row=3)
    async def confirm_event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        start_utc = self.get_selected_datetime()
        if start_utc <= datetime.now(timezone.utc):
            await interaction.response.send_message("❌ الموعد المحدد أصبح في الماضي؛ يرجى اختيار موعد قادم.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        client = None
        event_id: int | None = None
        try:
            client = await self.cog.bot.db.get_client()
            reminder_at = (start_utc - timedelta(hours=self.reminder_hours)).isoformat() if self.reminder_hours else None
            result = await client.table("events").insert({
                "guild_id": interaction.guild.id,
                "created_by": interaction.user.id,
                "title": self.title,
                "description": self.description,
                "channel_id": self.event_channel.id,
                "admin_channel_id": self.admin_channel.id if self.admin_channel else None,
                "starts_at": start_utc.isoformat(),
                "capacity": self.capacity,
                "status": "OPEN",
                "reminder_at": reminder_at,
            }).execute()
            event = result.data[0]
            event_id = int(event["id"])
            embed = build_event_embed(
                self.title,
                self.description,
                start_utc,
                self.capacity,
                event_id=event_id,
                organizer=interaction.user,
            )
            message = await self.event_channel.send(
                embed=embed,
                view=RegisterEventView(event_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.cog.bot.add_view(RegisterEventView(event_id))
            await client.table("events").update({"announcement_message_id": message.id}).eq("id", event["id"]).execute()
            admin_note = f" · إشعارات الإدارة: {self.admin_channel.mention}" if self.admin_channel else ""

            await interaction.edit_original_response(
                content=f"✅  **تم إنشاء الفعالية #{event_id} ونشرها بنجاح في {self.event_channel.mention}**{admin_note}!\n"
                        f"📅 **الموعد:** <t:{int(start_utc.timestamp())}:F> (<t:{int(start_utc.timestamp())}:R>)",
                view=None,
            )
        except Exception:
            logger.exception("Dashboard event creation failed")
            if event_id and client:
                try:
                    await client.table("events").update({"status": "CANCELLED"}).eq("id", event_id).execute()
                except Exception:
                    pass
            await interaction.followup.send("تعذر إنشاء الفعالية. يرجى مراجعة الصلاحيات وقاعدة البيانات.", ephemeral=True)

    @discord.ui.button(label="إلغاء ❌", style=discord.ButtonStyle.secondary, row=3)
    async def cancel_event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="❌ تم إلغاء إنشاء الفعالية.", view=None)


class EventCreateModal(discord.ui.Modal, title="إنشاء فعالية"):
    title_input = discord.ui.TextInput(label="اسم الفعالية", max_length=100)
    description = discord.ui.TextInput(label="الوصف", style=discord.TextStyle.paragraph, max_length=800)
    capacity = discord.ui.TextInput(label="عدد المقاعد", default="25", max_length=3)
    reminder = discord.ui.TextInput(label="التذكير قبل الموعد بالساعات (0 لتعطيله)", default="24", max_length=3)

    def __init__(self, cog: "DashboardCog", event_channel: discord.TextChannel, admin_channel: discord.TextChannel | None = None) -> None:
        super().__init__()
        self.cog = cog
        self.event_channel = event_channel
        self.admin_channel = admin_channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True)
            return
        try:
            capacity = int(self.capacity.value.strip())
            reminder_hours = int(self.reminder.value.strip())
            if not 1 <= capacity <= 500 or not 0 <= reminder_hours <= 168:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("تحقق من أن عدد المقاعد بين (1–500) والتذكير بين (0–168) ساعة.", ephemeral=True)
            return

        picker_view = EventDateTimePickerView(
            cog=self.cog,
            event_channel=self.event_channel,
            admin_channel=self.admin_channel,
            title=self.title_input.value.strip(),
            description=self.description.value.strip(),
            capacity=capacity,
            reminder_hours=reminder_hours,
        )
        await interaction.response.send_message(
            content=picker_view.render_content(),
            view=picker_view,
            ephemeral=True,
        )


class EventChannelPickerView(discord.ui.View):
    """Step 1: pick the public event channel. Step 2 (optional): pick admin channel."""

    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.event_channel: discord.TextChannel | None = None
        self.admin_channel: discord.TextChannel | None = None

        # Public event channel picker
        event_picker = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="📢  اختر القناة العامة لنشر الفعالية",
            min_values=1,
            max_values=1,
            row=0,
        )
        event_picker.callback = self._pick_event_channel  # type: ignore[method-assign]
        self.add_item(event_picker)

        # Admin/private notification channel picker (optional)
        admin_picker = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="🔔  اختر قناة الإشعارات الإدارية (اختياري)",
            min_values=0,
            max_values=1,
            row=1,
        )
        admin_picker.callback = self._pick_admin_channel  # type: ignore[method-assign]
        self.add_item(admin_picker)

    async def _pick_event_channel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("استخدم هذا داخل السيرفر.", ephemeral=True); return
        ch_id = int(interaction.data["values"][0])
        channel = interaction.guild.get_channel(ch_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("اختر قناة نصية.", ephemeral=True); return
        if not channel.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message(f"البوت لا يستطيع الإرسال في {channel.mention}.", ephemeral=True); return
        self.event_channel = channel
        await interaction.response.edit_message(
            content=self._summary(),
            view=self,
        )

    async def _pick_admin_channel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("استخدم هذا داخل السيرفر.", ephemeral=True); return
        values = interaction.data.get("values", [])
        if not values:
            self.admin_channel = None
            await interaction.response.edit_message(content=self._summary(), view=self)
            return
        ch_id = int(values[0])
        channel = interaction.guild.get_channel(ch_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("اختر قناة نصية.", ephemeral=True); return
        if not channel.permissions_for(interaction.guild.me).send_messages:
            await interaction.response.send_message(f"البوت لا يستطيع الإرسال في {channel.mention}.", ephemeral=True); return
        self.admin_channel = channel
        await interaction.response.edit_message(content=self._summary(), view=self)

    def _summary(self) -> str:
        ev = self.event_channel.mention if self.event_channel else "لم تُحدَّد"
        adm = self.admin_channel.mention if self.admin_channel else "بدون إشعارات إدارية"
        return (
            f"📢  **قناة الفعالية:** {ev}\n"
            f"🔔  **قناة الإشعارات الإدارية:** {adm}\n\n"
            "اختر القنوات ثم اضغط **التالي** لملء تفاصيل الفعالية."
        )

    @discord.ui.button(label="التالي ← تفاصيل الفعالية", style=discord.ButtonStyle.success, row=2)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.event_channel:
            await interaction.response.send_message("اختر قناة الفعالية العامة أولًا.", ephemeral=True); return
        await interaction.response.send_modal(
            EventCreateModal(self.cog, self.event_channel, self.admin_channel)
        )


class EventAttendanceModal(discord.ui.Modal, title="تسجيل الحضور"):
    event_id = discord.ui.TextInput(label="رقم الفعالية", max_length=12)
    member_id = discord.ui.TextInput(label="معرف العضو", max_length=24)
    attended = discord.ui.TextInput(label="الحضور: نعم أو لا", default="نعم", max_length=3)

    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(); self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _staff(interaction):
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True); return
        try:
            event_id, member_id = int(self.event_id.value), int(self.member_id.value)
            value = self.attended.value.strip().casefold()
            if value not in {"نعم", "لا", "yes", "no"}: raise ValueError
            attended = value in {"نعم", "yes"}
        except ValueError:
            await interaction.response.send_message("أدخل رقم فعالية ومعرف عضو صحيحين، واكتب نعم أو لا للحضور.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        result = await client.table("event_participants").update({"attended": attended, "attendance_by": interaction.user.id, "attendance_at": datetime.now(timezone.utc).isoformat()}).eq("event_id", event_id).eq("guild_id", interaction.guild.id).eq("user_id", member_id).execute()
        await interaction.followup.send("تم تحديث الحضور." if result.data else "لم أجد العضو مسجلًا في هذه الفعالية.", ephemeral=True)


class EventCloseSelectView(discord.ui.View):
    def __init__(self, cog: "DashboardCog", events: list[dict]) -> None:
        super().__init__(timeout=120); self.cog = cog
        select = discord.ui.Select(placeholder="اختر فعالية لإغلاق التسجيل", options=[discord.SelectOption(label=f"#{e['id']} · {e['title']}"[:100], value=str(e["id"])) for e in events[:25]])
        select.callback = self.close_event  # type: ignore[method-assign]
        self.add_item(select)

    async def close_event(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _staff(interaction):
            await interaction.response.send_message("هذه العملية للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        event_id = int(interaction.data["values"][0])
        event = (await client.table("events").select("*").eq("id", event_id).eq("guild_id", interaction.guild.id).limit(1).execute()).data
        if not event or event[0]["status"] != "OPEN":
            await interaction.followup.send("الفعالية غير موجودة أو التسجيل مغلق مسبقًا.", ephemeral=True); return
        await client.table("events").update({"status": "CLOSED", "closed_at": datetime.now(timezone.utc).isoformat()}).eq("id", event_id).eq("guild_id", interaction.guild.id).eq("status", "OPEN").execute()
        channel = self.cog.bot.get_channel(int(event[0]["channel_id"]))
        if isinstance(channel, discord.TextChannel) and event[0].get("announcement_message_id"):
            try:
                message = await channel.fetch_message(int(event[0]["announcement_message_id"]))
                embed = message.embeds[0] if message.embeds else discord.Embed(title=event[0]["title"])
                embed.color = discord.Color.dark_grey(); embed.set_footer(text="التسجيل مغلق")
                await message.edit(embed=embed, view=None)
            except discord.HTTPException: logger.exception("Could not update event announcement after close")
        await interaction.followup.send("أُغلق التسجيل في الفعالية.", ephemeral=True)


class EventDashboardView(discord.ui.View):
    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=300); self.cog = cog

    @discord.ui.button(label="إنشاء فعالية", emoji="✨", style=discord.ButtonStyle.success)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.send_message(
            "**الخطوة 1:** اختر قناة نشر الفعالية وقناة الإشعارات الإدارية (اختيارية)، ثم اضغط **التالي**.",
            view=EventChannelPickerView(self.cog),
            ephemeral=True,
        )

    @discord.ui.button(label="قائمة الفعاليات", emoji="📋", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_events(interaction, edit=True)

    @discord.ui.button(label="إغلاق تسجيل", emoji="🔒", style=discord.ButtonStyle.secondary)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        rows = (await client.table("events").select("id,title").eq("guild_id", interaction.guild.id).eq("status", "OPEN").order("starts_at").limit(25).execute()).data or []
        if not rows: await interaction.followup.send("لا توجد فعاليات مفتوحة.", ephemeral=True); return
        await interaction.followup.send("اختر الفعالية:", view=EventCloseSelectView(self.cog, rows), ephemeral=True)

    @discord.ui.button(label="تسجيل الحضور", emoji="✔️", style=discord.ButtonStyle.secondary)
    async def attendance(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.send_modal(EventAttendanceModal(self.cog))

    @discord.ui.button(label="الرئيسية", emoji="🏠", style=discord.ButtonStyle.secondary)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_home(interaction, edit=True)


async def submit_staff_case(
    cog: "DashboardCog",
    interaction: discord.Interaction,
    *,
    kind: str,
    subject: str,
    details: str,
    start: str | None = None,
    end: str | None = None,
    target_user: discord.Member | None = None,
) -> None:
    if not interaction.guild:
        await interaction.response.send_message("استخدم النموذج داخل السيرفر.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        config = await cog.bot.db.get_guild_config(interaction.guild.id)
        channel_key = "staffflow_review_channel_id" if kind == "إجازة" else "staffflow_complaint_channel_id"
        channel = interaction.guild.get_channel(int((config or {}).get(channel_key) or 0))
        if not isinstance(channel, discord.TextChannel):
            destination_label = "الإجازات" if kind == "إجازة" else "الشكاوى"
            setup_hint = (
                "ضبط قناة الإجازات من لوحة الطاقم"
                if kind == "إجازة"
                else "الأمر `/staffflow complaint-channel`"
            )
            await interaction.followup.send(f"لم يحدد المسؤول قناة {destination_label} بعد. اطلب من المالك أو Administrator إعدادها عبر {setup_hint}.", ephemeral=True)
            return
        client = await cog.bot.db.get_client()
        result = await client.table("staff_cases").insert({
            "guild_id": interaction.guild.id,
            "user_id": interaction.user.id,
            "user_name": str(interaction.user),
            "target_user_id": target_user.id if target_user else None,
            "kind": kind,
            "subject": subject,
            "details": details,
            "starts_at": start,
            "ends_at": end,
            "status": "PENDING",
        }).execute()
        case = result.data[0]
        embed = discord.Embed(title=f"طلب {kind} · #{case['id']}", description=details[:4000], color=discord.Color.orange())
        reporter_label = "مقدم الشكوى" if kind == "شكوى" else "مقدم الطلب"
        embed.add_field(name=reporter_label, value=interaction.user.mention, inline=False)
        if kind == "شكوى" and target_user:
            embed.add_field(name="المشكو عليه", value=target_user.mention, inline=False)
        embed.add_field(name="الموضوع", value=subject, inline=False)
        if start and end:
            embed.add_field(name="الفترة", value=f"{start} إلى {end}", inline=False)
        decision_view = StaffCaseDecisionView(cog, int(case["id"]))
        message = await channel.send(embed=embed, view=decision_view, allowed_mentions=discord.AllowedMentions.none())
        cog.bot.add_view(decision_view, message_id=message.id)
        await client.table("staff_cases").update({"review_channel_id": channel.id, "review_message_id": message.id}).eq("id", case["id"]).execute()
        submitted_message = f"وصل طلبك إلى الإدارة برقم #{case['id']}."
        if kind == "شكوى" and target_user:
            submitted_message += f" الشخص المشكو عليه: {target_user.mention}."
        await interaction.followup.send(
            submitted_message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        logger.exception("Could not submit dashboard staff case")
        await interaction.followup.send("تعذر إرسال الطلب. تحقق من إعدادات قاعدة البيانات والقناة.", ephemeral=True)


class LeaveDetailsModal(discord.ui.Modal, title="تفاصيل طلب الإجازة"):
    details = discord.ui.TextInput(label="ملاحظات أو تفاصيل إضافية", style=discord.TextStyle.paragraph, max_length=1500)

    def __init__(self, cog: "DashboardCog", start: str, end: str, reason: str) -> None:
        super().__init__()
        self.cog, self.start, self.end, self.reason = cog, start, end, reason

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _staff(interaction):
            await interaction.response.send_message("هذا الإجراء لطاقم السيرفر فقط.", ephemeral=True)
            return
        await submit_staff_case(self.cog, interaction, kind="إجازة", subject=self.reason,
                                details=self.details.value.strip(), start=self.start, end=self.end)


class OtherLeaveReasonModal(discord.ui.Modal, title="سبب الإجازة الآخر"):
    reason_input = discord.ui.TextInput(
        label="اكتب سبب الإجازة",
        placeholder="اكتب السبب الذي تريد إرساله للإدارة",
        max_length=100,
    )

    def __init__(self, view: "LeaveRequestView", source_interaction: discord.Interaction) -> None:
        super().__init__()
        self.view_state = view
        self.source_interaction = source_interaction

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _staff(interaction):
            await interaction.response.send_message("هذا الإجراء لطاقم السيرفر فقط.", ephemeral=True)
            return
        reason = self.reason_input.value.strip()
        if not reason:
            await interaction.response.send_message("اكتب سبب الإجازة قبل الإرسال.", ephemeral=True)
            return
        self.view_state.reason = reason
        self.view_state.reason_is_other = True
        self.view_state.refresh_selected_options()
        await interaction.response.send_message("تم حفظ السبب الآخر. أكمل اختيار التاريخين ثم اضغط متابعة.", ephemeral=True)
        try:
            await self.source_interaction.edit_original_response(
                content=self.view_state.summary_text(), view=self.view_state
            )
        except discord.HTTPException:
            logger.debug("Could not refresh the leave form after custom reason entry", exc_info=True)


class LeaveRequestView(discord.ui.View):
    """Guided leave request: dates and reason are selected from menus."""

    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.start_date: str | None = None
        self.end_date: str | None = None
        self.reason: str | None = None
        self.reason_is_other = False
        today = datetime.now(timezone.utc).date()
        self.start_select = discord.ui.Select(
            placeholder="اختر تاريخ بداية الإجازة",
            options=[discord.SelectOption(label=(today + timedelta(days=i)).isoformat(), value=(today + timedelta(days=i)).isoformat()) for i in range(25)],
            row=0,
        )
        self.start_select.callback = self.choose_start  # type: ignore[method-assign]
        self.end_select = discord.ui.Select(
            placeholder="اختر تاريخ البداية أولًا",
            options=[discord.SelectOption(label="اختر تاريخ البداية أولًا", value="__unset")],
            disabled=True,
            row=1,
        )
        self.end_select.callback = self.choose_end  # type: ignore[method-assign]
        self.reason_select = discord.ui.Select(
            placeholder="اختر سبب الإجازة",
            options=[
                discord.SelectOption(label="إجازة سنوية", value="إجازة سنوية", emoji="🌴"),
                discord.SelectOption(label="إجازة مرضية", value="إجازة مرضية", emoji="🩺"),
                discord.SelectOption(label="ظرف طارئ", value="ظرف طارئ", emoji="🚨"),
                discord.SelectOption(label="إجازة شخصية", value="إجازة شخصية", emoji="👤"),
                discord.SelectOption(label="سبب آخر", value="سبب آخر", emoji="📝"),
            ],
            row=2,
        )
        self.reason_select.callback = self.choose_reason  # type: ignore[method-assign]
        self.add_item(self.start_select)
        self.add_item(self.end_select)
        self.add_item(self.reason_select)

    async def choose_start(self, interaction: discord.Interaction) -> None:
        self.start_date = self.start_select.values[0]
        start = datetime.fromisoformat(self.start_date).date()
        self.end_select.options = [
            discord.SelectOption(label=(start + timedelta(days=i)).isoformat(), value=(start + timedelta(days=i)).isoformat())
            for i in range(25)
        ]
        self.end_select.placeholder = "اختر تاريخ نهاية الإجازة"
        self.end_select.disabled = False
        self.end_date = None
        self.refresh_selected_options()
        await interaction.response.edit_message(content=self.summary_text(), view=self)

    async def choose_end(self, interaction: discord.Interaction) -> None:
        self.end_date = self.end_select.values[0]
        self.refresh_selected_options()
        await interaction.response.edit_message(content=self.summary_text(), view=self)

    async def choose_reason(self, interaction: discord.Interaction) -> None:
        selected_reason = self.reason_select.values[0]
        if selected_reason == "سبب آخر":
            await interaction.response.send_modal(OtherLeaveReasonModal(self, interaction))
            return
        self.reason = selected_reason
        self.reason_is_other = False
        self.refresh_selected_options()
        await interaction.response.edit_message(content=self.summary_text(), view=self)

    def refresh_selected_options(self) -> None:
        today = datetime.now(timezone.utc).date()
        self.start_select.options = [
            discord.SelectOption(
                label=(today + timedelta(days=i)).isoformat(),
                value=(today + timedelta(days=i)).isoformat(),
                default=(today + timedelta(days=i)).isoformat() == self.start_date,
            )
            for i in range(25)
        ]
        if self.start_date:
            start = datetime.fromisoformat(self.start_date).date()
            self.end_select.options = [
                discord.SelectOption(
                    label=(start + timedelta(days=i)).isoformat(),
                    value=(start + timedelta(days=i)).isoformat(),
                    default=(start + timedelta(days=i)).isoformat() == self.end_date,
                )
                for i in range(25)
            ]
        reason_values = ("إجازة سنوية", "إجازة مرضية", "ظرف طارئ", "إجازة شخصية", "سبب آخر")
        reason_emojis = ("🌴", "🩺", "🚨", "👤", "📝")
        self.reason_select.options = [
            discord.SelectOption(
                label=value,
                value=value,
                emoji=reason_emojis[index],
                default=(value == "سبب آخر" and self.reason_is_other) or (value == self.reason and not self.reason_is_other),
            )
            for index, value in enumerate(reason_values)
        ]

    def summary_text(self) -> str:
        start = f"**{self.start_date}**" if self.start_date else "لم يُحدد"
        end = f"**{self.end_date}**" if self.end_date else "لم يُحدد"
        reason = f"**{self.reason}**" if self.reason else "لم يُحدد"
        return f"البداية: {start} · النهاية: {end} · السبب: {reason}. أكمل الاختيارات ثم اضغط **متابعة** لإضافة التفاصيل."

    @discord.ui.button(label="متابعة وإضافة التفاصيل", emoji="➡️", style=discord.ButtonStyle.success, row=3)
    async def continue_request(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.start_date or not self.end_date or not self.reason:
            await interaction.response.send_message("اختر تاريخ البداية والنهاية وسبب الإجازة أولًا.", ephemeral=True)
            return
        if self.end_date < self.start_date:
            await interaction.response.send_message("تاريخ النهاية يجب أن يكون في يوم البداية أو بعده.", ephemeral=True)
            return
        await interaction.response.send_modal(LeaveDetailsModal(self.cog, self.start_date, self.end_date, self.reason))


class ComplaintDetailsModal(discord.ui.Modal, title="تفاصيل الشكوى الإدارية"):
    details = discord.ui.TextInput(label="اشرح الشكوى", style=discord.TextStyle.paragraph, max_length=1500)

    def __init__(self, cog: "DashboardCog", subject: str, target_user: discord.Member) -> None:
        super().__init__()
        self.cog, self.subject, self.target_user = cog, subject, target_user

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _staff(interaction):
            await interaction.response.send_message("هذا الإجراء لطاقم السيرفر فقط.", ephemeral=True)
            return
        await submit_staff_case(
            self.cog,
            interaction,
            kind="شكوى",
            subject=self.subject,
            details=self.details.value.strip(),
            target_user=self.target_user,
        )


class ComplaintTargetView(discord.ui.View):
    def __init__(self, cog: "DashboardCog", subject: str) -> None:
        super().__init__(timeout=180)
        self.cog, self.subject = cog, subject
        self.picker = discord.ui.UserSelect(
            placeholder="اختر الشخص المشكو عليه",
            min_values=1,
            max_values=1,
        )
        self.picker.callback = self.choose_target  # type: ignore[method-assign]
        self.add_item(self.picker)

    async def choose_target(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _staff(interaction):
            await interaction.response.send_message("هذا الإجراء لطاقم السيرفر فقط.", ephemeral=True)
            return
        selected_users = self.picker.values
        if not selected_users:
            await interaction.response.send_message("تعذر العثور على العضو المختار في هذا السيرفر.", ephemeral=True)
            return
        selected = selected_users[0]
        if isinstance(selected, discord.Member):
            target = selected
        else:
            try:
                target = interaction.guild.get_member(selected.id)
                if target is None:
                    target = await interaction.guild.fetch_member(selected.id)
            except discord.NotFound:
                await interaction.response.send_message("تعذر العثور على العضو المختار في هذا السيرفر.", ephemeral=True)
                return
        if target.id == interaction.user.id:
            await interaction.response.send_message("اختر عضوًا آخر؛ لا يمكنك تقديم شكوى على نفسك.", ephemeral=True)
            return
        await interaction.response.send_modal(ComplaintDetailsModal(self.cog, self.subject, target))


class ComplaintCategoryView(discord.ui.View):
    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=180)
        self.cog = cog
        select = discord.ui.Select(
            placeholder="اختر تصنيف الشكوى",
            options=[
                discord.SelectOption(label="سلوك غير مناسب", value="سلوك غير مناسب"),
                discord.SelectOption(label="مخالفة إدارية", value="مخالفة إدارية"),
                discord.SelectOption(label="مشكلة في التعامل", value="مشكلة في التعامل"),
                discord.SelectOption(label="تقصير في المهام", value="تقصير في المهام"),
                discord.SelectOption(label="موضوع آخر", value="موضوع آخر"),
            ],
        )
        select.callback = self.choose  # type: ignore[method-assign]
        self.add_item(select)

    async def choose(self, interaction: discord.Interaction) -> None:
        if not _staff(interaction):
            await interaction.response.send_message("هذا الإجراء لطاقم السيرفر فقط.", ephemeral=True)
            return
        subject = interaction.data["values"][0]
        await interaction.response.edit_message(
            content="اختر الشخص الذي تريد تقديم الشكوى عليه:",
            view=ComplaintTargetView(self.cog, subject),
        )


class ShiftScheduleView(discord.ui.View):
    """Interactive Shift Scheduling with UserSelect and DateTime Dropdowns."""

    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.selected_member_id: int | None = None
        self.tz = timezone(timedelta(hours=3))
        now_local = datetime.now(self.tz)
        self.selected_date = now_local.date().isoformat()
        self.selected_time = f"{now_local.hour:02d}:00"
        self.selected_duration = 4

        self._build_components(now_local)

    def _build_components(self, now_local: datetime) -> None:
        # Member Select
        self.member_select = discord.ui.UserSelect(
            placeholder="👤 اختر عضو الطاقم المكلف بالمناوبة",
            min_values=1,
            max_values=1,
            row=0,
        )
        self.member_select.callback = self._on_member_change
        self.add_item(self.member_select)

        # Date Select
        arabic_days = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
        arabic_months = [
            "", "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
            "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"
        ]
        date_options = []
        for i in range(14):
            day = (now_local + timedelta(days=i)).date()
            weekday_name = arabic_days[day.weekday()]
            month_name = arabic_months[day.month]
            tag = " (اليوم)" if i == 0 else " (غداً)" if i == 1 else ""
            label = f"{weekday_name} {day.day} {month_name}{tag}"
            date_options.append(discord.SelectOption(
                label=label[:100],
                value=day.isoformat(),
                default=(day.isoformat() == self.selected_date),
            ))
        self.date_select = discord.ui.Select(
            placeholder="📅 اختر تاريخ بداية المناوبة",
            options=date_options,
            row=1,
        )
        self.date_select.callback = self._on_date_change
        self.add_item(self.date_select)

        # Time Select
        time_choices = [
            ("00:00", "12:00 AM منتصف الليل"),
            ("02:00", "02:00 AM فجراً"),
            ("04:00", "04:00 AM فجراً"),
            ("06:00", "06:00 AM صباحاً"),
            ("08:00", "08:00 AM صباحاً"),
            ("10:00", "10:00 AM صباحاً"),
            ("12:00", "12:00 PM ظهراً"),
            ("14:00", "02:00 PM ظهراً"),
            ("16:00", "04:00 PM عصراً"),
            ("18:00", "06:00 PM مساءً (المغرب)"),
            ("20:00", "08:00 PM مساءً (العشاء)"),
            ("22:00", "10:00 PM مساءً"),
        ]
        time_options = [
            discord.SelectOption(label=label, value=val, default=(val == self.selected_time))
            for val, label in time_choices
        ]
        self.time_select = discord.ui.Select(
            placeholder="⏰ اختر ساعة بداية المناوبة",
            options=time_options,
            row=2,
        )
        self.time_select.callback = self._on_time_change
        self.add_item(self.time_select)

        # Duration Select
        duration_options = [
            discord.SelectOption(label="ساعة واحدة (1h)", value="1"),
            discord.SelectOption(label="ساعتان (2h)", value="2"),
            discord.SelectOption(label="3 ساعات (3h)", value="3"),
            discord.SelectOption(label="4 ساعات (4h)", value="4", default=True),
            discord.SelectOption(label="6 ساعات (6h)", value="6"),
            discord.SelectOption(label="8 ساعات (8h)", value="8"),
            discord.SelectOption(label="12 ساعة (12h)", value="12"),
        ]
        self.duration_select = discord.ui.Select(
            placeholder="⏳ اختر مدة المناوبة بالساعات",
            options=duration_options,
            row=3,
        )
        self.duration_select.callback = self._on_duration_change
        self.add_item(self.duration_select)

    def get_selected_datetime(self) -> datetime:
        d = datetime.fromisoformat(self.selected_date).date()
        h, m = map(int, self.selected_time.split(":"))
        local_dt = datetime(d.year, d.month, d.day, h, m, tzinfo=self.tz)
        return local_dt.astimezone(timezone.utc)

    def render_content(self) -> str:
        dt_utc = self.get_selected_datetime()
        ts = int(dt_utc.timestamp())
        member_text = f"<@{self.selected_member_id}>" if self.selected_member_id else "لم يتم اختياره بعد"
        return (
            f"🕒 **جدولة مناوبة جديدة للطاقم**\n\n"
            f"👤 **عضو الطاقم:** {member_text}\n"
            f"📅 **بداية المناوبة:** <t:{ts}:F> (<t:{ts}:R>)\n"
            f"⏳ **المدة:** `{self.selected_duration}` ساعات\n\n"
            "👇 **اختر العضو والموعد والمدة من القوائم، ثم اضغط 'إدخال المهام وتأكيد المناوبة':**"
        )

    async def _on_member_change(self, interaction: discord.Interaction) -> None:
        values = self.member_select.values
        if values:
            self.selected_member_id = values[0].id
        await interaction.response.edit_message(content=self.render_content(), view=self)

    async def _on_date_change(self, interaction: discord.Interaction) -> None:
        self.selected_date = self.date_select.values[0]
        for opt in self.date_select.options:
            opt.default = (opt.value == self.selected_date)
        await interaction.response.edit_message(content=self.render_content(), view=self)

    async def _on_time_change(self, interaction: discord.Interaction) -> None:
        self.selected_time = self.time_select.values[0]
        for opt in self.time_select.options:
            opt.default = (opt.value == self.selected_time)
        await interaction.response.edit_message(content=self.render_content(), view=self)

    async def _on_duration_change(self, interaction: discord.Interaction) -> None:
        self.selected_duration = int(self.duration_select.values[0])
        for opt in self.duration_select.options:
            opt.default = (opt.value == str(self.selected_duration))
        await interaction.response.edit_message(content=self.render_content(), view=self)

    @discord.ui.button(label="إدخال المهام وتأكيد المناوبة 📝", style=discord.ButtonStyle.success, row=4)
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.selected_member_id:
            await interaction.response.send_message("❌ يرجى اختيار عضو الطاقم من القائمة أولاً.", ephemeral=True)
            return
        start_utc = self.get_selected_datetime()
        if start_utc <= datetime.now(timezone.utc):
            await interaction.response.send_message("❌ وقت بداية المناوبة يجب أن يكون مستقبلياً.", ephemeral=True)
            return

        await interaction.response.send_modal(
            ShiftDutiesModal(self.cog, self.selected_member_id, start_utc, self.selected_duration)
        )


class ShiftDutiesModal(discord.ui.Modal, title="مهام المناوبة"):
    duties = discord.ui.TextInput(
        label="مهام المناوبة والمسؤوليات",
        style=discord.TextStyle.paragraph,
        max_length=900,
        placeholder="مثال: الإشراف على التكتات ومراجعة الإعلانات وتنظيم الفعاليات...",
    )

    def __init__(self, cog: "DashboardCog", member_id: int, start_utc: datetime, duration_hours: int) -> None:
        super().__init__()
        self.cog = cog
        self.member_id = member_id
        self.start_utc = start_utc
        self.duration_hours = duration_hours

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _admin(interaction):
            await interaction.response.send_message("للإدارة فقط.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        end_utc = self.start_utc + timedelta(hours=self.duration_hours)
        duties_text = self.duties.value.strip()

        result = await client.table("staff_shifts").insert({
            "guild_id": interaction.guild.id,
            "user_id": self.member_id,
            "created_by": interaction.user.id,
            "starts_at": self.start_utc.isoformat(),
            "ends_at": end_utc.isoformat(),
            "duties": duties_text,
            "status": "SCHEDULED",
        }).execute()

        shift_id = result.data[0]["id"]
        await client.table("staff_audit").insert({
            "guild_id": interaction.guild.id,
            "actor_id": interaction.user.id,
            "target_user_id": self.member_id,
            "action": "SHIFT_SCHEDULED",
            "details": {
                "shift_id": shift_id,
                "starts_at": self.start_utc.isoformat(),
                "duration_hours": self.duration_hours,
                "duties": duties_text,
            },
        }).execute()

        ts = int(self.start_utc.timestamp())
        await interaction.followup.send(
            f"✅ **تمت جدولة المناوبة رقم #{shift_id} بنجاح!**\n"
            f"👤 **العضو المكلف:** <@{self.member_id}>\n"
            f"📅 **الموعد:** <t:{ts}:F> (<t:{ts}:R>)\n"
            f"⏳ **المدة:** `{self.duration_hours}` ساعات\n"
            f"📋 **المهام:** {duties_text}",
            ephemeral=True,
        )


class StaffDecisionModal(discord.ui.Modal):
    def __init__(self, cog: "DashboardCog", case_id: int, decision: str) -> None:
        super().__init__(title="مراجعة طلب الطاقم")
        self.cog, self.case_id, self.decision = cog, case_id, decision
        self.note = discord.ui.TextInput(
            label="سبب الرفض" if decision == "REJECTED" else "ملاحظة (اختيارية)",
            style=discord.TextStyle.paragraph,
            max_length=900,
            required=decision == "REJECTED",
            placeholder="اكتب سببًا واضحًا ليصل للمتقدم برسالة خاصة.",
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _admin(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        note = self.note.value.strip()
        if self.decision == "REJECTED" and not note:
            await interaction.followup.send("سبب الرفض مطلوب.", ephemeral=True); return
        response = await client.rpc("review_staff_case", {"p_case_id": self.case_id, "p_guild_id": interaction.guild.id, "p_reviewer_id": interaction.user.id, "p_decision": self.decision, "p_note": note}).execute()
        row = response.data[0] if isinstance(response.data, list) and response.data else response.data
        if not isinstance(row, dict) or not row.get("changed"):
            await interaction.followup.send("الطلب غير موجود أو سبق اتخاذ قرار فيه.", ephemeral=True); return
        case = row["case"]
        channel = self.cog.bot.get_channel(int(case.get("review_channel_id") or 0))
        if isinstance(channel, discord.TextChannel) and case.get("review_message_id"):
            try:
                card = await channel.fetch_message(int(case["review_message_id"]))
                embed = card.embeds[0] if card.embeds else discord.Embed(title=f"طلب الطاقم #{self.case_id}")
                embed.color = discord.Color.green() if self.decision == "APPROVED" else discord.Color.red()
                decision_label = "مقبول" if self.decision == "APPROVED" else "مرفوض"
                embed.add_field(name="القرار", value=f"{decision_label} · {interaction.user}\n{note or 'لا توجد ملاحظة'}", inline=False)
                await card.edit(embed=embed, view=None)
            except discord.HTTPException: logger.exception("Could not update staff case card")
        try:
            applicant = await self.cog.bot.fetch_user(int(case["user_id"]))
            result_text = "تم قبول طلبك" if self.decision == "APPROVED" else "تم رفض طلبك"
            dm = discord.Embed(title=f"نتيجة طلب {case.get('kind', 'الطاقم')}", description=f"{result_text} رقم **#{self.case_id}** في سيرفر **{interaction.guild.name}**.", color=discord.Color.green() if self.decision == "APPROVED" else discord.Color.red())
            if self.decision == "REJECTED": dm.add_field(name="سبب الرفض", value=note, inline=False)
            elif note: dm.add_field(name="ملاحظة الإدارة", value=note, inline=False)
            await applicant.send(embed=dm)
            dm_status = " وتم إشعار المتقدم خاصًا."
        except (discord.Forbidden, discord.HTTPException):
            dm_status = " لكن تعذر إرسال رسالة خاصة للمتقدم (قد تكون رسائله الخاصة مغلقة)."
        await interaction.followup.send(f"تم حفظ القرار على الطلب #{self.case_id}.{dm_status}", ephemeral=True)


class StaffCaseDecisionView(discord.ui.View):
    """Persistent approval/rejection buttons attached to staff case review messages."""

    def __init__(self, cog: "DashboardCog", case_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.case_id = case_id
        # Build buttons programmatically so custom_ids include the case_id and survive restarts.
        approve_btn = discord.ui.Button(
            label="قبول",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"adsentry:staff-case:{case_id}:approve",
        )
        approve_btn.callback = self._approve  # type: ignore[method-assign]
        self.add_item(approve_btn)

        reject_btn = discord.ui.Button(
            label="رفض",
            emoji="❌",
            style=discord.ButtonStyle.danger,
            custom_id=f"adsentry:staff-case:{case_id}:reject",
        )
        reject_btn.callback = self._reject  # type: ignore[method-assign]
        self.add_item(reject_btn)

    async def _approve(self, interaction: discord.Interaction) -> None:
        if not _admin(interaction):
            await interaction.response.send_message("مراجعة طلبات الإجازة متاحة للمالك وAdministrator فقط.", ephemeral=True)
            return
        await interaction.response.send_modal(StaffDecisionModal(self.cog, self.case_id, "APPROVED"))

    async def _reject(self, interaction: discord.Interaction) -> None:
        if not _admin(interaction):
            await interaction.response.send_message("مراجعة طلبات الإجازة متاحة للمالك وAdministrator فقط.", ephemeral=True)
            return
        await interaction.response.send_modal(StaffDecisionModal(self.cog, self.case_id, "REJECTED"))


class StaffFlowChannelSetupView(discord.ui.View):
    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(timeout=180)
        self.cog = cog
        picker = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text], placeholder="اختر قناة طلبات الإجازة")
        picker.callback = self.save_channel  # type: ignore[method-assign]
        self.add_item(picker)

    async def save_channel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _admin(interaction):
            await interaction.response.send_message("هذا الإعداد للمالك وAdministrator فقط.", ephemeral=True); return
        guild = interaction.guild
        channel = guild.get_channel(int(interaction.data["values"][0]))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("اختر قناة نصية من هذا السيرفر.", ephemeral=True); return
        bot_member = guild.me
        if bot_member is None or not channel.permissions_for(bot_member).manage_channels:
            await interaction.response.send_message("يحتاج البوت صلاحية Manage Channels لتأمين قناة الطلبات.", ephemeral=True); return
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True, manage_messages=True),
            }
            await channel.edit(overwrites=overwrites, reason=f"Private StaffFlow review channel configured by {interaction.user}")
            await self.cog.bot.db.set_staffflow_review_channel(guild.id, channel.id)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not secure StaffFlow review channel")
            await interaction.response.send_message("تعذر تأمين القناة أو حفظ إعدادها. تحقق من صلاحيات البوت وقاعدة البيانات.", ephemeral=True); return
        except Exception:
            logger.exception("Could not persist StaffFlow review channel")
            await interaction.response.send_message("تم تحديث صلاحيات القناة لكن تعذر حفظ إعدادها في قاعدة البيانات.", ephemeral=True); return
        await interaction.response.send_message(f"تم ضبط {channel.mention} لقضايا StaffFlow. أصبحت مخفية عن @everyone؛ المالك وأصحاب Administrator يستطيعون رؤيتها بصلاحيات Discord.", ephemeral=True)


class StaffCaseQueueView(discord.ui.View):
    def __init__(self, cog: "DashboardCog", cases: list[dict]) -> None:
        super().__init__(timeout=180); self.cog, self.case_id = cog, None
        select = discord.ui.Select(placeholder="اختر طلبًا معلقًا", options=[discord.SelectOption(label=f"#{c['id']} · {c['kind']} · {c['user_name']}"[:100], value=str(c["id"])) for c in cases[:25]])
        select.callback = self.select_case  # type: ignore[method-assign]
        self.add_item(select)

    async def select_case(self, interaction: discord.Interaction) -> None:
        if not _admin(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        self.case_id = int(interaction.data["values"][0])
        await interaction.response.edit_message(content=f"تم اختيار الطلب #{self.case_id}. اختر قرار المراجعة:", view=self)

    @discord.ui.button(label="قبول", emoji="✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.case_id: await interaction.response.send_message("اختر الطلب أولًا.", ephemeral=True); return
        await interaction.response.send_modal(StaffDecisionModal(self.cog, self.case_id, "APPROVED"))

    @discord.ui.button(label="رفض", emoji="❌", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.case_id: await interaction.response.send_message("اختر الطلب أولًا.", ephemeral=True); return
        await interaction.response.send_modal(StaffDecisionModal(self.cog, self.case_id, "REJECTED"))


class StaffDashboardView(discord.ui.View):
    def __init__(self, cog: "DashboardCog", can_manage: bool = False) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        if not can_manage:
            self.remove_item(self.review)
            self.remove_item(self.shift_create)
            self.remove_item(self.promote)
            self.remove_item(self.configure_leave_channel)

    @discord.ui.button(label="طلب إجازة", emoji="🌴", style=discord.ButtonStyle.primary, row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message("اختر تاريخ بداية ونهاية الإجازة وسببها:", view=LeaveRequestView(self.cog), ephemeral=True)

    @discord.ui.button(label="شكوى إدارية", emoji="📨", style=discord.ButtonStyle.primary, row=0)
    async def complaint(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message("اختر تصنيف الشكوى ثم اكتب تفاصيلها:", view=ComplaintCategoryView(self.cog), ephemeral=True)

    @discord.ui.button(label="مراجعة الطلبات", emoji="🗂️", style=discord.ButtonStyle.secondary, row=0)
    async def review(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not _admin(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        cases = (await client.table("staff_cases").select("id,kind,user_name").eq("guild_id", interaction.guild.id).eq("status", "PENDING").order("created_at").limit(25).execute()).data or []
        if not cases: await interaction.followup.send("لا توجد طلبات معلقة.", ephemeral=True); return
        await interaction.followup.send("اختر طلبًا ثم القرار:", view=StaffCaseQueueView(self.cog, cases), ephemeral=True)

    @discord.ui.button(label="جدولة مناوبة", emoji="🕒", style=discord.ButtonStyle.secondary, row=1)
    async def shift_create(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _admin(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        view = ShiftScheduleView(self.cog)
        await interaction.response.send_message(content=view.render_content(), view=view, ephemeral=True)

    @discord.ui.button(label="عرض المناوبات", emoji="📋", style=discord.ButtonStyle.secondary, row=1)
    async def shifts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not _staff(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await self.cog.show_shifts(interaction)

    @discord.ui.button(label="ضبط قناة الإجازات", emoji="🔒", style=discord.ButtonStyle.secondary, row=1)
    async def configure_leave_channel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not _admin(interaction):
            await interaction.response.send_message("هذا الإعداد للمالك وAdministrator فقط.", ephemeral=True); return
        await interaction.response.send_message("اختر قناة نصية مخصصة. سيضبط البوت صلاحياتها لتكون مخفية عن @everyone.", view=StaffFlowChannelSetupView(self.cog), ephemeral=True)

    @discord.ui.button(label="توثيق ترقية", emoji="⬆️", style=discord.ButtonStyle.secondary, row=1)
    async def promote(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _admin(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.send_modal(PositionChangeModal(self.cog))

    @discord.ui.button(label="الرئيسية", emoji="🏠", style=discord.ButtonStyle.secondary, row=1)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_home(interaction, edit=True)


class ShiftDashboardView(discord.ui.View):
    def __init__(self, cog: "DashboardCog", can_manage: bool = False) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        if not can_manage:
            self.remove_item(self.schedule)

    @discord.ui.button(label="جدولة مناوبة", emoji="🕒", style=discord.ButtonStyle.primary, row=0)
    async def schedule(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not _admin(interaction):
            await interaction.response.send_message("جدولة المناوبات متاحة للمالك وAdministrator فقط.", ephemeral=True)
            return
        view = ShiftScheduleView(self.cog)
        await interaction.response.send_message(content=view.render_content(), view=view, ephemeral=True)

    @discord.ui.button(label="تحديث الجدول", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not _staff(interaction):
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True)
            return
        await self.cog.show_shifts(interaction, edit=True)

    @discord.ui.button(label="الرئيسية", emoji="🏠", style=discord.ButtonStyle.secondary, row=0)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_home(interaction, edit=True)


class RecruitmentCloseSelectView(discord.ui.View):
    def __init__(self, cog: "DashboardCog", openings: list[dict]) -> None:
        super().__init__(timeout=120); self.cog = cog
        select = discord.ui.Select(placeholder="اختر تقديمًا لإغلاقه", options=[discord.SelectOption(label=f"#{row['id']} · {row['title']}"[:100], value=str(row["id"])) for row in openings[:25]])
        select.callback = self.close_opening  # type: ignore[method-assign]
        self.add_item(select)

    async def close_opening(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _admin(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        opening_id = int(interaction.data["values"][0])
        opening = await self.cog.bot.db.get_recruitment(opening_id)
        if not opening or opening["guild_id"] != interaction.guild.id:
            await interaction.followup.send("التقديم غير موجود.", ephemeral=True); return
        changed = await self.cog.bot.db.close_recruitment(opening_id, interaction.guild.id)
        if changed: await close_announcement(self.cog.bot, opening)
        await interaction.followup.send("تم إغلاق التقديم." if changed else "التقديم مغلق مسبقًا.", ephemeral=True)


class PositionChangeModal(discord.ui.Modal, title="توثيق ترقية أو تغيير منصب"):
    member_id = discord.ui.TextInput(label="معرف العضو", max_length=24)
    previous = discord.ui.TextInput(label="المنصب السابق", max_length=100, required=False)
    new = discord.ui.TextInput(label="المنصب الجديد", max_length=100)
    reason = discord.ui.TextInput(label="السبب", style=discord.TextStyle.paragraph, max_length=900)

    def __init__(self, cog: "DashboardCog") -> None:
        super().__init__(); self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _admin(interaction): await interaction.response.send_message("للإدارة فقط.", ephemeral=True); return
        try: member_id = int(self.member_id.value)
        except ValueError:
            await interaction.response.send_message("معرف العضو غير صحيح.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        client = await self.cog.bot.db.get_client()
        await client.table("staff_audit").insert({"guild_id": interaction.guild.id, "actor_id": interaction.user.id, "target_user_id": member_id, "action": "POSITION_CHANGE", "details": {"previous": self.previous.value or "غير محدد", "new": self.new.value, "reason": self.reason.value}}).execute()
        await interaction.followup.send("تم توثيق تغيير المنصب في سجل الإدارة.", ephemeral=True)


class DashboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Stable custom IDs make the operations-center buttons survive restarts.
        self.bot.add_view(PersistentDashboardView(self))
        try:
            client = await self.bot.db.get_client()
            cases = (await client.table("staff_cases").select("id,review_message_id").eq("status", "PENDING").not_.is_("review_message_id", "null").execute()).data or []
            for case in cases:
                if case.get("review_message_id"):
                    view = StaffCaseDecisionView(self, int(case["id"]))
                    self.bot.add_view(view, message_id=int(case["review_message_id"]))
        except Exception:
            logger.exception("Could not restore persistent StaffFlow decision buttons")

    async def dashboard_embed(self, guild: discord.Guild) -> discord.Embed:
        try:
            client = await self.bot.db.get_client()
            openings = (await client.table("recruitments").select("id").eq("guild_id", guild.id).eq("status", "OPEN").execute()).data or []
            events = (await client.table("events").select("id").eq("guild_id", guild.id).eq("status", "OPEN").execute()).data or []
            cases = (await client.table("staff_cases").select("id").eq("guild_id", guild.id).eq("status", "PENDING").execute()).data or []
            channels = (await client.table("recruitment_review_channels").select("channel_id").eq("guild_id", guild.id).order("slot").execute()).data or []
            ads = await self.bot.db.get_ad_stats(guild.id, days=7)
            channel_text = "، ".join(f"<#{row['channel_id']}>" for row in channels) or "لم يتم ضبطها"
            description = (f"مركز العمليات في **{guild.name}**\n\n"
                           f"**التوظيف:** {len(openings)} تقديم مفتوح\n**الفعاليات:** {len(events)} فعالية مفتوحة\n**طلبات الطاقم المعلقة:** {len(cases)}\n"
                           f"**الإعلانات خلال 7 أيام:** {ads['total_count']} (مقبول: {ads['approved_count']} · مرفوض: {ads['rejected_count']})\n"
                           f"**قنوات مراجعة المتقدمين:** {channel_text}\n\n"
                           "اختر نظامًا من الأزرار. ستُفتح أدوات الإدارة لك برسائل خاصة، وستبقى لوحة المركز هذه ثابتة هنا.")
        except Exception:
            logger.exception("Could not load dashboard summary for guild %s", guild.id)
            description = "مركز العمليات جاهز، لكن تعذر تحميل الإحصاءات. تحقق من تشغيل ترحيلات قاعدة البيانات."
        return discord.Embed(title="AdSentry · مركز العمليات", description=description, color=discord.Color.blurple())

    async def refresh_persistent_panel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("استخدم اللوحة داخل السيرفر.", ephemeral=True); return
        await interaction.response.defer()
        embed = await self.dashboard_embed(interaction.guild)
        await interaction.edit_original_response(embed=embed, view=PersistentDashboardView(self))

    async def publish_panel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not interaction.guild or not _admin(interaction):
            await interaction.response.send_message("إعداد مركز العمليات متاح للإدارة فقط.", ephemeral=True); return
        if channel.guild.id != interaction.guild.id:
            await interaction.response.send_message("اختر قناة من هذا السيرفر.", ephemeral=True); return
        bot_member = interaction.guild.me
        perms = channel.permissions_for(bot_member)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message("أحتاج View Channel وSend Messages وEmbed Links في قناة مركز العمليات.", ephemeral=True); return
        if channel.permissions_for(interaction.guild.default_role).view_channel:
            await interaction.response.send_message("قناة مركز العمليات ظاهرة للجميع. اجعلها خاصة بفريق الإدارة أولًا.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            client = await self.bot.db.get_client()
            saved = (await client.table("staff_dashboard_panels").select("channel_id,message_id").eq("guild_id", interaction.guild.id).limit(1).execute()).data or []
            if saved and saved[0].get("message_id"):
                current_channel = interaction.guild.get_channel(int(saved[0]["channel_id"]))
                if current_channel is None:
                    try: current_channel = await interaction.guild.fetch_channel(int(saved[0]["channel_id"]))
                    except discord.NotFound: current_channel = None
                if isinstance(current_channel, discord.TextChannel):
                    try:
                        message = await current_channel.fetch_message(int(saved[0]["message_id"]))
                        if current_channel.id != channel.id:
                            await interaction.followup.send(f"مركز العمليات موجود مسبقًا في {current_channel.mention}. لم أحذف اللوحة أو أنشئ نسخة ثانية.", ephemeral=True); return
                        await message.edit(embed=await self.dashboard_embed(interaction.guild), view=PersistentDashboardView(self))
                        await interaction.followup.send(f"تم تحديث مركز العمليات الثابت في {channel.mention}.", ephemeral=True); return
                    except discord.NotFound:
                        logger.warning("Saved operations panel was deleted outside the bot; recreating it")
            # Save the destination before sending; retries can recover if Discord delivery succeeds but persistence fails.
            await client.table("staff_dashboard_panels").upsert({"guild_id": interaction.guild.id, "channel_id": channel.id, "message_id": None, "updated_by": interaction.user.id}).execute()
            message = await channel.send(embed=await self.dashboard_embed(interaction.guild), view=PersistentDashboardView(self), allowed_mentions=discord.AllowedMentions.none())
            await client.table("staff_dashboard_panels").update({"message_id": message.id, "updated_by": interaction.user.id, "updated_at": datetime.now(timezone.utc).isoformat()}).eq("guild_id", interaction.guild.id).execute()
            try: await message.pin(reason="AdSentry permanent operations center")
            except discord.HTTPException: logger.info("Could not pin the dashboard panel in channel %s", channel.id)
            await interaction.followup.send(f"تم تثبيت مركز العمليات في {channel.mention}. أزراره ستبقى تعمل بعد إعادة تشغيل البوت.", ephemeral=True)
        except Exception:
            logger.exception("Could not publish persistent dashboard panel")
            await interaction.followup.send("تعذر إنشاء مركز العمليات. تأكد من تشغيل ترحيل لوحة التحكم وصلاحيات القناة.", ephemeral=True)

    async def show_home(self, interaction: discord.Interaction, edit: bool = False) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("لوحة التحكم متاحة للإدارة فقط.", ephemeral=True); return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=not edit)
        embed = await self.dashboard_embed(interaction.guild)
        view = SystemDashboardView(self)
        await interaction.edit_original_response(embed=embed, view=view)

    async def show_recruitment(self, interaction: discord.Interaction, edit: bool = False) -> None:
        if not _admin(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=not edit)
        try:
            client = await self.bot.db.get_client()
            rows = (await client.table("recruitments").select("id,title,seat_limit,closes_at,review_channel_id").eq("guild_id", interaction.guild.id).eq("status", "OPEN").order("created_at", desc=True).limit(8).execute()).data or []
        except Exception:
            logger.exception("Could not load recruitment dashboard page")
            await interaction.edit_original_response(content="تعذر تحميل التقديمات. تحقق من قاعدة البيانات وترحيل التوظيف.", embed=None, view=RecruitmentDashboardView(self))
            return
        if rows:
            lines = [f"• **#{row['id']} {row['title']}** · المقاعد: {row.get('seat_limit') or 'غير محدودة'} · بطاقات المتقدمين: <#{row['review_channel_id']}>" for row in rows]
            body = "\n".join(lines)
        else:
            body = "لا توجد طلبات انضمام مفتوحة حاليًا."
        embed = discord.Embed(title="لوحة التوظيف", description=f"{body}\n\nمن هنا تنشئ تقديمًا، وتحدد قناة مراجعة من القنوات الثلاث المضبوطة.", color=discord.Color.green())
        view = RecruitmentDashboardView(self)
        await interaction.edit_original_response(embed=embed, view=view)

    async def show_events(self, interaction: discord.Interaction, edit: bool = False) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True, thinking=not edit)
        try:
            client = await self.bot.db.get_client()
            rows = (await client.table("events").select("id,title,starts_at,capacity").eq("guild_id", interaction.guild.id).eq("status", "OPEN").order("starts_at").limit(8).execute()).data or []
            details = []
            for event in rows:
                count = (await client.table("event_participants").select("id", count="exact").eq("event_id", event["id"]).execute()).count or 0
                start = datetime.fromisoformat(event["starts_at"].replace("Z", "+00:00"))
                details.append(f"• **#{event['id']} {event['title']}** · <t:{int(start.timestamp())}:f> · {count}/{event['capacity']} مقعد")
            description = "\n".join(details) if details else "لا توجد فعاليات مفتوحة حاليًا."
            description += "\n\nاختر إجراءً من الأزرار لإدارة الفعاليات والتسجيل والحضور."
        except Exception:
            logger.exception("Could not load EventHub dashboard page")
            description = "تعذر تحميل الفعاليات. تأكد من تشغيل ترحيل EventHub."
        embed = discord.Embed(title="EventHub · إدارة الفعاليات", description=description, color=discord.Color.blurple())
        await interaction.edit_original_response(embed=embed, view=EventDashboardView(self))

    async def show_shifts(self, interaction: discord.Interaction, edit: bool = False) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=not edit)
        try:
            client = await self.bot.db.get_client()
            shifts = (
                await client.table("staff_shifts")
                .select("id,user_id,starts_at,ends_at,duties")
                .eq("guild_id", interaction.guild.id)
                .eq("status", "SCHEDULED")
                .order("starts_at")
                .limit(10)
                .execute()
            ).data or []
            embed = discord.Embed(
                title="🕒 جدول المناوبات القادمة",
                description=f"المناوبات المعروضة: **{len(shifts)}** (يعرض الجدول أقرب 10 مناوبات).",
                color=discord.Color.blurple(),
            )
            for shift in shifts:
                start = datetime.fromisoformat(shift["starts_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(shift["ends_at"].replace("Z", "+00:00"))
                embed.add_field(
                    name=f"#{shift['id']} · <@{shift['user_id']}>",
                    value=(
                        f"**البداية:** <t:{int(start.timestamp())}:F>\n"
                        f"**النهاية:** <t:{int(end.timestamp())}:t>\n"
                        f"**المهام:** {shift['duties'][:900]}"
                    ),
                    inline=False,
                )
            if not shifts:
                embed.description += "\nلا توجد مناوبات مجدولة حالياً."
        except Exception:
            logger.exception("Could not load upcoming shifts for guild %s", interaction.guild.id)
            embed = discord.Embed(
                title="🕒 جدول المناوبات",
                description="تعذر تحميل جدول المناوبات. تحقق من قاعدة البيانات وترحيل StaffFlow.",
                color=discord.Color.red(),
            )
        await interaction.edit_original_response(
            embed=embed,
            view=ShiftDashboardView(self, can_manage=_admin(interaction)),
        )

    async def show_staffflow(self, interaction: discord.Interaction, edit: bool = False) -> None:
        if not _staff(interaction) or not interaction.guild:
            await interaction.response.send_message("هذه اللوحة للإدارة فقط.", ephemeral=True); return
        if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True, thinking=not edit)
        try:
            client = await self.bot.db.get_client()
            cases = (await client.table("staff_cases").select("id").eq("guild_id", interaction.guild.id).eq("status", "PENDING").execute()).data or []
            shifts = (await client.table("staff_shifts").select("id").eq("guild_id", interaction.guild.id).eq("status", "SCHEDULED").execute()).data or []
            description = f"طلبات بانتظار المراجعة: **{len(cases)}**\nمناوبات مجدولة: **{len(shifts)}**\n\nاستخدم النماذج والأزرار أدناه لتقديم الطلبات ومراجعتها وجدولة المناوبات."
        except Exception:
            logger.exception("Could not load StaffFlow dashboard page")
            description = "تعذر تحميل بيانات الطاقم. تأكد من تشغيل ترحيل StaffFlow."
        embed = discord.Embed(title="StaffFlow · إدارة الطاقم", description=description, color=discord.Color.blurple())
        await interaction.edit_original_response(embed=embed, view=StaffDashboardView(self, can_manage=_admin(interaction)))

    @app_commands.command(name="dashboard", description="فتح أو تثبيت لوحة مركز عمليات AdSentry")
    @app_commands.describe(channel="قناة الإدارة الخاصة لتثبيت لوحة العمليات فيها")
    async def dashboard(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        if channel:
            await self.publish_panel(interaction, channel)
            return
        if not interaction.guild or not _staff(interaction):
            await interaction.response.send_message("مركز العمليات متاح للإدارة فقط.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            client = await self.bot.db.get_client()
            rows = (await client.table("staff_dashboard_panels").select("channel_id,message_id").eq("guild_id", interaction.guild.id).limit(1).execute()).data or []
        except Exception:
            logger.exception("Could not locate persistent dashboard")
            await interaction.followup.send("تأكد من تشغيل ترحيل لوحة التحكم، ثم استخدم `/dashboard channel:#قناة-الإدارة` لإنشاء المركز.", ephemeral=True); return
        if rows:
            channel_obj = interaction.guild.get_channel(int(rows[0]["channel_id"]))
            if isinstance(channel_obj, discord.TextChannel) and rows[0].get("message_id"):
                await interaction.followup.send(f"لوحة مركز العمليات الثابتة موجودة في {channel_obj.mention}.", ephemeral=True)
                return
        await interaction.followup.send("لم يتم تثبيت مركز العمليات بعد. استخدم `/dashboard channel:#قناة-الإدارة` مرة واحدة.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DashboardCog(bot))
